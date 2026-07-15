"""Shared helpers for multi-source station forecasts."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import TYPE_CHECKING  # noqa: F401 — reserved for future typing helpers

import requests

from app.metrics.scan_metrics import active_scan_metrics
from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.temperature_extract import _parse_target_date

from app.weather.provider_forecast import ProviderForecast

logger = logging.getLogger(__name__)


def track_weather_request() -> None:
    metrics = active_scan_metrics()
    if metrics is not None:
        metrics.weather_requests += 1


def fahrenheit_to_celsius(value_f: float) -> float:
    return (value_f - 32.0) * 5.0 / 9.0


def celsius_to_fahrenheit(value_c: float) -> float:
    return value_c * 9.0 / 5.0 + 32.0


def hourly_for_local_day(
    times: list[str],
    temps_c: list[float | None],
    target: date,
) -> list[tuple[str, float]]:
    target_iso = target.isoformat()
    samples: list[tuple[str, float]] = []
    for ts, temp in zip(times, temps_c):
        if temp is None:
            continue
        if not str(ts).startswith(target_iso):
            continue
        samples.append((str(ts), float(temp)))
    return samples


def daily_stats_from_hourly(
    hourly_samples: list[tuple[str, float]],
    event_type: NormalizedTemperatureType,
) -> tuple[float, float, float, str, str, str, str | None, str | None]:
    if not hourly_samples:
        raise ValueError("No hourly samples for local day")

    temps = [temp for _, temp in hourly_samples]
    daily_min = min(temps)
    daily_max = max(temps)
    if event_type == NormalizedTemperatureType.DAILY_LOW:
        selected = daily_min
        statistic = "daily_min"
        min_hour = min(hourly_samples, key=lambda item: item[1])[0]
        max_hour = None
    else:
        selected = daily_max
        statistic = "daily_max"
        max_hour = max(hourly_samples, key=lambda item: item[1])[0]
        min_hour = None

    return (
        daily_min,
        daily_max,
        selected,
        statistic,
        hourly_samples[0][0],
        hourly_samples[-1][0],
        min_hour,
        max_hour,
    )


def build_provider_forecast(
    *,
    provider_name: str,
    model_name: str,
    station,
    target: date,
    event_type: NormalizedTemperatureType,
    timezone: str,
    selected_c: float,
    daily_min_c: float,
    daily_max_c: float,
    statistic: str,
    hourly_samples: list[tuple[str, float]],
    window_start: str,
    window_end: str,
    min_hour: str | None,
    max_hour: str | None,
    init_time: str | None = None,
    source_url: str | None = None,
) -> ProviderForecast:
    return ProviderForecast(
        provider_name=provider_name,
        model_name=model_name,
        requested_latitude=station.latitude,
        requested_longitude=station.longitude,
        requested_elevation_m=station.elevation_m,
        requested_timezone=timezone,
        grid_latitude=station.latitude,
        grid_longitude=station.longitude,
        grid_elevation_m=station.elevation_m,
        grid_distance_km=0.0,
        target_date_local=target,
        forecast_window_start_local=window_start,
        forecast_window_end_local=window_end,
        daily_min_c=daily_min_c,
        daily_max_c=daily_max_c,
        selected_value_c=selected_c,
        selected_statistic=statistic,
        hourly_samples=[
            {"time": ts, "temperature_c": temp} for ts, temp in hourly_samples
        ],
        min_hour_local=min_hour,
        max_hour_local=max_hour,
        init_time=init_time,
        source_url=source_url,
    )


def parse_target_date(target_date_text: str | None) -> date | None:
    return _parse_target_date(target_date_text)


def local_day_from_period_name(name: str, target: date) -> bool:
    lower = name.lower()
    weekday = target.strftime("%A").lower()
    return weekday in lower or target.isoformat() in lower


def extract_weather_com_api_key() -> str | None:
    """Read the public client key embedded on Wunderground forecast pages."""
    try:
        response = requests.get(
            "https://www.wunderground.com/forecast",
            headers={"User-Agent": "TradingIntelligence/1.0"},
            timeout=15,
        )
        track_weather_request()
        response.raise_for_status()
        keys = set(re.findall(r"apiKey=([A-Za-z0-9]{32})", response.text))
        return next(iter(keys)) if keys else None
    except Exception as exc:
        logger.warning("Weather.com API key lookup failed: %s", exc)
        return None
