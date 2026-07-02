from app.polymarket.models import PolymarketMarket
from app.signals.comparator import WeatherComparisonInput, compare_weather_market
from app.signals.generator import WeatherSignal
from app.weather.forecast import fetch_weather_gov_forecast
from app.weather.resolution import parse_resolution_details
from app.weather.stations import get_station
from app.config.settings import Settings


def _market_text(market: PolymarketMarket) -> str:
    return "\n".join([
        market.question or "",
        market.description or "",
        market.rules or "",
        market.slug or "",
    ])


def _first_yes_price(market: PolymarketMarket) -> float | None:
    prices = market.outcome_prices

    if not prices:
        return None

    try:
        if isinstance(prices, list):
            return float(prices[0])
        return None
    except Exception:
        return None


def analyze_weather_market(
    settings: Settings,
    market: PolymarketMarket,
    forecast_temperature_f: float | None = None,
) -> WeatherSignal:
    text = _market_text(market)
    resolution = parse_resolution_details(text)

    station = get_station(resolution.station_code)

    forecast = None
    if station is not None:
        forecast = fetch_weather_gov_forecast(settings, station)

    return compare_weather_market(
        WeatherComparisonInput(
            market_title=market.question or market.slug or "Unknown Polymarket weather market",
            resolution=resolution,
            forecast=forecast,
            forecast_temperature_f=forecast_temperature_f,
            market_price=_first_yes_price(market),
        )
    )
