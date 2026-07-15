"""Expand discovered markets to full sibling event sets for canonical analysis."""

from __future__ import annotations

import re

from app.polymarket.client import PolymarketClient
from app.polymarket.models import PolymarketMarket

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


def fetch_event_children(
    client: PolymarketClient,
    slug: str,
) -> list[PolymarketMarket]:
    from app.polymarket.markets import _attach_event_context, get_event_by_slug

    event = get_event_by_slug(client, slug)
    if event is None or not event.markets:
        return []
    return [_attach_event_context(market, event) for market in event.markets]


def expand_group_children(
    client: PolymarketClient,
    children: list[PolymarketMarket],
) -> list[PolymarketMarket]:
    """Return all sibling markets for the group's event slug(s), if available."""
    if not children:
        return children

    slugs = _collect_slugs(children, include_paired=False)
    if not slugs:
        return children

    expanded: list[PolymarketMarket] = []
    seen: set[str] = set()
    for slug in sorted(slugs):
        for market in fetch_event_children(client, slug):
            key = market.id or market.slug or ""
            if not key or key in seen:
                continue
            seen.add(key)
            expanded.append(market)
    return expanded or children


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
