"""Regression tests for Weather discovery 422 hotfix + stale forecast filter."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.polymarket.discovery_errors import WeatherDiscoveryError
from app.polymarket.market_type import NormalizedTemperatureType, TemperatureMarketType
from app.polymarket.markets import _collect_markets_from_events, get_weather_markets
from app.polymarket.models import PolymarketMarket
from app.region.types import ScanRegion
from app.signals.forecast_display import DISPLAY_FORECAST_ONLY
from app.signals.market_analysis import MarketAnalysis, ScanProgress
from app.telegram.formatting import format_today_forecast_report
from app.telegram.handlers import (
    SCAN_DISCOVERY_UNAVAILABLE_MESSAGE,
    SCAN_FAILED_MESSAGE,
)
from app.weather.forecast_ensemble import fetch_forecast_ensemble
from app.weather.provider_forecast import ProviderForecast
from app.weather.stations import WeatherStation
from tests.conftest import make_test_settings


def _market(question: str, market_id: str = "m1") -> PolymarketMarket:
    return PolymarketMarket(
        id=market_id,
        slug="highest-temperature-in-austin-on-july-18-2026",
        question=question,
        description="Daily high temperature market",
        rules="",
        outcomes=["Yes", "No"],
        outcome_prices=[0.4, 0.6],
        volume=1000.0,
        liquidity=500.0,
        end_date=None,
        raw={"events": [{"id": "e1", "slug": "highest-temperature-in-austin-on-july-18-2026"}]},
    )


def _http_error(status: int) -> requests.HTTPError:
    response = MagicMock()
    response.status_code = status
    return requests.HTTPError(f"{status} Client Error", response=response)


def _analysis(**kwargs) -> MarketAnalysis:
    today = datetime.now(timezone.utc).date().isoformat()
    defaults = dict(
        market_title="Highest temperature in Paris?",
        market_url="https://polymarket.com/event/paris",
        city_location="Paris",
        station_code="LFPB",
        resolution_source="weather_underground",
        target_date="July 18",
        market_type=TemperatureMarketType.MAXIMUM_TEMPERATURE,
        threshold_f=87.8,
        threshold_unit="CELSIUS",
        yes_price=0.25,
        implied_probability=0.25,
        model_probability=0.34,
        edge=0.09,
        ev=0.09,
        kelly_fraction=0.02,
        kelly_cap=0.02,
        confidence="MEDIUM",
        recommendation_tier="SKIP",
        reason="test",
        data_sources=["MET Norway", "ECMWF IFS", "DWD ICON Global"],
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        message="",
        confidence_score=0.8,
        best_outcome="31°C",
        forecast_display="31°C",
        temperature_type=NormalizedTemperatureType.DAILY_HIGH,
        temperature_type_label="Daily high temperature",
        display_status=DISPLAY_FORECAST_ONLY,
        forecast_confidence="LOW",
        display_reason="Insufficient sources.",
        station_timezone="UTC",
        target_date_local=today,
    )
    defaults.update(kwargs)
    return MarketAnalysis(**defaults)


def _prov(name: str, val: float, station: WeatherStation, day: date) -> ProviderForecast:
    return ProviderForecast(
        provider_name=name,
        model_name="test",
        requested_latitude=station.latitude,
        requested_longitude=station.longitude,
        requested_elevation_m=station.elevation_m,
        requested_timezone=station.timezone or "UTC",
        grid_latitude=station.latitude,
        grid_longitude=station.longitude,
        grid_elevation_m=station.elevation_m,
        grid_distance_km=0.5,
        target_date_local=day,
        forecast_window_start_local=f"{day.isoformat()}T00:00",
        forecast_window_end_local=f"{day.isoformat()}T23:00",
        daily_min_c=val - 5,
        daily_max_c=val,
        selected_value_c=val,
        selected_statistic="daily_max",
        hourly_samples=[],
        min_hour_local=None,
        max_hour_local=None,
        init_time=None,
    )


class TestDiscoverySoftFail:
    def test_events_collector_omits_unsupported_order_param(self):
        client = MagicMock()
        client.get_json.return_value = []
        _collect_markets_from_events(client, limit=50)
        params = client.get_json.call_args.kwargs["params"]
        assert "order" not in params
        assert "ascending" not in params
        assert params.get("active") == "true"
        assert params.get("closed") == "false"

    def test_events_422_falls_back_to_markets(self, monkeypatch):
        austin = _market("Highest temperature in Austin on July 18?")

        def boom_events(client, limit):
            raise _http_error(422)

        monkeypatch.setattr(
            "app.polymarket.markets._collect_markets_from_events",
            boom_events,
        )
        monkeypatch.setattr(
            "app.polymarket.markets.get_active_markets",
            lambda client, limit=100: [austin],
        )
        monkeypatch.setattr(
            "app.polymarket.markets._collect_markets_from_gamma_discovery",
            lambda client: (_ for _ in ()).throw(_http_error(500)),
        )

        results, direct, _priority = get_weather_markets(client=MagicMock(), limit=10)
        assert len(results) >= 1
        assert "Austin" in (results[0].question or "")
        assert direct >= 1

    def test_one_collector_fails_markets_succeeds(self, monkeypatch):
        austin = _market("Highest temperature in Dallas on July 18?", market_id="d1")

        monkeypatch.setattr(
            "app.polymarket.markets._collect_markets_from_events",
            lambda client, limit: (_ for _ in ()).throw(_http_error(422)),
        )
        monkeypatch.setattr(
            "app.polymarket.markets.get_active_markets",
            lambda client, limit=100: [austin],
        )
        monkeypatch.setattr(
            "app.polymarket.markets._collect_markets_from_gamma_discovery",
            lambda client: [],
        )

        results, _, _ = get_weather_markets(client=MagicMock(), limit=10)
        assert any("Dallas" in (m.question or "") for m in results)

    def test_all_discovery_paths_fail_raises_clean_error(self, monkeypatch):
        monkeypatch.setattr(
            "app.polymarket.markets.get_active_markets",
            lambda client, limit=100: (_ for _ in ()).throw(_http_error(503)),
        )
        monkeypatch.setattr(
            "app.polymarket.markets._collect_markets_from_events",
            lambda client, limit: (_ for _ in ()).throw(_http_error(422)),
        )
        monkeypatch.setattr(
            "app.polymarket.markets._collect_markets_from_gamma_discovery",
            lambda client: (_ for _ in ()).throw(_http_error(500)),
        )

        with pytest.raises(WeatherDiscoveryError) as exc_info:
            get_weather_markets(client=MagicMock(), limit=10)
        assert "discovery" in str(exc_info.value).lower()
        assert "http" not in str(exc_info.value).lower()
        assert "gamma" not in str(exc_info.value).lower()


class TestProviderIsolation:
    def test_optional_provider_exception_does_not_abort(self):
        settings = make_test_settings()
        day = date.today() + timedelta(days=1)
        station = WeatherStation(
            "EGLL", "London", 51.47, -0.46, timezone="Europe/London", verified=True
        )
        with (
            patch("app.weather.forecast_ensemble.fetch_weather_com_daily", side_effect=RuntimeError("boom")),
            patch(
                "app.weather.forecast_ensemble.fetch_met_norway",
                return_value=_prov("MET Norway", 24.0, station, day),
            ),
            patch(
                "app.weather.forecast_ensemble.fetch_ecmwf_ifs",
                return_value=_prov("ECMWF IFS", 25.0, station, day),
            ),
            patch(
                "app.weather.forecast_ensemble.fetch_dwd_icon_global",
                return_value=_prov("DWD ICON Global", 24.5, station, day),
            ),
        ):
            result = fetch_forecast_ensemble(
                settings,
                station,
                day.isoformat(),
                NormalizedTemperatureType.DAILY_HIGH,
            )
        assert result is not None
        assert result.betting_skip_reason is None
        assert set(result.provider_names) == {"MET Norway", "ECMWF IFS", "DWD ICON Global"}

    def test_three_independent_sources_not_insufficient(self):
        settings = make_test_settings()
        day = date.today() + timedelta(days=1)
        station = WeatherStation(
            "ZSPD", "Shanghai", 31.14, 121.80, timezone="Asia/Shanghai", verified=True
        )
        with (
            patch("app.weather.forecast_ensemble.fetch_weather_com_daily", return_value=None),
            patch(
                "app.weather.forecast_ensemble.fetch_met_norway",
                return_value=_prov("MET Norway", 32.0, station, day),
            ),
            patch(
                "app.weather.forecast_ensemble.fetch_ecmwf_ifs",
                return_value=_prov("ECMWF IFS", 33.0, station, day),
            ),
            patch(
                "app.weather.forecast_ensemble.fetch_dwd_icon_global",
                return_value=_prov("DWD ICON Global", 32.5, station, day),
            ),
        ):
            result = fetch_forecast_ensemble(
                settings,
                station,
                day.isoformat(),
                NormalizedTemperatureType.DAILY_HIGH,
            )
        assert result is not None
        assert len(result.provider_names) == 3
        assert result.betting_skip_reason is None

    def test_usa_counts_five_when_all_succeed(self):
        settings = make_test_settings()
        day = date.today() + timedelta(days=1)
        station = WeatherStation(
            "KAUS", "Austin", 30.19, -97.66, timezone="America/Chicago", verified=True
        )
        with (
            patch(
                "app.weather.forecast_ensemble.fetch_nws_hourly",
                return_value=_prov("NWS", 33.0, station, day),
            ),
            patch(
                "app.weather.forecast_ensemble.fetch_weather_com_daily",
                return_value=_prov("Weather.com", 34.0, station, day),
            ),
            patch(
                "app.weather.forecast_ensemble.fetch_met_norway",
                return_value=_prov("MET Norway", 32.0, station, day),
            ),
            patch(
                "app.weather.forecast_ensemble.fetch_ecmwf_ifs",
                return_value=_prov("ECMWF IFS", 32.5, station, day),
            ),
            patch(
                "app.weather.forecast_ensemble.fetch_dwd_icon_global",
                return_value=_prov("DWD ICON Global", 33.5, station, day),
            ),
        ):
            result = fetch_forecast_ensemble(
                settings,
                station,
                day.isoformat(),
                NormalizedTemperatureType.DAILY_HIGH,
            )
        assert result is not None
        assert set(result.provider_names) == {
            "NWS",
            "Weather.com",
            "MET Norway",
            "ECMWF IFS",
            "DWD ICON Global",
        }
        assert result.betting_skip_reason is None


class TestStaleForecastSnapshots:
    def test_stale_local_date_hidden(self):
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        progress = ScanProgress(
            discovered=1,
            scan_region="ASIA",
            all_analyses=[
                _analysis(
                    city_location="Shanghai",
                    target_date_local=yesterday,
                    station_timezone="Asia/Shanghai",
                )
            ],
        )
        text = format_today_forecast_report(progress, region=ScanRegion.ASIA)
        assert "No current forecast results. Run a new scan." in text
        assert "SHANGHAI" not in text

    def test_current_local_date_visible(self):
        today = datetime.now(timezone.utc).date().isoformat()
        progress = ScanProgress(
            discovered=1,
            scan_region="USA",
            all_analyses=[
                _analysis(
                    city_location="Austin",
                    target_date_local=today,
                    station_timezone="UTC",
                    display_reason="Limited confidence.",
                )
            ],
        )
        text = format_today_forecast_report(progress, region=ScanRegion.USA)
        assert "AUSTIN" in text
        assert "USA FORECASTS" in text

    def test_future_local_date_visible(self):
        tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        progress = ScanProgress(
            discovered=1,
            scan_region="EUROPE",
            all_analyses=[
                _analysis(
                    city_location="Munich",
                    target_date_local=tomorrow,
                    station_timezone="Europe/Berlin",
                )
            ],
        )
        text = format_today_forecast_report(progress, region=ScanRegion.EUROPE)
        assert "MUNICH" in text

    def test_all_stale_empty_state_message(self):
        stale = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
        progress = ScanProgress(
            discovered=2,
            scan_region="ALL",
            all_analyses=[
                _analysis(city_location="Paris", target_date_local=stale),
                _analysis(city_location="Tokyo", target_date_local=stale, station_timezone="Asia/Tokyo"),
            ],
        )
        text = format_today_forecast_report(progress, region=ScanRegion.ALL)
        assert text.endswith("No current forecast results. Run a new scan.")
        assert "PARIS" not in text
        assert "TOKYO" not in text


class TestTelegramErrorCopy:
    def test_discovery_message_is_not_provider_error(self):
        assert "provider" not in SCAN_DISCOVERY_UNAVAILABLE_MESSAGE.lower()
        assert "discovery" in SCAN_DISCOVERY_UNAVAILABLE_MESSAGE.lower()
        assert "provider" not in SCAN_FAILED_MESSAGE.lower()
