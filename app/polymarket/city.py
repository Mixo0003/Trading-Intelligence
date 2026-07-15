import re

from app.config.settings import Settings

STATION_TO_CITY: dict[str, str] = {
    "KORD": "Chicago",
    "KJFK": "New York City",
    "KLGA": "New York City",
    "KMIA": "Miami",
    "KATL": "Atlanta",
    "KSEA": "Seattle",
    "KLAX": "Los Angeles",
}

CITY_ALIASES: dict[str, list[str]] = {
    "Chicago": ["chicago", "kord"],
    "New York City": ["new york", "nyc", "kjfk", "klga"],
    "Miami": ["miami", "kmia"],
    "Atlanta": ["atlanta", "katl"],
    "Seattle": ["seattle", "ksea"],
    "Los Angeles": ["los angeles", "la", "klax"],
}


def extract_city_from_text(text: str, station_code: str | None = None) -> str:
    if station_code and station_code.upper() in STATION_TO_CITY:
        return STATION_TO_CITY[station_code.upper()]

    if not text:
        return "Unknown"

    patterns = [
        r"(?:highest|lowest|maximum|minimum|high|low)\s+temperature\s+in\s+([A-Za-z\.\s'\-]+?)\s+(?:be|on|by)",
        r"temperature\s+in\s+([A-Za-z\.\s'\-]+?)\s+(?:be|on|by)",
        r"(?:in|at)\s+([A-Za-z\.\s'\-]+?)\s+be\s+\d",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            city = match.group(1).strip(" .'\"")
            if city:
                return city.title()

    return "Unknown"


def is_priority_us_city(city: str, station_code: str | None, priority_cities: list[str]) -> bool:
    haystack = " ".join([city or "", station_code or ""]).lower()
    for priority in priority_cities:
        aliases = CITY_ALIASES.get(priority, [priority.lower()])
        if any(alias in haystack for alias in aliases):
            return True
    return False


def text_matches_priority_city(text: str, slug: str, priority_cities: list[str]) -> bool:
    haystack = f"{text} {slug}".lower()
    for priority in priority_cities:
        aliases = CITY_ALIASES.get(priority, [priority.lower()])
        slug_alias = priority.lower().replace(" ", "-")
        if slug_alias in slug.lower():
            return True
        if any(alias in haystack for alias in aliases):
            return True
    return False


def default_priority_cities() -> list[str]:
    return list(CITY_ALIASES.keys())
