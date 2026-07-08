import re
import requests

from app.weather.stations import WeatherStation, get_station


def _clean_code(code: str | None) -> str | None:
    if not code:
        return None

    code = code.strip().upper()

    if re.fullmatch(r"K[A-Z]{3}", code):
        return code

    if re.fullmatch(r"[A-Z]{3}", code):
        return "K" + code

    return None


def extract_station_code_from_text(text: str) -> str | None:
    if not text:
        return None

    upper = text.upper()

    # Direct ICAO code: KLGA, KORD, KJFK
    match = re.search(r"\bK[A-Z]{3}\b", upper)
    if match:
        return match.group(0)

    # URLs often contain official station code: /KLGA, ?station=KLGA, stationId=KLGA, airport_code=KLGA
    url_code_patterns = [
        r"/(K[A-Z]{3})(?:\\b|/|\\?|$)",
        r"[?&](?:station|stationId|station_id|airport|airport_code|icao)=([A-Z]{3,4})(?:&|$)",
    ]

    for pattern in url_code_patterns:
        match = re.search(pattern, upper)
        if match:
            code = _clean_code(match.group(1))
            if code:
                return code

    # Station wording: LaGuardia Airport Station / JFK Airport Station
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


def _fetch_station_from_aviationweather(code: str) -> WeatherStation | None:
    try:
        response = requests.get(
            "https://aviationweather.gov/api/data/stationinfo",
            params={"ids": code, "format": "json"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, list) or not data:
            return None

        item = data[0]
        lat = item.get("lat") or item.get("latitude")
        lon = item.get("lon") or item.get("longitude")
        name = item.get("site") or item.get("name") or item.get("stationName") or code

        if lat is None or lon is None:
            return None

        return WeatherStation(
            code=code,
            name=str(name),
            latitude=float(lat),
            longitude=float(lon),
        )
    except Exception:
        return None


def resolve_station_from_text(text: str) -> WeatherStation | None:
    code = extract_station_code_from_text(text)

    if not code:
        return None

    known = get_station(code)
    if known is not None:
        return known

    return _fetch_station_from_aviationweather(code)
