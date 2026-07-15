"""METAR-based live triggers for actionable weather events."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from app.weather.stations import WeatherStation
from app.weather.temperature_units import TemperatureUnit, celsius_to_fahrenheit, format_temperature

logger = logging.getLogger(__name__)


def fetch_current_metar(station_code: str) -> dict | None:
    try:
        response = requests.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": station_code, "format": "json"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return data[0] if data else None
    except Exception as exc:
        logger.warning("METAR fetch failed for %s: %s", station_code, exc)
        return None


def build_live_triggers(
    station: WeatherStation,
    *,
    event_type_label: str,
    consensus_native: float,
    unit: TemperatureUnit,
    target_date_local: str | None,
) -> list[str]:
    metar = fetch_current_metar(station.code)
    if not metar:
        return []

    temp_c = metar.get("temp")
    if temp_c is None:
        return []

    if unit == TemperatureUnit.FAHRENHEIT:
        current = celsius_to_fahrenheit(float(temp_c))
        consensus = consensus_native
        symbol = "°F"
    else:
        current = float(temp_c)
        consensus = consensus_native
        symbol = "°C"

    wx = str(metar.get("wxString") or "")
    triggers: list[str] = []

    if "daily high" in event_type_label.lower():
        needed = consensus - 2.0
        triggers.append(
            f"Temperature must reach {needed:.0f}{symbol} by 13:00 local to support consensus."
        )
        if "TS" in wx or "RA" in wx:
            triggers.append("Convection/cloud cover may cap the high before peak heating.")
    else:
        triggers.append(
            f"Overnight low trajectory currently {current:.0f}{symbol}; watch after sunset."
        )

    if station.timezone:
        try:
            local_hour = datetime.now(ZoneInfo(station.timezone)).hour
            if local_hour >= 17 and "daily high" in event_type_label.lower():
                triggers.append("After 17:00 local, daily maximum is likely fixed unless late clearing.")
        except Exception:
            pass

    return triggers[:3]
