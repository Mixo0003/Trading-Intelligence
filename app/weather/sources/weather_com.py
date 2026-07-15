"""Weather.com daily/hourly forecasts via ICAO station code."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.provider_forecast import ProviderForecast
from app.weather.sources.common import (
    build_provider_forecast,
    daily_stats_from_hourly,
    extract_weather_com_api_key,
    fahrenheit_to_celsius,
    track_weather_request,
)
from app.weather.stations import WeatherStation

import requests

logger = logging.getLogger(__name__)

_API_KEY_CACHE: str | None = None


def _api_key() -> str | None:
    global _API_KEY_CACHE
    if _API_KEY_CACHE:
        return _API_KEY_CACHE
    _API_KEY_CACHE = extract_weather_com_api_key()
    return _API_KEY_CACHE


def fetch_weather_com_daily(
    station: WeatherStation,
    target: date,
    event_type: NormalizedTemperatureType,
    tz_name: str,
) -> ProviderForecast | None:
    key = _api_key()
    if not key:
        return None

    try:
        response = requests.get(
            "https://api.weather.com/v3/wx/forecast/daily/5day",
            params={
                "icaoCode": station.code,
                "format": "json",
                "units": "m",
                "language": "en-US",
                "apiKey": key,
            },
            timeout=20,
        )
        track_weather_request()
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning("Weather.com daily failed for %s: %s", station.code, exc)
        return None

    valid = data.get("validTimeUtc") or []
    maxes = data.get("calendarDayTemperatureMax") or data.get("temperatureMax") or []
    mins = data.get("calendarDayTemperatureMin") or data.get("temperatureMin") or []

    target_idx = None
    for idx, ts in enumerate(valid):
        try:
            day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        except (TypeError, ValueError):
            continue
        if day == target:
            target_idx = idx
            break

    if target_idx is None or target_idx >= len(maxes):
        return None

    max_c = float(maxes[target_idx])
    min_c = float(mins[target_idx]) if target_idx < len(mins) else max_c
    if event_type == NormalizedTemperatureType.DAILY_LOW:
        selected = min_c
        statistic = "daily_min"
    else:
        selected = max_c
        statistic = "daily_max"

    window_start = f"{target.isoformat()}T00:00"
    window_end = f"{target.isoformat()}T23:59"

    return build_provider_forecast(
        provider_name="Weather.com",
        model_name="daily5day",
        station=station,
        target=target,
        event_type=event_type,
        timezone=tz_name,
        selected_c=selected,
        daily_min_c=min_c,
        daily_max_c=max_c,
        statistic=statistic,
        hourly_samples=[(window_start, selected)],
        window_start=window_start,
        window_end=window_end,
        min_hour=None,
        max_hour=None,
    )


def fetch_weather_com_hourly(
    station: WeatherStation,
    target: date,
    event_type: NormalizedTemperatureType,
    tz_name: str,
) -> ProviderForecast | None:
    key = _api_key()
    if not key:
        return None

    try:
        response = requests.get(
            "https://api.weather.com/v3/wx/forecast/hourly/2day",
            params={
                "icaoCode": station.code,
                "format": "json",
                "units": "m",
                "language": "en-US",
                "apiKey": key,
            },
            timeout=20,
        )
        track_weather_request()
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning("Weather.com hourly failed for %s: %s", station.code, exc)
        return None

    times = data.get("validTimeLocal") or []
    temps = data.get("temperature") or []
    target_iso = target.isoformat()
    hourly_samples: list[tuple[str, float]] = []
    for ts, temp in zip(times, temps):
        if temp is None:
            continue
        if not str(ts).startswith(target_iso):
            continue
        value_c = fahrenheit_to_celsius(float(temp)) if float(temp) > 60 else float(temp)
        hourly_samples.append((str(ts), value_c))

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
        provider_name="Weather.com",
        model_name="hourly2day",
        station=station,
        target=target,
        event_type=event_type,
        timezone=tz_name,
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
