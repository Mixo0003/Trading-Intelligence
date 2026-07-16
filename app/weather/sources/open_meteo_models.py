"""ECMWF IFS and DWD ICON Global via dedicated Open-Meteo JSON model APIs.

Open-Meteo is transport only. Provider identities are ECMWF IFS and DWD ICON Global.
Never call the blended /v1/forecast Best Match endpoint.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from app.http.retry import get_with_retry, is_permanent_status
from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.provider_forecast import ProviderForecast
from app.weather.sources.common import (
    build_provider_forecast,
    daily_stats_from_hourly,
    hourly_for_local_day,
    track_weather_request,
)
from app.weather.stations import WeatherStation

logger = logging.getLogger(__name__)

ECMWF_ENDPOINT = "https://api.open-meteo.com/v1/ecmwf"
DWD_ICON_ENDPOINT = "https://api.open-meteo.com/v1/dwd-icon"

ECMWF_MODEL = "ecmwf_ifs025"
ICON_MODEL = "icon_global"

PROVIDER_ECMWF = "ECMWF IFS"
PROVIDER_ICON = "DWD ICON Global"

_CACHE_TTL_SECONDS = 45 * 60
_MIN_HOURS = 18
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 20
_MAX_ATTEMPTS = 3


@dataclass
class _CacheEntry:
    expires_at: float
    payload: dict[str, Any] | None
    forecast: ProviderForecast | None
    failure: str | None


_cache_lock = threading.Lock()
_response_cache: dict[tuple[Any, ...], _CacheEntry] = {}
_inflight: dict[tuple[Any, ...], threading.Event] = {}
_inflight_results: dict[tuple[Any, ...], _CacheEntry] = {}

_breaker_lock = threading.Lock()
_breaker_disabled_until: dict[str, float] = {}
_breaker_failures: dict[str, int] = {}
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN_SECONDS = 10 * 60


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _breaker_tripped(provider_key: str) -> bool:
    with _breaker_lock:
        until = _breaker_disabled_until.get(provider_key, 0.0)
        return time.time() < until


def _breaker_note_failure(provider_key: str) -> None:
    with _breaker_lock:
        _breaker_failures[provider_key] = _breaker_failures.get(provider_key, 0) + 1
        if _breaker_failures[provider_key] >= _BREAKER_THRESHOLD:
            _breaker_disabled_until[provider_key] = time.time() + _BREAKER_COOLDOWN_SECONDS
            _breaker_failures[provider_key] = 0
            logger.warning(
                "%s circuit open for %ss after repeated failures",
                provider_key,
                _BREAKER_COOLDOWN_SECONDS,
            )


def _breaker_note_success(provider_key: str) -> None:
    with _breaker_lock:
        _breaker_failures[provider_key] = 0
        _breaker_disabled_until.pop(provider_key, None)


def _validate_dedicated_response(
    *,
    endpoint: str,
    expected_model: str,
    data: dict[str, Any],
) -> str | None:
    if "/v1/forecast" in endpoint:
        return "BLENDED_ENDPOINT_FORBIDDEN"
    if expected_model == ECMWF_MODEL and "/v1/ecmwf" not in endpoint:
        return "WRONG_ECMWF_ENDPOINT"
    if expected_model == ICON_MODEL and "/v1/dwd-icon" not in endpoint:
        return "WRONG_ICON_ENDPOINT"

    hourly = data.get("hourly")
    if not isinstance(hourly, dict):
        return "MISSING_HOURLY"

    # Reject multi-model / best-match payload shapes from the blended forecast API.
    hourly_keys = {str(k).lower() for k in hourly.keys()}
    if any("best_match" in k for k in hourly_keys):
        return "BLENDED_MODEL_METADATA"
    model_suffixed = [k for k in hourly_keys if k.startswith("temperature_2m_")]
    if model_suffixed:
        return "BLENDED_MODEL_METADATA"
    if "temperature_2m" not in hourly_keys or "time" not in hourly_keys:
        return "MISSING_TEMPERATURE_SERIES"
    return None


def _extract_day_samples(
    data: dict[str, Any],
    target: date,
) -> list[tuple[str, float]]:
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    return hourly_for_local_day(
        [str(t) for t in times],
        [None if v is None else float(v) for v in temps],
        target,
    )


def _cache_get(key: tuple[Any, ...]) -> _CacheEntry | None:
    with _cache_lock:
        entry = _response_cache.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.time():
            _response_cache.pop(key, None)
            return None
        return entry


def _cache_put(key: tuple[Any, ...], entry: _CacheEntry) -> None:
    with _cache_lock:
        _response_cache[key] = entry


def clear_model_caches() -> None:
    """Test helper: reset TTL cache, inflight map, and circuit breakers."""
    with _cache_lock:
        _response_cache.clear()
        _inflight.clear()
        _inflight_results.clear()
    with _breaker_lock:
        _breaker_disabled_until.clear()
        _breaker_failures.clear()


def _fetch_json(
    *,
    endpoint: str,
    provider_key: str,
    station: WeatherStation,
    timezone_name: str,
    model: str,
    target: date,
) -> tuple[dict[str, Any] | None, str | None]:
    if _breaker_tripped(provider_key):
        return None, "CIRCUIT_OPEN"

    cache_key = (
        endpoint,
        model,
        round(station.latitude, 4),
        round(station.longitude, 4),
        timezone_name,
        target.isoformat(),
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached.payload, cached.failure

    # Concurrent identical request collapse.
    leader = False
    with _cache_lock:
        event = _inflight.get(cache_key)
        if event is None:
            event = threading.Event()
            _inflight[cache_key] = event
            leader = True

    if not leader:
        event.wait(timeout=_READ_TIMEOUT + _CONNECT_TIMEOUT + 5)
        with _cache_lock:
            shared = _inflight_results.get(cache_key)
        if shared is not None:
            return shared.payload, shared.failure
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached.payload, cached.failure
        return None, "COLLAPSE_MISS"

    failure: str | None = None
    payload: dict[str, Any] | None = None
    try:
        params = {
            "latitude": station.latitude,
            "longitude": station.longitude,
            "hourly": "temperature_2m,cloud_cover,precipitation,weather_code",
            "timezone": timezone_name,
            "forecast_days": 7,
            "models": model,
        }
        response = get_with_retry(
            endpoint,
            params=params,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            max_attempts=_MAX_ATTEMPTS,
        )
        track_weather_request()
        if is_permanent_status(response.status_code):
            failure = f"HTTP_{response.status_code}"
            _breaker_note_failure(provider_key)
        elif response.status_code != 200:
            failure = f"HTTP_{response.status_code}"
            if response.status_code in {429, 500, 502, 503, 504}:
                _breaker_note_failure(provider_key)
        else:
            data = response.json()
            if not isinstance(data, dict):
                failure = "INVALID_JSON"
            else:
                identity_error = _validate_dedicated_response(
                    endpoint=endpoint,
                    expected_model=model,
                    data=data,
                )
                if identity_error:
                    failure = identity_error
                else:
                    payload = data
                    _breaker_note_success(provider_key)
    except Exception as exc:
        failure = type(exc).__name__
        _breaker_note_failure(provider_key)
        logger.warning(
            "%s fetch failed for %s: %s",
            provider_key,
            station.code,
            failure,
        )

    entry = _CacheEntry(
        expires_at=time.time() + _CACHE_TTL_SECONDS,
        payload=payload,
        forecast=None,
        failure=failure,
    )
    _cache_put(cache_key, entry)
    with _cache_lock:
        _inflight_results[cache_key] = entry
        ev = _inflight.pop(cache_key, None)
        if ev is not None:
            ev.set()
        # Drop inflight result shortly after wake (cache holds the value).
        _inflight_results.pop(cache_key, None)
    return payload, failure


def _build_from_payload(
    *,
    provider_name: str,
    model_name: str,
    station: WeatherStation,
    target: date,
    event_type: NormalizedTemperatureType,
    timezone_name: str,
    data: dict[str, Any],
    source_url: str,
) -> tuple[ProviderForecast | None, str | None]:
    samples = _extract_day_samples(data, target)
    if not samples:
        return None, "WRONG_DATE_OR_EMPTY"
    if len(samples) < _MIN_HOURS:
        return None, "MISSING_HOURS"

    (
        daily_min,
        daily_max,
        selected,
        statistic,
        window_start,
        window_end,
        min_hour,
        max_hour,
    ) = daily_stats_from_hourly(samples, event_type)

    grid_lat = data.get("latitude")
    grid_lon = data.get("longitude")
    try:
        grid_lat_f = float(grid_lat) if grid_lat is not None else station.latitude
        grid_lon_f = float(grid_lon) if grid_lon is not None else station.longitude
    except (TypeError, ValueError):
        return None, "BAD_GRID_COORDS"

    distance = _haversine_km(
        station.latitude, station.longitude, grid_lat_f, grid_lon_f
    )
    # Global 0.25° grids can place the nearest cell >15 km from the station.
    # Keep coordinates for diagnostics; do not fail the NWS-oriented distance guard.
    _ = distance
    elev = data.get("elevation")
    try:
        elev_f = float(elev) if elev is not None else station.elevation_m
    except (TypeError, ValueError):
        elev_f = station.elevation_m

    retrieved = datetime.now(timezone.utc).isoformat()
    forecast = build_provider_forecast(
        provider_name=provider_name,
        model_name=model_name,
        station=station,
        target=target,
        event_type=event_type,
        timezone=timezone_name,
        selected_c=selected,
        daily_min_c=daily_min,
        daily_max_c=daily_max,
        statistic=statistic,
        hourly_samples=samples,
        window_start=window_start,
        window_end=window_end,
        min_hour=min_hour,
        max_hour=max_hour,
        init_time=retrieved,
        source_url=source_url,
    )
    return (
        ProviderForecast(
            provider_name=forecast.provider_name,
            model_name=forecast.model_name,
            requested_latitude=forecast.requested_latitude,
            requested_longitude=forecast.requested_longitude,
            requested_elevation_m=forecast.requested_elevation_m,
            requested_timezone=forecast.requested_timezone,
            grid_latitude=grid_lat_f,
            grid_longitude=grid_lon_f,
            grid_elevation_m=elev_f,
            grid_distance_km=None,
            target_date_local=forecast.target_date_local,
            forecast_window_start_local=forecast.forecast_window_start_local,
            forecast_window_end_local=forecast.forecast_window_end_local,
            daily_min_c=forecast.daily_min_c,
            daily_max_c=forecast.daily_max_c,
            selected_value_c=forecast.selected_value_c,
            selected_statistic=forecast.selected_statistic,
            hourly_samples=forecast.hourly_samples,
            min_hour_local=forecast.min_hour_local,
            max_hour_local=forecast.max_hour_local,
            init_time=forecast.init_time,
            usable=True,
            reject_reason=None,
            source_url=source_url,
        ),
        None,
    )


def fetch_ecmwf_ifs(
    station: WeatherStation,
    target: date,
    event_type: NormalizedTemperatureType,
    timezone_name: str,
) -> ProviderForecast | None:
    data, failure = _fetch_json(
        endpoint=ECMWF_ENDPOINT,
        provider_key=PROVIDER_ECMWF,
        station=station,
        timezone_name=timezone_name,
        model=ECMWF_MODEL,
        target=target,
    )
    if data is None:
        if failure:
            logger.info("ECMWF IFS unavailable for %s: %s", station.code, failure)
        return None
    forecast, parse_failure = _build_from_payload(
        provider_name=PROVIDER_ECMWF,
        model_name=ECMWF_MODEL,
        station=station,
        target=target,
        event_type=event_type,
        timezone_name=timezone_name,
        data=data,
        source_url=ECMWF_ENDPOINT,
    )
    if forecast is None:
        logger.info("ECMWF IFS rejected for %s: %s", station.code, parse_failure)
    return forecast


def fetch_dwd_icon_global(
    station: WeatherStation,
    target: date,
    event_type: NormalizedTemperatureType,
    timezone_name: str,
) -> ProviderForecast | None:
    data, failure = _fetch_json(
        endpoint=DWD_ICON_ENDPOINT,
        provider_key=PROVIDER_ICON,
        station=station,
        timezone_name=timezone_name,
        model=ICON_MODEL,
        target=target,
    )
    if data is None:
        if failure:
            logger.info("DWD ICON Global unavailable for %s: %s", station.code, failure)
        return None
    forecast, parse_failure = _build_from_payload(
        provider_name=PROVIDER_ICON,
        model_name=ICON_MODEL,
        station=station,
        target=target,
        event_type=event_type,
        timezone_name=timezone_name,
        data=data,
        source_url=DWD_ICON_ENDPOINT,
    )
    if forecast is None:
        logger.info("DWD ICON Global rejected for %s: %s", station.code, parse_failure)
    return forecast
