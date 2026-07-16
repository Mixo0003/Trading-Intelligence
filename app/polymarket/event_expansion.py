"""Expand discovered markets to full sibling event sets for canonical analysis."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from app.polymarket.client import PolymarketClient
from app.polymarket.models import PolymarketEvent, PolymarketMarket

logger = logging.getLogger(__name__)

_EVENT_CHILD_SLUG_RE = re.compile(
    r"^((?:lowest|highest)-temperature-in-[a-z0-9-]+-on-[a-z0-9-]+(?:-\d{4})?)"
)


def paired_temperature_event_slug(slug: str | None) -> str | None:
    if not slug:
        return None
    if "lowest-temperature" in slug:
        return slug.replace("lowest-temperature", "highest-temperature", 1)
    if "highest-temperature" in slug:
        return slug.replace("highest-temperature", "lowest-temperature", 1)
    return None


def _event_slug_from_market_slug(slug: str | None) -> str | None:
    if not slug:
        return None
    match = _EVENT_CHILD_SLUG_RE.match(slug.lower())
    if match:
        return match.group(1)
    return None


def _event_slug(market: PolymarketMarket) -> str | None:
    events = market.raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        slug = events[0].get("slug")
        if slug:
            return str(slug)
    return _event_slug_from_market_slug(market.slug)


def _event_id(market: PolymarketMarket) -> str | None:
    events = market.raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = events[0].get("id")
        if event_id is not None:
            return str(event_id)
    return None


def _collect_slugs(markets: list[PolymarketMarket], *, include_paired: bool = False) -> set[str]:
    slugs: set[str] = set()
    for market in markets:
        slug = _event_slug(market)
        if slug:
            slugs.add(slug)
            if include_paired:
                paired = paired_temperature_event_slug(slug)
                if paired:
                    slugs.add(paired)
    return slugs


def _event_is_fresh_and_open(event: PolymarketEvent) -> bool:
    """Reject events that are explicitly closed/inactive or whose window has passed.

    Used as a fallback validation when a re-fetched event cannot be confirmed
    to be the same event we originally discovered (see `fetch_event_children`).
    """
    if event.raw.get("closed") is True:
        return False
    if event.raw.get("active") is False:
        return False

    end_date = event.raw.get("endDate")
    if end_date:
        try:
            parsed = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
            if parsed.date() < datetime.now(timezone.utc).date():
                return False
        except ValueError:
            pass
    return True


def fetch_event_children(
    client: PolymarketClient,
    slug: str,
    *,
    known_event_ids: set[str] | None = None,
) -> list[PolymarketMarket]:
    """Fetch sibling markets for an event slug, guarding against stale substitution.

    `known_event_ids`, when provided, is the set of event ids we already
    discovered and eligibility-checked for this slug. The re-fetched event is
    only accepted if its id is one of those (the normal, expected case). If we
    have no such known ids to compare against, the re-fetched event must at
    least be open/active and not past its resolution window before we trust it.
    This closes the gap where Polymarket returning a different (closed,
    out-of-horizon) event for the same slug could silently replace correct,
    already-validated markets with stale ones.
    """
    from app.polymarket.markets import _attach_event_context, get_event_by_slug

    event = get_event_by_slug(client, slug)
    if event is None or not event.markets:
        return []

    fetched_id = str(event.raw.get("id")) if event.raw.get("id") is not None else None

    if known_event_ids:
        if fetched_id not in known_event_ids:
            logger.warning(
                "Rejected stale/mismatched event re-fetch: slug=%s fetched_id=%s "
                "fetched_title=%r known_ids=%s",
                slug,
                fetched_id,
                event.title,
                sorted(known_event_ids),
            )
            return []
    elif not _event_is_fresh_and_open(event):
        logger.warning(
            "Rejected stale/closed event re-fetch: slug=%s fetched_id=%s fetched_title=%r "
            "closed=%s active=%s endDate=%s",
            slug,
            fetched_id,
            event.title,
            event.raw.get("closed"),
            event.raw.get("active"),
            event.raw.get("endDate"),
        )
        return []

    return [_attach_event_context(market, event) for market in event.markets]


def expand_group_children(
    client: PolymarketClient,
    children: list[PolymarketMarket],
) -> list[PolymarketMarket]:
    """Return all sibling markets for the group's event slug(s), if available.

    Falls back to the original, already-eligibility-checked `children` whenever
    the re-fetch cannot be confirmed to be the same event (see
    `fetch_event_children`), rather than silently substituting stale data.
    """
    if not children:
        return children

    slugs = _collect_slugs(children, include_paired=False)
    if not slugs:
        return children

    known_event_ids = {eid for eid in (_event_id(market) for market in children) if eid}
    cache_key = f"{','.join(sorted(slugs))}|{','.join(sorted(known_event_ids))}"

    from app.signals.scan_runtime import active_scan_state

    state = active_scan_state()
    if state is not None and cache_key in state.expansion_cache:
        return list(state.expansion_cache[cache_key])

    expanded: list[PolymarketMarket] = []
    seen: set[str] = set()
    for slug in sorted(slugs):
        for market in fetch_event_children(client, slug, known_event_ids=known_event_ids or None):
            key = market.id or market.slug or ""
            if not key or key in seen:
                continue
            seen.add(key)
            expanded.append(market)

    result = expanded or children
    if state is not None:
        state.expansion_cache[cache_key] = list(result)
    return result


def supplement_discovered_markets(
    client: PolymarketClient | None,
    markets: list[PolymarketMarket],
) -> list[PolymarketMarket]:
    """Add missing sibling and paired high/low event markets to discovery output."""
    if client is None:
        return markets
    slugs = _collect_slugs(markets, include_paired=True)
    if not slugs:
        return markets

    result = list(markets)
    seen = {m.id or m.slug for m in markets if m.id or m.slug}
    for slug in sorted(slugs):
        for market in fetch_event_children(client, slug):
            key = market.id or market.slug
            if key and key not in seen:
                result.append(market)
                seen.add(key)
    return result
