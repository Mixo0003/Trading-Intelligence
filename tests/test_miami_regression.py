"""Regression tests for Miami Fahrenheit bracket sibling-outcome mapping."""

from __future__ import annotations

import pytest

from app.ev import determine_recommendation_tier
from app.ev.types import ConfidenceLevel, MarketCategory
from app.polymarket.models import PolymarketMarket
from app.polymarket.sibling_outcomes import (
    OutcomeKind,
    bucket_center,
    outcome_labels,
    parse_sibling_outcome,
    parse_sibling_outcomes,
)
from app.signals.bucket_probability import (
    DEFAULT_FORECAST_SIGMA_F,
    compute_sibling_probabilities,
    default_sigma,
)
from app.signals.event_analysis import (
    _validate_child_market_mapping,
    _validate_probability_distribution,
)
from app.signals.price_validation import validate_actionable_price
from app.weather.temperature_units import TemperatureUnit


def _market(**kwargs) -> PolymarketMarket:
    raw = kwargs.pop("raw", {})
    return PolymarketMarket(
        id=kwargs.get("id", "1"),
        slug=kwargs.get("slug", "test"),
        question=kwargs.get("question", ""),
        description=kwargs.get("description", ""),
        rules=kwargs.get("rules", ""),
        outcomes=["Yes", "No"],
        outcome_prices=kwargs.get("outcome_prices", [0.25, 0.75]),
        volume=None,
        liquidity=None,
        end_date=None,
        raw=raw,
    )


MIAMI_CHILDREN = [
    _market(
        id="tail_low",
        question="Will the highest temperature in Miami be 85°F or below on July 15?",
        raw={"groupItemTitle": "85°F or below"},
        outcome_prices=[0.0005, 0.9995],
    ),
    _market(
        id="b8687",
        question="Will the highest temperature in Miami be between 86-87°F on July 15?",
        raw={"groupItemTitle": "86-87°F"},
        outcome_prices=[0.0005, 0.9995],
    ),
    _market(
        id="b8889",
        question="Will the highest temperature in Miami be between 88-89°F on July 15?",
        raw={"groupItemTitle": "88-89°F"},
        outcome_prices=[0.009, 0.991],
    ),
    _market(
        id="b9091",
        question="Will the highest temperature in Miami be between 90-91°F on July 15?",
        raw={"groupItemTitle": "90-91°F"},
        outcome_prices=[0.04, 0.96],
    ),
    _market(
        id="b9293",
        question="Will the highest temperature in Miami be between 92-93°F on July 15?",
        raw={"groupItemTitle": "92-93°F"},
        outcome_prices=[0.35, 0.65],
    ),
    _market(
        id="b9495",
        question="Will the highest temperature in Miami be between 94-95°F on July 15?",
        raw={"groupItemTitle": "94-95°F"},
        outcome_prices=[0.495, 0.505],
    ),
    _market(
        id="b9697",
        question="Will the highest temperature in Miami be between 96-97°F on July 15?",
        raw={"groupItemTitle": "96-97°F"},
        outcome_prices=[0.098, 0.902],
    ),
    _market(
        id="tail_high",
        question="Will the highest temperature in Miami be 104°F or higher on July 15?",
        raw={"groupItemTitle": "104°F or higher"},
        outcome_prices=[0.0005, 0.9995],
    ),
]


