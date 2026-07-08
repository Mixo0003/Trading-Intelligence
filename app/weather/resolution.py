import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ResolutionDetails:
    source: str | None
    station_code: str | None
    metric: str | None
    threshold_f: float | None
    target_date: str | None
    raw_text: str


def extract_station_code(text: str) -> str | None:
    if not text:
        return None

    upper = text.upper()

    # Prefer explicit airport/weather station codes: KORD, KJFK, KLAX, KDFW, KPHX
    matches = re.findall(r"\bK[A-Z]{3}\b", upper)
    if matches:
        return matches[0]

    # Fallback only if text explicitly says station
    match = re.search(r"(?:STATION|WEATHER STATION|AIRPORT)\s+([A-Z]{3,4})\b", upper)
    if match:
        code = match.group(1)
        if len(code) == 3:
            return "K" + code
        return code

    return None


def extract_resolution_source(text: str) -> str | None:
    if not text:
        return None

    lower = text.lower()

    if "weather underground" in lower or "wunderground" in lower:
        return "weather_underground"

    if "weather.gov" in lower or "national weather service" in lower or "nws" in lower:
        return "weather_gov"

    if "noaa" in lower:
        return "noaa"

    return None


def extract_metric(text: str) -> str | None:
    lower = (text or "").lower()

    if any(w in lower for w in ["rain", "rainfall", "precipitation"]):
        return "rain"

    if any(w in lower for w in ["snow", "snowfall"]):
        return "snow"

    if any(w in lower for w in ["wind", "wind speed", "gust"]):
        return "wind"

    if any(w in lower for w in ["high temperature", "daily high", "maximum temperature", "max temperature"]):
        return "max_temperature_f"

    if any(w in lower for w in ["low temperature", "daily low", "minimum temperature", "min temperature"]):
        return "min_temperature_f"

    if "temperature" in lower:
        return "temperature_f"

    return None


def extract_threshold_f(text: str) -> float | None:
    if not text:
        return None

    patterns = [
        r"over\s+(\d{2,3}(?:\.\d+)?)\s*°?\s*f",
        r"above\s+(\d{2,3}(?:\.\d+)?)\s*°?\s*f",
        r"exceed(?:s|ed)?\s+(\d{2,3}(?:\.\d+)?)\s*°?\s*f",
        r"under\s+(\d{2,3}(?:\.\d+)?)\s*°?\s*f",
        r"below\s+(\d{2,3}(?:\.\d+)?)\s*°?\s*f",
        r"(\d{2,3}(?:\.\d+)?)\s*°\s*f",
        r"(\d{2,3}(?:\.\d+)?)\s*f\b",
    ]

    lower = text.lower()

    for pattern in patterns:
        match = re.search(pattern, lower)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None

    return None


def extract_date_text(text: str) -> str | None:
    if not text:
        return None

    month_names = (
        "january|february|march|april|may|june|july|august|"
        "september|october|november|december"
    )

    match = re.search(
        rf"\b({month_names})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*\d{{4}})?\b",
        text.lower(),
    )
    if match:
        return match.group(0)

    iso_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if iso_match:
        return iso_match.group(0)

    return None


def parse_resolution_details(text: str) -> ResolutionDetails:
    raw_text = text or ""

    return ResolutionDetails(
        source=extract_resolution_source(raw_text),
        station_code=extract_station_code(raw_text),
        metric=extract_metric(raw_text),
        threshold_f=extract_threshold_f(raw_text),
        target_date=extract_date_text(raw_text),
        raw_text=raw_text,
    )
