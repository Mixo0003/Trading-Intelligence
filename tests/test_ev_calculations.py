import pytest

from app.ev.calculations import (
    calculate_binary_ev,
    calculate_edge,
    calculate_fractional_kelly,
    decimal_odds_to_probability,
    prediction_market_price_to_probability,
)
from app.ev.exceptions import (
    InvalidDecimalOddsError,
    InvalidKellyFractionError,
    InvalidMarketPriceError,
    InvalidProbabilityError,
)
from app.ev.policy import (
    EV_THRESHOLD_NORMAL_BET,
    EV_THRESHOLD_SMALL_BET,
    EV_THRESHOLD_STRONG_OPPORTUNITY,
    MAX_BANKROLL_DEFAULT,
    MAX_BANKROLL_WEATHER,
)


class TestPredictionMarketPriceToProbability:
    def test_normal_price(self):
        assert prediction_market_price_to_probability(0.65) == 0.65

    def test_boundary_zero(self):
        assert prediction_market_price_to_probability(0.0) == 0.0

    def test_boundary_one(self):
        assert prediction_market_price_to_probability(1.0) == 1.0

    def test_invalid_negative(self):
        with pytest.raises(InvalidMarketPriceError):
            prediction_market_price_to_probability(-0.01)

    def test_invalid_above_one(self):
        with pytest.raises(InvalidMarketPriceError):
            prediction_market_price_to_probability(1.01)


class TestDecimalOddsToProbability:
    def test_normal_odds(self):
        assert decimal_odds_to_probability(2.0) == pytest.approx(0.5)

    def test_high_odds(self):
        assert decimal_odds_to_probability(10.0) == pytest.approx(0.1)

    def test_boundary_just_above_one(self):
        assert decimal_odds_to_probability(1.0001) == pytest.approx(1.0 / 1.0001)

    def test_invalid_one(self):
        with pytest.raises(InvalidDecimalOddsError):
            decimal_odds_to_probability(1.0)

    def test_invalid_below_one(self):
        with pytest.raises(InvalidDecimalOddsError):
            decimal_odds_to_probability(0.5)


class TestCalculateEdge:
    def test_positive_edge(self):
        assert calculate_edge(0.60, 0.50) == pytest.approx(0.10)

    def test_negative_edge(self):
        assert calculate_edge(0.40, 0.50) == pytest.approx(-0.10)

    def test_break_even(self):
        assert calculate_edge(0.50, 0.50) == pytest.approx(0.0)

    def test_boundary_probabilities(self):
        assert calculate_edge(0.0, 0.0) == pytest.approx(0.0)
        assert calculate_edge(1.0, 1.0) == pytest.approx(0.0)

    def test_invalid_model_probability(self):
        with pytest.raises(InvalidProbabilityError):
            calculate_edge(1.5, 0.5)

    def test_invalid_market_probability(self):
        with pytest.raises(InvalidProbabilityError):
            calculate_edge(0.5, -0.1)


class TestCalculateBinaryEv:
    def test_positive_ev(self):
        ev = calculate_binary_ev(0.60, 0.50)
        assert ev == pytest.approx(0.10)

    def test_negative_ev(self):
        ev = calculate_binary_ev(0.40, 0.50)
        assert ev == pytest.approx(-0.10)

    def test_break_even(self):
        assert calculate_binary_ev(0.50, 0.50) == pytest.approx(0.0)

    def test_ev_equals_edge(self):
        model_p, price = 0.72, 0.45
        assert calculate_binary_ev(model_p, price) == pytest.approx(
            calculate_edge(model_p, price)
        )

    def test_boundary_price_zero(self):
        assert calculate_binary_ev(0.75, 0.0) == pytest.approx(0.75)

    def test_boundary_price_one(self):
        assert calculate_binary_ev(0.75, 1.0) == pytest.approx(-0.25)

    def test_invalid_model_probability(self):
        with pytest.raises(InvalidProbabilityError):
            calculate_binary_ev(2.0, 0.5)

    def test_invalid_market_price(self):
        with pytest.raises(InvalidMarketPriceError):
            calculate_binary_ev(0.5, 1.5)


class TestCalculateFractionalKelly:
    def test_normal_positive_kelly(self):
        # full_kelly = (0.6 - 0.5) / (1 - 0.5) = 0.2; fraction 0.25 => 0.05
        result = calculate_fractional_kelly(0.60, 0.50, 0.25)
        assert result == pytest.approx(0.05)

    def test_break_even_returns_zero(self):
        assert calculate_fractional_kelly(0.50, 0.50, 0.25) == pytest.approx(0.0)

    def test_negative_edge_returns_zero(self):
        assert calculate_fractional_kelly(0.40, 0.50, 0.25) == pytest.approx(0.0)

    def test_never_negative(self):
        assert calculate_fractional_kelly(0.10, 0.90, 1.0) == pytest.approx(0.0)

    def test_capped_by_max_bankroll(self):
        # full_kelly = 0.6, fraction 1.0 => 0.6, capped to 0.05
        result = calculate_fractional_kelly(
            0.80, 0.50, 1.0, max_bankroll_fraction=MAX_BANKROLL_DEFAULT
        )
        assert result == pytest.approx(MAX_BANKROLL_DEFAULT)

    def test_weather_cap(self):
        result = calculate_fractional_kelly(
            0.80, 0.50, 1.0, max_bankroll_fraction=MAX_BANKROLL_WEATHER
        )
        assert result == pytest.approx(MAX_BANKROLL_WEATHER)

    def test_price_one_returns_zero(self):
        assert calculate_fractional_kelly(0.99, 1.0, 0.25) == pytest.approx(0.0)

    def test_fraction_zero(self):
        assert calculate_fractional_kelly(0.60, 0.50, 0.0) == pytest.approx(0.0)

    def test_fraction_one_boundary(self):
        # Full Kelly = 0.20; with fraction=1.0 and no practical cap, uncapped result is 0.20.
        result = calculate_fractional_kelly(
            0.60, 0.50, 1.0, max_bankroll_fraction=1.0
        )
        assert result == pytest.approx(0.20)

    def test_fraction_one_capped_by_default_bankroll(self):
        result = calculate_fractional_kelly(0.60, 0.50, 1.0)
        assert result == pytest.approx(MAX_BANKROLL_DEFAULT)

    def test_invalid_fraction_negative(self):
        with pytest.raises(InvalidKellyFractionError):
            calculate_fractional_kelly(0.60, 0.50, -0.1)

    def test_invalid_fraction_above_one(self):
        with pytest.raises(InvalidKellyFractionError):
            calculate_fractional_kelly(0.60, 0.50, 1.1)

    def test_invalid_model_probability(self):
        with pytest.raises(InvalidProbabilityError):
            calculate_fractional_kelly(-0.1, 0.50, 0.25)

    def test_invalid_market_price(self):
        with pytest.raises(InvalidMarketPriceError):
            calculate_fractional_kelly(0.60, 2.0, 0.25)


class TestEvThresholdConstants:
    def test_threshold_ordering(self):
        assert EV_THRESHOLD_SMALL_BET < EV_THRESHOLD_NORMAL_BET
        assert EV_THRESHOLD_NORMAL_BET < EV_THRESHOLD_STRONG_OPPORTUNITY
