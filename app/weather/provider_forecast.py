"""Provider forecast record shared across ensemble and source modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


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
    source_url: str | None = None
