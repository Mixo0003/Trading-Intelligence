"""User-facing outcome labels for temperature markets."""

from __future__ import annotations

import re

from app.polymarket.market_type import NormalizedTemperatureType
from app.polymarket.models import PolymarketMarket
from app.weather.temperature_units import TemperatureUnit, detect_unit_in_text, format_temperature, unit_symbol


def _dash_pattern() -> str:
    return r"[\-–—to]"


def extract_bracket_bounds(
    text: str,
    unit: TemperatureUnit | None = None,
) -> tuple[float | None, float | None]:
    lower = (text or "").lower()
    dash = _dash_pattern()

    patterns = [
        rf"(\d{{1,3}}(?:\.\d+)?)\s*{dash}\s*(\d{{1,3}}(?:\.\d+)?)\s*°?\s*c",
        rf"(\d{{1,3}}(?:\.\d+)?)\s*{dash}\s*(\d{{1,3}}(?:\.\d+)?)\s*°?\s*f",
        rf"(\d{{1,3}}(?:\.\d+)?)\s*{dash}\s*(\d{{1,3}}(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, lower)
        if match:
            low = float(match.group(1))
            high = float(match.group(2))
            if low > high:
                low, high = high, low
            return low, high

    if unit is None:
        unit = detect_unit_in_text(text)

    return None, None


def extract_exact_temperature(text: str) -> float | None:
    lower = (text or "").lower()
    patterns = [
        r"\bbe\s+(\d{1,3}(?:\.\d+)?)\s*°?\s*c\b",
        r"\bbe\s+(\d{1,3}(?:\.\d+)?)\s*°?\s*f\b",
        r"\bbe\s+(\d{1,3}(?:\.\d+)?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, lower)
        if match:
            return float(match.group(1))
    return None


def _format_range(low: float, high: float, unit: TemperatureUnit) -> str:
    symbol = unit_symbol(unit)
    low_text = int(low) if low.is_integer() else low
    high_text = int(high) if high.is_integer() else high
    return f"{low_text}–{high_text}{symbol}"


def build_best_outcome_label(
    market: PolymarketMarket,
    normalized_type: NormalizedTemperatureType,
    market_unit: TemperatureUnit,
    *,
    context_text: str,
    threshold_display: float | None = None,
) -> str:
    raw = market.raw
    group_title = str(raw.get("groupItemTitle") or "").strip()
    question = (market.question or "").strip()
    source_text = group_title or question or context_text

    low, high = extract_bracket_bounds(source_text, market_unit)
    if low is not None and high is not None:
        return _format_range(low, high, market_unit)

    if normalized_type == NormalizedTemperatureType.ABOVE_THRESHOLD:
        value = threshold_display or extract_exact_temperature(source_text)
        if value is not None:
            return f"Above {format_temperature(value, market_unit)}"

    if normalized_type == NormalizedTemperatureType.BELOW_THRESHOLD:
        value = threshold_display or extract_exact_temperature(source_text)
        if value is not None:
            return f"Below {format_temperature(value, market_unit)}"

    if normalized_type == NormalizedTemperatureType.EXACT_TEMPERATURE:
        value = extract_exact_temperature(source_text)
        if value is not None:
            return format_temperature(value, market_unit)

    if group_title:
        detected = detect_unit_in_text(group_title)
        if detected is not None or re.search(r"\d", group_title):
            return group_title

    if threshold_display is not None and normalized_type not in {
        NormalizedTemperatureType.DAILY_HIGH,
        NormalizedTemperatureType.DAILY_LOW,
    }:
        return format_temperature(threshold_display, market_unit)

    return ""
