from app.polymarket.models import PolymarketMarket


def extract_yes_price(market: PolymarketMarket) -> float | None:
    prices = market.outcome_prices
    if not prices:
        return None
    try:
        if isinstance(prices, list) and prices:
            value = float(prices[0])
            if 0.0 <= value <= 1.0:
                return value
    except (TypeError, ValueError):
        return None
    return None


def market_polymarket_url(market: PolymarketMarket) -> str:
    slug = market.slug or market.id or "unknown"
    if market.raw.get("eventSlug"):
        return f"https://polymarket.com/event/{market.raw['eventSlug']}"
    return f"https://polymarket.com/market/{slug}"
