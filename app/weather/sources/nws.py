"""NWS weather.gov point and hourly forecasts for exact station coordinates."""

from __future__ import annotations

import logging
from datetime import date

from app.config.settings import Settings
from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.provider_forecast import ProviderForecast
from app.weather.sources.common import (
    build_provider_forecast,
    daily_stats_from_hourly,
    fahrenheit_to_celsius,
    hourly_for_local_day,
    track_weather_request,
)
from app.weather.stations import WeatherStation

import requests

logger = logging.getLogger(__name__)

_NWS_HEADERS = {
    "User-Agent": "TradingIntelligence/1.0 github.com/Mixo0003/Trading-Intelligence",
    "Accept": "application/geo+json, application/json",
}


def fetch_nws_hourly(
    settings: Settings,
    station: WeatherStation,
    target: date,
    event_type: NormalizedTemperatureType,
    timezone: str,
) -> ProviderForecast | None:
    try:
        points_url = (
            f"{settings.weather_gov_base_url}/points/"
            f"{station.latitude},{station.longitude}"
        )
        points_response = requests.get(points_url, headers=_NWS_HEADERS, timeout=20)
        track_weather_request()
        points_response.raise_for_status()
        hourly_url = points_response.json().get("properties", {}).get("forecastHourly")
        if not hourly_url:
            return None

        hourly_response = requests.get(hourly_url, headers=_NWS_HEADERS, timeout=20)
        track_weather_request()
        hourly_response.raise_for_status()
        periods = hourly_response.json().get("properties", {}).get("periods", []) or []
    except Exception as exc:
        logger.warning("NWS hourly fetch failed for %s: %s", station.code, exc)
        return None

    target_iso = target.isoformat()
    hourly_samples: list[tuple[str, float]] = []
    for period in periods:
        start_time = str(period.get("startTime") or "")
        if not start_time.startswith(target_iso):
            continue
        temp = period.get("temperature")
        if temp is None:
            continue
        unit = str(period.get("temperatureUnit") or "F").upper()
        temp_c = (
            fahrenheit_to_celsius(float(temp))
            if unit.startswith("F")
            else float(temp)
        )
        hourly_samples.append((start_time, temp_c))

    if not hourly_samples:
        return None

    (
        daily_min,
        daily_max,
        selected,
        statistic,
        window_start,
        window_end,
        min_hour,
        max_hour,
    ) = daily_stats_from_hourly(hourly_samples, event_type)

    return build_provider_forecast(
        provider_name="NWS",
        model_name="forecastHourly",
        station=station,
        target=target,
        event_type=event_type,
        timezone=timezone,
        selected_c=selected,
        daily_min_c=daily_min,
        daily_max_c=daily_max,
        statistic=statistic,
        hourly_samples=hourly_samples,
        window_start=window_start,
        window_end=window_end,
        min_hour=min_hour,
        max_hour=max_hour,
    )


def fetch_nws_daily(
    settings: Settings,
    station: WeatherStation,
    target: date,
    event_type: NormalizedTemperatureType,
    timezone: str,
) -> ProviderForecast | None:
    try:
        points_url = (
            f"{settings.weather_gov_base_url}/points/"
            f"{station.latitude},{station.longitude}"
        )
        points_response = requests.get(points_url, headers=_NWS_HEADERS, timeout=20)
        track_weather_request()
        points_response.raise_for_status()
        forecast_url = points_response.json().get("properties", {}).get("forecast")
        if not forecast_url:
            return None

        forecast_response = requests.get(forecast_url, headers=_NWS_HEADERS, timeout=20)
        track_weather_request()
        forecast_response.raise_for_status()
        periods = forecast_response.json().get("properties", {}).get("periods", []) or []
    except Exception as exc:
        logger.warning("NWS daily fetch failed for %s: %s", station.code, exc)
        return None

    weekday = target.strftime("%A").lower()
    temps: list[float] = []
    wx_notes: list[str] = []
    for period in periods:
        name = str(period.get("name", "")).lower()
        if weekday not in name and target.isoformat() not in name:
            continue
        temp = period.get("temperature")
        if temp is None:
            continue
        unit = str(period.get("temperatureUnit") or "F").upper()
        temp_c = (
            fahrenheit_to_celsius(float(temp))
            if unit.startswith("F")
            else float(temp)
        )
        temps.append(temp_c)
        short = period.get("shortForecast")
        if short:
            wx_notes.append(str(short))

    if not temps:
        return None

    if event_type == NormalizedTemperatureType.DAILY_LOW:
        selected = min(temps)
        statistic = "daily_min"
    else:
        selected = max(temps)
        statistic = "daily_max"

    daily_min = min(temps)
    daily_max = max(temps)
    window_start = f"{target.isoformat()}T00:00"
    window_end = f"{target.isoformat()}T23:59"

    return build_provider_forecast(
        provider_name="NWS",
        model_name="forecastGrid",
        station=station,
        target=target,
        event_type=event_type,
        timezone=timezone,
        selected_c=selected,
        daily_min_c=daily_min,
        daily_max_c=daily_max,
        statistic=statistic,
        hourly_samples=[(window_start, selected)],
        window_start=window_start,
        window_end=window_end,
        min_hour=None,
        max_hour=None,
        init_time="; ".join(wx_notes[:2]) if wx_notes else None,
    )
