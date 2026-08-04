"""Strict construction of public Polymarket links from validated identifiers."""

from __future__ import annotations

import re
import unicodedata

POLYMARKET_EVENT_BASE_URL = "https://polymarket.com/event"
_EVENT_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,198}[A-Za-z0-9])?$")


def build_validated_market_link(slug: str | None) -> str | None:
    """Return the approved event URL, or ``None`` for any unsafe slug."""
    if not isinstance(slug, str) or not slug or slug != slug.strip():
        return None
    if any(char.isspace() for char in slug):
        return None
    if any(unicodedata.category(char).startswith("C") for char in slug):
        return None
    if not _EVENT_SLUG_RE.fullmatch(slug):
        return None
    return f"{POLYMARKET_EVENT_BASE_URL}/{slug}"
