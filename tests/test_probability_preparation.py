from app.signals.temperature_analysis import estimate_market_probability
from app.polymarket.market_type import NormalizedTemperatureType, ThresholdDirection
from app.weather.temperature_units import TemperatureUnit


class TestBracketProbability:
    def test_model_probability_within_unit_interval(self):
        probability, _, _ = estimate_market_probability(
            text="Will the highest temperature in London be 16-18°C?",
            forecast_f=62.6,
            threshold_f=62.6,
            normalized_type=NormalizedTemperatureType.DAILY_HIGH,
            direction=ThresholdDirection.ABOVE,
            market_unit=TemperatureUnit.CELSIUS,
        )
        assert 0.0 <= probability <= 1.0

    def test_forecast_temperature_not_probability(self):
        probability, _, _ = estimate_market_probability(
            text="Will Chicago high temperature be 86°F?",
            forecast_f=86.0,
            threshold_f=85.0,
            normalized_type=NormalizedTemperatureType.DAILY_HIGH,
            direction=ThresholdDirection.ABOVE,
            market_unit=TemperatureUnit.FAHRENHEIT,
        )
        assert probability != 86.0
        assert probability <= 1.0
