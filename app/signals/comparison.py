"""Objective forecast-vs-market comparison lines."""

from __future__ import annotations

from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.temperature_units import TemperatureUnit, format_temperature, unit_symbol


def _difference_text(delta: float, unit: TemperatureUnit) -> str:
    rounded = abs(round(delta))
    symbol = unit_symbol(unit)
    return f"{int(rounded)}{symbol}"


def _format_bound(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(round(value, 1))


def build_comparison_text(
    *,
    forecast_value: float | None,
    market_unit: TemperatureUnit,
    normalized_type: NormalizedTemperatureType,
    threshold_value: float | None = None,
    bracket_low: float | None = None,
    bracket_high: float | None = None,
    best_outcome: str = "",
) -> str:
    if forecast_value is None:
        return "Comparison unavailable."

    symbol = unit_symbol(market_unit)

    if bracket_low is not None and bracket_high is not None:
        low_text = _format_bound(bracket_low)
        high_text = _format_bound(bracket_high)
        if bracket_low <= forecast_value <= bracket_high:
            return "Forecast is inside the selected range."

        if forecast_value < bracket_low:
            delta = bracket_low - forecast_value
            return (
                f"Forecast is {_difference_text(delta, market_unit)} below the selected "
                f"{low_text}–{high_text}{symbol} bracket."
            )
        delta = forecast_value - bracket_high
        return (
            f"Forecast is {_difference_text(delta, market_unit)} above the selected "
            f"{low_text}–{high_text}{symbol} bracket."
        )

    if normalized_type == NormalizedTemperatureType.ABOVE_THRESHOLD and threshold_value is not None:
        delta = forecast_value - threshold_value
        if abs(delta) < 0.5:
            return f"Forecast is at the {format_temperature(threshold_value, market_unit)} threshold."
        if delta > 0:
            return (
                f"Forecast is {_difference_text(delta, market_unit)} above the market threshold."
            )
        return (
            f"Forecast is {_difference_text(delta, market_unit)} below the market threshold."
        )

    if normalized_type == NormalizedTemperatureType.BELOW_THRESHOLD and threshold_value is not None:
        delta = threshold_value - forecast_value
        if abs(delta) < 0.5:
            return f"Forecast is at the {format_temperature(threshold_value, market_unit)} threshold."
        if delta > 0:
            return (
                f"Forecast is {_difference_text(delta, market_unit)} below the market threshold."
            )
        return (
            f"Forecast is {_difference_text(delta, market_unit)} above the market threshold."
        )

    if normalized_type == NormalizedTemperatureType.EXACT_TEMPERATURE and threshold_value is not None:
        delta = forecast_value - threshold_value
        if abs(delta) < 0.5:
            return f"Forecast matches {format_temperature(threshold_value, market_unit)}."
        direction = "above" if delta > 0 else "below"
        return (
            f"Forecast is {_difference_text(delta, market_unit)} {direction} "
            f"{format_temperature(threshold_value, market_unit)}."
        )

    if best_outcome:
        return f"Forecast is closest to the {best_outcome} bracket."

    return "Comparison unavailable."
