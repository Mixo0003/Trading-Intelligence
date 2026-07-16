import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from app.config.settings import Settings
from app.diagnostics.scan_runtime import log_scan_runtime_diagnostics
from app.metrics.scan_metrics import reset_scan_metrics
from app.polymarket.client import PolymarketClient
from app.polymarket.discovery import SkipReason, evaluate_market_eligibility
from app.polymarket.event_expansion import expand_group_children
from app.polymarket.market_text import build_market_context_text
from app.polymarket.markets import get_weather_markets
from app.region.classifier import (
    RegionClassification,
    classify_market_region,
    event_matches_scan_region,
)
from app.region.registry import country_code_to_region, station_to_country
from app.region.types import ScanRegion
from app.signals.event_analysis import analyze_temperature_event
from app.signals.market_analysis import MarketAnalysis, ScanProgress, analyze_market
from app.signals.ranking import sort_analyses
from app.signals.scan_runtime import (
    note_worker_enter,
    note_worker_exit,
    scan_state_scope,
)
from app.weather.station_resolver import (
    extract_station_code_from_text,
    resolve_verified_station_from_text,
)
from app.weather.stations import get_station

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


def _event_metadata(market) -> dict:
    events = market.raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return events[0]
    return {}


def _resolve_parent_station(context_text: str):
    """Resolve coordinates for possible USA/unknown stations before filtering."""
    station_code = extract_station_code_from_text(context_text)
    known = get_station(station_code)
    if known is not None:
        return known

    station_country = station_to_country(station_code)
    station_region = country_code_to_region(station_country, station_code)
    if station_region is ScanRegion.USA or station_country is None:
        station, _ = resolve_verified_station_from_text(context_text)
        return station
    return None


def _classify_parent_event(children: list) -> RegionClassification:
    market = children[0]
    context_text = build_market_context_text(market)
    station = _resolve_parent_station(context_text)
    return classify_market_region(
        context_text,
        station_code=station.code if station else None,
        station=station,
        event_metadata=_event_metadata(market),
    )


def _analyze_eligible_group(
    settings: Settings,
    client: PolymarketClient,
    children: list,
    seen_keys: set[str],
) -> tuple[MarketAnalysis | None, int, int, list[str]]:
    """Analyze one eligible parent group. Returns analysis, accepted, loaded, skip reasons."""
    skip_reasons: list[str] = []
    eligible_children = []
    local_seen = set(seen_keys)
    for market in children:
        eligible, reason, _ = evaluate_market_eligibility(market, local_seen)
        if eligible:
            eligible_children.append(market)
        elif reason:
            skip_reasons.append(
                reason.value if isinstance(reason, SkipReason) else str(reason)
            )
    if not eligible_children:
        return None, 0, 0, skip_reasons

    analysis_children = expand_group_children(client, eligible_children)
    if len(analysis_children) > 1:
        analysis = analyze_temperature_event(settings, analysis_children)
    else:
        analysis = analyze_market(settings, analysis_children[0])
    return analysis, len(eligible_children), len(analysis_children), skip_reasons


