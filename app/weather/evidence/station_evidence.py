"""Gather station-first evidence and derive conservative analysis hints."""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config.settings import Settings
from app.polymarket.market_type import NormalizedTemperatureType
from app.weather.daily_forecast import DailyForecastStats
from app.weather.evidence.local_tv import accepted_local_tv, fetch_local_tv_evidence
from app.weather.evidence.metar import fetch_metar_evidence, is_pre_peak_precip
from app.weather.evidence.nws_discussion import fetch_nws_discussion
from app.weather.evidence.types import StationEvidence
from app.weather.evidence.wunderground_history import (
    compute_station_bias_f,
    fetch_wunderground_history_evidence,
)
from app.weather.stations import WeatherStation

logger = logging.getLogger(__name__)

_BIAS_APPLY_MIN_DAYS = 4


def _local_hour(station: WeatherStation) -> int | None:
    if not station.timezone:
        return None
    try:
        return datetime.now(ZoneInfo(station.timezone)).hour
    except Exception:
        return None


def _disagreement_f(forecast_stats: DailyForecastStats | None) -> float | None:
    if forecast_stats is None:
        return None
    if forecast_stats.reference_divergence_c is not None:
        return forecast_stats.reference_divergence_c * 9.0 / 5.0
    if forecast_stats.provider_spread_c:
        return forecast_stats.provider_spread_c * 9.0 / 5.0
    return None


def _pick_short_reason(
    *,
    event_type: NormalizedTemperatureType,
    pre_peak_storms: bool,
    post_peak_storms: bool,
    disagreement_f: float | None,
    bias_f: float | None,
    bias_days: int,
    cloud_clearing: bool | None,
    marine_layer: bool | None,
) -> str | None:
    if disagreement_f is not None and disagreement_f >= 4.0:
        return "Models disagree by 4°F"
    if event_type == NormalizedTemperatureType.DAILY_HIGH and pre_peak_storms and not post_peak_storms:
        return "Morning storms may cap the high"
    if marine_layer:
        return "Morning storms may cap the high"
    if cloud_clearing:
        return "Clear sky supports full heating"
    if (
        bias_f is not None
        and bias_days >= _BIAS_APPLY_MIN_DAYS
        and abs(bias_f) >= 0.8
    ):
        direction = "above" if bias_f > 0 else "below"
        return f"Station runs ~{abs(bias_f):.0f}°F {direction} city forecasts"
    return None


def gather_station_evidence(
    settings: Settings,
    station: WeatherStation,
    *,
    target: date | None,
    event_type: NormalizedTemperatureType,
    resolution_url: str | None,
    forecast_stats: DailyForecastStats | None,
) -> StationEvidence:
    """Collect observation + qualitative evidence. Failures never raise."""
    notes: list[str] = []
    wu = None
    metar = None
    discussion = None
    local_tv: tuple = ()

    try:
        wu = fetch_wunderground_history_evidence(
            station,
            target=target,
            resolution_url=resolution_url,
        )
    except Exception as exc:
        notes.append(f"wunderground:{type(exc).__name__}")

    try:
        metar = fetch_metar_evidence(station)
        if metar.stale:
            notes.append("metar:stale")
    except Exception as exc:
        notes.append(f"metar:{type(exc).__name__}")

    if station.code.upper().startswith("K"):
        try:
            discussion = fetch_nws_discussion(settings, station)
        except Exception as exc:
            notes.append(f"nws_discussion:{type(exc).__name__}")
        try:
            local_tv = fetch_local_tv_evidence(station.code)
        except Exception as exc:
            notes.append(f"local_tv:{type(exc).__name__}")
            local_tv = ()

    recent = list(wu.recent_days) if wu else []
    bias_f, bias_days = compute_station_bias_f(recent, min_days=3)

    hour = _local_hour(station)
    pre_peak = bool(metar and is_pre_peak_precip(metar, local_hour=hour))
    post_peak = bool(
        metar
        and metar.weather_code
        and any(t in metar.weather_code.upper() for t in ("TS", "RA"))
        and hour is not None
        and hour >= 17
    )
    storm_timing = None
    if pre_peak:
        storm_timing = "pre_peak"
    elif post_peak:
        storm_timing = "post_peak"

    cloud_clearing = None
    marine_layer = None
    frontal = None
    if discussion and not discussion.error:
        if discussion.cloud_clearing:
            cloud_clearing = True
        if discussion.marine_layer:
            marine_layer = True
        frontal = discussion.frontal_timing

    disagreement = _disagreement_f(forecast_stats)

    # Conservative confidence adjustments only — never rewrite probability core.
    delta = 0.0
    if disagreement is not None and disagreement >= 4.0:
        delta -= 0.08
    if pre_peak and event_type == NormalizedTemperatureType.DAILY_HIGH:
        delta -= 0.05
    if post_peak and event_type == NormalizedTemperatureType.DAILY_HIGH:
        # Post-peak storms should not automatically lower the maximum.
        delta += 0.0
    if discussion and discussion.confidence_language and "low confidence" in (
        discussion.confidence_language or ""
    ).lower():
        delta -= 0.04
    # Station bias is display-only unless enough days AND we had paired forecasts.
    # compute_station_bias_f without paired forecasts returns 0.0 — do not apply.
    if bias_f is not None and bias_days >= _BIAS_APPLY_MIN_DAYS and abs(bias_f) >= 0.8:
        notes.append(f"bias_display_only:{bias_f:.2f}F over {bias_days}d")

    short = _pick_short_reason(
        event_type=event_type,
        pre_peak_storms=pre_peak,
        post_peak_storms=post_peak,
        disagreement_f=disagreement,
        bias_f=bias_f if bias_days >= _BIAS_APPLY_MIN_DAYS else None,
        bias_days=bias_days,
        cloud_clearing=cloud_clearing,
        marine_layer=marine_layer,
    )

    tv_accepted = accepted_local_tv(local_tv)
    if tv_accepted:
        notes.append(f"local_tv_accepted:{tv_accepted[0].source_name}")

    return StationEvidence(
        station_code=station.code.upper(),
        wunderground=wu,
        metar=metar,
        nws_discussion=discussion,
        local_tv=tuple(local_tv),
        station_bias_f=bias_f,
        station_bias_days=bias_days,
        storm_timing_vs_peak=storm_timing,
        cloud_clearing=cloud_clearing,
        marine_layer=marine_layer,
        frontal_timing=frontal,
        source_disagreement_f=disagreement,
        short_reason=short,
        confidence_delta=delta,
        notes=tuple(notes),
    )


def qualitative_provider_count(_evidence: StationEvidence | None) -> int:
    """Qualitative evidence never counts as a numerical independent provider."""
    return 0
