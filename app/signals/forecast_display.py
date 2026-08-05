"""Forecast display status separate from betting validation."""

from __future__ import annotations

from app.signals.bucket_probability import OutcomeProbability
from app.weather.daily_forecast import DailyForecastStats
from app.weather.temperature_units import TemperatureUnit, unit_symbol

DISPLAY_VALIDATED = "ACTION"
DISPLAY_WATCH = "WATCH"
DISPLAY_FORECAST_ONLY = "FORECAST ONLY"
DISPLAY_SKIP_INSUFFICIENT = "SKIP"
DISPLAY_SKIP_MAPPING = "SKIP — MARKET_OUTCOME_MAPPING_FAILED"

MATERIAL_SPREAD_C = 2.0
INDEPENDENT_MIN_PROVIDERS = 3


def forecast_confidence_level(
    forecast_stats: DailyForecastStats | None,
    *,
    betting_skip_reason: str | None,
) -> str:
    if forecast_stats is None:
        return "LOW"
    count = forecast_stats.ensemble_provider_count
    divergence = forecast_stats.reference_divergence_c

    if (
        count >= INDEPENDENT_MIN_PROVIDERS
        and (divergence is None or divergence <= 1.0)
        and not betting_skip_reason
    ):
        return "HIGH"
    if count >= 2 and forecast_stats.provider_spread_c <= MATERIAL_SPREAD_C:
        return "MEDIUM"
    return "LOW"


def format_forecast_gateway_models(forecast_stats: DailyForecastStats | None) -> tuple[str, str]:
    if forecast_stats is None:
        return "n/a", "n/a"
    label = forecast_stats.forecast_provider_label or forecast_stats.provider or "n/a"
    names = ", ".join(forecast_stats.provider_names) if forecast_stats.provider_names else label
    return label, names


def format_source_divergence(
    forecast_stats: DailyForecastStats | None,
    *,
    unit: TemperatureUnit,
) -> str | None:
    if forecast_stats is None or forecast_stats.reference_divergence_c is None:
        return None
    delta = forecast_stats.reference_divergence_c
    if unit == TemperatureUnit.FAHRENHEIT:
        return f"{delta * 9.0 / 5.0:.1f}{unit_symbol(unit)}"
    return f"{delta:.1f}{unit_symbol(unit)}"


def pick_display_outcome(
    sibling_probs: list[OutcomeProbability],
) -> OutcomeProbability | None:
    if not sibling_probs:
        return None
    return max(sibling_probs, key=lambda item: item.probability)


_EVIDENCE_REASONS = frozenset({
    "Morning storms may cap the high",
    "Station runs ~1°F above city forecasts",
    "Models disagree by 4°F",
    "Clear sky supports full heating",
})


def short_display_reason(
    *,
    display_status: str,
    validation_error: str | None,
    betting_skip_reason: str | None,
    station_error: str | None,
    evidence_reason: str | None = None,
) -> str:
    """Return a short Telegram-facing reason (max 4 words after Reason:)."""
    if station_error in {"STATION_UNRESOLVED", "STATION_NOT_VERIFIED"}:
        return "Station unresolved."
    if betting_skip_reason == "FORECAST_SOURCE_DIVERGENCE" or validation_error == "FORECAST_SOURCE_DIVERGENCE":
        return "Conflicting forecasts."
    if betting_skip_reason == "INSUFFICIENT_PROVIDERS" or validation_error == "INSUFFICIENT_PROVIDERS":
        return "Insufficient sources."
    if validation_error in {"MISSING_PRICE", "missing market price"}:
        return "Price unavailable."
    if validation_error == "INVALID_PRICE":
        return "Market price too high."
    if validation_error == "FORECAST_UNAVAILABLE":
        return "Insufficient sources."
    if station_error or display_status == DISPLAY_SKIP_INSUFFICIENT:
        return "Insufficient sources."
    if display_status == DISPLAY_SKIP_MAPPING:
        return "No clear edge."
    # Prefer a concise evidence phrase on actionable/watch cards only.
    if (
        evidence_reason
        and display_status in {DISPLAY_VALIDATED, DISPLAY_WATCH, DISPLAY_FORECAST_ONLY}
    ):
        cleaned = evidence_reason.strip().rstrip(".")
        if cleaned in _EVIDENCE_REASONS or cleaned.startswith("Station runs ~"):
            return cleaned if cleaned.endswith(".") else f"{cleaned}."
        if cleaned == "Models disagree by 4°F":
            return "Models disagree by 4°F."
    if display_status == DISPLAY_WATCH:
        return "Potential value."
    if display_status == DISPLAY_VALIDATED:
        return "Strong value."
    if display_status == DISPLAY_FORECAST_ONLY:
        return "Limited confidence."
    return "No clear edge."


def compute_display_status(
    *,
    station_verified: bool,
    station_error: str | None,
    forecast_stats: DailyForecastStats | None,
    forecast_native: float | None,
    mapping_ok: bool,
    selected_in_event: bool,
    betting_valid: bool,
    recommendation_tier: str,
) -> str:
    """Separate forecast display from betting eligibility.

    A verified station forecast may be shown as FORECAST ONLY even when price
    mapping or betting thresholds fail. Missing station / provider / skip_reason
    remain unavailable (SKIP). Never invents a forecast.
    """
    if station_error in {"STATION_UNRESOLVED", "STATION_NOT_VERIFIED"}:
        return DISPLAY_SKIP_INSUFFICIENT
    if station_error or not station_verified or forecast_stats is None or forecast_native is None:
        return DISPLAY_SKIP_INSUFFICIENT
    if forecast_stats.skip_reason:
        return DISPLAY_SKIP_INSUFFICIENT
    if betting_valid and recommendation_tier not in {"SKIP"}:
        if recommendation_tier in {"SMALL BET", "NORMAL BET", "STRONG OPPORTUNITY"}:
            return DISPLAY_VALIDATED
        if recommendation_tier == "WATCH":
            return DISPLAY_WATCH
    if recommendation_tier == "WATCH" and mapping_ok and selected_in_event:
        return DISPLAY_WATCH
    # Verified forecast exists; missing price / mapping / edge → FORECAST ONLY.
    return DISPLAY_FORECAST_ONLY


def has_forecast_result(display_status: str, forecast_display: str | None) -> bool:
    if display_status == DISPLAY_SKIP_INSUFFICIENT:
        return False
    return bool(forecast_display and forecast_display not in {"N/A", ""})
