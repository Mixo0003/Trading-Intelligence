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


def _market_search_text(market: PolymarketMarket) -> str:
    return " ".join([
        market.question or "",
        market.description or "",
        market.rules or "",
        market.slug or "",
    ]).lower()


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

        if any(keyword in text for keyword in WEATHER_KEYWORDS):
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
