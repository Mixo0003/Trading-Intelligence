from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config.settings import Settings
from app.database.storage import save_analysis_record
from app.polymarket.market_type import NormalizedTemperatureType
from app.signals.forecast_display import (
    DISPLAY_FORECAST_ONLY,
    DISPLAY_SKIP_INSUFFICIENT,
    DISPLAY_VALIDATED,
    DISPLAY_WATCH,
    has_forecast_result,
)
from app.signals.market_analysis import MarketAnalysis, ScanProgress
from app.signals.price_validation import format_market_price_display
from app.region.types import ScanRegion, parse_scan_region
from app.telegram.scan_cache import CachedScanSnapshot

MAX_OPPORTUNITIES_PER_REPORT = 5
MAX_SCAN_RESULT_CARDS = 10
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
SCAN_COMPLETION_LINE = "Scan completed."
SCAN_PARTIAL_COMPLETION_LINE = "Scan completed with partial results."
NO_MARKETS_FOUND_MESSAGE = "No current temperature markets found."

_DECISION_LABELS = {
    DISPLAY_VALIDATED: "ACTION",
    DISPLAY_WATCH: "WATCH",
    DISPLAY_FORECAST_ONLY: "FORECAST ONLY",
}

_ALLOWED_REASONS = frozenset({
    "Strong value.",
    "Potential value.",
    "Limited confidence.",
    "Insufficient sources.",
    "No clear edge.",
    "Conflicting forecasts.",
    "Market price too high.",
    "Station unresolved.",
    "Morning storms may cap the high.",
    "Models disagree by 4°F.",
    "Clear sky supports full heating.",
})

_LONG_REASON_MAP = {
    "potentially interesting edge but confidence or agreement is insufficient.": "Potential value.",
    "potentially interesting edge, but insufficient confidence or agreement.": "Potential value.",
    "potentially interesting edge but confidence is insufficient.": "Potential value.",
    "meets watch criteria but not full validated betting thresholds.": "Potential value.",
    "passed conservative betting validation with multi-source agreement.": "Strong value.",
    "passed conservative betting validation.": "Strong value.",
    "fewer than three independent forecast sources available.": "Insufficient sources.",
    "only one forecast model available; betting requires broader agreement.": "Insufficient sources.",
    "independent forecast sources disagree beyond betting threshold.": "Conflicting forecasts.",
    "no usable independent forecast sources returned data.": "Insufficient sources.",
    "insufficient station or forecast data.": "Insufficient sources.",
    "exact resolution station could not be verified.": "Insufficient sources.",
    "resolution station could not be verified from market rules.": "Insufficient sources.",
    "forecast available but live outcome mapping failed.": "No clear edge.",
    "forecast shown for information only; no betting signal.": "Limited confidence.",
    "verified station forecast; actionable threshold not met.": "Potential value.",
}


def _decision_label(display_status: str) -> str:
    """Map internal display_status to the four allowed Telegram decision labels."""
    return _DECISION_LABELS.get(display_status, "SKIP")


def _market_type_display(analysis: MarketAnalysis) -> str:
    """Show Daily high/low only when confirmed from event classification metadata."""
    if analysis.temperature_type is NormalizedTemperatureType.DAILY_HIGH:
        return "Daily high temperature"
    if analysis.temperature_type is NormalizedTemperatureType.DAILY_LOW:
        return "Daily low temperature"
    label = (analysis.temperature_type_label or "").strip().lower()
    if label == "daily high temperature":
        return "Daily high temperature"
    if label == "daily low temperature":
        return "Daily low temperature"
    return "Temperature market"


def _short_reason_display(reason: str | None) -> str:
    """Map any reason to a short allowed Telegram label (max 4 words)."""
    if not reason:
        return ""
    text = reason.strip()
    if text in _ALLOWED_REASONS:
        return text
    # Allow dynamic station-bias phrasing: "Station runs ~1°F above city forecasts."
    if text.startswith("Station runs ~") and text.endswith("."):
        return text
    key = text.lower()
    if not key.endswith("."):
        key = f"{key}."
    mapped = _LONG_REASON_MAP.get(key)
    if mapped:
        return mapped
    words = text.rstrip(".").split()
    if len(words) <= 8 and text.endswith("."):
        return text
    return "Limited confidence."


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