async def scan_weather_markets_async(
    settings: Settings,
    limit: int | None = None,
    max_results: int | None = None,
    region: ScanRegion = ScanRegion.ALL,
) -> ScanProgress:
    """Async scan with bounded concurrency, deadline, and per-scan request cache."""
    log_scan_runtime_diagnostics()
    started = time.monotonic()
    scan_limit = limit or settings.max_markets_per_scan
    result_limit = max_results or settings.max_results_per_report
    raw_concurrency = getattr(settings, "weather_scan_concurrency", 6)
    concurrency = max(1, raw_concurrency if isinstance(raw_concurrency, int) else 6)
    raw_deadline = getattr(settings, "weather_scan_deadline_seconds", 600)
    deadline_seconds = max(30, raw_deadline if isinstance(raw_deadline, int) else 600)

    with scan_state_scope() as scan_state:
        metrics = reset_scan_metrics()
        metrics.scan_region = region.value
        progress = ScanProgress(metrics=metrics, scan_region=region.value)
        seen_keys: set[str] = set()
        client = PolymarketClient(settings)

        try:
            markets, direct_market_count, priority_added = await asyncio.to_thread(
                get_weather_markets,
                client,
                scan_limit,
                settings.priority_weather_cities,
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
        parent_groups = list(grouped.values()) + [[market] for market in standalone]

        classified_groups: list[tuple[list, RegionClassification]] = []
        for children in parent_groups:
            if not children:
                continue
            classification = await asyncio.to_thread(_classify_parent_event, children)
            classified_groups.append((children, classification))

        parent_events_discovered = len(classified_groups)
        eligible_jobs: list[tuple[int, list, RegionClassification]] = []
        for index, (children, classification) in enumerate(classified_groups):
            context_text = build_market_context_text(children[0])
            matches_region, region_skip = event_matches_scan_region(
                context_text,
                region,
                classification=classification,
            )
            if not matches_region:
                _record_skip(progress, region_skip or "REGION_UNRESOLVED")
                metrics.markets_excluded_before_weather += 1
                metrics.markets_skipped += 1
                continue
            eligible_jobs.append((index, children, classification))

        progress.parent_events_discovered = parent_events_discovered
        progress.child_markets_discovered = len(markets)
        progress.parent_events_in_region = len(eligible_jobs)
        progress.regional_count = len(eligible_jobs)
        metrics.parent_events_discovered = parent_events_discovered
        metrics.child_markets_discovered = len(markets)
        metrics.parent_events_in_region = len(eligible_jobs)

        remaining = max(1.0, deadline_seconds - (time.monotonic() - started))
        sem = asyncio.Semaphore(concurrency)
        results_by_index: dict[int, MarketAnalysis] = {}
        lock = asyncio.Lock()

        async def _run_job(job_index: int, children: list) -> None:
            async with sem:
                note_worker_enter(scan_state)
                try:
                    analysis, accepted, loaded, skip_reasons = await asyncio.to_thread(
                        _analyze_eligible_group,
                        settings,
                        client,
                        children,
                        seen_keys,
                    )
                    async with lock:
                        progress.accepted += accepted
                        metrics.markets_accepted += accepted
                        progress.child_markets_loaded_for_analysis += loaded
                        metrics.child_markets_loaded_for_analysis += loaded
                        progress.child_markets_analyzed += loaded
                        metrics.child_markets_analyzed += loaded
                        for reason in skip_reasons:
                            _record_skip(progress, reason)
                        if analysis is not None:
                            results_by_index[job_index] = analysis
                            metrics.markets_analyzed += 1
                        else:
                            progress.skipped += 1
                except Exception as exc:
                    logger.exception("Event analysis failed: %s", exc)
                    async with lock:
                        progress.skipped += 1
                        metrics.markets_skipped += 1
                        progress.skip_reasons["analysis error"] = (
                            progress.skip_reasons.get("analysis error", 0) + 1
                        )
                finally:
                    note_worker_exit(scan_state)

        tasks = [
            asyncio.create_task(_run_job(job_index, children))
            for job_index, children, _classification in eligible_jobs
        ]

        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                progress.partial = True
                progress.deadline_reached = True
                progress.incomplete_parent_events = len(pending)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                logger.warning(
                    "Scan deadline reached after %.1fs; completed=%s unfinished=%s",
                    time.monotonic() - started,
                    len(done),
                    len(pending),
                )

        analyzed = [results_by_index[i] for i in sorted(results_by_index)]
        analyzed = sort_analyses(analyzed)
        progress.all_analyses = analyzed
        progress.results = analyzed[:result_limit]
        progress.parent_events_analyzed = len(analyzed)
        metrics.parent_events_analyzed = len(analyzed)
        metrics.opportunities_returned = len(progress.results)
        progress.max_concurrency_observed = scan_state.max_concurrency_observed
        progress.weather_com_401_count = scan_state.weather_com_401_count
        progress.polymarket_429_count = scan_state.polymarket_429_count
        elapsed = time.monotonic() - started
        progress.scan_duration_seconds = elapsed
        metrics.scan_duration_seconds = elapsed
        metrics.markets_skipped = progress.skipped
        logger.info(
            "Regional scan %s complete in %.1fs: parent_events=%s child_markets=%s "
            "regional_parent_events=%s analyzed_parent_events=%s partial=%s "
            "max_concurrency=%s weather_com_401=%s polymarket_429=%s "
            "weather_req=%s excluded_before_weather=%s",
            region.value,
            elapsed,
            progress.parent_events_discovered,
            progress.child_markets_discovered,
            progress.parent_events_in_region,
            len(analyzed),
            progress.partial,
            progress.max_concurrency_observed,
            progress.weather_com_401_count,
            progress.polymarket_429_count,
            metrics.weather_requests,
            metrics.markets_excluded_before_weather,
        )
        return progress


def scan_weather_markets(
    settings: Settings,
    limit: int | None = None,
    max_results: int | None = None,
    region: ScanRegion = ScanRegion.ALL,
) -> ScanProgress:
    """Sync wrapper around the async scan path (tests and scripts)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            scan_weather_markets_async(
                settings,
                limit=limit,
                max_results=max_results,
                region=region,
            )
        )

    # Already inside an event loop (rare for sync callers): use a dedicated thread.
    result: list[ScanProgress] = []
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result.append(
                asyncio.run(
                    scan_weather_markets_async(
                        settings,
                        limit=limit,
                        max_results=max_results,
                        region=region,
                    )
                )
            )
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            error.append(exc)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_runner).result()
    if error:
        raise error[0]
    return result[0]


def analyze_market_by_url(settings: Settings, url: str) -> MarketAnalysis:
    from app.polymarket.markets import get_market_by_slug, get_event_by_slug
    import re

    with scan_state_scope():
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
