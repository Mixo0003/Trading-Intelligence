"""Station-aligned evidence types (observations + qualitative). Not numerical providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class DailyObservation:
    """One completed local calendar day of station observations."""

    station_code: str
    local_date: date
    high_f: float | None
    low_f: float | None
    source: str
    source_url: str | None = None
    day_complete: bool = True


@dataclass(frozen=True)
class WundergroundHistoryEvidence:
    """Wunderground history URL metadata plus observed daily values for that station."""

    station_code: str
    history_url: str | None
    recent_days: tuple[DailyObservation, ...] = ()
    target_day: DailyObservation | None = None
    raw_high_f: float | None = None
    raw_low_f: float | None = None
    rounded_high_f: int | None = None
    rounded_low_f: int | None = None
    observed_source: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class MetarEvidence:
    station_code: str
    observed_at: datetime | None
    temperature_c: float | None
    dewpoint_c: float | None
    wind_dir_deg: float | None
    wind_speed_kt: float | None
    cloud_cover: str | None
    weather_code: str | None
    daily_max_c: float | None
    stale: bool = False
    source_url: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class NwsDiscussionEvidence:
    office_id: str | None
    product_url: str | None
    issued_at: datetime | None
    thunderstorm_timing: str | None = None
    frontal_timing: str | None = None
    cloud_clearing: str | None = None
    marine_layer: str | None = None
    confidence_language: str | None = None
    raw_excerpt: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class LocalTvEvidence:
    station_code: str
    source_name: str
    source_url: str
    published_at: datetime | None
    high_f: float | None
    narrative: str | None
    accepted: bool = False
    reject_reason: str | None = None


@dataclass(frozen=True)
class StationEvidence:
    """Internal station-first evidence bundle. Qualitative items are never providers."""

    station_code: str
    wunderground: WundergroundHistoryEvidence | None = None
    metar: MetarEvidence | None = None
    nws_discussion: NwsDiscussionEvidence | None = None
    local_tv: tuple[LocalTvEvidence, ...] = ()
    station_bias_f: float | None = None
    station_bias_days: int = 0
    storm_timing_vs_peak: str | None = None
    cloud_clearing: bool | None = None
    marine_layer: bool | None = None
    frontal_timing: str | None = None
    source_disagreement_f: float | None = None
    short_reason: str | None = None
    confidence_delta: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)
