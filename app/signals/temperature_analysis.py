"""Prepare aligned temperature inputs for probability estimation."""

from __future__ import annotations

from app.polymarket.market_type import NormalizedTemperatureType, ThresholdDirection
from app.signals.outcome_labels import extract_bracket_bounds, extract_exact_temperature
from app.signals.probability import estimate_probability_from_temperature_edge
from app.weather.temperature_units import TemperatureUnit, convert_temperature


def threshold_in_unit(threshold_f: float | None, market_unit: TemperatureUnit) -> float | None:
    if threshold_f is None:
        return None
    return convert_temperature(
        threshold_f,
        TemperatureUnit.FAHRENHEIT,
        market_unit,
    )


def forecast_in_market_unit(
    forecast_f: float | None,
    provider_unit: TemperatureUnit,
    market_unit: TemperatureUnit,
) -> tuple[float | None, bool]:
    if forecast_f is None:
        return None, False
    if provider_unit == market_unit:
        return forecast_f, False
    return convert_temperature(forecast_f, provider_unit, market_unit), True


def prepare_probability_inputs(
    *,
    text: str,
    forecast_f: float | None,
    threshold_f: float | None,
    normalized_type: NormalizedTemperatureType,
    direction: ThresholdDirection,
    market_unit: TemperatureUnit,
) -> tuple[float | None, float | None, bool, float | None, float | None]:
    """Return forecast_f, threshold_f, below_threshold, bracket_low_f, bracket_high_f."""
    if forecast_f is None:
        return None, threshold_f, direction == ThresholdDirection.BELOW, None, None

    bracket_low, bracket_high = extract_bracket_bounds(text, market_unit)
    if bracket_low is not None and bracket_high is not None:
        low_f = convert_temperature(bracket_low, market_unit, TemperatureUnit.FAHRENHEIT)
        high_f = convert_temperature(bracket_high, market_unit, TemperatureUnit.FAHRENHEIT)
        if low_f <= forecast_f <= high_f:
            midpoint = (low_f + high_f) / 2.0
            return forecast_f, midpoint, False, low_f, high_f
        if forecast_f < low_f:
            return forecast_f, low_f, True, low_f, high_f
        return forecast_f, high_f, False, low_f, high_f

    if normalized_type == NormalizedTemperatureType.EXACT_TEMPERATURE:
        exact = extract_exact_temperature(text)
        if exact is not None:
            threshold_f = convert_temperature(exact, market_unit, TemperatureUnit.FAHRENHEIT)

    below = direction == ThresholdDirection.BELOW
    if normalized_type == NormalizedTemperatureType.BELOW_THRESHOLD:
        below = True
    if normalized_type == NormalizedTemperatureType.ABOVE_THRESHOLD:
        below = False

    return forecast_f, threshold_f, below, None, None


def estimate_market_probability(
    *,
    text: str,
    forecast_f: float | None,
    threshold_f: float | None,
    normalized_type: NormalizedTemperatureType,
    direction: ThresholdDirection,
    market_unit: TemperatureUnit,
) -> tuple[float, float | None, float | None]:
    forecast_f, threshold_f, below, low_f, high_f = prepare_probability_inputs(
        text=text,
        forecast_f=forecast_f,
        threshold_f=threshold_f,
        normalized_type=normalized_type,
        direction=direction,
        market_unit=market_unit,
    )
    if forecast_f is None or threshold_f is None:
        return 0.0, low_f, high_f
    probability = estimate_probability_from_temperature_edge(
        forecast_temperature_f=forecast_f,
        threshold_f=threshold_f,
        below_threshold=below,
    )
    return probability, low_f, high_f
