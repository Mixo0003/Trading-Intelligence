from app.polymarket.models import PolymarketMarket
from app.polymarket.market_type import ThresholdDirection, classify_temperature_market
from app.polymarket.pricing import extract_yes_price
from app.signals.comparator import WeatherComparisonInput, compare_weather_market
from app.signals.generator import WeatherSignal
from app.weather.forecast import fetch_weather_gov_forecast
from app.weather.resolution import parse_resolution_details
from app.weather.station_resolver import resolve_station_from_text
from app.weather.temperature_extract import extract_forecast_temperature_f
from app.config.settings import Settings


def _market_text(market: PolymarketMarket) -> str:
    from app.polymarket.market_text import build_market_context_text
    return build_market_context_text(market)


def analyze_weather_market(
    settings: Settings,
    market: PolymarketMarket,
    forecast_temperature_f: float | None = None,
) -> WeatherSignal:
    text = _market_text(market)
    resolution = parse_resolution_details(text)
    market_type, direction = classify_temperature_market(text)

    station = resolve_station_from_text(text)

    forecast = None
    if station is not None:
        forecast = fetch_weather_gov_forecast(settings, station)

    if forecast_temperature_f is None and forecast is not None:
        forecast_temperature_f = extract_forecast_temperature_f(
            forecast=forecast,
            target_date_text=resolution.target_date,
            market_type=market_type,
            direction=direction,
        )

    return compare_weather_market(
        WeatherComparisonInput(
            market_title=market.question or market.slug or "Unknown Polymarket weather market",
            resolution=resolution,
            forecast=forecast,
            forecast_temperature_f=forecast_temperature_f,
            market_price=extract_yes_price(market),
            below_threshold=direction == ThresholdDirection.BELOW,
        )
    )
