from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config.settings import Settings
from app.database.storage import save_analysis_record
from app.signals.forecast_display import (
    DISPLAY_FORECAST_ONLY,
    DISPLAY_SKIP_INSUFFICIENT,
    DISPLAY_VALIDATED,
    DISPLAY_WATCH,
    has_forecast_result,
)
from app.signals.market_analysis import MarketAnalysis, ScanProgress
from app.signals.price_validation import (
    format_market_price_display,
    format_model_probability_display,
    format_signed_percent_display,
)
from app.region.types import ScanRegion, parse_scan_region
from app.telegram.scan_cache import CachedScanSnapshot

MAX_OPPORTUNITIES_PER_REPORT = 5
MAX_SCAN_RESULT_CARDS = 10

_BETTING_TIERS = frozenset({"SMALL BET", "NORMAL BET", "STRONG OPPORTUNITY"})


def _analyses_for_report(progress: ScanProgress) -> list[MarketAnalysis]:
    return progress.all_analyses or progress.results


def _validated_analyses(analyses: list[MarketAnalysis]) -> list[MarketAnalysis]:
    return [item for item in analyses if item.display_status == DISPLAY_VALIDATED]


def _watchlist_analyses(analyses: list[MarketAnalysis]) -> list[MarketAnalysis]:
    return [
        item for item in analyses
        if item.display_status in {DISPLAY_VALIDATED, DISPLAY_WATCH}
    ]


def _forecast_analyses(analyses: list[MarketAnalysis]) -> list[MarketAnalysis]:
    return [
        item for item in analyses
        if has_forecast_result(item.display_status, item.forecast_display)
    ]


def _display_rank(item: MarketAnalysis) -> tuple:
    order = {
        DISPLAY_VALIDATED: 4,
        DISPLAY_WATCH: 3,
        DISPLAY_FORECAST_ONLY: 2,
    }
    confidence = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(item.forecast_confidence, 0)
    return (order.get(item.display_status, 0), confidence)


def _sorted_for_scan_display(analyses: list[MarketAnalysis]) -> list[MarketAnalysis]:
    return sorted(analyses, key=_display_rank, reverse=True)


def _station_local_today_iso(analysis: MarketAnalysis) -> str | None:
    tz_name = analysis.station_timezone
    if not tz_name:
        return None
    if tz_name.upper() == "UTC":
        return datetime.now(timezone.utc).date().isoformat()
    try:
        return datetime.now(ZoneInfo(tz_name)).date().isoformat()
    except ZoneInfoNotFoundError:
        return None
    except Exception:
        return None


def _matches_station_local_today(analysis: MarketAnalysis) -> bool:
    station_today = _station_local_today_iso(analysis)
    if station_today and analysis.target_date_local:
        return analysis.target_date_local == station_today
    return False


def format_actionable_weather_card(analysis: MarketAnalysis) -> str:
    city = (analysis.city_location or "Unknown").upper()
    date = (analysis.target_date or "N/A").upper()
    station = analysis.resolution_station or analysis.station_code or "N/A"
    market_type = analysis.temperature_type_label or "Unknown"

    if analysis.display_status in {DISPLAY_SKIP_INSUFFICIENT} or not analysis.forecast_display:
        forecast_value = "unavailable"
    else:
        forecast_value = analysis.forecast_display

    spread = analysis.provider_spread_display or "n/a"
    sources = analysis.forecast_sources_line or analysis.forecast_providers or "n/a"

    lines = [
        f"🌡 {city} — {date}",
        f"Station: {station}",
        f"Market: {market_type}",
        "",
        f"Forecasts: {sources} → consensus {forecast_value}",
        f"Spread: {spread}",
    ]

    if analysis.band_rows:
        lines.extend(["", "Band | Model | Market | Edge"])
        for label, model_p, market_p, edge_val in analysis.band_rows:
            edge_display = (
                format_signed_percent_display(edge_val)
                if edge_val is not None
                else "n/a"
            )
            lines.append(
                f"{label} | {format_model_probability_display(model_p)} | "
                f"{format_market_price_display(market_p)} | {edge_display}"
            )

    decision = analysis.display_status
    if decision == DISPLAY_VALIDATED:
        decision = "ACTION"
    best_price = format_market_price_display(analysis.implied_probability)
    ev_display = f"{analysis.ev:+.3f}" if analysis.ev is not None else "n/a"

    lines.extend([
        "",
        f"Decision: {decision}",
        f"Best: {analysis.best_outcome} @ {best_price}",
        f"EV: {ev_display}",
        f"Reason: {analysis.display_reason or analysis.reason}",
    ])

    if analysis.live_triggers:
        lines.append("")
        lines.append("Live:")
        for trigger in analysis.live_triggers:
            lines.append(f"• {trigger}")

    return "\n".join(lines)


