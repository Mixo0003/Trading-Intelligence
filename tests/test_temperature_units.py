import pytest

from app.polymarket.models import PolymarketMarket
from app.weather.temperature_units import (
    TemperatureUnit,
    celsius_to_fahrenheit,
    convert_temperature,
    detect_market_unit,
    detect_unit_in_text,
    fahrenheit_to_celsius,
    format_temperature,
    provider_unit_for_forecast,
)
from app.signals.temperature_analysis import forecast_in_market_unit


def _market(**kwargs) -> PolymarketMarket:
    raw = kwargs.pop("raw", {})
    return PolymarketMarket(
        id=kwargs.get("id", "1"),
        slug=kwargs.get("slug", "test-market"),
        question=kwargs.get("question", ""),
        description=kwargs.get("description", ""),
        rules=kwargs.get("rules", ""),
        outcomes=kwargs.get("outcomes", []),
        outcome_prices=kwargs.get("outcome_prices", []),
        volume=kwargs.get("volume"),
        liquidity=kwargs.get("liquidity"),
        end_date=kwargs.get("end_date"),
        raw=raw,
    )


class TestUnitDetection:
    def test_celsius_from_question(self):
        market = _market(question="Will the highest temperature in London be 18°C?")
        unit, source = detect_market_unit(market)
        assert unit == TemperatureUnit.CELSIUS
        assert source == "question"

    def test_celsius_from_group_item_title(self):
        market = _market(
            question="Highest temperature in Munich?",
            raw={"groupItemTitle": "32°C"},
        )
        unit, source = detect_market_unit(market)
        assert unit == TemperatureUnit.CELSIUS
        assert source == "groupItemTitle"

    def test_celsius_from_outcome_label(self):
        market = _market(question="London temperature", outcomes=["Yes", "No", "18°C"])
        unit, source = detect_market_unit(market)
        assert unit == TemperatureUnit.CELSIUS
        assert source == "outcomes"

    def test_celsius_from_rules(self):
        market = _market(
            question="London temperature",
            rules="Resolved using degrees Celsius from Weather Underground.",
        )
        unit, source = detect_market_unit(market)
        assert unit == TemperatureUnit.CELSIUS
        assert source == "rules"

    def test_fahrenheit_from_question(self):
        market = _market(question="Will Chicago high temperature be 90°F on July 14?")
        unit, source = detect_market_unit(market)
        assert unit == TemperatureUnit.FAHRENHEIT
        assert source == "question"

    def test_fahrenheit_from_outcome_label(self):
        market = _market(question="Chicago temperature", outcomes=["85-86°F", "No"])
        unit, source = detect_market_unit(market)
        assert unit == TemperatureUnit.FAHRENHEIT
        assert source == "outcomes"

    def test_fahrenheit_from_rules(self):
        market = _market(
            question="Chicago temperature",
            rules="Resolved in degrees Fahrenheit.",
        )
        unit, source = detect_market_unit(market)
        assert unit == TemperatureUnit.FAHRENHEIT
        assert source == "rules"

    def test_market_unit_overrides_regional_fallback(self):
        market = _market(question="Will Miami high temperature be 30°C on July 14?")
        unit, source = detect_market_unit(market, context_text="Miami Florida")
        assert unit == TemperatureUnit.CELSIUS
        assert source == "question"


class TestConversions:
    def test_celsius_to_fahrenheit(self):
        assert round(celsius_to_fahrenheit(0), 1) == 32.0
        assert round(celsius_to_fahrenheit(100), 1) == 212.0

    def test_fahrenheit_to_celsius(self):
        assert round(fahrenheit_to_celsius(32), 1) == 0.0

    def test_convert_temperature_roundtrip(self):
        value = convert_temperature(17, TemperatureUnit.CELSIUS, TemperatureUnit.FAHRENHEIT)
        back = convert_temperature(value, TemperatureUnit.FAHRENHEIT, TemperatureUnit.CELSIUS)
        assert round(back, 1) == 17.0

    def test_celsius_forecast_remains_celsius_for_celsius_market(self):
        forecast_f = celsius_to_fahrenheit(17)
        value, converted = forecast_in_market_unit(
            forecast_f,
            provider_unit_for_forecast("weather_gov"),
            TemperatureUnit.CELSIUS,
        )
        assert converted is True
        assert round(value, 1) == 17.0

    def test_fahrenheit_forecast_remains_fahrenheit_for_fahrenheit_market(self):
        value, converted = forecast_in_market_unit(
            86.0,
            provider_unit_for_forecast("weather_gov"),
            TemperatureUnit.FAHRENHEIT,
        )
        assert converted is False
        assert value == 86.0

    def test_provider_fahrenheit_converts_to_market_celsius(self):
        value, converted = forecast_in_market_unit(
            62.6,
            TemperatureUnit.FAHRENHEIT,
            TemperatureUnit.CELSIUS,
        )
        assert converted is True
        assert round(value, 1) == 17.0

    def test_provider_celsius_converts_to_market_fahrenheit(self):
        value, converted = forecast_in_market_unit(
            17.0,
            TemperatureUnit.CELSIUS,
            TemperatureUnit.FAHRENHEIT,
        )
        assert converted is True
        assert round(value, 1) == 62.6

    def test_format_temperature_celsius(self):
        assert format_temperature(17, TemperatureUnit.CELSIUS) == "17°C"

    def test_format_temperature_fahrenheit(self):
        assert format_temperature(86, TemperatureUnit.FAHRENHEIT) == "86°F"
