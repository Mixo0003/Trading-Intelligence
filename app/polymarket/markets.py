from __future__ import annotations

import logging
from typing import Callable

from app.polymarket.city import text_matches_priority_city
from app.polymarket.client import PolymarketClient
from app.polymarket.discovery_errors import WeatherDiscoveryError
from app.polymarket.event_expansion import supplement_discovered_markets
from app.polymarket.market_text import build_market_context_text, is_temperature_market_candidate
from app.polymarket.models import PolymarketEvent, PolymarketMarket
from app.polymarket.parser import parse_event, parse_market

logger = logging.getLogger(__name__)


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return int(status) if status is not None else None


def _safe_collect(
    collector_name: str,
    endpoint_type: str,
    fn: Callable[[], list[PolymarketMarket]],
) -> tuple[list[PolymarketMarket], bool]:
    """Run one discovery collector. Failures are non-fatal; returns (markets, ok)."""
    try:
        markets = fn()
        logger.info(
            "Discovery collector ok name=%s endpoint=%s count=%s",
            collector_name,
            endpoint_type,
            len(markets),
        )
        return markets, True
    except Exception as exc:
        logger.warning(
            "Discovery collector failed name=%s endpoint=%s status=%s error=%s",
            collector_name,
            endpoint_type,
            _http_status(exc),
            type(exc).__name__,
        )
        return [], False


def _collect_markets_from_events(
    client: PolymarketClient,
    limit: int,
) -> list[PolymarketMarket]:
    """Fetch active events without unsupported ordering (Gamma rejects volume_24hr)."""
    data = client.get_json(
        "/events",
        params={
            "active": "true",
            "closed": "false",
            "limit": min(limit, 100),
        },
    )
    items = data if isinstance(data, list) else data.get("events", data.get("data", []))
    markets: list[PolymarketMarket] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        event = parse_event(item)
        if not is_temperature_market_candidate_from_event(event):
            continue
        for market in event.markets:
            markets.append(_attach_event_context(market, event))
    return markets


def _collect_markets_from_gamma_discovery(client: PolymarketClient) -> list[PolymarketMarket]:
    """Full worldwide discovery via `/events/keyset` (tag-seeded + closed=false fallback).

    Unlike `_collect_markets_from_events`, this follows every page instead of
    stopping after one fixed-size page, so events beyond the first ~100
    site-wide (all-category) results are still reachable.

    Uses the short-lived discovery cache so USA/Europe/Asia/All share one
    worldwide fetch within the configured TTL.
    """
    from app.config.settings import load_settings
    from app.polymarket.discovery_cache import get_cached_temperature_events

    ttl = 300
    try:
        ttl = load_settings().weather_discovery_cache_ttl_seconds
    except Exception:
        pass

    events, _stats, _hit = get_cached_temperature_events(client, ttl_seconds=ttl)
    markets: list[PolymarketMarket] = []
    for item in events:
        event = parse_event(item)
        for market in event.markets:
            markets.append(_attach_event_context(market, event))
    return markets


def is_temperature_market_candidate_from_event(event: PolymarketEvent) -> bool:
    slug = (event.slug or "").lower()
    text = "\n".join([
        event.title or "",
        event.description or "",
        slug,
    ]).lower()
    if "temperature" in text:
        return True
    return any(
        pattern in slug
        for pattern in (
            "highest-temperature",
            "lowest-temperature",
            "high-temperature",
            "low-temperature",
        )
    )


def _attach_event_context(market: PolymarketMarket, event: PolymarketEvent) -> PolymarketMarket:
    raw = dict(market.raw)
    events = raw.get("events")
    if not isinstance(events, list) or not events:
        raw["events"] = [event.raw]
    return PolymarketMarket(
        id=market.id,
        slug=market.slug,
        question=market.question,
        description=market.description or event.description,
        rules=market.rules or event.raw.get("rules") or "",
        outcomes=market.outcomes,
        outcome_prices=market.outcome_prices,
        volume=market.volume,
        liquidity=market.liquidity,
        end_date=market.end_date,
        raw=raw,
    )


def get_active_markets(
    client: PolymarketClient,
    limit: int = 100,
) -> list[PolymarketMarket]:
    data = client.get_json(
        "/markets",
        params={
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume",
            "ascending": "false",
        },
    )

    items = data if isinstance(data, list) else data.get("markets", [])

    return [
        parse_market(item)
        for item in items
        if isinstance(item, dict)
    ]


def _dedupe_markets(markets: list[PolymarketMarket]) -> list[PolymarketMarket]:
    seen: set[str] = set()
    result: list[PolymarketMarket] = []
    for market in markets:
        key = market.id or market.slug or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(market)
    return result


