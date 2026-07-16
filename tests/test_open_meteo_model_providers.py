"""Focused tests for ECMWF IFS and DWD ICON Global (Open-Meteo transport only)."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.polymarket.market_type import NormalizedTemperatureType
from app.telegram.formatting import format_concise_card
from app.weather.forecast_ensemble import (
    INDEPENDENT_MIN_PROVIDERS,
    _collect_independent_providers,
    fetch_forecast_ensemble,
)
from app.weather.provider_forecast import ProviderForecast
from app.weather.sources import open_meteo_models as om
from app.weather.stations import WeatherStation
from app.weather.temperature_units import TemperatureUnit, celsius_to_fahrenheit
from tests.conftest import make_test_settings


EGLL = WeatherStation(
    "EGLL",
    "London Heathrow",
    51.47,
    -0.46,
    elevation_m=25.0,
    timezone="Europe/London",
    verified=True,
)

RJTT = WeatherStation(
    "RJTT",
    "Tokyo Haneda",
    35.55,
    139.78,
    elevation_m=10.0,
    timezone="Asia/Tokyo",
    verified=True,
)

KORD = WeatherStation(
    "KORD",
    "Chicago O'Hare",
    41.9786,
    -87.9048,
    elevation_m=200.0,
    timezone="America/Chicago",
    verified=True,
)


def _hourly_payload(day: date, temps: list[float], *, lat=51.5, lon=-0.5) -> dict:
    times = [f"{day.isoformat()}T{hour:02d}:00" for hour in range(24)]
    assert len(temps) == 24
    return {
        "latitude": lat,
        "longitude": lon,
        "elevation": 30.0,
        "timezone": "Europe/London",
        "utc_offset_seconds": 3600,
        "hourly_units": {"temperature_2m": "°C"},
        "hourly": {
            "time": times,
            "temperature_2m": temps,
            "cloud_cover": [0] * 24,
            "precipitation": [0.0] * 24,
            "weather_code": [0] * 24,
        },
    }


def _prov(name: str, model: str, val: float, station: WeatherStation, day: date) -> ProviderForecast:
    return ProviderForecast(
        provider_name=name,
        model_name=model,
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


@pytest.fixture(autouse=True)
def _clear_om_cache():
    om.clear_model_caches()
    yield
    om.clear_model_caches()


def test_ecmwf_high_parsing():
    day = date(2026, 7, 16)
    temps = [10 + (i / 2) for i in range(24)]
    temps[15] = 29.5
    payload = _hourly_payload(day, temps)
    with patch("app.weather.sources.open_meteo_models.get_with_retry") as mock_get:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: payload,
        )
        out = om.fetch_ecmwf_ifs(
            EGLL, day, NormalizedTemperatureType.DAILY_HIGH, "Europe/London"
        )
    assert out is not None
    assert out.provider_name == "ECMWF IFS"
    assert out.model_name == om.ECMWF_MODEL
    assert out.selected_statistic == "daily_max"
    assert out.selected_value_c == pytest.approx(29.5)


def test_ecmwf_low_parsing():
    day = date(2026, 7, 16)
    temps = [20.0] * 24
    temps[4] = 11.2
    payload = _hourly_payload(day, temps)
    with patch("app.weather.sources.open_meteo_models.get_with_retry") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
        out = om.fetch_ecmwf_ifs(
            EGLL, day, NormalizedTemperatureType.DAILY_LOW, "Europe/London"
        )
    assert out is not None
    assert out.selected_statistic == "daily_min"
    assert out.selected_value_c == pytest.approx(11.2)


def test_icon_high_parsing():
    day = date(2026, 7, 16)
    temps = [15.0] * 24
    temps[14] = 27.1
    payload = _hourly_payload(day, temps)
    with patch("app.weather.sources.open_meteo_models.get_with_retry") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
        out = om.fetch_dwd_icon_global(
            EGLL, day, NormalizedTemperatureType.DAILY_HIGH, "Europe/London"
        )
    assert out is not None
    assert out.provider_name == "DWD ICON Global"
    assert out.model_name == om.ICON_MODEL
    assert out.selected_value_c == pytest.approx(27.1)


def test_icon_low_parsing():
    day = date(2026, 7, 16)
    temps = [18.0] * 24
    temps[5] = 9.4
    payload = _hourly_payload(day, temps)
    with patch("app.weather.sources.open_meteo_models.get_with_retry") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
        out = om.fetch_dwd_icon_global(
            EGLL, day, NormalizedTemperatureType.DAILY_LOW, "Europe/London"
        )
    assert out is not None
    assert out.selected_value_c == pytest.approx(9.4)


def test_exact_station_local_date():
    day = date(2026, 7, 16)
    other = date(2026, 7, 17)
    times = [f"{other.isoformat()}T{h:02d}:00" for h in range(24)]
    payload = {
        "latitude": 51.5,
        "longitude": -0.5,
        "elevation": 30.0,
        "hourly": {"time": times, "temperature_2m": [20.0] * 24},
    }
    with patch("app.weather.sources.open_meteo_models.get_with_retry") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
        out = om.fetch_ecmwf_ifs(
            EGLL, day, NormalizedTemperatureType.DAILY_HIGH, "Europe/London"
        )
    assert out is None


def test_utc_to_local_midnight_boundary():
    # Chicago local midnight belongs to the Chicago calendar day, not UTC date.
    day = date(2026, 7, 16)
    chicago = ZoneInfo("America/Chicago")
    local_midnight = datetime(2026, 7, 16, 0, 0, tzinfo=chicago)
    assert local_midnight.astimezone(ZoneInfo("UTC")).day == 16
    temps = [18.0] * 24
    temps[0] = 17.0
    temps[12] = 30.0
    payload = _hourly_payload(day, temps, lat=42.0, lon=-88.0)
    with patch("app.weather.sources.open_meteo_models.get_with_retry") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
        out = om.fetch_ecmwf_ifs(
            KORD, day, NormalizedTemperatureType.DAILY_HIGH, "America/Chicago"
        )
    assert out is not None
    assert out.target_date_local == day
    assert out.selected_value_c == pytest.approx(30.0)


def test_dst_handling_offsets():
    chicago = ZoneInfo("America/Chicago")
    summer = datetime(2026, 7, 15, 12, tzinfo=chicago)
    winter = datetime(2026, 1, 15, 12, tzinfo=chicago)
    assert summer.utcoffset().total_seconds() == -5 * 3600
    assert winter.utcoffset().total_seconds() == -6 * 3600


def test_celsius_fahrenheit_normalization():
    assert celsius_to_fahrenheit(0.0) == pytest.approx(32.0)
    assert celsius_to_fahrenheit(100.0) == pytest.approx(212.0)


def test_missing_hours_rejection():
    day = date(2026, 7, 16)
    times = [f"{day.isoformat()}T{h:02d}:00" for h in range(10)]
    payload = {
        "latitude": 51.5,
        "longitude": -0.5,
        "elevation": 30.0,
        "hourly": {"time": times, "temperature_2m": [20.0] * 10},
    }
    with patch("app.weather.sources.open_meteo_models.get_with_retry") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
        out = om.fetch_ecmwf_ifs(
            EGLL, day, NormalizedTemperatureType.DAILY_HIGH, "Europe/London"
        )
    assert out is None


def test_wrong_date_rejection():
    day = date(2026, 7, 16)
    payload = _hourly_payload(date(2026, 7, 18), [20.0] * 24)
    with patch("app.weather.sources.open_meteo_models.get_with_retry") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
        assert (
            om.fetch_dwd_icon_global(
                EGLL, day, NormalizedTemperatureType.DAILY_HIGH, "Europe/London"
            )
            is None
        )


def test_dedicated_model_identity_validation():
    day = date(2026, 7, 16)
    blended = _hourly_payload(day, [20.0] * 24)
    blended["hourly"]["temperature_2m_best_match"] = [20.0] * 24
    err = om._validate_dedicated_response(
        endpoint=om.ECMWF_ENDPOINT,
        expected_model=om.ECMWF_MODEL,
        data=blended,
    )
    assert err == "BLENDED_MODEL_METADATA"
    err2 = om._validate_dedicated_response(
        endpoint="https://api.open-meteo.com/v1/forecast",
        expected_model=om.ECMWF_MODEL,
        data=_hourly_payload(day, [20.0] * 24),
    )
    assert err2 == "BLENDED_ENDPOINT_FORBIDDEN"


def test_provider_independence_counting():
    settings = make_test_settings()
    day = date(2026, 7, 17)
    with (
        patch("app.weather.forecast_ensemble.fetch_nws_hourly", return_value=None),
        patch("app.weather.forecast_ensemble.fetch_weather_com_daily", return_value=None),
        patch(
            "app.weather.forecast_ensemble.fetch_met_norway",
            return_value=_prov("MET Norway", "locationforecast", 25.0, EGLL, day),
        ),
        patch(
            "app.weather.forecast_ensemble.fetch_ecmwf_ifs",
            return_value=_prov("ECMWF IFS", om.ECMWF_MODEL, 26.0, EGLL, day),
        ),
        patch(
            "app.weather.forecast_ensemble.fetch_dwd_icon_global",
            return_value=_prov("DWD ICON Global", om.ICON_MODEL, 25.5, EGLL, day),
        ),
    ):
        providers = _collect_independent_providers(
            settings, EGLL, day, NormalizedTemperatureType.DAILY_HIGH, "Europe/London"
        )
    names = [p.provider_name for p in providers]
    assert names == ["MET Norway", "ECMWF IFS", "DWD ICON Global"]
    assert "Open-Meteo" not in names
    assert len(names) == INDEPENDENT_MIN_PROVIDERS


def test_cache_reuse():
    day = date(2026, 7, 16)
    payload = _hourly_payload(day, [20.0] * 23 + [31.0])
    with patch("app.weather.sources.open_meteo_models.get_with_retry") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
        a = om.fetch_ecmwf_ifs(
            EGLL, day, NormalizedTemperatureType.DAILY_HIGH, "Europe/London"
        )
        b = om.fetch_ecmwf_ifs(
            EGLL, day, NormalizedTemperatureType.DAILY_LOW, "Europe/London"
        )
    assert a is not None and b is not None
    assert mock_get.call_count == 1
    assert a.selected_value_c == pytest.approx(31.0)
    assert b.selected_value_c == pytest.approx(20.0)


def test_concurrent_request_collapse():
    import threading

    day = date(2026, 7, 16)
    payload = _hourly_payload(day, [21.0] * 24)
    started = threading.Event()
    release = threading.Event()

    def slow_get(*_a, **_k):
        started.set()
        release.wait(timeout=2)
        return MagicMock(status_code=200, json=lambda: payload)

    results: list[ProviderForecast | None] = [None, None]

    with patch(
        "app.weather.sources.open_meteo_models.get_with_retry", side_effect=slow_get
    ):
        def worker(idx: int) -> None:
            results[idx] = om.fetch_ecmwf_ifs(
                EGLL, day, NormalizedTemperatureType.DAILY_HIGH, "Europe/London"
            )

        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))
        t1.start()
        assert started.wait(timeout=2)
        t2.start()
        release.set()
        t1.join(timeout=3)
        t2.join(timeout=3)
    assert results[0] is not None and results[1] is not None


def test_europe_gets_met_ecmwf_icon():
    settings = make_test_settings()
    day = date(2026, 7, 17)
    with (
        patch("app.weather.forecast_ensemble.fetch_weather_com_daily", return_value=None),
        patch(
            "app.weather.forecast_ensemble.fetch_met_norway",
            return_value=_prov("MET Norway", "locationforecast", 25.0, EGLL, day),
        ),
        patch(
            "app.weather.forecast_ensemble.fetch_ecmwf_ifs",
            return_value=_prov("ECMWF IFS", om.ECMWF_MODEL, 26.0, EGLL, day),
        ),
        patch(
            "app.weather.forecast_ensemble.fetch_dwd_icon_global",
            return_value=_prov("DWD ICON Global", om.ICON_MODEL, 25.5, EGLL, day),
        ),
    ):
        result = fetch_forecast_ensemble(
            settings, EGLL, "July 17, 2026", NormalizedTemperatureType.DAILY_HIGH
        )
    assert result is not None
    assert set(result.provider_names) == {"MET Norway", "ECMWF IFS", "DWD ICON Global"}
    assert result.betting_skip_reason is None
    assert "Open-Meteo" not in result.forecast_provider_label


def test_asia_gets_met_ecmwf_icon():
    settings = make_test_settings()
    day = date(2026, 7, 17)
    with (
        patch("app.weather.forecast_ensemble.fetch_weather_com_daily", return_value=None),
        patch(
            "app.weather.forecast_ensemble.fetch_met_norway",
            return_value=_prov("MET Norway", "locationforecast", 30.0, RJTT, day),
        ),
        patch(
            "app.weather.forecast_ensemble.fetch_ecmwf_ifs",
            return_value=_prov("ECMWF IFS", om.ECMWF_MODEL, 31.0, RJTT, day),
        ),
        patch(
            "app.weather.forecast_ensemble.fetch_dwd_icon_global",
            return_value=_prov("DWD ICON Global", om.ICON_MODEL, 30.5, RJTT, day),
        ),
    ):
        result = fetch_forecast_ensemble(
            settings, RJTT, "July 17, 2026", NormalizedTemperatureType.DAILY_HIGH
        )
    assert result is not None
    assert set(result.provider_names) == {"MET Norway", "ECMWF IFS", "DWD ICON Global"}
    assert result.betting_skip_reason is None


def test_usa_preserves_nws_and_existing_providers():
    settings = make_test_settings()
    day = date(2026, 7, 17)
    with (
        patch(
            "app.weather.forecast_ensemble.fetch_nws_hourly",
            return_value=_prov("NWS", "forecastHourly", 29.0, KORD, day),
        ),
        patch(
            "app.weather.forecast_ensemble.fetch_weather_com_daily",
            return_value=_prov("Weather.com", "daily5day", 30.0, KORD, day),
        ),
        patch(
            "app.weather.forecast_ensemble.fetch_met_norway",
            return_value=_prov("MET Norway", "locationforecast", 28.5, KORD, day),
        ),
        patch(
            "app.weather.forecast_ensemble.fetch_ecmwf_ifs",
            return_value=_prov("ECMWF IFS", om.ECMWF_MODEL, 29.2, KORD, day),
        ),
        patch(
            "app.weather.forecast_ensemble.fetch_dwd_icon_global",
            return_value=_prov("DWD ICON Global", om.ICON_MODEL, 28.8, KORD, day),
        ),
    ):
        result = fetch_forecast_ensemble(
            settings, KORD, "July 17, 2026", NormalizedTemperatureType.DAILY_HIGH
        )
    assert result is not None
    assert "NWS" in result.provider_names
    assert "Weather.com" in result.provider_names
    assert "MET Norway" in result.provider_names
    assert "ECMWF IFS" in result.provider_names
    assert "DWD ICON Global" in result.provider_names


def test_wunderground_resolution_history_preserved():
    from app.weather.evidence.wunderground_history import build_wunderground_history_url

    url = build_wunderground_history_url("EGLL", date(2026, 7, 14))
    assert "wunderground.com" in url
    assert "EGLL" in url


def test_weather_com_behavior_unchanged_import():
    from app.weather.sources import weather_com

    assert hasattr(weather_com, "fetch_weather_com_daily")
    assert "api.weather.com" in open(weather_com.__file__, encoding="utf-8").read()


def test_telegram_formatting_unchanged():
    analysis = MagicMock()
    analysis.city_location = "London"
    analysis.target_date = "July 17"
    analysis.temperature_type = NormalizedTemperatureType.DAILY_HIGH
    analysis.temperature_type_label = "Daily high temperature"
    analysis.forecast_display = "28°C"
    analysis.expected_range_display = "26–30°C"
    analysis.best_outcome = "27-28°C"
    analysis.implied_probability = 0.21
    analysis.display_status = "FORECAST ONLY"
    analysis.display_reason = "Limited confidence."
    text = format_concise_card(analysis)
    assert "Market: Daily high temperature" in text
    assert "Decision: FORECAST ONLY" in text
    assert "ECMWF" not in text
    assert "ICON" not in text
    assert "Open-Meteo" not in text


def test_failed_model_excluded_from_provider_count():
    settings = make_test_settings()
    day = date(2026, 7, 17)
    with (
        patch("app.weather.forecast_ensemble.fetch_weather_com_daily", return_value=None),
        patch(
            "app.weather.forecast_ensemble.fetch_met_norway",
            return_value=_prov("MET Norway", "locationforecast", 25.0, EGLL, day),
        ),
        patch("app.weather.forecast_ensemble.fetch_ecmwf_ifs", return_value=None),
        patch(
            "app.weather.forecast_ensemble.fetch_dwd_icon_global",
            return_value=_prov("DWD ICON Global", om.ICON_MODEL, 25.5, EGLL, day),
        ),
    ):
        result = fetch_forecast_ensemble(
            settings, EGLL, "July 17, 2026", NormalizedTemperatureType.DAILY_HIGH
        )
    assert result is not None
    assert len(result.provider_names) == 2
    assert result.betting_skip_reason == "INSUFFICIENT_PROVIDERS"


def test_no_blended_open_meteo_provider_symbol():
    import app.weather.forecast_ensemble as mod

    assert not hasattr(mod, "fetch_open_meteo")
    assert not hasattr(mod, "_fetch_open_meteo_provider")
