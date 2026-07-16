"""Public Iowa State IEM ASOS daily observations for an exact ICAO station."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import requests

from app.weather.evidence.types import DailyObservation

logger = logging.getLogger(__name__)

_IEM_DAILY = "https://mesonet.agron.iastate.edu/api/1/daily.json"
_AVWX_STATION = "https://aviationweather.gov/api/data/stationinfo"

# Common ASOS network prefixes for US ICAO (Kxxx → {STATE}_ASOS).
_NETWORK_HINTS: dict[str, str] = {
    "KORD": "IL_ASOS",
    "KATL": "GA_ASOS",
    "KSEA": "WA_ASOS",
    "KMIA": "FL_ASOS",
    "KDAL": "TX_ASOS",
    "KJFK": "NY_ASOS",
    "KLGA": "NY_ASOS",
    "KLAX": "CA_ASOS",
    "KAUS": "TX_ASOS",
    "KDFW": "TX_ASOS",
    "KPHX": "AZ_ASOS",
    "KLAS": "NV_ASOS",
    "KHOU": "TX_ASOS",
    "KSFO": "CA_ASOS",
    "KBKF": "CO_ASOS",
}

_NETWORK_CACHE: dict[str, str | None] = {}


def _iem_station_id(icao: str) -> str:
    code = icao.upper().strip()
    if code.startswith("K") and len(code) == 4:
        return code[1:]
    return code


def _resolve_asos_network(icao: str) -> str | None:
    code = icao.upper().strip()
    if code in _NETWORK_HINTS:
        return _NETWORK_HINTS[code]
    if code in _NETWORK_CACHE:
        return _NETWORK_CACHE[code]
    network: str | None = None
    try:
        response = requests.get(
            _AVWX_STATION,
            params={"ids": code, "format": "json"},
            timeout=15,
        )
        response.raise_for_status()
        rows = response.json()
        if isinstance(rows, list) and rows:
            state = str(rows[0].get("state") or "").strip().upper()
            country = str(rows[0].get("country") or "").strip().upper()
            if state and country in {"US", "USA", ""}:
                network = f"{state}_ASOS"
    except Exception as exc:
        logger.debug("ASOS network resolve failed for %s: %s", code, type(exc).__name__)
    _NETWORK_CACHE[code] = network
    return network


def fetch_asos_daily_range(
    station_code: str,
    *,
    end: date,
    days: int = 5,
    network: str | None = None,
) -> list[DailyObservation]:
    """Fetch recent completed daily high/low °F for the exact station via IEM."""
    code = station_code.upper().strip()
    if not code:
        return []

    net = network or _resolve_asos_network(code)
    if not net:
        logger.warning("IEM network unknown for %s; skipping daily history", code)
        return []

    station_id = _iem_station_id(code)
    start = end - timedelta(days=max(1, days) + 2)
    years = sorted({start.year, end.year})
    rows: list[dict] = []

    for year in years:
        params: dict[str, str | int] = {
            "station": station_id,
            "network": net,
            "year": year,
        }
        try:
            response = requests.get(_IEM_DAILY, params=params, timeout=20)
            response.raise_for_status()
            payload = response.json()
            chunk = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(chunk, list):
                rows.extend(item for item in chunk if isinstance(item, dict))
        except Exception as exc:
            logger.warning("IEM daily fetch failed for %s: %s", code, type(exc).__name__)
            continue

    by_day: dict[date, DailyObservation] = {}
    for row in rows:
        raw_date = str(row.get("date") or "")[:10]
        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if day < start or day > end:
            continue
        high = row.get("max_tmpf")
        low = row.get("min_tmpf")
        try:
            high_f = float(high) if high is not None else None
            low_f = float(low) if low is not None else None
        except (TypeError, ValueError):
            continue
        if high_f is None and low_f is None:
            continue
        day_complete = high_f is not None and low_f is not None
        by_day[day] = DailyObservation(
            station_code=code,
            local_date=day,
            high_f=high_f,
            low_f=low_f,
            source="asos_iem",
            source_url=_IEM_DAILY,
            day_complete=day_complete,
        )

    ordered = sorted(by_day.values(), key=lambda item: item.local_date)
    return ordered[-days:] if days > 0 else ordered
