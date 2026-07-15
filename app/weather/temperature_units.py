"""Temperature unit detection and conversion helpers."""

from __future__ import annotations

import re
from enum import Enum

from app.polymarket.models import PolymarketMarket


class TemperatureUnit(str, Enum):
    CELSIUS = "CELSIUS"
    FAHRENHEIT = "FAHRENHEIT"


CELSIUS_PATTERNS = (
    r"°\s*c\b",
    r"\bdegrees?\s+celsius\b",
    r"\bcelsius\b",
    r"(\d+(?:\.\d+)?)\s*°?\s*c\b",
)

FAHRENHEIT_PATTERNS = (
    r"°\s*f\b",
    r"\bdegrees?\s+fahrenheit\b",
    r"\bfahrenheit\b",
    r"(\d+(?:\.\d+)?)\s*°?\s*f\b",
)

EUROPEAN_CITIES_CELSIUS = frozenset({
    "london", "munich", "wuhan", "guangzhou", "paris", "berlin", "madrid", "rome",
    "amsterdam", "brussels", "vienna", "prague", "warsaw", "dublin", "lisbon",
})

US_CITIES_FAHRENHEIT = frozenset({
    "chicago", "new york city", "new york", "miami", "atlanta", "seattle",
    "los angeles", "houston", "dallas", "phoenix", "denver", "boston",
})


def celsius_to_fahrenheit(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


def fahrenheit_to_celsius(value: float) -> float:
    return (value - 32.0) * 5.0 / 9.0


def convert_temperature(
    value: float,
    from_unit: TemperatureUnit,
    to_unit: TemperatureUnit,
) -> float:
    if from_unit == to_unit:
        return value
    if from_unit == TemperatureUnit.CELSIUS and to_unit == TemperatureUnit.FAHRENHEIT:
        return celsius_to_fahrenheit(value)
    return fahrenheit_to_celsius(value)


def unit_symbol(unit: TemperatureUnit) -> str:
    return "°C" if unit == TemperatureUnit.CELSIUS else "°F"


def _text_has_unit(text: str, patterns: tuple[str, ...]) -> bool:
    lower = (text or "").lower()
    return any(re.search(pattern, lower) for pattern in patterns)


def detect_unit_in_text(text: str) -> TemperatureUnit | None:
    if not text:
        return None
    has_c = _text_has_unit(text, CELSIUS_PATTERNS)
    has_f = _text_has_unit(text, FAHRENHEIT_PATTERNS)
    if has_c and not has_f:
        return TemperatureUnit.CELSIUS
    if has_f and not has_c:
        return TemperatureUnit.FAHRENHEIT
    return None


def _outcome_labels_text(market: PolymarketMarket) -> str:
    outcomes = market.outcomes or []
    return " ".join(str(item) for item in outcomes)


def _regional_fallback_unit(text: str) -> TemperatureUnit | None:
    lower = (text or "").lower()
    for city in EUROPEAN_CITIES_CELSIUS:
        if city in lower:
            return TemperatureUnit.CELSIUS
    for city in US_CITIES_FAHRENHEIT:
        if city in lower:
            return TemperatureUnit.FAHRENHEIT
    return None


def detect_market_unit(
    market: PolymarketMarket,
    *,
    context_text: str | None = None,
) -> tuple[TemperatureUnit, str]:
    """Return (unit, source_label) using market-specific precedence."""
    text = context_text or ""
    raw = market.raw

    question = market.question or ""
    unit = detect_unit_in_text(question)
    if unit is not None:
        return unit, "question"

    group_title = str(raw.get("groupItemTitle") or "")
    unit = detect_unit_in_text(group_title)
    if unit is not None:
        return unit, "groupItemTitle"

    unit = detect_unit_in_text(_outcome_labels_text(market))
    if unit is not None:
        return unit, "outcomes"

    rules = "\n".join([
        market.rules or "",
        market.description or "",
        str(raw.get("resolutionSource") or ""),
    ])
    unit = detect_unit_in_text(rules)
    if unit is not None:
        return unit, "rules"

    stored = raw.get("temperatureUnit") or raw.get("temperature_unit")
    if isinstance(stored, str):
        normalized = stored.strip().upper()
        if normalized in {"C", "CELSIUS"}:
            return TemperatureUnit.CELSIUS, "metadata"
        if normalized in {"F", "FAHRENHEIT"}:
            return TemperatureUnit.FAHRENHEIT, "metadata"

    unit = _regional_fallback_unit(text)
    if unit is not None:
        return unit, "regional_fallback"

    return TemperatureUnit.FAHRENHEIT, "default"


def provider_unit_for_forecast(source: str) -> TemperatureUnit:
    if source == "open_meteo":
        return TemperatureUnit.FAHRENHEIT
    if source == "weather_gov":
        return TemperatureUnit.FAHRENHEIT
    return TemperatureUnit.FAHRENHEIT


def format_temperature(value: float | None, unit: TemperatureUnit) -> str:
    if value is None:
        return "N/A"
    rounded = round(value)
    if abs(value - rounded) < 0.05:
        value = float(rounded)
    else:
        value = round(value, 1)
    suffix = unit_symbol(unit)
    if isinstance(value, float) and value.is_integer():
        return f"{int(value)}{suffix}"
    return f"{value}{suffix}"
