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


def short_display_reason(
    *,
    display_status: str,
    validation_error: str | None,
    betting_skip_reason: str | None,
    station_error: str | None,
) -> str:
    if station_error == "STATION_UNRESOLVED" or station_error == "STATION_NOT_VERIFIED":
        return "Resolution station could not be verified from market rules."
    if display_status == DISPLAY_SKIP_INSUFFICIENT:
        if station_error:
            return "Exact resolution station could not be verified."
        if validation_error == "FORECAST_UNAVAILABLE":
            return "No usable independent forecast sources returned data."
        return "Insufficient station or forecast data."
    if display_status == DISPLAY_SKIP_MAPPING:
        return "Forecast available but live outcome mapping failed."
    if betting_skip_reason == "FORECAST_SOURCE_DIVERGENCE":
        return "Independent forecast sources disagree beyond betting threshold."
    if betting_skip_reason == "INSUFFICIENT_PROVIDERS":
        return "Fewer than three independent forecast sources available."
    if display_status == DISPLAY_WATCH:
        return "Potentially interesting edge but confidence or agreement is insufficient."
    if display_status == DISPLAY_VALIDATED:
        return "Passed conservative betting validation with multi-source agreement."
    if validation_error:
        return validation_error.replace("_", " ").lower().capitalize() + "."
    return "Forecast shown for information only; no betting signal."


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
    if station_error in {"STATION_UNRESOLVED", "STATION_NOT_VERIFIED"}:
        return DISPLAY_SKIP_INSUFFICIENT
    if station_error or not station_verified or forecast_stats is None or forecast_native is None:
        return DISPLAY_SKIP_INSUFFICIENT
    if forecast_stats.skip_reason:
        return DISPLAY_SKIP_INSUFFICIENT
    if not mapping_ok or not selected_in_event:
        return DISPLAY_SKIP_MAPPING
    if betting_valid and recommendation_tier not in {"SKIP"}:
        if recommendation_tier in {"SMALL BET", "NORMAL BET", "STRONG OPPORTUNITY"}:
            return DISPLAY_VALIDATED
        if recommendation_tier == "WATCH":
            return DISPLAY_WATCH
    if recommendation_tier == "WATCH":
        return DISPLAY_WATCH
    if forecast_stats.betting_skip_reason:
        return DISPLAY_FORECAST_ONLY
    return DISPLAY_FORECAST_ONLY


def has_forecast_result(display_status: str, forecast_display: str | None) -> bool:
    if display_status == DISPLAY_SKIP_INSUFFICIENT:
        return False
    return bool(forecast_display and forecast_display not in {"N/A", ""})
