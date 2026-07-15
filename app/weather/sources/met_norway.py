"""MET Norway locationforecast — independent global model source."""

from __future__ import annotations

import logging
from datetime import date, datetime

from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.provider_forecast import ProviderForecast
from app.weather.sources.common import (
    build_provider_forecast,
    daily_stats_from_hourly,
    track_weather_request,
)
from app.weather.stations import WeatherStation

import requests

logger = logging.getLogger(__name__)

_MET_NO_HEADERS = {
    "User-Agent": "TradingIntelligence/1.0 github.com/Mixo0003/Trading-Intelligence",
}


def fetch_met_norway(
    station: WeatherStation,
    target: date,
    event_type: NormalizedTemperatureType,
    timezone: str,
) -> ProviderForecast | None:
    try:
        response = requests.get(
            "https://api.met.no/weatherapi/locationforecast/2.0/compact",
            params={"lat": station.latitude, "lon": station.longitude},
            headers=_MET_NO_HEADERS,
            timeout=20,
        )
        track_weather_request()
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning("MET Norway fetch failed for %s: %s", station.code, exc)
        return None

    target_iso = target.isoformat()
    hourly_samples: list[tuple[str, float]] = []
    for entry in data.get("properties", {}).get("timeseries", []) or []:
        ts = str(entry.get("time") or "")
        if not ts.startswith(target_iso):
            continue
        instant = entry.get("data", {}).get("instant", {}).get("details", {})
        temp = instant.get("air_temperature")
        if temp is None:
            continue
        hourly_samples.append((ts, float(temp)))

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

    updated = data.get("properties", {}).get("meta", {}).get("updated_at")

    return build_provider_forecast(
        provider_name="MET Norway",
        model_name="locationforecast",
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
        init_time=str(updated) if updated else None,
    )
