"""Validated known Polymarket category identifiers for Opportunity UX."""

from __future__ import annotations

import re

_CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

# Curated closed catalog for Telegram toggles (callback-safe identifiers).
KNOWN_CATEGORY_IDS: tuple[str, ...] = (
    "politics",
    "geopolitics",
    "weather",
    "sports",
    "crypto",
    "finance",
    "pop-culture",
    "science",
    "tech",
    "business",
    "world",
    "elections",
)

KNOWN_CATEGORY_SET = frozenset(KNOWN_CATEGORY_IDS)

CATEGORY_LABELS: dict[str, str] = {
    "politics": "Politics",
    "geopolitics": "Geopolitics",
    "weather": "Weather",
    "sports": "Sports",
    "crypto": "Crypto",
    "finance": "Finance",
    "pop-culture": "Pop Culture",
    "science": "Science",
    "tech": "Tech",
    "business": "Business",
    "world": "World",
    "elections": "Elections",
}


def is_known_category_id(category_id: str) -> bool:
    return (
        isinstance(category_id, str)
        and bool(_CATEGORY_RE.fullmatch(category_id))
        and category_id in KNOWN_CATEGORY_SET
    )


def validate_known_category_ids(category_ids: frozenset[str]) -> frozenset[str]:
    if not category_ids:
        raise ValueError("category allowlist must not be empty")
    for category_id in category_ids:
        if not is_known_category_id(category_id):
            raise ValueError("unknown or malformed category identifier")
    return frozenset(category_ids)
