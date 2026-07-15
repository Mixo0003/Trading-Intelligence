import pytest

from app.polymarket.event_type import detect_event_temperature_type, validate_event_type_consistency
from app.polymarket.market_type import NormalizedTemperatureType
from app.polymarket.models import PolymarketMarket
from app.polymarket.sibling_outcomes import outcome_labels, parse_sibling_outcome, parse_sibling_outcomes
from app.signals.bucket_probability import compute_sibling_probabilities
from app.signals.price_validation import validate_actionable_price


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


PARIS_CHILDREN = [
    _market(
        id="a",
        question="Will the highest temperature in Paris be 29°C or below on July 16?",
        raw={"groupItemTitle": "29°C or below"},
        outcome_prices=[0.008, 0.992],
    ),
    _market(
        id="b",
        question="Will the highest temperature in Paris be 30°C on July 16?",
        raw={"groupItemTitle": "30°C"},
        outcome_prices=[0.054, 0.946],
    ),
    _market(
        id="c",
        question="Will the highest temperature in Paris be 31°C on July 16?",
        raw={"groupItemTitle": "31°C"},
        outcome_prices=[0.25, 0.75],
    ),
    _market(
        id="d",
        question="Will the highest temperature in Paris be 36°C or higher on July 16?",
        raw={"groupItemTitle": "36°C or higher"},
        outcome_prices=[0.01, 0.99],
    ),
]


class TestParisMarketType:
    def test_highest_temperature_paris_is_daily_high(self):
        title = "Highest temperature in Paris on July 16?"
        assert detect_event_temperature_type(title) == NormalizedTemperatureType.DAILY_HIGH

    def test_parent_highest_overrides_child_below_phrase(self):
        title = "Highest temperature in Paris on July 16?"
        valid, err = validate_event_type_consistency(title, NormalizedTemperatureType.DAILY_HIGH)
        assert valid is True
        assert err is None

    def test_mismatch_produces_market_type_mismatch(self):
        title = "Highest temperature in Paris on July 16?"
        valid, err = validate_event_type_consistency(title, NormalizedTemperatureType.DAILY_LOW)
        assert valid is False
        assert err == "MARKET_TYPE_MISMATCH"

    def test_parent_lowest_is_daily_low(self):
        title = "Lowest temperature in Paris on July 16?"
        assert detect_event_temperature_type(title) == NormalizedTemperatureType.DAILY_LOW


class TestParisOutcomeMapping:
    def test_paris_outcome_set(self):
        outcomes = parse_sibling_outcomes(PARIS_CHILDREN)
        labels = outcome_labels(outcomes)
        assert "31°c" in labels
        assert "29°c or below" in labels
        assert "24°c" not in labels

    def test_24c_not_selectable(self):
        outcomes = parse_sibling_outcomes(PARIS_CHILDREN)
        labels = outcome_labels(outcomes)
        assert "24°c" not in labels

    def test_child_price_matches_outcome(self):
        outcomes = parse_sibling_outcomes(PARIS_CHILDREN)
        by_label = {o.label.lower(): o for o in outcomes}
        assert by_label["31°c"].yes_price == pytest.approx(0.25)


class TestSiblingProbabilityCoherence:
    def test_probabilities_sum_to_one(self):
        outcomes = parse_sibling_outcomes(PARIS_CHILDREN)
        probs = compute_sibling_probabilities(outcomes, forecast_c=31.0)
        total = sum(item.probability for item in probs)
        assert 0.95 <= total <= 1.05

    def test_forecast_not_reused_as_probability(self):
        outcomes = parse_sibling_outcomes(PARIS_CHILDREN)
        probs = compute_sibling_probabilities(outcomes, forecast_c=31.0)
        for item in probs:
            assert 0.0 <= item.probability <= 1.0
            assert item.probability != 31.0

    def test_peak_probability_near_forecast_bucket(self):
        outcomes = parse_sibling_outcomes(PARIS_CHILDREN)
        probs = compute_sibling_probabilities(outcomes, forecast_c=31.0)
        by_label = {item.outcome.label: item.probability for item in probs}
        assert by_label["31°C"] >= by_label["30°C"]


class TestPriceValidationGuards:
    def test_missing_price_invalid(self):
        assert validate_actionable_price(None)[0] is False

    def test_tiny_price_valid_but_not_zero_percent(self):
        valid, _ = validate_actionable_price(0.003)
        assert valid is True

    def test_extreme_edge_low_price_pair_flagged_in_event_analysis(self):
        from app.signals.event_analysis import _structural_validation_passed
        from app.polymarket.sibling_outcomes import parse_sibling_outcome
        from app.signals.bucket_probability import OutcomeProbability
        from app.weather.daily_forecast import DailyForecastStats
        from datetime import date

        outcome = parse_sibling_outcome(PARIS_CHILDREN[0])
        stats = DailyForecastStats(
            provider="open_meteo",
            station_code="LFPB",
            target_date_local=date(2026, 7, 16),
            station_timezone="Europe/Paris",
            forecast_window_start_local="2026-07-16T00:00",
            forecast_window_end_local="2026-07-16T23:00",
            daily_min_c=18.0,
            daily_max_c=31.0,
            selected_statistic="daily_max",
            selected_value_c=31.0,
            hourly_samples=[],
        )
        outcomes = parse_sibling_outcomes(PARIS_CHILDREN)
        sibling_probs = compute_sibling_probabilities(outcomes, forecast_c=31.0)
        ok, reason = _structural_validation_passed(
            event_type=NormalizedTemperatureType.DAILY_HIGH,
            type_valid=True,
            forecast_stats=stats,
            selected=outcome,
            sibling_probs=sibling_probs,
            model_probability=0.85,
            market_price=0.003,
            edge=0.847,
            active_labels=outcome_labels(outcomes),
        )
        assert ok is False
        assert reason == "LOW_PRICE_HIGH_PROBABILITY_MISMATCH"
