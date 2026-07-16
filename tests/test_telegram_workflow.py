from datetime import date, timedelta

import pytest
from unittest.mock import MagicMock

from app.polymarket.discovery import SkipReason, evaluate_market_eligibility
from app.polymarket.market_type import (
    TemperatureMarketType,
    ThresholdDirection,
    classify_temperature_market,
)
from app.polymarket.models import PolymarketMarket
from app.polymarket.pricing import extract_yes_price
from app.signals.scan_service import scan_weather_markets
from app.region.types import ScanRegion
from app.telegram.buttons import ALL_NAV_BUTTONS
from tests.conftest import make_test_settings


def _market(
    *,
    question: str,
    description: str = "",
    rules: str = "",
    slug: str = "test-market",
    prices: list | None = None,
    closed: bool = False,
    market_id: str = "1",
) -> PolymarketMarket:
    outcome_prices = [0.45, 0.55] if prices is None else prices
    return PolymarketMarket(
        id=market_id,
        slug=slug,
        question=question,
        description=description,
        rules=rules,
        outcomes=["Yes", "No"],
        outcome_prices=outcome_prices,
        volume=1000.0,
        liquidity=500.0,
        end_date="2099-01-01T00:00:00Z",
        raw={"closed": closed},
    )


def _horizon_target_date_str(*, days_ahead: int = 2) -> str:
    """Target date within the default 7-day forecast eligibility window."""
    return (date.today() + timedelta(days=days_ahead)).strftime("%B %d, %Y")


class TestMarketTypeClassification:
    def test_maximum_temperature(self):
        mtype, direction = classify_temperature_market(
            "Will the daily high temperature at KORD exceed 90°F on July 15, 2026?"
        )
        assert mtype == TemperatureMarketType.MAXIMUM_TEMPERATURE
        assert direction == ThresholdDirection.ABOVE

    def test_minimum_temperature(self):
        mtype, direction = classify_temperature_market(
            "Will the daily low temperature at KJFK be below 32°F on January 10, 2026?"
        )
        assert mtype == TemperatureMarketType.MINIMUM_TEMPERATURE
        assert direction == ThresholdDirection.BELOW

    def test_above_threshold(self):
        mtype, _ = classify_temperature_market(
            "Will temperature at KLAX be above 100°F on August 1, 2026?"
        )
        assert mtype == TemperatureMarketType.ABOVE_THRESHOLD

    def test_below_threshold(self):
        mtype, direction = classify_temperature_market(
            "Will temperature at KORD be below 20°F on February 2, 2026?"
        )
        assert mtype == TemperatureMarketType.BELOW_THRESHOLD
        assert direction == ThresholdDirection.BELOW


class TestDiscoveryFiltering:
    def _valid_market(self) -> PolymarketMarket:
        target = _horizon_target_date_str()
        return _market(
            question=f"Will the daily high temperature at KORD exceed 90°F on {target}?",
            description="Resolved using Weather Underground for station KORD.",
            rules="Weather Underground KORD daily high temperature.",
            slug="kord-high-horizon",
        )

    def test_accepts_valid_market(self):
        seen: set[str] = set()
        ok, reason, _ = evaluate_market_eligibility(self._valid_market(), seen)
        assert ok is True
        assert reason is None

    def test_excludes_closed_market(self):
        seen: set[str] = set()
        market = self._valid_market()
        market = PolymarketMarket(**{**market.__dict__, "raw": {"closed": True}})
        ok, reason, _ = evaluate_market_eligibility(market, seen)
        assert ok is False
        assert reason == SkipReason.CLOSED

    def test_excludes_missing_price(self):
        seen: set[str] = set()
        market = PolymarketMarket(**{**self._valid_market().__dict__, "outcome_prices": []})
        ok, reason, _ = evaluate_market_eligibility(market, seen)
        assert ok is False
        assert reason == SkipReason.MISSING_PRICE

    def test_excludes_duplicate(self):
        seen: set[str] = set()
        market = self._valid_market()
        assert evaluate_market_eligibility(market, seen)[0] is True
        ok, reason, _ = evaluate_market_eligibility(market, seen)
        assert ok is False
        assert reason == SkipReason.DUPLICATE

    def test_excludes_non_temperature(self):
        seen: set[str] = set()
        market = _market(question="Will it rain in Chicago tomorrow?")
        ok, reason, _ = evaluate_market_eligibility(market, seen)
        assert ok is False
        assert reason == SkipReason.NOT_WEATHER


