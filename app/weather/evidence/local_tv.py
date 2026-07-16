"""Limited local TV meteorologist proof-of-concept (public pages only)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

import requests

from app.weather.evidence.types import LocalTvEvidence

logger = logging.getLogger(__name__)

_UA = {
    "User-Agent": "Mozilla/5.0 (compatible; TradingIntelligence/1.0; +https://github.com/Mixo0003/Trading-Intelligence)",
    "Accept": "text/html,application/xhtml+xml",
}

# Station → one or two public local ABC/NBC/FOX/CBS weather pages.
LOCAL_TV_SOURCES: dict[str, tuple[dict[str, str], ...]] = {
    "KORD": (
        {
            "name": "ABC7 Chicago",
            "url": "https://abc7chicago.com/weather/",
            "align": "chicago|o'hare|ohare|ord",
        },
        {
            "name": "NBC Chicago",
            "url": "https://www.nbcchicago.com/weather/",
            "align": "chicago|o'hare|ohare|ord",
        },
    ),
    "KATL": (
        {
            "name": "WSB-TV Atlanta",
            "url": "https://www.wsbtv.com/weather/",
            "align": "atlanta|hartsfield|atl",
        },
    ),
    "KSEA": (
        {
            "name": "KING 5 Seattle",
            "url": "https://www.king5.com/weather",
            "align": "seattle|tacoma|sea-tac|seatac",
        },
    ),
    "KMIA": (
        {
            "name": "Local 10 Miami",
            "url": "https://www.local10.com/weather/",
            "align": "miami|mia",
        },
    ),
    "KDAL": (
        {
            "name": "NBC DFW",
            "url": "https://www.nbcdfw.com/weather/",
            "align": "dallas|love field|dal",
        },
        {
            "name": "WFAA Dallas",
            "url": "https://www.wfaa.com/weather",
            "align": "dallas|love field|dal",
        },
    ),
}

_STALE_HOURS = 18


def _parse_page_time(html: str) -> datetime | None:
    patterns = [
        r'property="article:modified_time"\s+content="([^"]+)"',
        r'property="og:updated_time"\s+content="([^"]+)"',
        r'"dateModified"\s*:\s*"([^"]+)"',
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'datetime="([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if not match:
            continue
        raw = match.group(1).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _extract_high_f(html: str) -> float | None:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    patterns = [
        r"(?:high(?:s)?|upper)\s*(?:near|around|in the)?\s*(\d{2,3})\s*°?\s*F",
        r"(?:high(?:s)?|upper)\s*(?:near|around|in the)?\s*(\d{2,3})\b",
        r"(\d{2,3})\s*°\s*F\s*(?:for\s+)?(?:a\s+)?high",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = float(match.group(1))
            if 20 <= value <= 130:
                return value
    return None


def _extract_narrative(html: str) -> str | None:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 40:
        return None
    # Prefer a sentence mentioning weather timing.
    match = re.search(
        r"([^.?!]{20,160}(?:storm|rain|cloud|clear|front|heat|cool)[^.?!]{0,80}[.?!])",
        text,
        re.I,
    )
    if match:
        return match.group(1).strip()
    return text[:160]


def _aligned(html: str, align_pattern: str) -> bool:
    return bool(re.search(align_pattern, html, re.I))


def fetch_local_tv_evidence(station_code: str) -> tuple[LocalTvEvidence, ...]:
    code = station_code.upper().strip()
    sources = LOCAL_TV_SOURCES.get(code)
    if not sources:
        return ()

    results: list[LocalTvEvidence] = []
    now = datetime.now(timezone.utc)
    for source in sources:
        url = source["url"]
        name = source["name"]
        try:
            response = requests.get(url, headers=_UA, timeout=20)
            if response.status_code != 200:
                results.append(
                    LocalTvEvidence(
                        station_code=code,
                        source_name=name,
                        source_url=url,
                        published_at=None,
                        high_f=None,
                        narrative=None,
                        accepted=False,
                        reject_reason=f"HTTP_{response.status_code}",
                    )
                )
                continue
            html = response.text or ""
            if not _aligned(html, source["align"]):
                results.append(
                    LocalTvEvidence(
                        station_code=code,
                        source_name=name,
                        source_url=url,
                        published_at=None,
                        high_f=None,
                        narrative=None,
                        accepted=False,
                        reject_reason="AIRPORT_NOT_ALIGNED",
                    )
                )
                continue

            published = _parse_page_time(html)
            high_f = _extract_high_f(html)
            narrative = _extract_narrative(html)
            reject = None
            accepted = True
            if high_f is None:
                accepted = False
                reject = "AMBIGUOUS_OR_MISSING_HIGH"
            elif published is None:
                accepted = False
                reject = "MISSING_TIMESTAMP"
            elif now - published > timedelta(hours=_STALE_HOURS):
                accepted = False
                reject = "STALE"

            results.append(
                LocalTvEvidence(
                    station_code=code,
                    source_name=name,
                    source_url=url,
                    published_at=published,
                    high_f=high_f if accepted else high_f,
                    narrative=narrative if accepted else narrative,
                    accepted=accepted,
                    reject_reason=reject,
                )
            )
        except Exception as exc:
            logger.info("Local TV fetch failed for %s (%s): %s", code, name, type(exc).__name__)
            results.append(
                LocalTvEvidence(
                    station_code=code,
                    source_name=name,
                    source_url=url,
                    published_at=None,
                    high_f=None,
                    narrative=None,
                    accepted=False,
                    reject_reason=type(exc).__name__,
                )
            )
    return tuple(results)


def accepted_local_tv(items: Iterable[LocalTvEvidence]) -> list[LocalTvEvidence]:
    return [item for item in items if item.accepted and item.high_f is not None]
