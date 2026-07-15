"""Multi-provider station-specific forecast ensemble with validation guards."""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from app.config.settings import Settings
from app.metrics.scan_metrics import active_scan_metrics
from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.station_resolver import haversine_distance_km
from app.weather.stations import WeatherStation
from app.weather.temperature_extract import _parse_target_date
from app.weather.temperature_units import TemperatureUnit

logger = logging.getLogger(__name__)

OPEN_METEO_SOURCE = "open_meteo"
OPEN_METEO_MODELS = (
    ("GFS", "gfs_seamless"),
    ("ECMWF", "ecmwf_ifs04"),
)
OPEN_METEO_MODEL_NAMES = frozenset(model for _, model in OPEN_METEO_MODELS)


def _track_weather_request() -> None:
    metrics = active_scan_metrics()
    if metrics is not None:
        metrics.weather_requests += 1


def _fahrenheit_to_celsius(value_f: float) -> float:
    return (value_f - 32.0) * 5.0 / 9.0


def _celsius_to_fahrenheit(value_c: float) -> float:
    return value_c * 9.0 / 5.0 + 32.0


@dataclass(frozen=True)
class ProviderForecast:
    provider_name: str
    model_name: str
    requested_latitude: float
    requested_longitude: float
    requested_elevation_m: float | None
    requested_timezone: str
    grid_latitude: float | None
    grid_longitude: float | None
    grid_elevation_m: float | None
    grid_distance_km: float | None
    target_date_local: date
    forecast_window_start_local: str
    forecast_window_end_local: str
    daily_min_c: float
    daily_max_c: float
    selected_value_c: float
    selected_statistic: str
    hourly_samples: list[dict]
    min_hour_local: str | None
    max_hour_local: str | None
    init_time: str | None
    usable: bool = True
    reject_reason: str | None = None


@dataclass(frozen=True)
class ForecastEnsemble:
    station_code: str
    target_date_local: date
    station_timezone: str
    event_type: NormalizedTemperatureType
    providers: tuple[ProviderForecast, ...]
    ensemble_value_c: float
    provider_spread_c: float
    selected_statistic: str
    daily_min_c: float
    daily_max_c: float
    forecast_window_start_local: str
    forecast_window_end_local: str
    hourly_samples: list[dict]
    min_hour_local: str | None
    reference_provider: str | None
    reference_value_c: float | None
    reference_divergence_c: float | None
    open_meteo_ensemble_value_c: float | None
    open_meteo_divergence_c: float | None
    gfs_forecast_c: float | None
    ecmwf_forecast_c: float | None
    station_latitude: float
    station_longitude: float
    station_elevation_m: float | None
    open_meteo_grid_latitude: float | None
    open_meteo_grid_longitude: float | None
    open_meteo_grid_distance_km: float | None
    forecast_provider_label: str
    provider_names: tuple[str, ...]
    skip_reason: str | None = None
    betting_skip_reason: str | None = None
    grid_distance_km: float | None = None