class TestMiamiSiblingParsing:
    def test_bracket_buckets_parsed_as_bracket_kind(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        by_id = {o.market_id: o for o in outcomes}
        assert by_id["b8889"].kind == OutcomeKind.BRACKET
        assert by_id["b8889"].lower_bound == 88.0
        assert by_id["b8889"].upper_bound == 89.0
        assert by_id["b8889"].unit == TemperatureUnit.FAHRENHEIT

    def test_tail_buckets_parsed_correctly(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        by_id = {o.market_id: o for o in outcomes}
        assert by_id["tail_low"].kind == OutcomeKind.BELOW_TAIL
        assert by_id["tail_high"].kind == OutcomeKind.ABOVE_TAIL

    def test_mapping_by_market_id_not_list_position(self):
        shuffled = list(reversed(MIAMI_CHILDREN))
        outcomes = parse_sibling_outcomes(shuffled)
        by_id = {o.market_id: o for o in outcomes}
        assert by_id["b9495"].label == "94-95°F"
        assert by_id["b9495"].yes_price == pytest.approx(0.495)


class TestMiamiProbabilityDistribution:
    def test_95f_forecast_does_not_assign_85pct_to_8889(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        probs = compute_sibling_probabilities(
            outcomes,
            95.0,
            unit=TemperatureUnit.FAHRENHEIT,
            sigma=DEFAULT_FORECAST_SIGMA_F,
        )
        by_id = {item.outcome.market_id: item.probability for item in probs}
        assert by_id["b8889"] < 0.60
        assert by_id["b8889"] != pytest.approx(0.85, abs=0.05)

    def test_near_center_buckets_higher_than_distant(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        probs = compute_sibling_probabilities(
            outcomes,
            95.0,
            unit=TemperatureUnit.FAHRENHEIT,
            sigma=DEFAULT_FORECAST_SIGMA_F,
        )
        by_id = {item.outcome.market_id: item.probability for item in probs}
        assert by_id["b9495"] > by_id["b8889"]
        assert by_id["b9495"] > by_id["b8687"]
        assert by_id["b9293"] > by_id["b8889"]

    def test_9495_is_near_center_for_95f(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        probs = compute_sibling_probabilities(
            outcomes,
            95.0,
            unit=TemperatureUnit.FAHRENHEIT,
            sigma=DEFAULT_FORECAST_SIGMA_F,
        )
        by_id = {item.outcome.market_id: item.probability for item in probs}
        assert by_id["b9495"] >= by_id["b9091"]
        assert by_id["b9495"] >= by_id["b9697"]

    def test_8889_is_distant_from_95f(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        distant = parse_sibling_outcome(MIAMI_CHILDREN[2])
        center = bucket_center(distant)
        assert center == pytest.approx(88.5)
        assert abs(center - 95.0) > 4.0

    def test_sibling_probabilities_sum_to_one(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        probs = compute_sibling_probabilities(
            outcomes,
            95.0,
            unit=TemperatureUnit.FAHRENHEIT,
            sigma=DEFAULT_FORECAST_SIGMA_F,
        )
        total = sum(item.probability for item in probs)
        assert 0.95 <= total <= 1.05

    def test_unit_mismatch_celsius_forecast_on_fahrenheit_buckets_is_inconsistent(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        from app.signals.event_analysis import _validate_forecast_in_sibling_range

        ok, reason = _validate_forecast_in_sibling_range(
            33.7,
            outcomes,
            unit=TemperatureUnit.FAHRENHEIT,
        )
        assert ok is False
        assert reason == "PROBABILITY_DISTRIBUTION_INCONSISTENT"


class TestMiamiChildMarketMapping:
    def test_probability_price_label_share_child_market_id(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        probs = compute_sibling_probabilities(
            outcomes,
            95.0,
            unit=TemperatureUnit.FAHRENHEIT,
            sigma=DEFAULT_FORECAST_SIGMA_F,
        )
        target = next(item for item in probs if item.outcome.market_id == "b9495")
        ok, reason = _validate_child_market_mapping(
            target.outcome,
            target,
            target.outcome.yes_price,
        )
        assert ok is True
        assert reason is None

    def test_mismatched_child_market_id_fails(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        probs = compute_sibling_probabilities(
            outcomes,
            95.0,
            unit=TemperatureUnit.FAHRENHEIT,
            sigma=DEFAULT_FORECAST_SIGMA_F,
        )
        wrong = next(item for item in probs if item.outcome.market_id == "b8889")
        selected = parse_sibling_outcome(MIAMI_CHILDREN[5])
        ok, reason = _validate_child_market_mapping(selected, wrong, selected.yes_price)
        assert ok is False
        assert reason == "CHILD_MARKET_MAPPING_MISMATCH"

    def test_yes_no_prices_not_swapped(self):
        market = MIAMI_CHILDREN[5]
        outcome = parse_sibling_outcome(market)
        assert outcome.yes_price == pytest.approx(0.495)
        assert market.outcome_prices[1] == pytest.approx(0.505)


class TestMiamiStructuralGuards:
    def test_extreme_distant_bucket_edge_distribution_invalid(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        probs = compute_sibling_probabilities(
            outcomes,
            95.0,
            unit=TemperatureUnit.FAHRENHEIT,
            sigma=DEFAULT_FORECAST_SIGMA_F,
        )
        ok, _ = _validate_probability_distribution(
            probs,
            95.0,
            sigma=DEFAULT_FORECAST_SIGMA_F,
            unit=TemperatureUnit.FAHRENHEIT,
        )
        assert ok is True

    def test_low_price_high_probability_mismatch_for_tail(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        tail = parse_sibling_outcome(MIAMI_CHILDREN[0])
        from app.signals.event_analysis import _structural_validation_passed
        from app.polymarket.market_type import NormalizedTemperatureType
        from app.weather.daily_forecast import DailyForecastStats
        from datetime import date

        probs = compute_sibling_probabilities(
            outcomes,
            95.0,
            unit=TemperatureUnit.FAHRENHEIT,
            sigma=DEFAULT_FORECAST_SIGMA_F,
        )
        stats = DailyForecastStats(
            provider="open_meteo",
            station_code="KMIA",
            target_date_local=date(2026, 7, 15),
            station_timezone="America/New_York",
            forecast_window_start_local="2026-07-15T00:00",
            forecast_window_end_local="2026-07-15T23:00",
            daily_min_c=24.0,
            daily_max_c=35.0,
            selected_statistic="daily_max",
            selected_value_c=35.0,
            hourly_samples=[],
        )
        ok, reason = _structural_validation_passed(
            event_type=NormalizedTemperatureType.DAILY_HIGH,
            type_valid=True,
            forecast_stats=stats,
            selected=tail,
            sibling_probs=probs,
            model_probability=0.85,
            market_price=0.009,
            edge=0.84,
            active_labels=outcome_labels(outcomes),
        )
        assert ok is False
        assert reason == "LOW_PRICE_HIGH_PROBABILITY_MISMATCH"

    def test_9495_negative_edge_at_market_price(self):
        outcomes = parse_sibling_outcomes(MIAMI_CHILDREN)
        probs = compute_sibling_probabilities(
            outcomes,
            95.0,
            unit=TemperatureUnit.FAHRENHEIT,
            sigma=DEFAULT_FORECAST_SIGMA_F,
        )
        target = next(item for item in probs if item.outcome.market_id == "b9495")
        result = determine_recommendation_tier(
            target.probability,
            target.outcome.yes_price,
            ConfidenceLevel.MEDIUM,
            market_category=MarketCategory.WEATHER,
        )
        assert result.edge is not None
        assert result.edge < 0

    def test_default_sigma_fahrenheit(self):
        assert default_sigma(TemperatureUnit.FAHRENHEIT) == pytest.approx(3.6)

    def test_selected_outcome_exists_in_sibling_set(self):
        labels = outcome_labels(parse_sibling_outcomes(MIAMI_CHILDREN))
        assert "88-89°f" in labels
        assert "24-25°f" not in labels

    def test_invalid_price_still_detected(self):
        assert validate_actionable_price(0.009)[0] is True
