from app.polymarket.market_type import NormalizedTemperatureType
from app.polymarket.models import PolymarketMarket
from app.signals.comparison import build_comparison_text
from app.signals.outcome_labels import build_best_outcome_label
from app.weather.temperature_units import TemperatureUnit


def _market(**kwargs) -> PolymarketMarket:
    raw = kwargs.pop("raw", {})
    return PolymarketMarket(
        id="1",
        slug=kwargs.get("slug", "test"),
        question=kwargs.get("question", ""),
        description=kwargs.get("description", ""),
        rules=kwargs.get("rules", ""),
        outcomes=kwargs.get("outcomes", ["Yes", "No"]),
        outcome_prices=kwargs.get("outcome_prices", [0.24, 0.76]),
        volume=None,
        liquidity=None,
        end_date=None,
        raw=raw,
    )


class TestOutcomeMapping:
    def test_selected_bracket_displayed_not_yes(self):
        market = _market(
            question="Will the highest temperature in London be between 16-18°C?",
            raw={"groupItemTitle": "16-18°C"},
        )
        label = build_best_outcome_label(
            market,
            NormalizedTemperatureType.DAILY_HIGH,
            TemperatureUnit.CELSIUS,
            context_text=market.question,
        )
        assert label == "16–18°C"
        assert label != "Yes"

    def test_above_threshold_outcome(self):
        market = _market(question="Will Miami temperature be above 90°F on July 14?")
        label = build_best_outcome_label(
            market,
            NormalizedTemperatureType.ABOVE_THRESHOLD,
            TemperatureUnit.FAHRENHEIT,
            context_text=market.question,
            threshold_display=90,
        )
        assert label == "Above 90°F"

    def test_below_threshold_outcome(self):
        market = _market(question="Will Seattle temperature be below 60°F on July 14?")
        label = build_best_outcome_label(
            market,
            NormalizedTemperatureType.BELOW_THRESHOLD,
            TemperatureUnit.FAHRENHEIT,
            context_text=market.question,
            threshold_display=60,
        )
        assert label == "Below 60°F"

    def test_exact_temperature_outcome(self):
        market = _market(
            question="Will the temperature be 22°C on July 14?",
            raw={"groupItemTitle": "22°C"},
        )
        label = build_best_outcome_label(
            market,
            NormalizedTemperatureType.EXACT_TEMPERATURE,
            TemperatureUnit.CELSIUS,
            context_text=market.question,
        )
        assert label == "22°C"


class TestComparisonText:
    def test_forecast_inside_bracket(self):
        text = build_comparison_text(
            forecast_value=17,
            market_unit=TemperatureUnit.CELSIUS,
            normalized_type=NormalizedTemperatureType.DAILY_HIGH,
            bracket_low=16,
            bracket_high=18,
            best_outcome="16–18°C",
        )
        assert "inside" in text.lower()
        assert "selected range" in text.lower()

    def test_forecast_below_bracket(self):
        text = build_comparison_text(
            forecast_value=15,
            market_unit=TemperatureUnit.CELSIUS,
            normalized_type=NormalizedTemperatureType.DAILY_HIGH,
            bracket_low=16,
            bracket_high=18,
            best_outcome="16–18°C",
        )
        assert "below" in text.lower()

    def test_forecast_above_bracket(self):
        text = build_comparison_text(
            forecast_value=19,
            market_unit=TemperatureUnit.CELSIUS,
            normalized_type=NormalizedTemperatureType.DAILY_HIGH,
            bracket_low=16,
            bracket_high=18,
            best_outcome="16–18°C",
        )
        assert "above" in text.lower()

    def test_forecast_above_threshold(self):
        text = build_comparison_text(
            forecast_value=92,
            market_unit=TemperatureUnit.FAHRENHEIT,
            normalized_type=NormalizedTemperatureType.ABOVE_THRESHOLD,
            threshold_value=90,
        )
        assert "2°F above" in text

    def test_forecast_below_threshold(self):
        text = build_comparison_text(
            forecast_value=66,
            market_unit=TemperatureUnit.FAHRENHEIT,
            normalized_type=NormalizedTemperatureType.BELOW_THRESHOLD,
            threshold_value=70,
        )
        assert "below" in text.lower()

    def test_converted_unit_difference(self):
        text = build_comparison_text(
            forecast_value=17,
            market_unit=TemperatureUnit.CELSIUS,
            normalized_type=NormalizedTemperatureType.ABOVE_THRESHOLD,
            threshold_value=15,
        )
        assert "2°C above" in text
