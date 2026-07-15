from app.polymarket.market_type import (
    NormalizedTemperatureType,
    normalize_temperature_type,
    normalized_type_label,
)


class TestMarketTypeDetection:
    def test_daily_high(self):
        text = "Will the highest temperature in London be 18°C on July 14?"
        assert normalize_temperature_type(text) == NormalizedTemperatureType.DAILY_HIGH

    def test_daily_low(self):
        text = "Will the lowest temperature in Chicago be 55°F on July 14?"
        assert normalize_temperature_type(text) == NormalizedTemperatureType.DAILY_LOW

    def test_above_threshold(self):
        text = "Will Miami temperature be above 90°F on July 14?"
        assert normalize_temperature_type(text) == NormalizedTemperatureType.ABOVE_THRESHOLD

    def test_below_threshold(self):
        text = "Will Seattle temperature be below 60°F on July 14?"
        assert normalize_temperature_type(text) == NormalizedTemperatureType.BELOW_THRESHOLD

    def test_bracket_range(self):
        text = "Temperature range 16-18°C for London on July 14"
        assert normalize_temperature_type(text) == NormalizedTemperatureType.BRACKET_RANGE

    def test_exact_temperature(self):
        text = "Will the temperature be 22°C on July 14?"
        assert normalize_temperature_type(text) == NormalizedTemperatureType.EXACT_TEMPERATURE

    def test_daily_high_not_confused_by_wunderground(self):
        text = (
            "Will the highest temperature in Chicago be 85-86°F on July 14?\n"
            "Resolved using Wunderground for station KORD."
        )
        assert normalize_temperature_type(text) == NormalizedTemperatureType.DAILY_HIGH

    def test_user_facing_labels(self):
        assert normalized_type_label(NormalizedTemperatureType.DAILY_HIGH) == "Daily high temperature"
        assert normalized_type_label(NormalizedTemperatureType.BRACKET_RANGE) == "Temperature range"
