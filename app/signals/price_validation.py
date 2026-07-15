"""Live market price validation and display formatting."""

from __future__ import annotations

EXTREME_EDGE_THRESHOLD = 0.50
EXTREME_EDGE_MAX_PRICE = 0.02


def is_numeric_price(price: object) -> bool:
    try:
        float(price)
        return True
    except (TypeError, ValueError):
        return False


def validate_actionable_price(price: float | None) -> tuple[bool, str | None]:
    if price is None:
        return False, "missing"
    if not is_numeric_price(price):
        return False, "invalid"
    numeric = float(price)
    if numeric <= 0.0 or numeric >= 1.0:
        return False, "out_of_range"
    return True, None


def format_market_price_display(price: float | None) -> str:
    if price is None:
        return "N/A"
    pct = price * 100.0
    if pct < 1.0:
        formatted = f"{pct:.3f}".rstrip("0").rstrip(".")
        return f"{formatted}%"
    if pct < 10.0:
        return f"{pct:.1f}%"
    return f"{pct:.0f}%"


def format_model_probability_display(probability: float | None) -> str:
    if probability is None:
        return "N/A"
    return format_market_price_display(probability)


def _signed_percent(value: float) -> str:
    pct = value * 100.0
    if abs(pct) < 0.1:
        text = f"{pct:+.3f}".rstrip("0").rstrip(".")
        return f"{text}%"
    if abs(pct) < 1.0:
        return f"{pct:+.1f}%"
    return f"{pct:+.0f}%"


def format_signed_percent_display(value: float | None) -> str:
    if value is None:
        return "N/A"
    return _signed_percent(value)


def extreme_edge_requires_validation(edge: float | None, price: float | None) -> bool:
    if edge is None or price is None:
        return False
    return abs(edge) >= EXTREME_EDGE_THRESHOLD and price <= EXTREME_EDGE_MAX_PRICE


def price_skip_reason(code: str | None) -> str:
    return "Invalid or unavailable live price"