def _hourly_for_local_day(
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


def _daily_stats_from_hourly(
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


def _fetch_open_meteo_provider(
    station: WeatherStation,
    target: date,
    event_type: NormalizedTemperatureType,
    *,
    provider_label: str,
    model: str,
    timezone: str,
) -> ProviderForecast | None:
    params: dict = {
        "latitude": station.latitude,
        "longitude": station.longitude,
        "hourly": "temperature_2m",
        "models": model,
        "timezone": timezone,
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
        "temperature_unit": "celsius",
    }
    if station.elevation_m is not None:
        params["elevation"] = int(round(station.elevation_m))

    try:
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params,
            timeout=20,
        )
        _track_weather_request()
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning(
            "Open-Meteo %s fetch failed for %s: %s",
            provider_label,
            station.code,
            exc,
        )
        return None

    hourly = data.get("hourly", {}) or {}
    temp_key = f"temperature_2m_{model}"
    temps = hourly.get(temp_key, []) or []
    if not any(temp is not None for temp in temps):
        temps = hourly.get("temperature_2m", []) or []
    times = hourly.get("time", []) or []
    if not any(temp is not None for temp in temps):
        logger.info(
            "Open-Meteo %s returned no hourly data for %s on %s",
            provider_label,
            station.code,
            target.isoformat(),
        )
        return None

    hourly_samples = _hourly_for_local_day(times, temps, target)
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
    ) = _daily_stats_from_hourly(hourly_samples, event_type)

    grid_lat = data.get("latitude")
    grid_lon = data.get("longitude")
    grid_elev = data.get("elevation")
    distance_km = None
    if grid_lat is not None and grid_lon is not None:
        distance_km = haversine_distance_km(
            station.latitude,
            station.longitude,
            float(grid_lat),
            float(grid_lon),
        )

    logger.info(
        "Open-Meteo %s %s: requested=(%.5f, %.5f, elev=%s, tz=%s) "
        "grid=(%.5f, %.5f, elev=%s) distance_km=%.2f target=%s "
        "selected=%s %.2fC init=%s",
        provider_label,
        station.code,
        station.latitude,
        station.longitude,
        station.elevation_m,
        timezone,
        grid_lat,
        grid_lon,
        grid_elev,
        distance_km or 0.0,
        target.isoformat(),
        statistic,
        selected,
        data.get("generationtime_ms"),
    )

    return ProviderForecast(
        provider_name=OPEN_METEO_SOURCE,
        model_name=model,
        requested_latitude=station.latitude,
        requested_longitude=station.longitude,
        requested_elevation_m=station.elevation_m,
        requested_timezone=timezone,
        grid_latitude=float(grid_lat) if grid_lat is not None else None,
        grid_longitude=float(grid_lon) if grid_lon is not None else None,
        grid_elevation_m=float(grid_elev) if grid_elev is not None else None,
        grid_distance_km=distance_km,
        target_date_local=target,
        forecast_window_start_local=window_start,
        forecast_window_end_local=window_end,
        daily_min_c=daily_min,
        daily_max_c=daily_max,
        selected_value_c=selected,
        selected_statistic=statistic,
        hourly_samples=[
            {"time": ts, "temperature_c": temp} for ts, temp in hourly_samples
        ],
        min_hour_local=min_hour,
        max_hour_local=max_hour,
        init_time=str(data.get("generationtime_ms")),
    )


def _fetch_nws_hourly_provider(
    settings: Settings,
    station: WeatherStation,
    target: date,
    event_type: NormalizedTemperatureType,
    timezone: str,
) -> ProviderForecast | None:
    headers = {
        "User-Agent": "Trading Intelligence Bot contact@example.com",
        "Accept": "application/geo+json, application/json",
    }
    try:
        points_url = (
            f"{settings.weather_gov_base_url}/points/"
            f"{station.latitude},{station.longitude}"
        )
        points_response = requests.get(points_url, headers=headers, timeout=20)
        _track_weather_request()
        points_response.raise_for_status()
        points_data = points_response.json()

        hourly_url = points_data.get("properties", {}).get("forecastHourly")
        if not hourly_url:
            return None

        hourly_response = requests.get(hourly_url, headers=headers, timeout=20)
        _track_weather_request()
        hourly_response.raise_for_status()
        hourly_data = hourly_response.json()
    except Exception as exc:
        logger.warning("NWS hourly fetch failed for %s: %s", station.code, exc)
        return None

    periods = hourly_data.get("properties", {}).get("periods", []) or []
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
            _fahrenheit_to_celsius(float(temp))
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
    ) = _daily_stats_from_hourly(hourly_samples, event_type)

    logger.info(
        "NWS hourly %s: requested=(%.5f, %.5f, tz=%s) target=%s "
        "selected=%s %.2fC (%.1fF) hours=%d",
        station.code,
        station.latitude,
        station.longitude,
        timezone,
        target.isoformat(),
        statistic,
        selected,
        _celsius_to_fahrenheit(selected),
        len(hourly_samples),
    )

    return ProviderForecast(
        provider_name="NWS",
        model_name="forecastHourly",
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
        daily_min_c=daily_min,
        daily_max_c=daily_max,
        selected_value_c=selected,
        selected_statistic=statistic,
        hourly_samples=[
            {"time": ts, "temperature_c": temp} for ts, temp in hourly_samples
        ],
        min_hour_local=min_hour,
        max_hour_local=max_hour,
        init_time=None,
    )


def _resolve_timezone(
    settings: Settings,
    station: WeatherStation,
) -> str:
    if station.timezone:
        return station.timezone

    if station.code.startswith("K"):
        from app.weather.station_resolver import _fetch_nws_timezone

        tz = _fetch_nws_timezone(settings, station.latitude, station.longitude)
        if tz:
            return tz

    return "UTC"


