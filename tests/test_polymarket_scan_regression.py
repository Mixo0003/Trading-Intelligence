import json
from pathlib import Path

import pytest

from app.polymarket.discovery import SkipReason, evaluate_market_eligibility
from app.polymarket.market_text import build_market_context_text, is_temperature_market_candidate
from app.polymarket.markets import get_weather_markets
from app.polymarket.parser import parse_market
from app.weather.resolution import parse_resolution_details


FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str):
    return parse_market(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


class TestScanRegressionFixtures:
    def test_munich_extracts_icao_station_celsius_threshold_and_source(self):
        market = _load_fixture("polymarket_munich_temperature.json")
        text = build_market_context_text(market)
        resolution = parse_resolution_details(text)

        assert is_temperature_market_candidate(market) is True
        assert resolution.source == "weather_underground"
        assert resolution.station_code == "EDDM"
        assert resolution.threshold_f == pytest.approx(89.6, abs=0.1)
        assert resolution.target_date is not None

        ok, reason, _ = evaluate_market_eligibility(market, set())
        assert ok is True
        assert reason is None

    def test_golden_glove_excluded_from_temperature_discovery(self):
        market = _load_fixture("polymarket_golden_glove.json")
        assert is_temperature_market_candidate(market) is False

        ok, reason, _ = evaluate_market_eligibility(market, set())
        assert ok is False
        assert reason == SkipReason.NOT_WEATHER

    def test_netflix_daily_high_excluded_from_temperature_discovery(self):
        market = _load_fixture("polymarket_nflx_daily_high.json")
        assert is_temperature_market_candidate(market) is False

        ok, reason, _ = evaluate_market_eligibility(market, set())
        assert ok is False
        assert reason == SkipReason.NOT_WEATHER

    def test_qingdao_station_from_wunderground_url(self):
        market = _load_fixture("polymarket_munich_temperature.json")
        market = parse_market({
            **json.loads((FIXTURES / "polymarket_munich_temperature.json").read_text(encoding="utf-8")),
            "question": "Will the highest temperature in Qingdao be 30°C on July 14?",
            "slug": "highest-temperature-in-qingdao-on-july-14-2026-30c",
            "description": "Qingdao Jiaodong International Airport Station on 14 Jul '26. https://www.wunderground.com/history/daily/cn/qingdao/ZSQD",
            "resolutionSource": "https://www.wunderground.com/history/daily/cn/qingdao/ZSQD",
            "groupItemTitle": "30°C",
        })
        resolution = parse_resolution_details(build_market_context_text(market))
        assert resolution.station_code == "ZSQD"
        assert resolution.threshold_f == pytest.approx(86.0, abs=0.2)


class TestDiscoveryFilter:
    def test_get_weather_markets_requires_temperature_evidence(self, monkeypatch):
        munich = _load_fixture("polymarket_munich_temperature.json")
        glove = _load_fixture("polymarket_golden_glove.json")
        nflx = _load_fixture("polymarket_nflx_daily_high.json")

        monkeypatch.setattr(
            "app.polymarket.markets._collect_markets_from_events",
            lambda client, limit: [],
        )
        monkeypatch.setattr(
            "app.polymarket.markets.get_active_markets",
            lambda client, limit=100: [munich, glove, nflx],
        )

        results, worldwide, priority = get_weather_markets(
            client=None, limit=10, priority_cities=["Chicago"]
        )
        assert len(results) == 1
        assert "Munich" in (results[0].question or "")
