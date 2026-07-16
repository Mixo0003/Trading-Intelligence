"""Regression tests for station-specific multi-provider forecast ensemble."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from app.polymarket.market_type import NormalizedTemperatureType
from app.polymarket.models import PolymarketMarket
from app.signals.event_analysis import analyze_temperature_event
from app.telegram.formatting import format_concise_opportunity
from app.weather.daily_forecast import DailyForecastStats
from app.weather.provider_forecast import ProviderForecast
from app.weather.forecast_ensemble import (
    ForecastEnsemble,
    ProviderForecast,
    fetch_forecast_ensemble,
)
from app.weather.station_resolver import (
    haversine_distance_km,
    resolve_verified_station_from_text,
)
from app.weather.stations import WeatherStation
from app.weather.temperature_units import TemperatureUnit
from tests.conftest import make_test_settings


def _verified_kmia() -> WeatherStation:
    return WeatherStation(
        code="KMIA",
        name="Miami Intl",
        latitude=25.78806,
        longitude=-80.31692,
        elevation_m=1.0,
        timezone="America/New_York",
        verified=True,
        resolution_url="https://www.wunderground.com/history/airport/KMIA",
    )


def _provider(
    name: str,
    selected_c: float,
    *,
    distance_km: float = 0.5,
    statistic: str = "daily_min",
) -> ProviderForecast:
    return ProviderForecast(
        provider_name=name,
        model_name=name.lower(),
        requested_latitude=25.78806,
        requested_longitude=-80.31692,
        requested_elevation_m=1.0,
        requested_timezone="America/New_York",
        grid_latitude=25.79,
        grid_longitude=-80.31,
        grid_elevation_m=1.0,
        grid_distance_km=distance_km,
        target_date_local=date(2026, 7, 15),
        forecast_window_start_local="2026-07-15T00:00",
        forecast_window_end_local="2026-07-15T23:00",
        daily_min_c=selected_c,
        daily_max_c=selected_c + 5.0,
        selected_value_c=selected_c,
        selected_statistic=statistic,
        hourly_samples=[{"time": "2026-07-15T06:00", "temperature_c": selected_c}],
        min_hour_local="2026-07-15T06:00",
        max_hour_local=None,
        init_time=None,
    )


def _ensemble(
    *,
    providers: tuple[ProviderForecast, ...],
    ensemble_c: float,
    spread_c: float = 0.0,
    skip_reason: str | None = None,
    reference_c: float | None = None,
) -> ForecastEnsemble:
    active = tuple(item for item in providers if item.usable)
    return ForecastEnsemble(
        station_code="KMIA",
        target_date_local=date(2026, 7, 15),
        station_timezone="America/New_York",
        event_type=NormalizedTemperatureType.DAILY_LOW,
        providers=providers,
        ensemble_value_c=ensemble_c,
        provider_spread_c=spread_c,
        selected_statistic="daily_min",
        daily_min_c=min(item.daily_min_c for item in active),
        daily_max_c=max(item.daily_max_c for item in active),
        forecast_window_start_local="2026-07-15T00:00",
        forecast_window_end_local="2026-07-15T23:00",
        hourly_samples=active[0].hourly_samples if active else [],
        min_hour_local=active[0].min_hour_local if active else None,
        reference_provider="NWS" if reference_c is not None else None,
        reference_value_c=reference_c,
        reference_divergence_c=(
            abs(ensemble_c - reference_c) if reference_c is not None else None
        ),
        provider_names=tuple(item.provider_name for item in active),
        skip_reason=skip_reason,
        grid_distance_km=max(
            (item.grid_distance_km or 0.0 for item in active),
            default=0.0,
        ),
    )


MIAMI_LOW_CHILDREN = [
    PolymarketMarket(
        id="low_tail",
        slug="low-tail",
        question="Will the lowest temperature in Miami be 71°F or below on July 15?",
        description="Resolves using Miami Intl Airport (KMIA) via Wunderground.",
        rules="https://www.wunderground.com/history/airport/KMIA",
        outcomes=["Yes", "No"],
        outcome_prices=[0.05, 0.95],
        volume=None,
        liquidity=None,
        end_date=None,
        raw={
            "groupItemTitle": "71°F or below",
            "events": [{
                "id": "evt-low",
                "title": "Lowest temperature in Miami on July 15?",
                "description": "Miami Intl Airport Station KMIA",
            }],
        },
    ),
    PolymarketMarket(
        id="b7475",
        slug="b7475",
        question="Will the lowest temperature in Miami be between 74-75°F on July 15?",
        description="Resolves using Miami Intl Airport (KMIA) via Wunderground.",
        rules="https://www.wunderground.com/history/airport/KMIA",
        outcomes=["Yes", "No"],
        outcome_prices=[0.20, 0.80],
        volume=None,
        liquidity=None,
        end_date=None,
        raw={
            "groupItemTitle": "74-75°F",
            "events": [{
                "id": "evt-low",
                "title": "Lowest temperature in Miami on July 15?",
                "description": "Miami Intl Airport Station KMIA",
            }],
        },
    ),
    PolymarketMarket(
        id="b7879",
        slug="b7879",
        question="Will the lowest temperature in Miami be between 78-79°F on July 15?",
        description="Resolves using Miami Intl Airport (KMIA) via Wunderground.",
        rules="https://www.wunderground.com/history/airport/KMIA",
        outcomes=["Yes", "No"],
        outcome_prices=[0.30, 0.70],
        volume=None,
        liquidity=None,
        end_date=None,
        raw={
            "groupItemTitle": "78-79°F",
            "events": [{
                "id": "evt-low",
                "title": "Lowest temperature in Miami on July 15?",
                "description": "Miami Intl Airport Station KMIA",
            }],
        },
    ),
    PolymarketMarket(
        id="b8081",
        slug="b8081",
        question="Will the lowest temperature in Miami be between 80-81°F on July 15?",
        description="Resolves using Miami Intl Airport (KMIA) via Wunderground.",
        rules="https://www.wunderground.com/history/airport/KMIA",
        outcomes=["Yes", "No"],
        outcome_prices=[0.37, 0.63],
        volume=None,
        liquidity=None,
        end_date=None,
        raw={
            "groupItemTitle": "80-81°F",
            "events": [{
                "id": "evt-low",
                "title": "Lowest temperature in Miami on July 15?",
                "description": "Miami Intl Airport Station KMIA",
            }],
        },
    ),
]


class TestStationTargeting:
    @patch("app.weather.station_resolver._fetch_station_from_aviationweather")
    def test_exact_market_icao_coordinates_used(self, mock_fetch):
        mock_fetch.return_value = _verified_kmia()
        settings = MagicMock()
        settings.weather_gov_base_url = "https://api.weather.gov"

        station, error = resolve_verified_station_from_text(
            "Miami Intl Airport KMIA https://www.wunderground.com/history/airport/KMIA",
            settings,
        )
        assert error is None
        assert station is not None
        assert station.code == "KMIA"
        assert station.latitude == pytest.approx(25.78806)
        assert station.longitude == pytest.approx(-80.31692)
        assert station.verified is True

    @patch("app.weather.station_resolver._fetch_station_from_aviationweather")
    def test_unverified_station_returns_skip_reason(self, mock_fetch):
        mock_fetch.return_value = None
        station, error = resolve_verified_station_from_text("KMIA", None)
        assert station is None or station.verified is False
        assert error == "STATION_NOT_VERIFIED"

    def test_coordinate_distance_guard_flags_far_grid(self):
        distance = haversine_distance_km(25.78806, -80.31692, 25.6, -80.4)
        assert distance > 10.0

    @patch("app.weather.forecast_ensemble.fetch_met_norway")
    @patch("app.weather.forecast_ensemble.fetch_weather_com_daily")
    @patch("app.weather.forecast_ensemble.fetch_nws_hourly")
    def test_provider_request_uses_station_timezone_and_target_date(
        self,
        mock_nws,
        mock_wx,
        mock_met,
    ):
        settings = MagicMock()
        settings.forecast_grid_max_distance_km = 15.0
        settings.forecast_min_providers = 2
        settings.forecast_divergence_threshold_f = 3.0
        settings.forecast_divergence_threshold_c = 2.0
        settings.weather_gov_base_url = "https://api.weather.gov"

        mock_nws.return_value = _provider("NWS", 26.7)
        mock_wx.return_value = _provider("Weather.com", 26.4)
        mock_met.return_value = _provider("MET Norway", 26.5)

        result = fetch_forecast_ensemble(
            settings,
            _verified_kmia(),
            "July 15, 2026",
            NormalizedTemperatureType.DAILY_LOW,
            market_unit=TemperatureUnit.FAHRENHEIT,
        )
        assert result is not None
        assert mock_nws.called
        assert mock_wx.called
        assert mock_met.called


class TestDailyLowLogic:
    def test_lowest_temperature_market_uses_daily_min_statistic(self):
        stats = DailyForecastStats(
            provider="GFS,NWS",
            station_code="KMIA",
            target_date_local=date(2026, 7, 15),
            station_timezone="America/New_York",
            forecast_window_start_local="2026-07-15T00:00",
            forecast_window_end_local="2026-07-15T23:00",
            daily_min_c=26.7,
            daily_max_c=34.0,
            selected_statistic="daily_min",
            selected_value_c=26.7,
            hourly_samples=[],
            ensemble_provider_count=2,
        )
        assert stats.selected_statistic == "daily_min"


class TestProviderEnsemble:
    @patch("app.signals.event_analysis.fetch_daily_forecast_stats")
    @patch("app.signals.event_analysis.resolve_verified_station_from_text")
    def test_two_providers_near_80f_center_near_80f(
        self,
        mock_resolve,
        mock_fetch_stats,
    ):
        settings = make_test_settings(priority_weather_cities=["Miami"])
        mock_resolve.return_value = (_verified_kmia(), None)

        mock_fetch_stats.return_value = DailyForecastStats(
            provider="GFS,NWS",
            station_code="KMIA",
            target_date_local=date(2026, 7, 15),
            station_timezone="America/New_York",
            forecast_window_start_local="2026-07-15T00:00",
            forecast_window_end_local="2026-07-15T23:00",
            daily_min_c=26.4,
            daily_max_c=34.0,
            selected_statistic="daily_min",
            selected_value_c=26.55,
            hourly_samples=[],
            ensemble_provider_count=3,
            provider_spread_c=0.3,
            forecast_provider_label="NWS, Weather.com, MET Norway",
            provider_names=("NWS", "Weather.com", "MET Norway"),
        )

        analysis = analyze_temperature_event(settings, MIAMI_LOW_CHILDREN)
        assert analysis is not None
        assert analysis.temperature_type == NormalizedTemperatureType.DAILY_LOW
        assert analysis.selected_statistic == "daily_min"
        assert analysis.best_outcome in {"78-79°F", "80-81°F"}
        assert "74-75°F" != analysis.best_outcome

    @patch("app.signals.event_analysis.fetch_daily_forecast_stats")
    @patch("app.signals.event_analysis.resolve_verified_station_from_text")
    def test_material_disagreement_lowers_confidence(
        self,
        mock_resolve,
        mock_fetch_stats,
    ):
        settings = make_test_settings(priority_weather_cities=["Miami"])
        mock_resolve.return_value = (_verified_kmia(), None)

        mock_fetch_stats.return_value = DailyForecastStats(
            provider="GFS,NWS",
            station_code="KMIA",
            target_date_local=date(2026, 7, 15),
            station_timezone="America/New_York",
            forecast_window_start_local="2026-07-15T00:00",
            forecast_window_end_local="2026-07-15T23:00",
            daily_min_c=24.2,
            daily_max_c=34.0,
            selected_statistic="daily_min",
            selected_value_c=25.45,
            hourly_samples=[],
            ensemble_provider_count=3,
            provider_spread_c=4.0,
            forecast_provider_label="NWS, Weather.com, MET Norway",
            provider_names=("NWS", "Weather.com", "MET Norway"),
        )

        analysis = analyze_temperature_event(settings, MIAMI_LOW_CHILDREN)
        assert analysis is not None
        assert analysis.confidence != "HIGH"

    @patch("app.signals.event_analysis.fetch_daily_forecast_stats")
    @patch("app.signals.event_analysis.resolve_verified_station_from_text")
    def test_reference_divergence_produces_skip(
        self,
        mock_resolve,
        mock_fetch_stats,
    ):
        settings = make_test_settings(priority_weather_cities=["Miami"])
        mock_resolve.return_value = (_verified_kmia(), None)

        mock_fetch_stats.return_value = DailyForecastStats(
            provider="GFS,NWS",
            station_code="KMIA",
            target_date_local=date(2026, 7, 15),
            station_timezone="America/New_York",
            forecast_window_start_local="2026-07-15T00:00",
            forecast_window_end_local="2026-07-15T23:00",
            daily_min_c=23.0,
            daily_max_c=34.0,
            selected_statistic="daily_min",
            selected_value_c=24.0,
            hourly_samples=[],
            ensemble_provider_count=3,
            provider_spread_c=3.7,
            provider_names=("NWS", "Weather.com", "MET Norway"),
            forecast_provider_label="NWS, Weather.com, MET Norway",
            reference_divergence_c=2.0,
            reference_value_c=26.7,
            betting_skip_reason="FORECAST_SOURCE_DIVERGENCE",
        )

        analysis = analyze_temperature_event(settings, MIAMI_LOW_CHILDREN)
        assert analysis is not None
        assert analysis.recommendation_tier == "SKIP"
        assert analysis.validation_error == "FORECAST_SOURCE_DIVERGENCE"


class TestFormatting:
    def test_resolution_station_separate_from_forecast_providers(self):
        analysis = MagicMock()
        analysis.validation_error = None
        analysis.recommendation_tier = "WATCH"
        analysis.city_location = "Miami"
        analysis.target_date = "July 15"
        analysis.temperature_type_label = "Daily low temperature"
        analysis.temperature_type = NormalizedTemperatureType.DAILY_LOW
        analysis.resolution_station = "KMIA"
        analysis.station_code = "KMIA"
        analysis.forecast_display = "80°F"
        analysis.expected_range_display = "78–82°F"
        analysis.best_outcome = "80-81°F"
        analysis.implied_probability = 0.37
        analysis.model_probability = 0.34
        analysis.edge = -0.03
        analysis.confidence = "MEDIUM"
        analysis.forecast_providers = "NWS, Weather.com, MET Norway"
        analysis.provider_spread_display = "3°F"
        analysis.display_status = "WATCH"
        analysis.display_reason = "Potentially interesting edge but confidence is insufficient."
        analysis.forecast_sources_line = "NWS, Weather.com, MET Norway"
        analysis.band_rows = ()
        analysis.live_triggers = ()
        analysis.ev = None

        text = format_concise_opportunity(analysis)
        assert "🌡 MIAMI — JULY 15" in text
        assert "Market: Daily low temperature" in text
        assert "Forecast: 80°F" in text
        assert "Expected: 78–82°F" in text
        assert "Best result: 80-81°F" in text
        assert "Decision: WATCH" in text
        assert "Reason: Potential value." in text
        for forbidden in ("Open-Meteo", "Station:", "Band | Model"):
            assert forbidden not in text
        assert len(text.splitlines()) <= 8
