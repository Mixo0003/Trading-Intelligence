import pytest

from app.metrics.scan_metrics import ScanMetrics
from app.polymarket.city import extract_city_from_text, is_priority_us_city, text_matches_priority_city
from app.polymarket.market_type import TemperatureMarketType, classify_temperature_market
from app.signals.bracket import is_bracket_market, select_best_outcome
from app.signals.ranking import sort_analyses
from app.telegram.formatting import format_concise_opportunity, format_empty_report
from app.telegram.handlers import schedule_report_jobs
from tests.conftest import make_test_settings


class TestPriorityCities:
    def test_chicago_slug_matches(self):
        assert text_matches_priority_city(
            "highest temperature in chicago",
            "highest-temperature-in-chicago-on-july-15",
            ["Chicago"],
        )

    def test_is_priority_us_city_by_station(self):
        assert is_priority_us_city("Unknown", "KORD", ["Chicago"]) is True


class TestBracketMarkets:
    def test_bracket_detection(self):
        text = "Will the highest temperature in Munich be 32°C on July 16?"
        assert is_bracket_market(text) is True
        mtype, _ = classify_temperature_market(text)
        assert mtype == TemperatureMarketType.BRACKET

    def test_select_best_outcome_fallback(self):
        outcome, ev = select_best_outcome(0.7, 0.4)
        assert outcome == "Yes"
        assert ev is not None


class TestConciseFormatting:
    def test_empty_report(self):
        text = format_empty_report(5)
        assert "SCAN COMPLETED" in text
        assert "Parent events discovered globally: 5" in text
        assert "Child markets discovered globally: 5" in text

    def test_concise_opportunity_fields(self):
        from app.signals.market_analysis import MarketAnalysis
        from app.polymarket.market_type import NormalizedTemperatureType
        from datetime import datetime, timezone

        item = MarketAnalysis(
            market_title="Test",
            market_url="https://polymarket.com/market/test",
            city_location="Munich",
            station_code="EDDM",
            resolution_source="weather_underground",
            target_date="July 16",
            market_type=TemperatureMarketType.BRACKET,
            threshold_f=89.6,
            threshold_unit="CELSIUS",
            yes_price=0.06,
            implied_probability=0.06,
            model_probability=0.45,
            edge=0.39,
            ev=0.35,
            kelly_fraction=0.02,
            kelly_cap=0.02,
            confidence="HIGH",
            recommendation_tier="NORMAL BET",
            reason="test",
            data_sources=["weather_underground"],
            analyzed_at=datetime.now(timezone.utc).isoformat(),
            message="msg",
            confidence_score=0.8,
            best_outcome="32°C",
            forecast_display="31°C",
            temperature_type=NormalizedTemperatureType.DAILY_HIGH,
            temperature_type_label="Daily high temperature",
            comparison_text="Forecast is closest to the 32°C bracket.",
        )
        text = format_concise_opportunity(item)
        assert "🌤 MUNICH" in text
        assert "Market: Daily high temperature" in text
        assert "32°C" in text


class TestSchedule:
    def test_schedule_times_configured(self):
        settings = make_test_settings(
            auto_reports_enabled=True,
            report_timezone="America/Chicago",
            report_times=["08:00", "12:00", "20:00"],
        )
        assert settings.scheduled_reports_configured is True
        assert settings.report_times == ["08:00", "12:00", "20:00"]

    def test_schedule_jobs_registered(self, monkeypatch):
        settings = make_test_settings(
            auto_reports_enabled=True,
            report_timezone="America/Chicago",
            report_times=["08:00", "12:00", "20:00"],
        )

        class FakeJobQueue:
            def __init__(self):
                self.jobs = []

            def run_daily(self, callback, time, name):
                self.jobs.append(name)

        from datetime import timezone

        app = type("App", (), {"job_queue": FakeJobQueue(), "bot_data": {}})()
        monkeypatch.setattr(
            "app.telegram.handlers.load_timezone",
            lambda _key: timezone.utc,
        )
        schedule_report_jobs(app, settings)
        schedule_report_jobs(app, settings)
        assert len(app.job_queue.jobs) == 3

    def test_schedule_disabled_registers_no_jobs(self):
        settings = make_test_settings(auto_reports_enabled=False)

        class FakeJobQueue:
            def __init__(self):
                self.jobs = []

            def run_daily(self, callback, time, name):
                self.jobs.append(name)

        app = type("App", (), {"job_queue": FakeJobQueue(), "bot_data": {}})()
        schedule_report_jobs(app, settings)
        assert app.job_queue.jobs == []


class TestMetrics:
    def test_zero_anthropic_by_default(self):
        metrics = ScanMetrics()
        assert metrics.anthropic_requests == 0

    def test_city_extraction(self):
        city = extract_city_from_text(
            "Will the highest temperature in Chicago be 90°F on July 15?"
        )
        assert city == "Chicago"
