"""Classify Polymarket temperature events into scan regions before expensive analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.polymarket.city import extract_city_from_text
from app.region.registry import (
    city_to_country,
    country_code_to_region,
    station_to_country,
)
from app.region.types import ScanRegion
from app.weather.resolution import parse_resolution_details
from app.weather.station_resolver import extract_resolution_url_from_text
from app.weather.stations import WeatherStation


@dataclass(frozen=True)
class RegionClassification:
    region: ScanRegion | None
    country_code: str | None
    source: str
    reason: str
    station_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    evidence: tuple[str, ...] = ()
    conflict: bool = False

    @property
    def is_unknown(self) -> bool:
        return self.region is None and self.country_code is None and not self.conflict

    @property
    def is_conflict(self) -> bool:
        return self.conflict


_COUNTRY_TEXT_TO_ISO: tuple[tuple[str, str], ...] = (
    ("united states", "US"), ("u.s.a.", "US"), ("usa", "US"),
    ("united kingdom", "GB"), ("u.k.", "GB"), ("uk", "GB"),
    ("france", "FR"), ("germany", "DE"), ("spain", "ES"), ("italy", "IT"),
    ("netherlands", "NL"), ("belgium", "BE"), ("switzerland", "CH"),
    ("finland", "FI"), ("ireland", "IE"), ("austria", "AT"),
    ("china", "CN"), ("japan", "JP"), ("south korea", "KR"), ("korea", "KR"),
    ("taiwan", "TW"), ("hong kong", "HK"), ("singapore", "SG"),
    ("india", "IN"), ("thailand", "TH"), ("saudi arabia", "SA"),
    ("panama", "PA"),
)

_US_STATE_CODES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
})

_US_COORDINATE_BOUNDS = (
    (24.396308, 49.384358, -124.848974, -66.885444, "contiguous US"),
    (51.214183, 71.365162, -179.148909, -129.9795, "Alaska"),
    (18.910361, 22.2356, -160.2471, -154.8068, "Hawaii"),
)


def _country_from_event_text(text: str) -> str | None:
    lower = text.lower()
    for needle, iso in _COUNTRY_TEXT_TO_ISO:
        if re.search(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", lower):
            return iso
    return None


def _normalize_country(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"[A-Za-z]{2}", text):
        return text.upper()
    return _country_from_event_text(text)


def _country_from_event_metadata(event_metadata: dict[str, Any] | None) -> str | None:
    if not event_metadata:
        return None
    for key in ("countryCode", "country_code", "country", "isoCountryCode"):
        country = _normalize_country(event_metadata.get(key))
        if country:
            return country
    for key in ("location", "venue", "city", "state"):
        value = event_metadata.get(key)
        country = _country_from_event_text(str(value or ""))
        if country:
            return country
        if value and re.search(
            rf"\b(?:{'|'.join(sorted(_US_STATE_CODES))})\b",
            str(value).upper(),
        ):
            return "US"
    return None


def _country_from_resolution_url(text: str) -> str | None:
    url = extract_resolution_url_from_text(text)
    if not url:
        return None
    match = re.search(
        r"wunderground\.com/history/daily/([a-z]{2})(?:/|$)",
        url,
        re.IGNORECASE,
    )
    return match.group(1).upper() if match else None


def coordinates_in_united_states(
    latitude: float | None,
    longitude: float | None,
) -> tuple[bool, str | None]:
    """Return whether coordinates fall in the contiguous US, Alaska, or Hawaii."""
    if latitude is None or longitude is None:
        return False, None
    for min_lat, max_lat, min_lon, max_lon, label in _US_COORDINATE_BOUNDS:
        if min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon:
            return True, label
    return False, "outside supported US bounds"


def classify_market_region(
    text: str,
    *,
    station_code: str | None = None,
    station: WeatherStation | None = None,
    event_metadata: dict[str, Any] | None = None,
) -> RegionClassification:
    """Classify a parent event using verified geographic evidence in priority order."""
    if not station_code:
        resolution = parse_resolution_details(text)
        station_code = resolution.station_code
    if station is not None:
        resolved_code = station.code.upper()
        if station_code and station_code.upper() != resolved_code:
            return RegionClassification(
                region=None,
                country_code=None,
                source="conflict",
                reason=(
                    "REGION_CONFLICT: parsed station "
                    f"{station_code.upper()} != resolved station {resolved_code}"
                ),
                station_code=resolved_code,
                latitude=station.latitude,
                longitude=station.longitude,
                evidence=(
                    f"parsed station={station_code.upper()}",
                    f"resolved station={resolved_code}",
                ),
                conflict=True,
            )
        station_code = resolved_code

    latitude = station.latitude if station is not None else None
    longitude = station.longitude if station is not None else None
    inside_us, coordinate_label = coordinates_in_united_states(latitude, longitude)
    station_country = station_to_country(station_code)
    metadata_country = _country_from_event_metadata(event_metadata)
    url_country = _country_from_resolution_url(text)
    event_country = _country_from_event_text(text)
    city = extract_city_from_text(text, station_code)
    city_country = city_to_country(city)

    candidates: list[tuple[str, str, str]] = []
    if inside_us:
        candidates.append(
            ("coordinates", "US", f"station coordinates inside {coordinate_label}")
        )
    if station_country:
        candidates.append(
            ("station", station_country, f"ICAO station {station_code} -> {station_country}")
        )
    if metadata_country:
        candidates.append(
            ("event_metadata", metadata_country, f"parent metadata -> {metadata_country}")
        )
    if url_country:
        candidates.append(
            ("resolution_url", url_country, f"resolution URL -> {url_country}")
        )
    if event_country:
        candidates.append(("event_text", event_country, f"event text -> {event_country}"))
    if city_country:
        candidates.append(("city_registry", city_country, f"city {city} -> {city_country}"))

    evidence = tuple(detail for _, _, detail in candidates)
    countries = {country for _, country, _ in candidates}
    if coordinate_label == "outside supported US bounds" and "US" in countries:
        evidence += ("station coordinates outside supported US bounds",)
        return RegionClassification(
            region=None,
            country_code=None,
            source="conflict",
            reason="REGION_CONFLICT: US evidence conflicts with station coordinates",
            station_code=station_code,
            latitude=latitude,
            longitude=longitude,
            evidence=evidence,
            conflict=True,
        )
    if len(countries) > 1:
        return RegionClassification(
            region=None,
            country_code=None,
            source="conflict",
            reason="REGION_CONFLICT: " + "; ".join(evidence),
            station_code=station_code,
            latitude=latitude,
            longitude=longitude,
            evidence=evidence,
            conflict=True,
        )
    if candidates:
        source, country, detail = candidates[0]
        return RegionClassification(
            region=country_code_to_region(country, station_code),
            country_code=country,
            source=source,
            reason=detail,
            station_code=station_code,
            latitude=latitude,
            longitude=longitude,
            evidence=evidence,
        )

    return RegionClassification(
        region=None,
        country_code=None,
        source="unknown",
        reason="REGION_UNRESOLVED",
        station_code=station_code,
        latitude=latitude,
        longitude=longitude,
        evidence=(
            f"station coordinates {latitude},{longitude} outside supported US bounds",
        ) if latitude is not None and longitude is not None else (),
    )


def event_matches_scan_region(
    text: str,
    scan_region: ScanRegion,
    *,
    station_code: str | None = None,
    station: WeatherStation | None = None,
    event_metadata: dict[str, Any] | None = None,
    classification: RegionClassification | None = None,
) -> tuple[bool, str | None]:
    """Return whether an event should proceed to expensive analysis."""
    if scan_region is ScanRegion.ALL:
        return True, None

    classification = classification or classify_market_region(
        text,
        station_code=station_code,
        station=station,
        event_metadata=event_metadata,
    )
    if classification.is_conflict:
        return False, "REGION_CONFLICT"
    if classification.is_unknown:
        return False, "REGION_UNRESOLVED"
    if classification.region != scan_region:
        return False, "REGION_MISMATCH"
    return True, None
