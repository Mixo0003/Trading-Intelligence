"""Resolve official Polymarket Gamma tags (e.g. the Weather tag) without hardcoding IDs."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.polymarket.client import PolymarketClient

logger = logging.getLogger(__name__)

# Polymarket has used more than one slug for the umbrella weather tag over
# time (observed live: "climate-weather", historically "weather"). Match
# either known slug, or a standalone "weather" word in the label, rather
# than hardcoding one exact undocumented value. `\bweather\b` excludes
# unrelated compound matches such as the "mayweather" tag.
_WEATHER_TAG_SLUGS = frozenset({"weather", "climate-weather"})
_WEATHER_LABEL_RE = re.compile(r"\bweather\b", re.IGNORECASE)
_TAGS_PAGE_LIMIT = 100
_TAGS_MAX_PAGES = 30  # Safety net only; live catalog is a few hundred tags.


def _is_weather_tag(item: dict) -> bool:
    slug = str(item.get("slug") or "").strip().lower()
    label = str(item.get("label") or "")
    if slug in _WEATHER_TAG_SLUGS:
        return True
    return bool(_WEATHER_LABEL_RE.search(label))


@dataclass(frozen=True)
class TagInfo:
    id: str
    slug: str | None
    label: str | None


def resolve_weather_tag(client: PolymarketClient) -> TagInfo | None:
    """Find the official Weather tag via ``GET /tags`` by slug/label match.

    Never hardcodes a numeric tag id. Paginates with ``offset`` until a
    short page is returned or the safety cap is hit. Returns ``None`` if the
    tag cannot be resolved live -- callers must still run the full
    ``closed=false`` fallback discovery pass in that case.
    """
    offset = 0
    for _ in range(_TAGS_MAX_PAGES):
        data = client.get_json(
            "/tags",
            params={"limit": _TAGS_PAGE_LIMIT, "offset": offset},
        )
        items = data if isinstance(data, list) else (data or {}).get("tags", [])
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            if _is_weather_tag(item):
                tag = TagInfo(
                    id=str(item.get("id")),
                    slug=item.get("slug"),
                    label=item.get("label"),
                )
                logger.info(
                    "Resolved official weather tag: id=%s slug=%s label=%s",
                    tag.id,
                    tag.slug,
                    tag.label,
                )
                return tag
        if len(items) < _TAGS_PAGE_LIMIT:
            break
        offset += _TAGS_PAGE_LIMIT

    logger.warning(
        "Could not resolve an official 'weather' tag via /tags; "
        "relying on full closed=false fallback discovery only."
    )
    return None
