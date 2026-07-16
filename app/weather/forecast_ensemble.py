"""Multi-provider station-specific forecast ensemble without Open-Meteo."""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config.settings import Settings
from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.provider_forecast import ProviderForecast
from app.weather.sources.met_norway import fetch_met_norway
from app.weather.sources.nws import fetch_nws_hourly
from app.weather.sources.weather_com import fetch_weather_com_daily
from app.weather.sources.common import parse_target_date
from app.weather.stations import WeatherStation
from app.weather.temperature_units import TemperatureUnit

logger = logging.getLogger(__name__)

INDEPENDENT_MIN_PROVIDERS = 3


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


def _resolve_timezone(settings: Settings, station: WeatherStation) -> str:
    if station.timezone:
        return station.timezone
    if station.code.startswith("K"):
        from app.weather.station_resolver import _fetch_nws_timezone

        tz = _fetch_nws_timezone(settings, station.latitude, station.longitude)
        if tz:
            return tz
    return "UTC"


def _divergence_threshold_c(settings: Settings, unit: TemperatureUnit) -> float:
    if unit == TemperatureUnit.FAHRENHEIT:
        return settings.forecast_divergence_threshold_f * 5.0 / 9.0
    return settings.forecast_divergence_threshold_c


def _delta_fahrenheit(delta_c: float) -> float:
    return delta_c * 9.0 / 5.0


def _collect_independent_providers(
    settings: Settings,
    station: WeatherStation,
    target: date,
    event_type: NormalizedTemperatureType,
    timezone: str,
) -> list[ProviderForecast]:
    providers: list[ProviderForecast] = []
    seen_vendors: set[str] = set()

    if station.code.startswith("K"):
        nws = fetch_nws_hourly(settings, station, target, event_type, timezone)
        if nws is not None and "NWS" not in seen_vendors:
            providers.append(nws)
            seen_vendors.add("NWS")

    weather_daily = fetch_weather_com_daily(station, target, event_type, timezone)
    if weather_daily is not None and "Weather.com" not in seen_vendors:
        providers.append(weather_daily)
        seen_vendors.add("Weather.com")

    met = fetch_met_norway(station, target, event_type, timezone)
    if met is not None and "MET Norway" not in seen_vendors:
        providers.append(met)
        seen_vendors.add("MET Norway")

    return providers


def _format_provider_label(active: list[ProviderForecast]) -> str:
    parts = []
    for item in active:
        label = f"{item.provider_name} ({item.model_name})"
        if label not in parts:
            parts.append(label)
    return ", ".join(parts)


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

    target = parse_target_date(target_date_text)
    if target is None:
        return None

    from app.signals.scan_runtime import active_scan_state

    cache_key = (station.code, target.isoformat(), str(event_type), market_unit.value)
    state = active_scan_state()
    if state is not None and cache_key in state.forecast_cache:
        return state.forecast_cache[cache_key]

    timezone = _resolve_timezone(settings, station)
    raw_providers = _collect_independent_providers(
        settings, station, target, event_type, timezone
    )
    if not raw_providers:
        return None

    usable: list[ProviderForecast] = []
    for provider in raw_providers:
        if (
            provider.grid_distance_km is not None
            and provider.grid_distance_km > settings.forecast_grid_max_distance_km
        ):
            usable.append(
                replace(provider, usable=False, reject_reason="FORECAST_GRID_TOO_FAR")
            )
        else:
            usable.append(provider)

    active = [item for item in usable if item.usable]
    if not active:
        ensemble = ForecastEnsemble(
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
        )
        if state is not None:
            state.forecast_cache[cache_key] = ensemble
        return ensemble

    selected_values = [item.selected_value_c for item in active]
    ensemble_value = statistics.median(selected_values)
    spread = max(selected_values) - min(selected_values) if len(selected_values) > 1 else 0.0

    reference = next((item for item in active if item.provider_name == "NWS"), None)
    reference_value = reference.selected_value_c if reference else None
    reference_provider = reference.provider_name if reference else None

    non_reference = [v for item, v in zip(active, selected_values) if item.provider_name != "NWS"]
    max_divergence = None
    if reference_value is not None and non_reference:
        max_divergence = max(abs(reference_value - v) for v in non_reference)

    min_required = max(INDEPENDENT_MIN_PROVIDERS, settings.forecast_min_providers)
    betting_skip_reason = None
    if len(active) < min_required:
        betting_skip_reason = "INSUFFICIENT_PROVIDERS"
    elif (
        max_divergence is not None
        and max_divergence > _divergence_threshold_c(settings, market_unit)
    ):
        betting_skip_reason = "FORECAST_SOURCE_DIVERGENCE"
        logger.warning(
            "Forecast divergence %.2fC (%.1fF) exceeds threshold for %s",
            max_divergence,
            _delta_fahrenheit(max_divergence),
            station.code,
        )

    label = _format_provider_label(active)
    primary = active[0]
    daily_mins = [item.daily_min_c for item in active]
    daily_maxes = [item.daily_max_c for item in active]

    logger.info(
        "Ensemble %s %s: providers=%s count=%d ensemble=%.2fC spread=%.2fC skip=%s",
        station.code,
        target.isoformat(),
        label,
        len(active),
        ensemble_value,
        spread,
        betting_skip_reason,
    )

    return_ensemble = ForecastEnsemble(
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
        reference_divergence_c=max_divergence,
        open_meteo_ensemble_value_c=None,
        open_meteo_divergence_c=max_divergence,
        gfs_forecast_c=None,
        ecmwf_forecast_c=None,
        station_latitude=station.latitude,
        station_longitude=station.longitude,
        station_elevation_m=station.elevation_m,
        open_meteo_grid_latitude=None,
        open_meteo_grid_longitude=None,
        open_meteo_grid_distance_km=None,
        forecast_provider_label=label,
        provider_names=tuple(item.provider_name for item in active),
        skip_reason=None,
        betting_skip_reason=betting_skip_reason,
        grid_distance_km=0.0,
    )
    if state is not None:
        state.forecast_cache[cache_key] = return_ensemble
    return return_ensemble


def ensemble_to_daily_stats(ensemble: ForecastEnsemble) -> "DailyForecastStats":
    from app.weather.daily_forecast import DailyForecastStats

    return DailyForecastStats(
        provider=ensemble.forecast_provider_label,
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
        ensemble_provider_count=len([p for p in ensemble.providers if p.usable]),
        provider_spread_c=ensemble.provider_spread_c,
        provider_names=ensemble.provider_names,
        reference_provider=ensemble.reference_provider,
        reference_divergence_c=ensemble.reference_divergence_c,
        grid_distance_km=ensemble.grid_distance_km,
        skip_reason=ensemble.skip_reason,
        betting_skip_reason=ensemble.betting_skip_reason,
        min_hour_local=ensemble.min_hour_local,
        open_meteo_ensemble_value_c=None,
        open_meteo_divergence_c=ensemble.reference_divergence_c,
        gfs_forecast_c=None,
        ecmwf_forecast_c=None,
        reference_value_c=ensemble.reference_value_c,
        station_latitude=ensemble.station_latitude,
        station_longitude=ensemble.station_longitude,
        station_elevation_m=ensemble.station_elevation_m,
        open_meteo_grid_latitude=None,
        open_meteo_grid_longitude=None,
        forecast_provider_label=ensemble.forecast_provider_label,
    )