def _parent_event_key(market: PolymarketMarket) -> str:
    events = market.raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        event = events[0]
        event_id = event.get("id")
        if event_id is not None:
            return f"id:{event_id}"
        event_slug = event.get("slug")
        if event_slug:
            return f"slug:{event_slug}"
    event_id = market.raw.get("eventId") or market.raw.get("event_id")
    if event_id is not None:
        return f"id:{event_id}"
    return f"market:{market.id or market.slug or id(market)}"


def _limit_parent_events(
    markets: list[PolymarketMarket],
    parent_limit: int,
) -> list[PolymarketMarket]:
    """Keep complete child sets for at most parent_limit discovered events."""
    selected_keys: set[str] = set()
    result: list[PolymarketMarket] = []
    for market in markets:
        key = _parent_event_key(market)
        if key not in selected_keys:
            if len(selected_keys) >= parent_limit:
                continue
            selected_keys.add(key)
        result.append(market)
    return result


def _filter_priority_city_markets(
    markets: list[PolymarketMarket],
    priority_cities: list[str],
) -> list[PolymarketMarket]:
    selected: list[PolymarketMarket] = []
    for market in markets:
        if not is_temperature_market_candidate(market):
            continue
        text = build_market_context_text(market)
        slug = market.slug or ""
        if text_matches_priority_city(text, slug, priority_cities):
            selected.append(market)
    return selected


def get_weather_markets(
    client: PolymarketClient,
    limit: int = 100,
    priority_cities: list[str] | None = None,
) -> tuple[list[PolymarketMarket], int, int]:
    """Return child markets, direct-market count, and priority-only additions.

    Individual collectors may fail (e.g. Gamma 422 on unsupported order params).
    The scan aborts only when every path fails and no usable markets remain.
    """
    weather_markets: list[PolymarketMarket] = []
    collector_ok = 0
    collector_fail = 0

    def _absorb(batch: list[PolymarketMarket], ok: bool) -> None:
        nonlocal collector_ok, collector_fail
        if ok:
            collector_ok += 1
        else:
            collector_fail += 1
        for market in batch:
            if is_temperature_market_candidate(market):
                weather_markets.append(market)

    markets_batch, markets_ok = _safe_collect(
        "active_markets",
        "markets",
        lambda: get_active_markets(client, limit=limit),
    )
    _absorb(markets_batch, markets_ok)

    direct_market_count = len(_dedupe_markets(weather_markets))

    events_batch, events_ok = _safe_collect(
        "events_page",
        "events",
        lambda: _collect_markets_from_events(client, limit=limit),
    )
    _absorb(events_batch, events_ok)

    # Full worldwide discovery via /events/keyset (paginated).
    # `client` is only None in tests that stub the legacy collectors above.
    if client is not None:
        keyset_batch, keyset_ok = _safe_collect(
            "events_keyset",
            "events_keyset",
            lambda: _collect_markets_from_gamma_discovery(client),
        )
        _absorb(keyset_batch, keyset_ok)

    merged = _dedupe_markets(weather_markets)

    if not merged and collector_ok == 0 and collector_fail > 0:
        raise WeatherDiscoveryError(
            "Market discovery is temporarily unavailable. Please retry shortly."
        )

    priority_added = 0
    if priority_cities:
        batch, batch_ok = _safe_collect(
            "priority_markets",
            "markets",
            lambda: get_active_markets(client, limit=min(limit * 2, 100)),
        )
        if batch_ok:
            priority_candidates = _filter_priority_city_markets(batch, priority_cities)
            existing_keys = {m.id or m.slug for m in merged}
            for market in priority_candidates:
                key = market.id or market.slug
                if key and key not in existing_keys:
                    merged.append(market)
                    existing_keys.add(key)
                    priority_added += 1

    # `limit` bounds parent events, not child brackets. Slicing the child list
    # previously let five 11-bracket events exhaust a limit of 50 and omit
    # lower-volume USA parent events before regional classification.
    core = _limit_parent_events(merged, limit)
    try:
        final = _dedupe_markets(supplement_discovered_markets(client, core))
    except Exception as exc:
        logger.warning(
            "Discovery supplement failed status=%s error=%s; using core markets",
            _http_status(exc),
            type(exc).__name__,
        )
        final = core
    return final, direct_market_count, priority_added


def get_event_by_slug(
    client: PolymarketClient,
    slug: str,
) -> PolymarketEvent | None:
    data = client.get_json("/events", params={"slug": slug})

    if isinstance(data, list) and data:
        return parse_event(data[0])

    if isinstance(data, dict):
        events = data.get("events") or data.get("data") or []
        if events:
            return parse_event(events[0])
        if data.get("slug"):
            return parse_event(data)

    return None


def get_market_by_slug(
    client: PolymarketClient,
    slug: str,
) -> PolymarketMarket | None:
    data = client.get_json("/markets", params={"slug": slug})

    if isinstance(data, list) and data:
        return parse_market(data[0])

    if isinstance(data, dict):
        markets = data.get("markets") or data.get("data") or []
        if markets:
            return parse_market(markets[0])
        if data.get("slug"):
            return parse_market(data)

    return None