def format_concise_opportunity(analysis: MarketAnalysis) -> str:
    return format_actionable_weather_card(analysis)


def format_market_result_card(analysis: MarketAnalysis) -> str:
    return format_actionable_weather_card(analysis)


def format_scan_completed_summary(progress: ScanProgress) -> str:
    analyses = _analyses_for_report(progress)
    forecasts = _forecast_analyses(analyses)
    validated = _validated_analyses(analyses)
    region = parse_scan_region(progress.scan_region, ScanRegion.ALL)
    parent_events = progress.parent_events_discovered or progress.discovered
    child_markets = progress.child_markets_discovered or progress.discovered
    regional_parent_events = (
        progress.parent_events_in_region or progress.regional_count
    )
    analyzed_parent_events = progress.parent_events_analyzed or len(analyses)
    lines = [
        f"🌦 {region.display_label.upper()} SCAN COMPLETED",
        "",
        f"Parent events discovered globally: {parent_events}",
        f"Child markets discovered globally: {child_markets}",
        f"Parent events retained in region: {regional_parent_events}",
        (
            "Child markets loaded for analysis: "
            f"{progress.child_markets_loaded_for_analysis}"
        ),
        f"Child markets analyzed: {progress.child_markets_analyzed}",
        f"Parent event analyses completed: {analyzed_parent_events}",
        f"Forecasts available: {len(forecasts)}",
        f"Validated opportunities: {len(validated)}",
    ]
    if progress.skip_reasons:
        lines.extend(["", "Skip reasons:"])
        for reason, count in sorted(progress.skip_reasons.items(), key=lambda item: -item[1]):
            lines.append(f"• {reason}: {count}")
    return "\n".join(lines)


def format_empty_report(analyzed_count: int, progress: ScanProgress | None = None) -> str:
    if progress is not None:
        return format_scan_report(progress)
    return format_scan_completed_summary(
        ScanProgress(discovered=analyzed_count, all_analyses=[])
    )


def format_opportunities_bundle(
    analyses: list[MarketAnalysis],
    *,
    header: str | None = None,
) -> str:
    if not analyses:
        return format_empty_report(0, progress=None)
    blocks: list[str] = []
    if header:
        blocks.extend([header, ""])
    for item in analyses:
        blocks.append(format_market_result_card(item))
    return "\n\n".join(blocks)


def format_scheduled_report(progress: ScanProgress) -> list[str]:
    validated = _validated_analyses(_analyses_for_report(progress))
    if not validated:
        return [format_scan_report(progress)]
    return [
        format_opportunities_bundle(
            validated[:MAX_OPPORTUNITIES_PER_REPORT],
            header="🌦 TI WEATHER",
        )
    ]


def format_scan_summary(progress: ScanProgress) -> str:
    analyses = _analyses_for_report(progress)
    return (
        "🔍 Scan complete\n\n"
        f"Discovered: {progress.discovered}\n"
        f"Accepted: {progress.accepted}\n"
        f"Skipped: {progress.skipped}\n"
        f"Forecasts: {len(_forecast_analyses(analyses))}\n"
        f"Validated: {len(_validated_analyses(analyses))}"
    )


