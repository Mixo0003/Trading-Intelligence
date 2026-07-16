import logging
import math
import re
from typing import TYPE_CHECKING

import requests

from app.metrics.scan_metrics import active_scan_metrics
from app.weather.stations import WeatherStation, get_station

if TYPE_CHECKING:
    from app.config.settings import Settings

logger = logging.getLogger(__name__)

_STATION_WORD_BLOCKLIST = frozenset({
    "WILL", "THAN", "THIS", "THAT", "WITH", "FROM", "HAVE", "BEEN", "WERE",
    "WHEN", "WHAT", "HIGH", "BELOW", "OVER", "INTO", "ONLY", "ALSO", "EACH",
    "MORE", "MOST", "SOME", "SUCH", "UPON", "THEM", "THEN", "DAILY", "JULY",
    "JUNE", "AUGU", "SEPT", "OCTO", "NOVE", "DECE", "JANU", "MOND", "TUES",
    "WEDN", "THUR", "FRID", "SATU", "SUND", "YES", "NO",
    "KONG",
})

_EXPLICIT_STATION_PATTERNS = (
    r"\b(?:AT|FOR|USING|VIA)\s+(?:STATION\s+)?([A-Z]{4})\b",
    r"\bSTATION\s+([A-Z]{4})\b",
    r"\b([A-Z]{4})\s+(?:AIRPORT|STATION)\b",
    r"\b(?:ICAO|AIRPORT)\s+([A-Z]{3,4})\b",
)


def _clean_code(code: str | None) -> str | None:
    if not code:
        return None

    code = code.strip().upper()

    if re.fullmatch(r"[A-Z]{4}", code):
        return code

    if re.fullmatch(r"[A-Z]{3}", code):
        return "K" + code

    return None


def extract_station_code_from_text(text: str) -> str | None:
    if not text:
        return None

    upper = text.upper()

    for url in extract_urls_from_text(text):
        clean_url = url.rstrip(".,;)")
        last_part = clean_url.rstrip("/").split("/")[-1].split("?")[0].upper()
        code = _clean_code(last_part)
        if code:
            return code

    query_match = re.search(
        r"[?&](?:station|stationId|station_id|airport|airport_code|icao)=([A-Z]{3,4})(?:&|$)",
        upper,
    )
    if query_match:
        code = _clean_code(query_match.group(1))
        if code:
            return code

    for pattern in _EXPLICIT_STATION_PATTERNS:
        match = re.search(pattern, upper)
        if match:
            code = _clean_code(match.group(1))
            if code and code not in _STATION_WORD_BLOCKLIST:
                return code

    for match in re.finditer(r"\b[A-Z]{4}\b", upper):
        code = _clean_code(match.group(0))
        if not code or code in _STATION_WORD_BLOCKLIST:
            continue
        if get_station(code) is not None:
            return code
        from app.region.registry import STATION_COUNTRY
        if code in STATION_COUNTRY:
            return code

    match = re.search(r"\b([A-Z]{3})\s+(?:AIRPORT|STATION)\b", upper)
    if match:
        return _clean_code(match.group(1))

    return None


def extract_urls_from_text(text: str) -> list[str]:
    if not text:
        return []

    return re.findall(r"https?://[^\s\)\]\}\"']+", text)


def extract_resolution_url_from_text(text: str) -> str | None:
    urls = extract_urls_from_text(text)

    if not urls:
        return None

    preferred_keywords = [
        "wunderground.com",
        "weather.gov",
        "noaa.gov",
        "aviationweather.gov",
        "meteostat",
    ]

    for keyword in preferred_keywords:
        for url in urls:
            if keyword in url.lower():
                return url.rstrip(".,;")

    return urls[0].rstrip(".,;")


def _track_reference_request() -> None:
    metrics = active_scan_metrics()
    if metrics is not None:
        metrics.reference_requests += 1


def _fetch_station_from_aviationweather(code: str) -> WeatherStation | None:
    try:
        response = requests.get(
            "https://aviationweather.gov/api/data/stationinfo",
            params={"ids": code, "format": "json"},
            timeout=15,
        )
        _track_reference_request()
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, list) or not data:
            return None

        item = data[0]
        lat = item.get("lat") or item.get("latitude")
        lon = item.get("lon") or item.get("longitude")
        name = item.get("site") or item.get("name") or item.get("stationName") or code
        elevation = item.get("elev") or item.get("elevation")

        if lat is None or lon is None:
            return None

        elev_m = float(elevation) if elevation is not None else None

        return WeatherStation(
            code=code,
            name=str(name),
            latitude=float(lat),
            longitude=float(lon),
            elevation_m=elev_m,
            verified=True,
        )
    except Exception as exc:
        logger.warning("AviationWeather station lookup failed for %s: %s", code, exc)
        return None


def _fetch_nws_timezone(settings: Settings, latitude: float, longitude: float) -> str | None:
    headers = {
        "User-Agent": "Trading Intelligence Bot contact@example.com",
        "Accept": "application/geo+json, application/json",
    }
    try:
        points_url = (
            f"{settings.weather_gov_base_url}/points/{latitude},{longitude}"
        )
        response = requests.get(points_url, headers=headers, timeout=15)
        _track_reference_request()
        response.raise_for_status()
        data = response.json()
        return data.get("properties", {}).get("timeZone")
    except Exception as exc:
        logger.warning("NWS timezone lookup failed: %s", exc)
        return None


def resolve_verified_station_from_text(
    text: str,
    settings: Settings | None = None,
) -> tuple[WeatherStation | None, str | None]:
    """Resolve the ICAO station from market rules and verify coordinates via AviationWeather."""
    from app.signals.scan_runtime import active_scan_state

    state = active_scan_state()
    cache_key = (text or "").strip()
    if state is not None and cache_key and cache_key in state.station_cache:
        return state.station_cache[cache_key]

    code = extract_station_code_from_text(text)
    if not code:
        result = (None, "STATION_NOT_VERIFIED")
        if state is not None and cache_key:
            state.station_cache[cache_key] = result
        return result

    resolution_url = extract_resolution_url_from_text(text)
    station = _fetch_station_from_aviationweather(code)

    if station is None:
        known = get_station(code)
        if known is None:
            result = (None, "STATION_NOT_VERIFIED")
            if state is not None and cache_key:
                state.station_cache[cache_key] = result
            return result
        result = (
            WeatherStation(
                code=known.code,
                name=known.name,
                latitude=known.latitude,
                longitude=known.longitude,
                resolution_url=resolution_url,
                verified=False,
            ),
            "STATION_NOT_VERIFIED",
        )
        if state is not None and cache_key:
            state.station_cache[cache_key] = result
        return result

    timezone = station.timezone
    if timezone is None and settings is not None and code.startswith("K"):
        timezone = _fetch_nws_timezone(settings, station.latitude, station.longitude)

    result = (
        WeatherStation(
            code=station.code,
            name=station.name,
            latitude=station.latitude,
            longitude=station.longitude,
            elevation_m=station.elevation_m,
            timezone=timezone,
            verified=True,
            resolution_url=resolution_url,
        ),
        None,
    )
    if state is not None and cache_key:
        state.station_cache[cache_key] = result
    return result


def resolve_station_from_text(text: str) -> WeatherStation | None:
    station, _ = resolve_verified_station_from_text(text)
    return station


def haversine_distance_km(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
