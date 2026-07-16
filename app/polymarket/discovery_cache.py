"""Short-lived Gamma temperature-event discovery cache shared across regions."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from app.polymarket.client import PolymarketClient
from app.polymarket.gamma_discovery import DiscoveryStats, discover_temperature_events

logger = logging.getLogger(__name__)

_async_lock = asyncio.Lock()
_thread_lock = threading.RLock()

_cached_events: list[dict[str, Any]] | None = None
_cached_stats: DiscoveryStats | None = None
_cached_at_monotonic: float | None = None
_cached_day: str | None = None


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _is_stale_or_closed(event: dict[str, Any]) -> bool:
    if event.get("closed") is True:
        return True
    if event.get("active") is False:
        return True
    end_date = event.get("endDate") or event.get("end_date")
    if not end_date:
        return False
    try:
        parsed = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        return parsed.date() < datetime.now(timezone.utc).date()
    except ValueError:
        return False


def _filter_fresh(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if not _is_stale_or_closed(event)]


def clear_discovery_cache() -> None:
    global _cached_events, _cached_stats, _cached_at_monotonic, _cached_day
    with _thread_lock:
        _cached_events = None
        _cached_stats = None
        _cached_at_monotonic = None
        _cached_day = None


def _cache_is_valid(ttl_seconds: int) -> bool:
    if _cached_events is None or _cached_at_monotonic is None or _cached_day is None:
        return False
    if _cached_day != _utc_day():
        return False
    if (time.monotonic() - _cached_at_monotonic) > max(0, ttl_seconds):
        return False
    return True


def get_cached_temperature_events(
    client: PolymarketClient,
    *,
    ttl_seconds: int = 300,
) -> tuple[list[dict[str, Any]], DiscoveryStats, bool]:
    """Return fresh worldwide discovery events, reusing cache within TTL.

    Returns ``(events, stats, cache_hit)``. Never returns closed/inactive/past
    events even if they were present when the cache was filled.
    """
    global _cached_events, _cached_stats, _cached_at_monotonic, _cached_day

    with _thread_lock:
        if _cache_is_valid(ttl_seconds):
            fresh = _filter_fresh(list(_cached_events or []))
            if len(fresh) != len(_cached_events or []):
                _cached_events = fresh
            stats = _cached_stats or DiscoveryStats()
            logger.info(
                "Discovery cache hit: retained=%s ttl=%ss",
                len(fresh),
                ttl_seconds,
            )
            return fresh, stats, True

        events, stats = discover_temperature_events(client)
        fresh = _filter_fresh(events)
        _cached_events = fresh
        _cached_stats = stats
        _cached_at_monotonic = time.monotonic()
        _cached_day = _utc_day()
        logger.info(
            "Discovery cache miss/store: retained=%s ttl=%ss",
            len(fresh),
            ttl_seconds,
        )
        return list(fresh), stats, False


async def get_cached_temperature_events_async(
    client: PolymarketClient,
    *,
    ttl_seconds: int = 300,
) -> tuple[list[dict[str, Any]], DiscoveryStats, bool]:
    """Async-safe wrapper that serializes cache fills with an asyncio lock."""
    async with _async_lock:
        return await asyncio.to_thread(
            get_cached_temperature_events,
            client,
            ttl_seconds=ttl_seconds,
        )