def _is_open_meteo_model(provider: ProviderForecast) -> bool:
    return provider.model_name in OPEN_METEO_MODEL_NAMES


def _model_label(provider: ProviderForecast) -> str | None:
    for label, model in OPEN_METEO_MODELS:
        if provider.model_name == model:
            return label
    return None


def _is_us_next_day_market(
    station: WeatherStation,
    target: date,
    timezone: str,
) -> bool:
    if not station.code.startswith("K"):
        return False
    try:
        local_today = datetime.now(ZoneInfo(timezone)).date()
    except Exception:
        return True
    return target in {local_today, local_today + timedelta(days=1)}


def _format_forecast_provider_label(
    open_meteo_models: list[str],
    reference_provider: str | None,
) -> str:
    parts: list[str] = []
    if open_meteo_models:
        parts.append(f"Open-Meteo ({', '.join(open_meteo_models)})")
    if reference_provider:
        parts.append(f"reference: {reference_provider}")
    return "; ".join(parts)


def _divergence_threshold_c(settings: Settings, unit: TemperatureUnit) -> float:
    if unit == TemperatureUnit.FAHRENHEIT:
        return settings.forecast_divergence_threshold_f * 5.0 / 9.0
    return settings.forecast_divergence_threshold_c


def _delta_fahrenheit(delta_c: float) -> float:
    return delta_c * 9.0 / 5.0