def format_top_signals_unavailable(region: ScanRegion = ScanRegion.ALL) -> str:
    return (
        f"No completed {region.display_label} scan is available yet.\n"
        f"Use {region.scan_button} to refresh."
    )


def format_top_opportunities(progress: ScanProgress, limit: int = 5) -> str:
    top = _watchlist_analyses(_analyses_for_report(progress))
    if not top:
        snapshot = CachedScanSnapshot("", progress)
        return format_top_signals_empty(snapshot)
    return format_opportunities_bundle(top[:limit], header="📊 Best Opportunities")


def format_top_signals_empty(snapshot: CachedScanSnapshot) -> str:
    watchlist = snapshot.watchlist_count
    forecasts = snapshot.forecast_result_count
    region = snapshot.region
    return (
        f"📈 {region.display_label.upper()} TOP SIGNALS\n\n"
        f"No validated opportunities in the latest {region.display_label} scan.\n\n"
        f"Forecasts available: {forecasts}\n"
        f"Watchlist items: {watchlist}\n"
        f"Last scan: {snapshot.completed_at}\n\n"
        f"Use {region.scan_button} to refresh."
    )


def format_top_signals_report(snapshot: CachedScanSnapshot, limit: int = 5) -> str:
    top = _watchlist_analyses(snapshot.progress.all_analyses or snapshot.progress.results)
    if not top:
        return format_top_signals_empty(snapshot)
    region = snapshot.region
    return format_opportunities_bundle(
        top[:limit],
        header=f"📈 {region.display_label.upper()} TOP SIGNALS",
    )


def format_analysis_summary(analysis: MarketAnalysis) -> str:
    return format_market_result_card(analysis)


def format_analysis_detail(analysis: MarketAnalysis) -> str:
    return format_market_result_card(analysis)


def format_scan_report(progress: ScanProgress) -> str:
    analyses = _sorted_for_scan_display(_analyses_for_report(progress))
    display_items = [
        item for item in analyses
        if item.display_status != DISPLAY_SKIP_INSUFFICIENT
        or has_forecast_result(item.display_status, item.forecast_display)
    ][:MAX_SCAN_RESULT_CARDS]
    if not display_items:
        display_items = analyses[:MAX_SCAN_RESULT_CARDS]

    parts = [format_scan_completed_summary(progress)]
    if display_items:
        parts.extend(["", "Results:", ""])
        parts.append(
            "\n\n".join(format_market_result_card(item) for item in display_items)
        )
    return "\n".join(parts)


def format_today_forecast_card(analysis: MarketAnalysis) -> str:
    return format_actionable_weather_card(analysis)


def format_today_forecast_report(progress: ScanProgress, region: ScanRegion | None = None) -> str:
    scan_region = region or parse_scan_region(progress.scan_region, ScanRegion.ALL)
    analyses = _forecast_analyses(_analyses_for_report(progress))
    if not analyses:
        return (
            f"📅 {scan_region.display_label.upper()} FORECASTS\n\n"
            f"No forecasts in the latest {scan_region.display_label} scan.\n"
            f"Use {scan_region.scan_button} to refresh."
        )
    cards = [format_today_forecast_card(item) for item in analyses[:MAX_SCAN_RESULT_CARDS]]
    return f"📅 {scan_region.display_label.upper()} FORECASTS\n\n" + "\n\n".join(cards)


def format_today_forecast_from_snapshot(snapshot: CachedScanSnapshot) -> str:
    return format_today_forecast_report(snapshot.progress, region=snapshot.region)


def persist_scan_results(
    settings: Settings,
    telegram_id: int,
    progress: ScanProgress,
) -> None:
    for item in progress.results:
        save_analysis_record(
            settings=settings,
            telegram_id=telegram_id,
            market_url=item.market_url,
            market_title=item.market_title,
            decision=item.display_status or item.recommendation_tier,
            confidence=item.confidence_score,
            ev=item.ev,
            edge=item.edge,
            payload=format_market_result_card(item),
        )
