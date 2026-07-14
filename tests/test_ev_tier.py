import pytest

from app.ev.policy import (
    MAX_BANKROLL_DEFAULT,
    MAX_BANKROLL_LOW_CONFIDENCE,
    MAX_BANKROLL_WEATHER,
)
from app.ev.tier import determine_recommendation_tier
from app.ev.types import ConfidenceLevel, MarketCategory, RecommendationTier


class TestRecommendationTier:
    def test_negative_ev_is_watch(self):
        result = determine_recommendation_tier(0.40, 0.50, ConfidenceLevel.HIGH)
        assert result.tier == RecommendationTier.WATCH
        assert result.ev == pytest.approx(-0.10)

    def test_below_threshold_ev_is_watch(self):
        result = determine_recommendation_tier(0.52, 0.50, ConfidenceLevel.MEDIUM)
        assert result.tier == RecommendationTier.WATCH
        assert result.ev == pytest.approx(0.02)

    def test_small_bet_tier(self):
        result = determine_recommendation_tier(0.55, 0.50, ConfidenceLevel.MEDIUM)
        assert result.tier == RecommendationTier.SMALL_BET
        assert result.ev == pytest.approx(0.05)

    def test_normal_bet_tier(self):
        result = determine_recommendation_tier(0.60, 0.50, ConfidenceLevel.MEDIUM)
        assert result.tier == RecommendationTier.NORMAL_BET
        assert result.ev == pytest.approx(0.10)

    def test_strong_opportunity_tier(self):
        result = determine_recommendation_tier(0.70, 0.50, ConfidenceLevel.HIGH)
        assert result.tier == RecommendationTier.STRONG_OPPORTUNITY
        assert result.ev == pytest.approx(0.20)

    def test_break_even_is_watch(self):
        result = determine_recommendation_tier(0.50, 0.50, ConfidenceLevel.HIGH)
        assert result.tier == RecommendationTier.WATCH
        assert result.ev == pytest.approx(0.0)
        assert result.kelly_fraction == pytest.approx(0.0)

    def test_low_confidence_downgrades_strong_to_normal(self):
        result = determine_recommendation_tier(0.70, 0.50, ConfidenceLevel.LOW)
        assert result.tier == RecommendationTier.NORMAL_BET

    def test_low_confidence_downgrades_normal_to_small(self):
        result = determine_recommendation_tier(0.60, 0.50, ConfidenceLevel.LOW)
        assert result.tier == RecommendationTier.SMALL_BET

    def test_low_confidence_downgrades_small_to_watch(self):
        result = determine_recommendation_tier(0.55, 0.50, ConfidenceLevel.LOW)
        assert result.tier == RecommendationTier.WATCH

    def test_low_confidence_kelly_cap(self):
        result = determine_recommendation_tier(0.70, 0.50, ConfidenceLevel.LOW)
        assert result.max_bankroll_fraction == MAX_BANKROLL_LOW_CONFIDENCE
        assert result.kelly_fraction <= MAX_BANKROLL_LOW_CONFIDENCE

    def test_weather_kelly_cap(self):
        result = determine_recommendation_tier(
            0.70,
            0.50,
            ConfidenceLevel.HIGH,
            market_category=MarketCategory.WEATHER,
        )
        assert result.max_bankroll_fraction == MAX_BANKROLL_WEATHER
        assert result.kelly_fraction <= MAX_BANKROLL_WEATHER

    def test_default_category_cap(self):
        result = determine_recommendation_tier(
            0.70,
            0.50,
            ConfidenceLevel.HIGH,
            market_category=MarketCategory.DEFAULT,
        )
        assert result.max_bankroll_fraction == MAX_BANKROLL_DEFAULT

    def test_invalid_probability_returns_skip(self):
        result = determine_recommendation_tier(1.5, 0.50, ConfidenceLevel.HIGH)
        assert result.tier == RecommendationTier.SKIP

    def test_invalid_market_price_returns_skip(self):
        result = determine_recommendation_tier(0.60, -0.10, ConfidenceLevel.HIGH)
        assert result.tier == RecommendationTier.SKIP

    def test_result_includes_edge_and_probabilities(self):
        result = determine_recommendation_tier(0.60, 0.50, ConfidenceLevel.MEDIUM)
        assert result.model_probability == pytest.approx(0.60)
        assert result.market_probability == pytest.approx(0.50)
        assert result.edge == pytest.approx(0.10)

    def test_tier_values_are_display_strings(self):
        assert RecommendationTier.SMALL_BET.value == "SMALL BET"
        assert RecommendationTier.NORMAL_BET.value == "NORMAL BET"
        assert RecommendationTier.STRONG_OPPORTUNITY.value == "STRONG OPPORTUNITY"
