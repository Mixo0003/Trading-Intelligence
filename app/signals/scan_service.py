import logging
import time

from app.config.settings import Settings
from app.metrics.scan_metrics import reset_scan_metrics
from app.polymarket.client import PolymarketClient
from app.polymarket.discovery import SkipReason, evaluate_market_eligibility
from app.polymarket.event_expansion import expand_group_children
from app.polymarket.market_text import build_market_context_text
from app.polymarket.markets import get_weather_markets
from app.region.classifier import event_matches_scan_region
from app.region.types import ScanRegion
from app.signals.event_analysis import analyze_temperature_event
from app.signals.market_analysis import MarketAnalysis, ScanProgress, analyze_market
from app.signals.ranking import sort_analyses
from app.diagnostics.scan_runtime import log_scan_runtime_diagnostics

logger = logging.getLogger(__name__)


def _record_skip(progress: ScanProgress, reason: SkipReason | str) -> None:
    progress.skipped += 1
    key = reason.value if isinstance(reason, SkipReason) else str(reason)
    progress.skip_reasons[key] = progress.skip_reasons.get(key, 0) + 1


def _event_id(market) -> str | None:
    events = market.raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = events[0].get("id")
        return str(event_id) if event_id is not None else None
    raw_id = market.raw.get("eventId") or market.raw.get("event_id")
    return str(raw_id) if raw_id is not None else None


def _group_markets_by_event(markets) -> tuple[dict[str, list], list]:
    grouped: dict[str, list] = {}
    standalone: list = []
    for market in markets:
        eid = _event_id(market)
        if eid:
            grouped.setdefault(eid, []).append(market)
        else:
            standalone.append(market)
    return grouped, standalone


def _count_regional_events(
    grouped: dict[str, list],
    standalone: list,
    scan_region: ScanRegion,
) -> int:
    if scan_region is ScanRegion.ALL:
        return len(grouped) + len(standalone)

    count = 0
    for children in grouped.values():
        if not children:
            continue
        text = build_market_context_text(children[0])
        matches, _ = event_matches_scan_region(text, scan_region)
        if matches:
            count += 1
    for market in standalone:
        text = build_market_context_text(market)
        matches, _ = event_matches_scan_region(text, scan_region)
        if matches:
            count += 1
    return count


def scan_weather_markets(
    settings: Settings,
    limit: int | None = None,
    max_results: int | None = None,
    region: ScanRegion = ScanRegion.ALL,
) -> ScanProgress:
    log_scan_runtime_diagnostics()
    started = time.monotonic()
    scan_limit = limit or settings.max_markets_per_scan
    result_limit = max_results or settings.max_results_per_report
    metrics = reset_scan_metrics()
    metrics.scan_region = region.value
    progress = ScanProgress(metrics=metrics, scan_region=region.value)
    seen_keys: set[str] = set()
    client = PolymarketClient(settings)

    try:
        markets, direct_market_count, priority_added = get_weather_markets(
            client=client,
            limit=scan_limit,
            priority_cities=settings.priority_weather_cities,
        )
    except Exception as exc:
        logger.exception("Polymarket discovery failed: %s", exc)
        raise

    progress.discovered = len(markets)
    progress.worldwide_discovered = direct_market_count
    progress.direct_temperature_markets_discovered = direct_market_count
    progress.priority_us_discovered = priority_added
    metrics.markets_discovered = len(markets)
    metrics.worldwide_discovered = direct_market_count
    metrics.direct_temperature_markets_discovered = direct_market_count
    metrics.priority_us_discovered = priority_added

    grouped, standalone = _group_markets_by_event(markets)
    parent_events_discovered = len(grouped) + len(standalone)
    parent_events_in_region = _count_regional_events(grouped, standalone, region)
    progress.parent_events_discovered = parent_events_discovered
    progress.child_markets_discovered = len(markets)
    progress.parent_events_in_region = parent_events_in_region
    progress.regional_count = parent_events_in_region
    metrics.parent_events_discovered = parent_events_discovered
    metrics.child_markets_discovered = len(markets)
    metrics.parent_events_in_region = parent_events_in_region
    analyzed_events: set[str] = set()
    analyzed: list[MarketAnalysis] = []

    def _analyze_group(children: list) -> None:
        if not children:
            return
        eid = _event_id(children[0])
        if eid and eid in analyzed_events:
            return

        context_text = build_market_context_text(children[0])
        matches_region, region_skip = event_matches_scan_region(context_text, region)
        if not matches_region:
            _record_skip(progress, region_skip or "REGION_UNRESOLVED")
            metrics.markets_excluded_before_weather += 1
            metrics.markets_skipped += 1
            return

        try:
            eligible_children = []
            for market in children:
                eligible, reason, _ = evaluate_market_eligibility(market, seen_keys)
                if eligible:
                    eligible_children.append(market)
                elif reason:
                    _record_skip(progress, reason)
            if not eligible_children:
                return
            progress.accepted += len(eligible_children)
            metrics.markets_accepted += len(eligible_children)

            analysis_children = expand_group_children(client, eligible_children)
            progress.child_markets_loaded_for_analysis += len(analysis_children)
            metrics.child_markets_loaded_for_analysis += len(analysis_children)
            if len(analysis_children) > 1:
                analysis = analyze_temperature_event(settings, analysis_children)
            else:
                analysis = analyze_market(settings, analysis_children[0])

            if analysis is None:
                progress.skipped += 1
                return
            if eid:
                analyzed_events.add(eid)
            metrics.markets_analyzed += 1
            analyzed.append(analysis)
        except Exception as exc:
            logger.exception("Event analysis failed: %s", exc)
            progress.skipped += 1
            metrics.markets_skipped += 1
            progress.skip_reasons["analysis error"] = (
                progress.skip_reasons.get("analysis error", 0) + 1
            )

    for children in grouped.values():
        _analyze_group(children)
    for market in standalone:
        _analyze_group([market])

    metrics.markets_skipped = progress.skipped
    analyzed = sort_analyses(analyzed)
    progress.all_analyses = analyzed
    progress.results = analyzed[:result_limit]
    progress.parent_events_analyzed = len(analyzed)
    metrics.parent_events_analyzed = len(analyzed)
    metrics.opportunities_returned = len(progress.results)
    elapsed = time.monotonic() - started
    progress.scan_duration_seconds = elapsed
    metrics.scan_duration_seconds = elapsed
    logger.info(
        "Regional scan %s complete in %.1fs: parent_events=%s child_markets=%s "
        "regional_parent_events=%s analyzed_parent_events=%s "
        "weather_req=%s excluded_before_weather=%s",
        region.value,
        elapsed,
        progress.parent_events_discovered,
        progress.child_markets_discovered,
        progress.parent_events_in_region,
        len(analyzed),
        metrics.weather_requests,
        metrics.markets_excluded_before_weather,
    )
    return progress


def analyze_market_by_url(settings: Settings, url: str) -> MarketAnalysis:
    from app.polymarket.markets import get_market_by_slug, get_event_by_slug
    import re

    client = PolymarketClient(settings)
    match = re.search(r"polymarket\.com/(?:event|market)/([^/?#]+)", url, re.I)
    if not match:
        raise ValueError("Invalid Polymarket URL.")
    slug = match.group(1)
    event = get_event_by_slug(client, slug)
    if event and event.markets:
        if len(event.markets) > 1:
            result = analyze_temperature_event(settings, event.markets)
            if result is not None:
                return result
        return analyze_market(settings, event.markets[0])
    market = get_market_by_slug(client, slug)
    if market is None:
        raise ValueError("Market not found for URL.")
    return analyze_market(settings, market)