def fetch_forecast_ensemble(
    settings: Settings,
    station: WeatherStation,
    target_date_text: str | None,
    event_type: NormalizedTemperatureType,
    *,
    market_unit: TemperatureUnit = TemperatureUnit.FAHRENHEIT,
) -> ForecastEnsemble | None:
    if not station.verified:
        return None

    target = _parse_target_date(target_date_text)
    if target is None:
        return None

    timezone = _resolve_timezone(settings, station)
    providers: list[ProviderForecast] = []

    for label, model in OPEN_METEO_MODELS:
        provider = _fetch_open_meteo_provider(
            station,
            target,
            event_type,
            provider_label=label,
            model=model,
            timezone=timezone,
        )
        if provider is not None:
            providers.append(provider)

    if station.code.startswith("K"):
        nws = _fetch_nws_hourly_provider(
            settings,
            station,
            target,
            event_type,
            timezone,
        )
        if nws is not None:
            providers.append(nws)

    if not providers:
        return None

    usable = []
    max_grid_distance = 0.0
    for provider in providers:
        reject_reason = None
        if (
            provider.grid_distance_km is not None
            and provider.grid_distance_km > settings.forecast_grid_max_distance_km
        ):
            reject_reason = "FORECAST_GRID_TOO_FAR"
            logger.warning(
                "Open-Meteo %s grid distance %.2f km exceeds limit %.2f km for %s",
                _model_label(provider) or provider.model_name,
                provider.grid_distance_km,
                settings.forecast_grid_max_distance_km,
                station.code,
            )
        if reject_reason:
            usable.append(
                replace(provider, usable=False, reject_reason=reject_reason)
            )
        else:
            usable.append(provider)
            if provider.grid_distance_km is not None:
                max_grid_distance = max(max_grid_distance, provider.grid_distance_km)

    active = [item for item in usable if item.usable]
    if not active:
        return ForecastEnsemble(
            station_code=station.code,
            target_date_local=target,
            station_timezone=timezone,
            event_type=event_type,
            providers=tuple(usable),
            ensemble_value_c=0.0,
            provider_spread_c=0.0,
            selected_statistic=(
                "daily_min"
                if event_type == NormalizedTemperatureType.DAILY_LOW
                else "daily_max"
            ),
            daily_min_c=0.0,
            daily_max_c=0.0,
            forecast_window_start_local="",
            forecast_window_end_local="",
            hourly_samples=[],
            min_hour_local=None,
            reference_provider=None,
            reference_value_c=None,
            reference_divergence_c=None,
            open_meteo_ensemble_value_c=None,
            open_meteo_divergence_c=None,
            gfs_forecast_c=None,
            ecmwf_forecast_c=None,
            station_latitude=station.latitude,
            station_longitude=station.longitude,
            station_elevation_m=station.elevation_m,
            open_meteo_grid_latitude=None,
            open_meteo_grid_longitude=None,
            open_meteo_grid_distance_km=None,
            forecast_provider_label="",
            provider_names=tuple(),
            skip_reason="FORECAST_GRID_TOO_FAR",
            grid_distance_km=max_grid_distance,
        )

    selected_values = [item.selected_value_c for item in active]
    open_meteo_active = [item for item in active if _is_open_meteo_model(item)]
    open_meteo_values = [item.selected_value_c for item in open_meteo_active]
    open_meteo_ensemble = (
        statistics.median(open_meteo_values) if open_meteo_values else None
    )

    reference = next((item for item in active if item.provider_name == "NWS"), None)
    reference_value = reference.selected_value_c if reference else None
    reference_provider = reference.provider_name if reference else None

    gfs_forecast_c = None
    ecmwf_forecast_c = None
    open_meteo_model_labels: list[str] = []
    open_meteo_grid_lat = None
    open_meteo_grid_lon = None
    open_meteo_grid_distance = None
    for item in open_meteo_active:
        label = _model_label(item)
        if label == "GFS":
            gfs_forecast_c = item.selected_value_c
            open_meteo_model_labels.append("GFS")
        elif label == "ECMWF":
            ecmwf_forecast_c = item.selected_value_c
            open_meteo_model_labels.append("ECMWF")
        if open_meteo_grid_lat is None:
            open_meteo_grid_lat = item.grid_latitude
            open_meteo_grid_lon = item.grid_longitude
            open_meteo_grid_distance = item.grid_distance_km

    if open_meteo_ensemble is not None and reference_value is not None:
        ensemble_value = statistics.median([open_meteo_ensemble, reference_value])
    elif open_meteo_ensemble is not None:
        ensemble_value = open_meteo_ensemble
    else:
        ensemble_value = statistics.median(selected_values)

    spread = max(selected_values) - min(selected_values) if len(selected_values) > 1 else 0.0

    open_meteo_divergence = (
        abs(open_meteo_ensemble - reference_value)
        if open_meteo_ensemble is not None and reference_value is not None
        else None
    )

    skip_reason = None
    betting_skip_reason = None
    if len(active) < settings.forecast_min_providers:
        betting_skip_reason = "INSUFFICIENT_PROVIDERS"
    elif (
        _is_us_next_day_market(station, target, timezone)
        and open_meteo_ensemble is not None
        and reference_value is not None
        and open_meteo_divergence is not None
        and open_meteo_divergence > _divergence_threshold_c(settings, market_unit)
    ):
        betting_skip_reason = "FORECAST_SOURCE_DIVERGENCE"
        logger.warning(
            "Open-Meteo divergence %.2fC (%.1fF) exceeds threshold for %s "
            "open_meteo=%.2fC (%.1fF) reference=%.2fC (%.1fF) "
            "station=(%.5f, %.5f) grid=(%s, %s) grid_km=%s "
            "gfs=%s ecmwf=%s",
            open_meteo_divergence,
            _delta_fahrenheit(open_meteo_divergence),
            station.code,
            open_meteo_ensemble,
            _celsius_to_fahrenheit(open_meteo_ensemble),
            reference_value,
            _celsius_to_fahrenheit(reference_value),
            station.latitude,
            station.longitude,
            open_meteo_grid_lat,
            open_meteo_grid_lon,
            open_meteo_grid_distance,
            (
                f"{_celsius_to_fahrenheit(gfs_forecast_c):.1f}F"
                if gfs_forecast_c is not None
                else "n/a"
            ),
            (
                f"{_celsius_to_fahrenheit(ecmwf_forecast_c):.1f}F"
                if ecmwf_forecast_c is not None
                else "n/a"
            ),
        )

    forecast_provider_label = _format_forecast_provider_label(
        open_meteo_model_labels,
        reference_provider,
    )
    provider_names = tuple(
        item for item in [forecast_provider_label] if item
    )

    primary = open_meteo_active[0] if open_meteo_active else min(active, key=lambda item: item.provider_name)
    daily_mins = [item.daily_min_c for item in active]
    daily_maxes = [item.daily_max_c for item in active]

    logger.info(
        "Ensemble %s %s: forecast=%s ensemble=%.2fC spread=%.2fC "
        "open_meteo=%.2fC reference=%s open_meteo_divergence=%s skip=%s "
        "station=(%.5f, %.5f, elev=%s) grid=(%s, %s, dist_km=%s) "
        "gfs=%s ecmwf=%s ref=%s",
        station.code,
        target.isoformat(),
        forecast_provider_label,
        ensemble_value,
        spread,
        open_meteo_ensemble or 0.0,
        reference_provider,
        open_meteo_divergence,
        skip_reason,
        station.latitude,
        station.longitude,
        station.elevation_m,
        open_meteo_grid_lat,
        open_meteo_grid_lon,
        open_meteo_grid_distance,
        gfs_forecast_c,
        ecmwf_forecast_c,
        reference_value,
    )

    return ForecastEnsemble(
        station_code=station.code,
        target_date_local=target,
        station_timezone=timezone,
        event_type=event_type,
        providers=tuple(usable),
        ensemble_value_c=ensemble_value,
        provider_spread_c=spread,
        selected_statistic=primary.selected_statistic,
        daily_min_c=min(daily_mins),
        daily_max_c=max(daily_maxes),
        forecast_window_start_local=primary.forecast_window_start_local,
        forecast_window_end_local=primary.forecast_window_end_local,
        hourly_samples=primary.hourly_samples,
        min_hour_local=primary.min_hour_local,
        reference_provider=reference_provider,
        reference_value_c=reference_value,
        reference_divergence_c=open_meteo_divergence,
        open_meteo_ensemble_value_c=open_meteo_ensemble,
        open_meteo_divergence_c=open_meteo_divergence,
        gfs_forecast_c=gfs_forecast_c,
        ecmwf_forecast_c=ecmwf_forecast_c,
        station_latitude=station.latitude,
        station_longitude=station.longitude,
        station_elevation_m=station.elevation_m,
        open_meteo_grid_latitude=open_meteo_grid_lat,
        open_meteo_grid_longitude=open_meteo_grid_lon,
        open_meteo_grid_distance_km=open_meteo_grid_distance,
        forecast_provider_label=forecast_provider_label,
        provider_names=provider_names,
        skip_reason=skip_reason,
        betting_skip_reason=betting_skip_reason,
        grid_distance_km=max_grid_distance or None,
    )