def format_concise_card(analysis: MarketAnalysis) -> str:
    """Render one event as the strict concise card.

    Exactly 7-8 visible lines, English only, no table, no scan summary, no
    station/spread/provider diagnostics. Price always belongs to the same
    child market as best_outcome (both come from the same selected sibling
    outcome upstream in analyze_temperature_event/analyze_market).
    """
    city = (analysis.city_location or "Unknown").upper()
    date = (analysis.target_date or "N/A").upper()
    market_type = _market_type_display(analysis)
    forecast = analysis.forecast_display or "N/A"
    expected = analysis.expected_range_display or "N/A"
    best = analysis.best_outcome or "N/A"
    price = format_market_price_display(analysis.implied_probability)
    decision = _decision_label(analysis.display_status)
    reason = _short_reason_display(analysis.display_reason)

    lines = [
        f"🌡 {city} — {date}",
        f"Market: {market_type}",
        f"Forecast: {forecast}",
        f"Expected: {expected}",
        f"Best result: {best}",
        f"Price: {price}",
        f"Decision: {decision}",
    ]
    if reason:
        lines.append(f"Reason: {reason}")
    return "\n".join(lines)


def format_actionable_weather_card(analysis: MarketAnalysis) -> str:
    return format_concise_card(analysis)


def format_concise_opportunity(analysis: MarketAnalysis) -> str:
    return format_concise_card(analysis)


def format_market_result_card(analysis: MarketAnalysis) -> str:
    return format_concise_card(analysis)


def format_scan_completed_summary(progress: ScanProgress) -> str:
    """Telegram-facing scan header only. Counters/skip reasons stay internal
    (see ScanProgress.metrics / ScanMetrics + logs), never shown here."""
    region = parse_scan_region(progress.scan_region, ScanRegion.ALL)
    return f"🌦 {region.display_label.upper()} SCAN COMPLETED"


def format_empty_report(analyzed_count: int, progress: ScanProgress | None = None) -> str:
    if progress is not None:
        return format_scan_report(progress)
    return NO_MARKETS_FOUND_MESSAGE


def format_opportunities_bundle(
    analyses: list[MarketAnalysis],
    *,
    header: str | None = None,
) -> str:
    if not analyses:
        return NO_MARKETS_FOUND_MESSAGE
    blocks: list[str] = []
    if header:
        blocks.extend([header, ""])
    for item in analyses:
        blocks.append(format_concise_card(item))
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
    return format_scan_completed_summary(progress)


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
    region = snapshot.region
    return (
        f"📈 {region.display_label.upper()} TOP SIGNALS\n\n"
        f"No validated opportunities in the latest {region.display_label} scan.\n\n"
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
    return format_concise_card(analysis)


def format_analysis_detail(analysis: MarketAnalysis) -> str:
    return format_concise_card(analysis)


def format_scan_report(progress: ScanProgress) -> str:
    messages = format_scan_delivery_messages(progress)
    return "\n\n".join(messages)


def format_scan_delivery_messages(progress: ScanProgress) -> list[str]:
    """Split scan output into Telegram-safe message batches.

    Concise cards are unchanged; only packaging/delivery changes.
    """
    analyses = _sorted_for_scan_display(_analyses_for_report(progress))
    display_items = [
        item for item in analyses
        if item.display_status != DISPLAY_SKIP_INSUFFICIENT
        or has_forecast_result(item.display_status, item.forecast_display)
    ][:MAX_SCAN_RESULT_CARDS]
    if not display_items:
        display_items = analyses[:MAX_SCAN_RESULT_CARDS]

    completion = (
        SCAN_PARTIAL_COMPLETION_LINE
        if getattr(progress, "partial", False) or getattr(progress, "deadline_reached", False)
        else SCAN_COMPLETION_LINE
    )

    if not display_items:
        if progress.parent_events_discovered or progress.discovered:
            return [format_scan_completed_summary(progress), completion]
        return [NO_MARKETS_FOUND_MESSAGE]

    header = format_scan_completed_summary(progress)
    cards = [format_concise_card(item) for item in display_items]
    messages: list[str] = []
    current = header
    for card in cards:
        candidate = f"{current}\n\n{card}" if current else card
        if len(candidate) > TELEGRAM_MAX_MESSAGE_LENGTH and current:
            messages.append(current)
            current = card
        else:
            current = candidate
    if current:
        messages.append(current)
    messages.append(completion)
    return [msg for msg in messages if msg]


def format_today_forecast_card(analysis: MarketAnalysis) -> str:
    return format_concise_card(analysis)


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
            payload=format_concise_card(item),
        )
