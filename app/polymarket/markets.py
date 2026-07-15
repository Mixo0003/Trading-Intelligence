from app.polymarket.city import text_matches_priority_city
from app.polymarket.client import PolymarketClient
from app.polymarket.event_expansion import supplement_discovered_markets
from app.polymarket.market_text import build_market_context_text, is_temperature_market_candidate
from app.polymarket.models import PolymarketEvent, PolymarketMarket
from app.polymarket.parser import parse_event, parse_market


def _collect_markets_from_events(
    client: PolymarketClient,
    limit: int,
) -> list[PolymarketMarket]:
    """Fetch active events and expand to child markets with event metadata attached."""
    data = client.get_json(
        "/events",
        params={
            "active": "true",
            "closed": "false",
            "limit": min(limit, 100),
            "order": "volume_24hr",
            "ascending": "false",
        },
    )
    items = data if isinstance(data, list) else data.get("events", data.get("data", []))
    markets: list[PolymarketMarket] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        event = parse_event(item)
        if not is_temperature_market_candidate_from_event(event):
            continue
        for market in event.markets:
            markets.append(_attach_event_context(market, event))
    return markets


def is_temperature_market_candidate_from_event(event: PolymarketEvent) -> bool:
    slug = (event.slug or "").lower()
    text = "\n".join([
        event.title or "",
        event.description or "",
        slug,
    ]).lower()
    if "temperature" in text:
        return True
    return any(
        pattern in slug
        for pattern in (
            "highest-temperature",
            "lowest-temperature",
            "high-temperature",
            "low-temperature",
        )
    )


def _attach_event_context(market: PolymarketMarket, event: PolymarketEvent) -> PolymarketMarket:
    raw = dict(market.raw)
    events = raw.get("events")
    if not isinstance(events, list) or not events:
        raw["events"] = [event.raw]
    return PolymarketMarket(
        id=market.id,
        slug=market.slug,
        question=market.question,
        description=market.description or event.description,
        rules=market.rules or event.raw.get("rules") or "",
        outcomes=market.outcomes,
        outcome_prices=market.outcome_prices,
        volume=market.volume,
        liquidity=market.liquidity,
        end_date=market.end_date,
        raw=raw,
    )


def get_active_markets(
    client: PolymarketClient,
    limit: int = 100,
) -> list[PolymarketMarket]:
    data = client.get_json(
        "/markets",
        params={
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume",
            "ascending": "false",
        },
    )

    items = data if isinstance(data, list) else data.get("markets", [])

    return [
        parse_market(item)
        for item in items
        if isinstance(item, dict)
    ]


def _dedupe_markets(markets: list[PolymarketMarket]) -> list[PolymarketMarket]:
    seen: set[str] = set()
    result: list[PolymarketMarket] = []
    for market in markets:
        key = market.id or market.slug or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(market)
    return result


def _filter_priority_city_markets(
    markets: list[PolymarketMarket],
    priority_cities: list[str],
) -> list[PolymarketMarket]:
    selected: list[PolymarketMarket] = []
    for market in markets:
        if not is_temperature_market_candidate(market):
            continue
        text = build_market_context_text(market)
        slug = market.slug or ""
        if text_matches_priority_city(text, slug, priority_cities):
            selected.append(market)
    return selected


def get_weather_markets(
    client: PolymarketClient,
    limit: int = 100,
    priority_cities: list[str] | None = None,
) -> tuple[list[PolymarketMarket], int, int]:
    """Return child markets, direct-market count, and priority-only additions."""
    markets = get_active_markets(client, limit=limit)
    weather_markets: list[PolymarketMarket] = []

    for market in markets:
        if is_temperature_market_candidate(market):
            weather_markets.append(market)

    direct_market_count = len(_dedupe_markets(weather_markets))

    for market in _collect_markets_from_events(client, limit=limit):
        if is_temperature_market_candidate(market):
            weather_markets.append(market)

    merged = _dedupe_markets(weather_markets)

    priority_added = 0
    if priority_cities:
        batch = get_active_markets(client, limit=min(limit * 2, 100))
        priority_candidates = _filter_priority_city_markets(batch, priority_cities)
        existing_keys = {m.id or m.slug for m in merged}
        for market in priority_candidates:
            key = market.id or market.slug
            if key and key not in existing_keys:
                merged.append(market)
                existing_keys.add(key)
                priority_added += 1

    core = merged[:limit]
    final = _dedupe_markets(supplement_discovered_markets(client, core))
    return final, direct_market_count, priority_added


def get_event_by_slug(
    client: PolymarketClient,
    slug: str,
) -> PolymarketEvent | None:
    data = client.get_json("/events", params={"slug": slug})

    if isinstance(data, list) and data:
        return parse_event(data[0])

    if isinstance(data, dict):
        events = data.get("events") or data.get("data") or []
        if events:
            return parse_event(events[0])
        if data.get("slug"):
            return parse_event(data)

    return None


def get_market_by_slug(
    client: PolymarketClient,
    slug: str,
) -> PolymarketMarket | None:
    data = client.get_json("/markets", params={"slug": slug})

    if isinstance(data, list) and data:
        return parse_market(data[0])

    if isinstance(data, dict):
        markets = data.get("markets") or data.get("data") or []
        if markets:
            return parse_market(markets[0])
        if data.get("slug"):
            return parse_market(data)

    return None
