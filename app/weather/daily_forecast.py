"""Deterministic daily forecast statistics from multi-provider station ensemble."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from app.config.settings import Settings
from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.forecast_ensemble import ensemble_to_daily_stats, fetch_forecast_ensemble
from app.weather.stations import WeatherStation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyForecastStats:
    provider: str
    station_code: str
    target_date_local: date
    station_timezone: str
    forecast_window_start_local: str
    forecast_window_end_local: str
    daily_min_c: float
    daily_max_c: float
    selected_statistic: str
    selected_value_c: float
    hourly_samples: list[dict]
    ensemble_provider_count: int = 1
    provider_spread_c: float = 0.0
    provider_names: tuple[str, ...] = ()
    reference_provider: str | None = None
    reference_divergence_c: float | None = None
    grid_distance_km: float | None = None
    skip_reason: str | None = None
    betting_skip_reason: str | None = None
    min_hour_local: str | None = None
    open_meteo_ensemble_value_c: float | None = None
    open_meteo_divergence_c: float | None = None
    gfs_forecast_c: float | None = None
    ecmwf_forecast_c: float | None = None
    reference_value_c: float | None = None
    station_latitude: float | None = None
    station_longitude: float | None = None
    station_elevation_m: float | None = None
    open_meteo_grid_latitude: float | None = None
    open_meteo_grid_longitude: float | None = None
    forecast_provider_label: str | None = None


def fetch_daily_forecast_stats(
    station: WeatherStation,
    target_date_text: str | None,
    event_type: NormalizedTemperatureType,
    *,
    settings: Settings | None = None,
) -> DailyForecastStats | None:
    if settings is None:
        from app.config.settings import load_settings

        settings = load_settings()

    if not station.verified:
        return None

    ensemble = fetch_forecast_ensemble(
        settings,
        station,
        target_date_text,
        event_type,
    )
    if ensemble is None:
        return None

    return ensemble_to_daily_stats(ensemble)
