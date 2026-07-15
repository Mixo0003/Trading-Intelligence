"""Early region filtering before expensive weather calls."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.polymarket.market_type import TemperatureMarketType
from app.polymarket.models import PolymarketMarket
from app.region.types import ScanRegion
from app.signals.market_analysis import MarketAnalysis
from app.signals.scan_service import scan_weather_markets
from tests.conftest import make_test_settings


def _market(question: str, market_id: str) -> PolymarketMarket:
    return PolymarketMarket(
        id=market_id,
        slug=f"market-{market_id}",
        question=question,
        description="Weather Underground resolution.",
        rules="",
        outcomes=["Yes", "No"],
        outcome_prices=[0.4, 0.6],
        volume=1000.0,
        liquidity=500.0,
        end_date="2099-01-01T00:00:00Z",
        raw={"closed": False},
    )


def _analysis_for(market: PolymarketMarket) -> MarketAnalysis:
    return MarketAnalysis(
        market_title=market.question or "",
        market_url=f"https://polymarket.com/market/{market.id}",
        city_location="Test",
        station_code="KORD",
        resolution_source="weather_underground",
        target_date="July 15, 2026",
        market_type=TemperatureMarketType.MAXIMUM_TEMPERATURE,
        threshold_f=90.0,
        threshold_unit="FAHRENHEIT",
        yes_price=0.4,
        implied_probability=0.4,
        model_probability=0.5,
        edge=0.1,
        ev=0.1,
        kelly_fraction=0.02,
        kelly_cap=0.02,
        confidence="MEDIUM",
        recommendation_tier="WATCH",
        reason="test",
        data_sources=["test"],
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        message="msg",
        confidence_score=0.7,
        forecast_display="88°F",
        display_status="FORECAST ONLY",
    )


@pytest.fixture
def settings():
    return make_test_settings(max_results_per_report=10)


class TestEarlyRegionFilter:
    def test_usa_scan_skips_paris_before_weather(self, settings, monkeypatch):
        miami = _market(
            "Will the daily high temperature at KMIA exceed 90°F on July 15, 2026?",
            "1",
        )
        paris = _market(
            "Will the daily high temperature at LFPB exceed 90°F on July 16, 2026?",
            "2",
        )
        weather_calls: list[str] = []

        def fake_get_weather_markets(client, limit=100, priority_cities=None):
            return [miami, paris], 2, 0

        def fake_analyze(settings_arg, market):
            weather_calls.append(market.id or "")
            return _analysis_for(market)

        monkeypatch.setattr("app.signals.scan_service.get_weather_markets", fake_get_weather_markets)
        monkeypatch.setattr("app.signals.scan_service.analyze_market", fake_analyze)
        monkeypatch.setattr("app.signals.scan_service.analyze_temperature_event", fake_analyze)
        monkeypatch.setattr(
            "app.signals.scan_service.evaluate_market_eligibility",
            lambda market, seen: (True, None, ""),
        )

        progress = scan_weather_markets(settings, region=ScanRegion.USA)

        assert "2" not in weather_calls
        assert progress.skip_reasons.get("REGION_MISMATCH", 0) >= 1
        assert progress.metrics.markets_excluded_before_weather >= 1

    def test_europe_scan_skips_miami_before_weather(self, settings, monkeypatch):
        miami = _market(
            "Will the daily high temperature at KMIA exceed 90°F on July 15, 2026?",
            "1",
        )
        paris = _market(
            "Will the daily high temperature at LFPB exceed 90°F on July 16, 2026?",
            "2",
        )
        weather_calls: list[str] = []

        def fake_analyze(settings_arg, market):
            weather_calls.append(market.id or "")
            return _analysis_for(market)

        monkeypatch.setattr(
            "app.signals.scan_service.get_weather_markets",
            lambda *args, **kwargs: ([miami, paris], 2, 0),
        )
        monkeypatch.setattr("app.signals.scan_service.analyze_market", fake_analyze)
        monkeypatch.setattr("app.signals.scan_service.analyze_temperature_event", fake_analyze)
        monkeypatch.setattr(
            "app.signals.scan_service.evaluate_market_eligibility",
            lambda market, seen: (True, None, ""),
        )

        scan_weather_markets(settings, region=ScanRegion.EUROPE)

        assert "1" not in weather_calls

    def test_asia_scan_skips_london_before_weather(self, settings, monkeypatch):
        london = _market(
            "Will the daily high temperature at EGLL exceed 90°F on July 16, 2026?",
            "1",
        )
        tokyo = _market(
            "Will the daily high temperature at RJTT exceed 90°F on July 16, 2026?",
            "2",
        )
        weather_calls: list[str] = []

        def fake_analyze(settings_arg, market):
            weather_calls.append(market.id or "")
            return _analysis_for(market)

        monkeypatch.setattr(
            "app.signals.scan_service.get_weather_markets",
            lambda *args, **kwargs: ([london, tokyo], 2, 0),
        )
        monkeypatch.setattr("app.signals.scan_service.analyze_market", fake_analyze)
        monkeypatch.setattr("app.signals.scan_service.analyze_temperature_event", fake_analyze)
        monkeypatch.setattr(
            "app.signals.scan_service.evaluate_market_eligibility",
            lambda market, seen: (True, None, ""),
        )

        scan_weather_markets(settings, region=ScanRegion.ASIA)

        assert "1" not in weather_calls
