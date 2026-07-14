import re

from app.polymarket.client import PolymarketClient
from app.polymarket.models import PolymarketEvent, PolymarketMarket
from app.polymarket.parser import parse_event, parse_market


WEATHER_KEYWORDS = [
    "weather",
    "rain",
    "rainfall",
    "precipitation",
    "snow",
    "snowfall",
    "wind",
    "wind speed",
    "gust",
    "temperature",
    "high temperature",
    "low temperature",
    "daily high",
    "daily low",
    "hotter",
    "colder",
    "heat",
    "cold",
    "noaa",
    "nws",
    "national weather service",
    "weather.gov",
    "weather underground",
    "wunderground",
]

DAILY_TEMPERATURE_TAG_ID = "103040"


def _market_search_text(market: PolymarketMarket) -> str:
    return " ".join([
        market.question or "",
        market.description or "",
        market.rules or "",
        market.slug or "",
    ]).lower()


def _contains_weather_keyword(text: str) -> bool:
    lowered = text.lower()
    return any(
        re.search(rf"\b{re.escape(keyword)}\b", lowered)
        for keyword in WEATHER_KEYWORDS
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


def get_weather_markets(
    client: PolymarketClient,
    limit: int = 100,
) -> list[PolymarketMarket]:
    markets = get_active_markets(client, limit=limit)

    weather_markets: list[PolymarketMarket] = []
    for market in markets:
        text = _market_search_text(market)

        if _contains_weather_keyword(text):
            weather_markets.append(market)

    return weather_markets


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


def get_weather_events(
    client: PolymarketClient,
    limit: int = 200,
) -> list[PolymarketEvent]:
    """Return active weather events, including their live sibling markets."""
    items = []
    for offset in range(0, limit, 100):
        page_limit = min(100, limit - offset)
        data = client.get_json(
            "/events",
            params={
                "active": "true",
                "closed": "false",
                "limit": page_limit,
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
                "tag_id": DAILY_TEMPERATURE_TAG_ID,
            },
        )
        page = data if isinstance(data, list) else data.get("events", [])
        items.extend(page)
        if len(page) < page_limit:
            break
    events = [
        parse_event(item)
        for item in items
        if isinstance(item, dict)
    ]
    return [
        event
        for event in events
        if _contains_weather_keyword(
            " ".join([
                event.title or "",
                event.description,
                " ".join(
                    _market_search_text(market)
                    for market in event.markets
                ),
            ])
        )
    ]
