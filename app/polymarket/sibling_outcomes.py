"""Parse sibling child-market outcomes for multi-bucket temperature events."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from app.polymarket.models import PolymarketMarket
from app.polymarket.pricing import extract_yes_price
from app.signals.outcome_labels import extract_bracket_bounds
from app.weather.temperature_units import TemperatureUnit, detect_unit_in_text


class OutcomeKind(str, Enum):
    EXACT = "exact"
    BRACKET = "bracket"
    BELOW_TAIL = "below_tail"
    ABOVE_TAIL = "above_tail"


@dataclass(frozen=True)
class SiblingOutcome:
    market_id: str | None
    market: PolymarketMarket
    label: str
    kind: OutcomeKind
    unit: TemperatureUnit
    exact_value: float | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    yes_price: float | None = None


def _parse_value(text: str) -> float | None:
    match = re.search(r"(\d{1,3}(?:\.\d+)?)", text)
    if match:
        return float(match.group(1))
    return None


def _detect_unit(label: str, market: PolymarketMarket) -> TemperatureUnit:
    unit = detect_unit_in_text(label)
    if unit is not None:
        return unit
    unit = detect_unit_in_text(market.question or "")
    if unit is not None:
        return unit
    return TemperatureUnit.FAHRENHEIT


def parse_sibling_outcome(market: PolymarketMarket) -> SiblingOutcome | None:
    raw = market.raw
    label = str(raw.get("groupItemTitle") or market.question or "").strip()
    if not label:
        return None

    lower = label.lower()
    yes_price = extract_yes_price(market)
    unit = _detect_unit(label, market)

    if "or below" in lower or "or lower" in lower:
        value = _parse_value(label)
        return SiblingOutcome(
            market_id=market.id,
            market=market,
            label=label,
            kind=OutcomeKind.BELOW_TAIL,
            unit=unit,
            upper_bound=value,
            yes_price=yes_price,
        )

    if "or higher" in lower or "or above" in lower:
        value = _parse_value(label)
        return SiblingOutcome(
            market_id=market.id,
            market=market,
            label=label,
            kind=OutcomeKind.ABOVE_TAIL,
            unit=unit,
            lower_bound=value,
            yes_price=yes_price,
        )

    bracket_low, bracket_high = extract_bracket_bounds(label, unit)
    if bracket_low is not None and bracket_high is not None and bracket_low != bracket_high:
        return SiblingOutcome(
            market_id=market.id,
            market=market,
            label=label,
            kind=OutcomeKind.BRACKET,
            unit=unit,
            lower_bound=bracket_low,
            upper_bound=bracket_high,
            yes_price=yes_price,
        )

    value = _parse_value(label)
    if value is None:
        return None

    return SiblingOutcome(
        market_id=market.id,
        market=market,
        label=label,
        kind=OutcomeKind.EXACT,
        unit=unit,
        exact_value=value,
        yes_price=yes_price,
    )


def parse_sibling_outcomes(markets: list[PolymarketMarket]) -> list[SiblingOutcome]:
    by_id: dict[str, SiblingOutcome] = {}
    for market in markets:
        market_id = market.id or market.slug
        if not market_id:
            continue
        parsed = parse_sibling_outcome(market)
        if parsed is not None:
            by_id[str(market_id)] = parsed
    return list(by_id.values())


def outcome_labels(outcomes: list[SiblingOutcome]) -> set[str]:
    return {item.label.strip().lower() for item in outcomes if item.label}


def bucket_center(outcome: SiblingOutcome) -> float | None:
    if outcome.kind == OutcomeKind.BRACKET:
        if outcome.lower_bound is not None and outcome.upper_bound is not None:
            return (outcome.lower_bound + outcome.upper_bound) / 2.0
        return None
    if outcome.kind == OutcomeKind.EXACT and outcome.exact_value is not None:
        return outcome.exact_value
    if outcome.kind == OutcomeKind.BELOW_TAIL and outcome.upper_bound is not None:
        return outcome.upper_bound - 1.0
    if outcome.kind == OutcomeKind.ABOVE_TAIL and outcome.lower_bound is not None:
        return outcome.lower_bound + 1.0
    return None
