"""Live METAR observations for an exact ICAO station (evidence only, not a forecast provider)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from app.weather.evidence.types import MetarEvidence
from app.weather.stations import WeatherStation

logger = logging.getLogger(__name__)

_METAR_URL = "https://aviationweather.gov/api/data/metar"
_NWS_OBS_URL = "https://api.weather.gov/stations/{code}/observations"
_NWS_HEADERS = {
    "User-Agent": "TradingIntelligence/1.0 github.com/Mixo0003/Trading-Intelligence",
    "Accept": "application/geo+json, application/json",
}
_STALE_AFTER = timedelta(hours=2)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _cloud_from_metar(raw: dict) -> str | None:
    clouds = raw.get("clouds")
    if isinstance(clouds, list) and clouds:
        parts = []
        for layer in clouds:
            if not isinstance(layer, dict):
                continue
            cover = layer.get("cover") or layer.get("type")
            base = layer.get("base")
            if cover and base is not None:
                parts.append(f"{cover}{base}")
            elif cover:
                parts.append(str(cover))
        return " ".join(parts) if parts else None
    cover = raw.get("cover")
    return str(cover) if cover else None


def fetch_metar_evidence(station: WeatherStation) -> MetarEvidence:
    code = station.code.upper()
    source_url = f"{_METAR_URL}?ids={code}&format=json"
    try:
        response = requests.get(
            _METAR_URL,
            params={"ids": code, "format": "json"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        raw = data[0] if isinstance(data, list) and data else None
    except Exception as exc:
        logger.warning("METAR fetch failed for %s: %s", code, type(exc).__name__)
        return MetarEvidence(
            station_code=code,
            observed_at=None,
            temperature_c=None,
            dewpoint_c=None,
            wind_dir_deg=None,
            wind_speed_kt=None,
            cloud_cover=None,
            weather_code=None,
            daily_max_c=None,
            stale=True,
            source_url=source_url,
            error=type(exc).__name__,
        )

    if not isinstance(raw, dict):
        return MetarEvidence(
            station_code=code,
            observed_at=None,
            temperature_c=None,
            dewpoint_c=None,
            wind_dir_deg=None,
            wind_speed_kt=None,
            cloud_cover=None,
            weather_code=None,
            daily_max_c=None,
            stale=True,
            source_url=source_url,
            error="EMPTY",
        )

    observed_at = _parse_iso(str(raw.get("reportTime") or raw.get("obsTime") or ""))
    now = datetime.now(timezone.utc)
    stale = observed_at is None or (now - observed_at) > _STALE_AFTER

    temp = raw.get("temp")
    dewp = raw.get("dewp")
    try:
        temperature_c = float(temp) if temp is not None else None
    except (TypeError, ValueError):
        temperature_c = None
    try:
        dewpoint_c = float(dewp) if dewp is not None else None
    except (TypeError, ValueError):
        dewpoint_c = None

    wind_dir = raw.get("wdir")
    wind_spd = raw.get("wspd")
    try:
        wind_dir_deg = float(wind_dir) if wind_dir is not None else None
    except (TypeError, ValueError):
        wind_dir_deg = None
    try:
        wind_speed_kt = float(wind_spd) if wind_spd is not None else None
    except (TypeError, ValueError):
        wind_speed_kt = None

    daily_max_c = None if stale else _daily_max_from_nws_obs(code, temperature_c)

    return MetarEvidence(
        station_code=code,
        observed_at=observed_at,
        temperature_c=temperature_c,
        dewpoint_c=dewpoint_c,
        wind_dir_deg=wind_dir_deg,
        wind_speed_kt=wind_speed_kt,
        cloud_cover=_cloud_from_metar(raw),
        weather_code=str(raw.get("wxString") or "") or None,
        daily_max_c=daily_max_c,
        stale=stale,
        source_url=source_url,
        error=None,
    )


def _daily_max_from_nws_obs(code: str, current_c: float | None) -> float | None:
    """Derive today's max only from valid same-local-day NWS observation temperatures."""
    try:
        response = requests.get(
            _NWS_OBS_URL.format(code=code),
            headers=_NWS_HEADERS,
            params={"limit": 40},
            timeout=20,
        )
        response.raise_for_status()
        features = response.json().get("features") or []
    except Exception:
        return current_c

    temps: list[float] = []
    today = datetime.now(timezone.utc).date()
    for feature in features:
        props = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(props, dict):
            continue
        ts = _parse_iso(str(props.get("timestamp") or ""))
        if ts is None or ts.astimezone(timezone.utc).date() != today:
            continue
        temp_obj = props.get("temperature") or {}
        value = temp_obj.get("value") if isinstance(temp_obj, dict) else None
        if value is None:
            continue
        try:
            temps.append(float(value))
        except (TypeError, ValueError):
            continue
    if current_c is not None:
        temps.append(current_c)
    return max(temps) if temps else current_c


def is_pre_peak_precip(metar: MetarEvidence, *, local_hour: int | None) -> bool:
    if metar.stale or not metar.weather_code:
        return False
    wx = metar.weather_code.upper()
    raining = any(token in wx for token in ("RA", "TS", "SH", "DZ"))
    if not raining:
        return False
    if local_hour is None:
        return True
    return local_hour < 15