def ensemble_to_daily_stats(ensemble: ForecastEnsemble) -> "DailyForecastStats":
    from app.weather.daily_forecast import DailyForecastStats

    source_count = 0
    if ensemble.open_meteo_ensemble_value_c is not None:
        source_count += 1
    if ensemble.reference_value_c is not None:
        source_count += 1

    return DailyForecastStats(
        provider=ensemble.forecast_provider_label or OPEN_METEO_SOURCE,
        station_code=ensemble.station_code,
        target_date_local=ensemble.target_date_local,
        station_timezone=ensemble.station_timezone,
        forecast_window_start_local=ensemble.forecast_window_start_local,
        forecast_window_end_local=ensemble.forecast_window_end_local,
        daily_min_c=ensemble.daily_min_c,
        daily_max_c=ensemble.daily_max_c,
        selected_statistic=ensemble.selected_statistic,
        selected_value_c=ensemble.ensemble_value_c,
        hourly_samples=ensemble.hourly_samples,
        ensemble_provider_count=source_count,
        provider_spread_c=ensemble.provider_spread_c,
        provider_names=ensemble.provider_names,
        reference_provider=ensemble.reference_provider,
        reference_divergence_c=ensemble.open_meteo_divergence_c,
        grid_distance_km=ensemble.open_meteo_grid_distance_km,
        skip_reason=ensemble.skip_reason,
        betting_skip_reason=ensemble.betting_skip_reason,
        min_hour_local=ensemble.min_hour_local,
        open_meteo_ensemble_value_c=ensemble.open_meteo_ensemble_value_c,
        open_meteo_divergence_c=ensemble.open_meteo_divergence_c,
        gfs_forecast_c=ensemble.gfs_forecast_c,
        ecmwf_forecast_c=ensemble.ecmwf_forecast_c,
        reference_value_c=ensemble.reference_value_c,
        station_latitude=ensemble.station_latitude,
        station_longitude=ensemble.station_longitude,
        station_elevation_m=ensemble.station_elevation_m,
        open_meteo_grid_latitude=ensemble.open_meteo_grid_latitude,
        open_meteo_grid_longitude=ensemble.open_meteo_grid_longitude,
        forecast_provider_label=ensemble.forecast_provider_label,
    )
