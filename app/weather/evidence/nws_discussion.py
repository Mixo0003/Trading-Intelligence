"""NWS Area Forecast Discussion — qualitative USA evidence only."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from app.config.settings import Settings
from app.weather.evidence.types import NwsDiscussionEvidence
from app.weather.stations import WeatherStation

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "TradingIntelligence/1.0 github.com/Mixo0003/Trading-Intelligence",
    "Accept": "application/geo+json, application/json, text/plain",
}


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


def _office_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/offices/([A-Z]{3,4})\b", url)
    return match.group(1) if match else None


def _first_match(patterns: list[str], text: str, window: int = 120) -> str | None:
    lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, lower, re.I | re.DOTALL)
        if match:
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + window)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            return snippet[:180] if snippet else match.group(0)
    return None


def extract_discussion_signals(product_text: str) -> dict[str, str | None]:
    text = product_text or ""
    return {
        "thunderstorm_timing": _first_match(
            [
                r"thunder(?:storms?)?.{0,80}?(?:morning|afternoon|evening|overnight|between|\d{1,2}\s*(?:am|pm))",
                r"(?:morning|afternoon).{0,40}?thunder",
            ],
            text,
        ),
        "frontal_timing": _first_match(
            [
                r"(?:cold|warm|stationary)\s+front.{0,80}?(?:pass|move|arrive|tonight|morning|afternoon)",
                r"frontal\s+passage.{0,60}",
            ],
            text,
        ),
        "cloud_clearing": _first_match(
            [
                r"cloud(?:s|y)?.{0,60}?(?:clear|clearing|break|thin)",
                r"(?:clearing|sunshine).{0,40}",
            ],
            text,
        ),
        "marine_layer": _first_match(
            [
                r"marine\s+layer.{0,80}",
                r"coastal\s+(?:stratus|clouds|fog).{0,60}",
            ],
            text,
        ),
        "confidence_language": _first_match(
            [
                r"(?:low|limited|high)\s+confidence.{0,60}",
                r"uncertainty.{0,60}",
                r"forecast\s+confidence.{0,60}",
            ],
            text,
        ),
    }


def fetch_nws_discussion(
    settings: Settings,
    station: WeatherStation,
) -> NwsDiscussionEvidence:
    if not station.code.upper().startswith("K"):
        return NwsDiscussionEvidence(
            office_id=None,
            product_url=None,
            issued_at=None,
            error="NON_USA_STATION",
        )

    try:
        points_url = (
            f"{settings.weather_gov_base_url}/points/"
            f"{station.latitude},{station.longitude}"
        )
        points = requests.get(points_url, headers=_HEADERS, timeout=20)
        points.raise_for_status()
        props = points.json().get("properties") or {}
        office_id = _office_id_from_url(str(props.get("forecastOffice") or ""))
        if not office_id:
            return NwsDiscussionEvidence(
                office_id=None,
                product_url=None,
                issued_at=None,
                error="NO_OFFICE",
            )

        list_url = (
            f"{settings.weather_gov_base_url}/products/types/AFD/locations/{office_id}"
        )
        listing = requests.get(list_url, headers=_HEADERS, timeout=20)
        listing.raise_for_status()
        graph = listing.json().get("@graph") or []
        if not graph:
            return NwsDiscussionEvidence(
                office_id=office_id,
                product_url=None,
                issued_at=None,
                error="NO_PRODUCTS",
            )

        latest = graph[0]
        product_id = latest.get("id")
        issued_at = _parse_iso(str(latest.get("issuanceTime") or ""))
        product_url = f"{settings.weather_gov_base_url}/products/{product_id}"
        product = requests.get(product_url, headers=_HEADERS, timeout=20)
        product.raise_for_status()
        body = product.json().get("productText") or ""
        signals = extract_discussion_signals(body)
        excerpt = re.sub(r"\s+", " ", body.strip())[:240] if body else None

        return NwsDiscussionEvidence(
            office_id=office_id,
            product_url=product_url,
            issued_at=issued_at,
            thunderstorm_timing=signals["thunderstorm_timing"],
            frontal_timing=signals["frontal_timing"],
            cloud_clearing=signals["cloud_clearing"],
            marine_layer=signals["marine_layer"],
            confidence_language=signals["confidence_language"],
            raw_excerpt=excerpt,
            error=None,
        )
    except Exception as exc:
        logger.warning(
            "NWS discussion fetch failed for %s: %s",
            station.code,
            type(exc).__name__,
        )
        return NwsDiscussionEvidence(
            office_id=None,
            product_url=None,
            issued_at=None,
            error=type(exc).__name__,
        )
