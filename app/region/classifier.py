"""Classify Polymarket temperature events into scan regions before expensive analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.polymarket.city import extract_city_from_text
from app.region.registry import (
    city_to_country,
    country_code_to_region,
    station_to_country,
)
from app.region.types import ScanRegion
from app.weather.resolution import parse_resolution_details


@dataclass(frozen=True)
class RegionClassification:
    region: ScanRegion | None
    country_code: str | None
    source: str
    reason: str

    @property
    def is_unknown(self) -> bool:
        return self.region is None


_COUNTRY_TEXT_PATTERNS = [
    r"\b(united states|u\.s\.a\.|usa)\b",
    r"\b(united kingdom|u\.k\.|uk)\b",
    r"\b(france|germany|spain|italy|netherlands|belgium|switzerland)\b",
    r"\b(china|japan|south korea|korea|taiwan|hong kong|singapore|india|thailand)\b",
]

_COUNTRY_TEXT_TO_ISO: list[tuple[str, str]] = [
    ("united states", "US"), ("u.s.a.", "US"), ("usa", "US"),
    ("united kingdom", "GB"), ("u.k.", "GB"), ("uk", "GB"),
    ("france", "FR"), ("germany", "DE"), ("spain", "ES"), ("italy", "IT"),
    ("netherlands", "NL"), ("belgium", "BE"), ("switzerland", "CH"),
    ("china", "CN"), ("japan", "JP"), ("south korea", "KR"), ("korea", "KR"),
    ("taiwan", "TW"), ("hong kong", "HK"), ("singapore", "SG"),
    ("india", "IN"), ("thailand", "TH"),
]


def _country_from_event_text(text: str) -> str | None:
    lower = text.lower()
    for needle, iso in _COUNTRY_TEXT_TO_ISO:
        if needle in lower:
            return iso
    return None


def classify_market_region(
    text: str,
    *,
    station_code: str | None = None,
) -> RegionClassification:
    """Return region from station metadata, event location, or city mapping."""
    if not station_code:
        resolution = parse_resolution_details(text)
        station_code = resolution.station_code

    country = station_to_country(station_code)
    if country:
        region = country_code_to_region(country, station_code)
        if region is not None:
            return RegionClassification(
                region=region,
                country_code=country,
                source="station",
                reason=f"station {station_code} -> {country}",
            )

    event_country = _country_from_event_text(text)
    if event_country:
        region = country_code_to_region(event_country)
        if region is not None:
            return RegionClassification(
                region=region,
                country_code=event_country,
                source="event",
                reason=f"event text -> {event_country}",
            )

    city = extract_city_from_text(text, station_code)
    city_country = city_to_country(city)
    if city_country:
        region = country_code_to_region(city_country)
        if region is not None:
            return RegionClassification(
                region=region,
                country_code=city_country,
                source="city",
                reason=f"city {city} -> {city_country}",
            )

    return RegionClassification(
        region=None,
        country_code=country or city_country or event_country,
        source="unknown",
        reason="REGION_UNRESOLVED",
    )


def event_matches_scan_region(
    text: str,
    scan_region: ScanRegion,
    *,
    station_code: str | None = None,
) -> tuple[bool, str | None]:
    """Return whether an event should proceed to expensive analysis."""
    if scan_region is ScanRegion.ALL:
        return True, None

    classification = classify_market_region(text, station_code=station_code)
    if classification.is_unknown:
        return False, "REGION_UNRESOLVED"
    if classification.region != scan_region:
        return False, "REGION_MISMATCH"
    return True, None
