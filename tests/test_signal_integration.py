from unittest.mock import MagicMock, patch

import pytest

from app.ev.types import RecommendationTier
from app.signals.comparator import WeatherComparisonInput, compare_weather_market
from app.signals.generator import WeatherSignal, generate_weather_signal_message
from app.signals.scanner import _BET_TIERS, scan_polymarket_weather_markets
from app.signals.market_analysis import MarketAnalysis, ScanProgress
from app.polymarket.market_type import TemperatureMarketType
from app.weather.forecast import WeatherForecast
from app.weather.resolution import ResolutionDetails


def _valid_resolution() -> ResolutionDetails:
    return ResolutionDetails(
        source="Weather Underground",
        station_code="KORD",
        metric="temperature",
        threshold_f=75.0,
        target_date="2026-07-15",
        raw_text="sample rules",
    )


def _valid_forecast() -> WeatherForecast:
    return WeatherForecast(
        station_code="KORD",
        station_name="Chicago O'Hare",
        source="weather.gov",
        forecast_text="High 80F",
        raw={},
    )


def _make_signal(
    *,
    decision: str,
    edge: float | None,
    probability: float = 0.60,
    confidence_score: float = 0.80,
) -> WeatherSignal:
    return WeatherSignal(
        decision=decision,
        probability=probability,
        confidence_score=confidence_score,
        confidence_label="HIGH",
        edge=edge,
        ev=edge,
        kelly_fraction=0.02,
        recommendation_tier=decision,
        market_probability=0.50,
        message="test signal",
    )


class TestGeneratorIntegration:
    def test_valid_market_price_populates_ev_fields(self):
        signal = generate_weather_signal_message(
            market_title="Chicago high temp",
            station_code="KORD",
            resolution_source="Weather Underground",
            threshold_f=75.0,
            forecast_temperature_f=80.0,
            model_probability=0.60,
            confidence_score=0.80,
            market_price=0.50,
        )

        assert signal.decision == RecommendationTier.NORMAL_BET.value
        assert signal.recommendation_tier == RecommendationTier.NORMAL_BET.value
        assert signal.edge == pytest.approx(0.10)
        assert signal.ev == pytest.approx(0.10)
        assert signal.market_probability == pytest.approx(0.50)
        assert signal.kelly_fraction is not None
        assert signal.kelly_fraction > 0.0
        assert "EV:" in signal.message
        assert "Kelly Fraction:" in signal.message
        assert "Recommendation:" in signal.message

    def test_missing_market_price_returns_watch_with_none_ev_fields(self):
        signal = generate_weather_signal_message(
            market_title="Chicago high temp",
            station_code="KORD",
            resolution_source="Weather Underground",
            threshold_f=75.0,
            forecast_temperature_f=80.0,
            model_probability=0.60,
            confidence_score=0.80,
            market_price=None,
        )

        assert signal.decision == RecommendationTier.WATCH.value
        assert signal.recommendation_tier == RecommendationTier.WATCH.value
        assert signal.edge is None
        assert signal.ev is None
        assert signal.kelly_fraction is None
        assert signal.market_probability is None

    def test_low_confidence_downgrades_one_tier(self):
        signal = generate_weather_signal_message(
            market_title="Chicago high temp",
            station_code="KORD",
            resolution_source="Weather Underground",
            threshold_f=75.0,
            forecast_temperature_f=80.0,
            model_probability=0.70,
            confidence_score=0.40,
            market_price=0.50,
        )

        assert signal.decision == RecommendationTier.NORMAL_BET.value
        assert signal.ev == pytest.approx(0.20)


class TestComparatorSkipBehavior:
    def test_invalid_inputs_remain_skip(self):
        signal = compare_weather_market(
            WeatherComparisonInput(
                market_title="Broken market",
                resolution=ResolutionDetails(
                    source=None,
                    station_code=None,
                    metric=None,
                    threshold_f=None,
                    target_date=None,
                    raw_text="",
                ),
                forecast=None,
                forecast_temperature_f=None,
                market_price=0.50,
            )
        )

        assert signal.decision == "SKIP"
        assert signal.recommendation_tier == "SKIP"
        assert signal.edge is None
        assert signal.ev is None
        assert signal.kelly_fraction is None
        assert "SKIPPED" in signal.message

    def test_valid_inputs_use_ev_engine(self):
        signal = compare_weather_market(
            WeatherComparisonInput(
                market_title="Chicago high temp",
                resolution=_valid_resolution(),
                forecast=_valid_forecast(),
                forecast_temperature_f=80.0,
                market_price=0.50,
            )
        )

        assert signal.decision != "SKIP"
        assert signal.ev is not None
        assert signal.recommendation_tier is not None


class TestScannerIntegration:
    def test_bet_tier_constants_include_new_labels(self):
        assert RecommendationTier.SMALL_BET.value in _BET_TIERS
        assert RecommendationTier.NORMAL_BET.value in _BET_TIERS
        assert RecommendationTier.STRONG_OPPORTUNITY.value in _BET_TIERS
        assert "POSSIBLE VALUE" not in _BET_TIERS

    def test_scanner_accepts_small_bet_label(self):
        settings = MagicMock()
        bet_analysis = MagicMock(
            recommendation_tier=RecommendationTier.SMALL_BET.value,
            edge=0.06,
            model_probability=0.6,
            confidence_score=0.8,
            confidence="HIGH",
            ev=0.06,
            kelly_fraction=0.02,
            implied_probability=0.5,
            message="test",
        )
        watch_analysis = MagicMock(
            recommendation_tier=RecommendationTier.WATCH.value,
            edge=0.06,
            model_probability=0.6,
            confidence_score=0.8,
            confidence="HIGH",
            ev=0.06,
            kelly_fraction=0.02,
            implied_probability=0.5,
            message="test",
        )
        progress = ScanProgress(discovered=2, accepted=2, skipped=0, results=[bet_analysis, watch_analysis])

        with patch("app.signals.scanner.scan_weather_markets", return_value=progress):
            result = scan_polymarket_weather_markets(settings=settings, limit=2, min_edge=0.05)

        assert len(result.signals) == 1
        assert result.signals[0].decision == RecommendationTier.SMALL_BET.value

    def test_scanner_rejects_watch_label(self):
        settings = MagicMock()
        watch_analysis = MagicMock(
            recommendation_tier=RecommendationTier.WATCH.value,
            edge=0.10,
            model_probability=0.6,
            confidence_score=0.8,
            confidence="HIGH",
            ev=0.10,
            kelly_fraction=0.02,
            implied_probability=0.5,
            message="test",
        )
        progress = ScanProgress(discovered=1, accepted=1, skipped=0, results=[watch_analysis])

        with patch("app.signals.scanner.scan_weather_markets", return_value=progress):
            result = scan_polymarket_weather_markets(settings=settings, limit=1, min_edge=0.05)

        assert len(result.signals) == 0
