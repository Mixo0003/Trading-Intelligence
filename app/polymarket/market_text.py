"""Build searchable text from Polymarket market + parent event metadata."""

from app.polymarket.models import PolymarketMarket


def build_market_context_text(market: PolymarketMarket) -> str:
    """Combine child-market fields with parent event context (no cross-market inference)."""
    parts: list[str] = [
        market.question or "",
        market.description or "",
        market.rules or "",
        market.slug or "",
    ]

    raw = market.raw
    for key in ("groupItemTitle", "groupItemThreshold", "resolutionSource", "title"):
        value = raw.get(key)
        if value is not None and value != "":
            parts.append(str(value))

    events = raw.get("events")
    if isinstance(events, list) and events:
        event = events[0]
        if isinstance(event, dict):
            for key in ("title", "description", "resolutionSource", "rules", "slug"):
                value = event.get(key)
                if value:
                    parts.append(str(value))

    return "\n".join(part for part in parts if part)


def is_temperature_market_candidate(market: PolymarketMarket) -> bool:
    """True when structured evidence indicates a temperature market."""
    text = build_market_context_text(market).lower()
    slug = (market.slug or "").lower()

    if "temperature" in text:
        return True

    slug_patterns = (
        "highest-temperature",
        "lowest-temperature",
        "high-temperature",
        "low-temperature",
        "maximum-temperature",
        "minimum-temperature",
    )
    return any(pattern in slug for pattern in slug_patterns)
