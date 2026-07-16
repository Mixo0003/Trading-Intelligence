"""Worldwide temperature-event discovery via the official Gamma `/events/keyset` API.

Replaces single fixed-size-page discovery with full cursor pagination so that
every currently active daily-temperature event is reachable, not just the
first ~100 events returned by volume-sorted `/events` or `/markets`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.metrics.scan_metrics import active_scan_metrics
from app.polymarket.client import PolymarketClient
from app.polymarket.tags import resolve_weather_tag

logger = logging.getLogger(__name__)

_KEYSET_PAGE_LIMIT = 100
_KEYSET_MAX_PAGES = 60  # Safety net only, never a substitute for cursor-driven stopping.

_REJECT_KEYWORDS = (
    "precipitation",
    "rainfall",
    "rain accumulation",
    "snowfall",
    "snow accumulation",
    "hurricane",
    "tropical storm",
    "named storm",
    "typhoon",
    "cyclone",
    "earthquake",
    "volcano",
    "volcanic",
    "wildfire",
    "drought",
    "hottest year",
    "hottest month",
    "on record",
    "climate policy",
    "paris agreement",
    "emissions",
    "el nino",
    "la nina",
    "sea ice",
    "temperature increase",
    "global temperature",
    "world cup",
    "olympic",
    "election",
)

_ACCEPT_TITLE_RE = re.compile(
    r"\b(highest|lowest|maximum|minimum|max|min)\s+temperature\b|\bdaily\s+(high|low)\b",
    re.IGNORECASE,
)

_ACCEPT_SLUG_RE = re.compile(
    r"^(?:lowest|highest|high|low|maximum|minimum)-temperature-in-[a-z0-9-]+-on-[a-z0-9-]+",
)


@dataclass
class DiscoveryStats:
    tag_id: str | None = None
    tag_slug: str | None = None
    pages_fetched: int = 0
    active_events_inspected: int = 0
    temperature_parents_retained: int = 0
    events_per_page: list[int] = field(default_factory=list)


def fetch_events_keyset(
    client: PolymarketClient,
    *,
    closed: bool = False,
    tag_id: str | int | None = None,
    order: str = "volume24hr",
    ascending: bool = False,
    limit: int = _KEYSET_PAGE_LIMIT,
    max_pages: int = _KEYSET_MAX_PAGES,
) -> tuple[list[dict], int]:
    """Fetch every page of `/events/keyset` until exhausted.

    Stops when `next_cursor` is absent/empty, a page returns no events, a
    repeated cursor is detected (guards against a buggy/looping API), or the
    `max_pages` hard cap is reached (safety net, not a normal stopping path).
    Never sends `offset` and never assumes a single fixed-size page covers
    all results.
    """
    events_out: list[dict] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages_fetched = 0

    for _ in range(max_pages):
        params: dict[str, Any] = {
            "closed": "true" if closed else "false",
            "limit": limit,
            "order": order,
            "ascending": "true" if ascending else "false",
        }
        if tag_id is not None:
            params["tag_id"] = tag_id
        if cursor:
            params["after_cursor"] = cursor

        data = client.get_json("/events/keyset", params=params)
        pages_fetched += 1

        events = data.get("events") if isinstance(data, dict) else None
        events = [e for e in events if isinstance(e, dict)] if events else []
        logger.info(
            "Gamma /events/keyset page %s: %s events (tag_id=%s closed=%s)",
            pages_fetched,
            len(events),
            tag_id,
            closed,
        )
        events_out.extend(events)

        if not events:
            break

        next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    logger.info(
        "Gamma /events/keyset discovery pass complete: pages=%s events=%s tag_id=%s closed=%s",
        pages_fetched,
        len(events_out),
        tag_id,
        closed,
    )
    return events_out, pages_fetched


def _is_stale_or_closed(event: dict) -> bool:
    if event.get("closed") is True:
        return True
    if event.get("active") is False:
        return True
    end_date = event.get("endDate")
    if end_date:
        try:
            parsed = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed.date() < datetime.now(timezone.utc).date():
                return True
        except ValueError:
            pass
    return False


def is_daily_temperature_event(event: dict) -> bool:
    """Classify a raw Gamma event dict as a genuine daily high/low temperature event.

    Uses title, slug, description, tags, and child-market structure. Rejects
    precipitation, hurricanes, climate-policy, generic-weather, sports, and
    other unrelated events even when they mention "temperature" in passing.
    """
    title = str(event.get("title") or "")
    slug = str(event.get("slug") or "").lower()
    description = str(event.get("description") or "")

    tags = event.get("tags")
    tag_text = ""
    if isinstance(tags, list):
        tag_text = " ".join(
            f"{t.get('label') or ''} {t.get('slug') or ''}"
            for t in tags
            if isinstance(t, dict)
        )

    combined_lower = " ".join([title, description, tag_text]).lower()
    if any(keyword in combined_lower for keyword in _REJECT_KEYWORDS):
        return False

    if _ACCEPT_SLUG_RE.match(slug):
        return True
    if _ACCEPT_TITLE_RE.search(title):
        return True

    markets = event.get("markets")
    if isinstance(markets, list):
        for market in markets:
            if not isinstance(market, dict):
                continue
            market_slug = str(market.get("slug") or "").lower()
            market_question = str(market.get("question") or "")
            if _ACCEPT_SLUG_RE.match(market_slug) or _ACCEPT_TITLE_RE.search(market_question):
                return True

    return False


def discover_temperature_events(client: PolymarketClient) -> tuple[list[dict], DiscoveryStats]:
    """Discover every active, non-closed daily-temperature event worldwide.

    Runs a tag-seeded pass (diagnostics only, never the sole source) plus a
    full `closed=false` keyset fallback pass, classifies candidates by
    content, deduplicates by event id, and rejects closed/inactive/past/
    non-temperature events. Logs resolved tag + page/event counts internally;
    nothing here is Telegram-facing.
    """
    stats = DiscoveryStats()

    tag = resolve_weather_tag(client)
    if tag is not None:
        stats.tag_id = tag.id
        stats.tag_slug = tag.slug

    seed_events: list[dict] = []
    if tag is not None:
        seed_events, seed_pages = fetch_events_keyset(client, closed=False, tag_id=tag.id)
        stats.pages_fetched += seed_pages

    fallback_events, fallback_pages = fetch_events_keyset(client, closed=False, tag_id=None)
    stats.pages_fetched += fallback_pages

    combined: dict[str, dict] = {}
    for event in seed_events + fallback_events:
        event_id = event.get("id")
        if event_id is None:
            continue
        combined.setdefault(str(event_id), event)

    stats.active_events_inspected = len(combined)

    accepted: dict[str, dict] = {}
    for event_id, event in combined.items():
        if _is_stale_or_closed(event):
            continue
        if not is_daily_temperature_event(event):
            continue
        accepted[event_id] = event

    stats.temperature_parents_retained = len(accepted)

    logger.info(
        "Gamma discovery summary: tag=%s/%s pages_fetched=%s active_events_inspected=%s "
        "temperature_parents_retained=%s",
        stats.tag_slug,
        stats.tag_id,
        stats.pages_fetched,
        stats.active_events_inspected,
        stats.temperature_parents_retained,
    )

    metrics = active_scan_metrics()
    if metrics is not None:
        metrics.gamma_tag_id = stats.tag_id
        metrics.gamma_tag_slug = stats.tag_slug
        metrics.gamma_pages_fetched = stats.pages_fetched
        metrics.gamma_active_events_inspected = stats.active_events_inspected
        metrics.gamma_temperature_parents_retained = stats.temperature_parents_retained

    return list(accepted.values()), stats