class TestPricing:
    def test_extract_yes_price(self):
        assert extract_yes_price(_market(question="x", prices=[0.42, 0.58])) == pytest.approx(0.42)

    def test_missing_price(self):
        assert extract_yes_price(_market(question="x", prices=[])) is None


class TestReplyKeyboardRouting:
    def test_persistent_nav_removed(self):
        assert ALL_NAV_BUTTONS == ()


class TestScanOrdering:
    def test_scan_result_sorting_and_limit(self, monkeypatch):
        settings = make_test_settings(
            max_results_per_report=2,
            report_timezone="",
            report_times=[],
            auto_report_regions=[ScanRegion.USA],
        )

        class FakeAnalysis:
            def __init__(self, ev, confidence_score, title):
                self.ev = ev
                self.confidence_score = confidence_score
                self.market_title = title
                self.recommendation_tier = "WATCH"
                self.market_url = f"https://polymarket.com/market/{title}"
                self.edge = ev
                self.confidence_label = "MEDIUM"

        def fake_get_weather_markets(client, limit=100, priority_cities=None):
            return [_market(question="m1"), _market(question="m2", market_id="2")], 2, 0

        def fake_analyze(settings, market):
            from app.signals.market_analysis import MarketAnalysis
            from app.polymarket.market_type import TemperatureMarketType
            from datetime import datetime, timezone
            ev = 0.10 if market.id == "1" else 0.05
            return MarketAnalysis(
                market_title=market.question or "",
                market_url="https://polymarket.com/market/x",
                city_location="KORD",
                station_code="KORD",
                resolution_source="weather_gov",
                target_date="July 15, 2026",
                market_type=TemperatureMarketType.ABOVE_THRESHOLD,
                threshold_f=90.0,
                threshold_unit="°F",
                yes_price=0.5,
                implied_probability=0.5,
                model_probability=0.6,
                edge=ev,
                ev=ev,
                kelly_fraction=0.02,
                kelly_cap=0.02,
                confidence="MEDIUM",
                recommendation_tier="WATCH",
                reason="test",
                data_sources=["test"],
                analyzed_at=datetime.now(timezone.utc).isoformat(),
                message="msg",
                confidence_score=0.8 if market.id == "1" else 0.6,
            )

        monkeypatch.setattr("app.signals.scan_service.get_weather_markets", fake_get_weather_markets)
        monkeypatch.setattr("app.signals.scan_service.analyze_market", fake_analyze)
        monkeypatch.setattr(
            "app.signals.scan_service.evaluate_market_eligibility",
            lambda market, seen: (True, None, ""),
        )
        monkeypatch.setattr(
            "app.signals.scan_service.event_matches_scan_region",
            lambda *args, **kwargs: (True, None),
        )
        monkeypatch.setattr(
            "app.signals.scan_service._classify_parent_event",
            lambda children: MagicMock(),
        )
        monkeypatch.setattr(
            "app.signals.scan_service.expand_group_children",
            lambda client, children: children,
        )

        progress = scan_weather_markets(settings)
        assert len(progress.results) == 2
        assert progress.results[0].ev == pytest.approx(0.10)


class TestScheduledReportConfig:
    def test_not_configured_without_timezone(self):
        settings = make_test_settings(
            report_timezone="",
            auto_reports_enabled=True,
            auto_report_regions=[],
        )
        assert settings.scheduled_reports_configured is False

    def test_configured_when_complete(self):
        settings = make_test_settings(
            report_timezone="America/Chicago",
            report_times=["08:00", "12:00", "20:00"],
            auto_reports_enabled=True,
            auto_report_regions=[ScanRegion.USA, ScanRegion.EUROPE],
        )
        assert settings.scheduled_reports_configured is True


class TestNoExecutionCapability:
    def test_repository_has_no_order_execution_symbols(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        forbidden = ["place_order", "sign_transaction", "private_key", "wallet.sign"]
        hits = []
        for path in root.glob("app/**/*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    hits.append(f"{path.name}:{token}")
        assert hits == []
