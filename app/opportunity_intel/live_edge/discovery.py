"""Independent Live Edge discovery — all active Polymarket markets, no sports-only cap."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.opportunity_intel.discovery import (
    DISCOVERY_PAGE_LIMIT,
    GammaDiscoveryClient,
    ResultStatus,
)
from app.opportunity_intel.live_edge.classify import (
    ACTIONABLE_LIVE_CATEGORIES,
    classify_market,
    event_date_from_text,
    scan_priority,
)
from app.opportunity_intel.live_edge.matching import (
    identity_hits_live_index,
    live_name_index,
    match_event,
)
from app.opportunity_intel.live_edge.models import (
    DiscoveredMarket,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
    MarketKind,
    MatchStatus,
)

LIVE_EDGE_MAX_PAGES = 100
_CURSOR_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,512}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class DiscoveryPageClient(Protocol):
    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any: ...


@dataclass(frozen=True)
class LiveEdgeDiscoveryResult:
    status: ResultStatus
    markets: tuple[DiscoveredMarket, ...]
    pages_fetched: int
    truncated: bool
    category_counts: dict[str, int]
    sports_only: bool = False


def discover_active_markets(
    client: DiscoveryPageClient | GammaDiscoveryClient,
    *,
    now: datetime | None = None,
    limit: int = DISCOVERY_PAGE_LIMIT,
    max_pages: int = LIVE_EDGE_MAX_PAGES,
) -> LiveEdgeDiscoveryResult:
    """Paginate Gamma active events without sports/volume/time shortlist filters."""
    _ = now
    if not 1 <= limit <= DISCOVERY_PAGE_LIMIT:
        raise ValueError("limit out of bounds")
    raw_events: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages = 0
    terminated = False
    for _ in range(max_pages):
        params: dict[str, Any] = {"closed": "false", "limit": limit}
        if cursor is not None:
            params["after_cursor"] = cursor
        payload = client.get_json("/events/keyset", params=params)
        pages += 1
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            return LiveEdgeDiscoveryResult(
                status=ResultStatus.PERMANENT_FAILURE,
                markets=(),
                pages_fetched=pages,
                truncated=False,
                category_counts={},
            )
        events = payload["events"]
        if any(not isinstance(item, dict) for item in events):
            return LiveEdgeDiscoveryResult(
                status=ResultStatus.PERMANENT_FAILURE,
                markets=(),
                pages_fetched=pages,
                truncated=False,
                category_counts={},
            )
        raw_events.extend(events)
        if not events:
            terminated = True
            break
        next_cursor = payload.get("next_cursor")
        if next_cursor is None:
            terminated = True
            break
        if not isinstance(next_cursor, str) or not _CURSOR_RE.fullmatch(next_cursor):
            return LiveEdgeDiscoveryResult(
                status=ResultStatus.PERMANENT_FAILURE,
                markets=(),
                pages_fetched=pages,
                truncated=False,
                category_counts={},
            )
        if next_cursor in seen_cursors:
            return LiveEdgeDiscoveryResult(
                status=ResultStatus.PERMANENT_FAILURE,
                markets=(),
                pages_fetched=pages,
                truncated=False,
                category_counts={},
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    markets: list[DiscoveredMarket] = []
    for raw_event in raw_events:
        markets.extend(_markets_from_event(raw_event))
    counts: dict[str, int] = {}
    for market in markets:
        identity = classify_market(market)
        counts[identity.category.value] = counts.get(identity.category.value, 0) + 1
    sports_categories = {
        LiveEdgeCategory.ESPORTS_LOL.value,
        LiveEdgeCategory.ESPORTS_CS2.value,
        LiveEdgeCategory.ESPORTS_DOTA.value,
        LiveEdgeCategory.TENNIS.value,
        LiveEdgeCategory.BASKETBALL.value,
        LiveEdgeCategory.BASEBALL.value,
        LiveEdgeCategory.SOCCER.value,
    }
    only_sports = bool(markets) and set(counts) <= sports_categories
    return LiveEdgeDiscoveryResult(
        status=ResultStatus.COMPLETE if terminated else ResultStatus.COMPLETE,
        markets=tuple(markets),
        pages_fetched=pages,
        truncated=not terminated,
        category_counts=counts,
        sports_only=only_sports,
    )


def prioritize_markets(
    markets: tuple[DiscoveredMarket, ...],
    *,
    now: datetime | None = None,
) -> tuple[DiscoveredMarket, ...]:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    decorated = []
    for market in markets:
        identity = classify_market(market)
        decorated.append(
            (
                scan_priority(identity, market, now=clock),
                market.end_at or datetime.max.replace(tzinfo=timezone.utc),
                market,
            )
        )
    decorated.sort(key=lambda item: (item[0], item[1], item[2].market_id))
    return tuple(item[2] for item in decorated)


# Reserved slots so politics/general near-res cannot fill the evaluation cap.
EVAL_BUDGET_LIVE_MAP = 40
EVAL_BUDGET_LIVE_SERIES = 30
EVAL_BUDGET_NEAR_RESOLUTION = 25
EVAL_BUDGET_LIVE_SOURCE = 20
EVAL_BUDGET_WATCH_CANDIDATE = 20
EVAL_BUDGET_OTHER_LIVE = 5
EVAL_QUEUE_TOTAL_CAP = (
    EVAL_BUDGET_LIVE_MAP
    + EVAL_BUDGET_LIVE_SERIES
    + EVAL_BUDGET_NEAR_RESOLUTION
    + EVAL_BUDGET_LIVE_SOURCE
    + EVAL_BUDGET_WATCH_CANDIDATE
    + EVAL_BUDGET_OTHER_LIVE
)
_PRIORITY_BUDGETS: dict[int, int] = {
    1: EVAL_BUDGET_LIVE_MAP,
    2: EVAL_BUDGET_LIVE_SERIES,
    3: EVAL_BUDGET_NEAR_RESOLUTION,
    4: EVAL_BUDGET_LIVE_SOURCE,
    5: EVAL_BUDGET_WATCH_CANDIDATE,
    6: EVAL_BUDGET_OTHER_LIVE,
    7: 0,
    8: 0,
    9: 0,
}


def harvest_live_snapshots(provider, *, now: datetime) -> tuple[LiveStateSnapshot, ...]:
    """One snapshot harvest per category so live events can drive evaluation order."""
    dummy = DiscoveredMarket(
        event_id="harvest",
        market_id="harvest",
        condition_id="harvest",
        event_slug="harvest",
        question="Live vs Index",
        category_ids=(),
        tags=(),
        live=True,
        liquidity_usd=Decimal("0"),
        volume_24h_usd=Decimal("0"),
        end_at=now,
        outcomes=(("Yes", "harvest-yes"), ("No", "harvest-no")),
        price_hints=(None, None),
        accepting_orders=False,
        order_book_enabled=False,
        event_title="Live vs Index",
        start_at=now,
    )
    found: list[LiveStateSnapshot] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for category in ACTIONABLE_LIVE_CATEGORIES:
        identity = MarketIdentity(
            kind=MarketKind.SERIES_WINNER,
            category=category,
            participants=("Live", "Index"),
        )
        for snap in provider.snapshots_for(dummy, identity, now=now) or ():
            provider_name = snap.provenance.provider if snap.provenance else ""
            event_id = snap.provenance.event_identifier if snap.provenance else ""
            key = (provider_name, event_id, snap.participants)
            if key in seen:
                continue
            seen.add(key)
            found.append(snap)
    return tuple(found)


def select_evaluation_queue(
    markets: tuple[DiscoveredMarket, ...],
    *,
    now: datetime | None = None,
    total_cap: int | None = None,
    live_snapshots: tuple[LiveStateSnapshot, ...] = (),
) -> tuple[DiscoveredMarket, ...]:
    """Category/priority queues. Live provider matches outrank unmatched sports."""
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ordered = prioritize_markets(markets, now=clock)
    buckets: dict[int, list[DiscoveredMarket]] = {index: [] for index in range(1, 10)}
    matched_maps: list[DiscoveredMarket] = []
    matched_live: list[DiscoveredMarket] = []
    name_index = live_name_index(live_snapshots) if live_snapshots else {}
    for market in ordered:
        identity = classify_market(market)
        priority = min(max(scan_priority(identity, market, now=clock), 1), 9)
        if (
            live_snapshots
            and len(identity.participants) >= 1
            and identity_hits_live_index(identity, name_index)
        ):
            match = match_event(market, identity, live_snapshots, now=clock)
            if match.status in {MatchStatus.VERIFIED_MATCH, MatchStatus.PROBABLE_MATCH}:
                if _title_date_elapsed(market, clock):
                    buckets[9].append(market)
                    continue
                if identity.kind in {MarketKind.MAP_WINNER, MarketKind.GAME_WINNER}:
                    matched_maps.append(market)
                    continue
                if identity.kind == MarketKind.SERIES_WINNER:
                    matched_live.append(market)
                    continue
        buckets[priority].append(market)
    selected: list[DiscoveredMarket] = []
    taken_maps = matched_maps[:EVAL_BUDGET_LIVE_MAP]
    taken_live = matched_live[:EVAL_BUDGET_LIVE_SERIES]
    selected.extend(taken_maps)
    selected.extend(taken_live)
    leftover = 0
    for priority in range(1, 10):
        if priority >= 8:
            leftover = 0
            budget = 0
        elif priority == 1:
            budget = max(0, EVAL_BUDGET_LIVE_MAP - len(taken_maps)) + leftover
        elif priority == 2:
            budget = max(0, EVAL_BUDGET_LIVE_SERIES - len(taken_live)) + leftover
        else:
            budget = _PRIORITY_BUDGETS.get(priority, 0) + leftover
        taken = buckets[priority][:budget]
        selected.extend(taken)
        leftover = max(0, budget - len(taken))
    cap = EVAL_QUEUE_TOTAL_CAP if total_cap is None else total_cap
    return tuple(selected[:cap])


def _title_date_elapsed(market: DiscoveredMarket, clock: datetime) -> bool:
    stamped = event_date_from_text(market.question, market.event_title, market.description)
    if stamped is None:
        return False
    return stamped.date() < clock.astimezone(timezone.utc).date()


def discovery_sees_non_sports(result: LiveEdgeDiscoveryResult) -> bool:
    return not result.sports_only and len(result.category_counts) > 1


def _markets_from_event(raw_event: dict[str, Any]) -> list[DiscoveredMarket]:
    if raw_event.get("closed") is True or raw_event.get("archived") is True:
        return []
    if raw_event.get("active") is False:
        return []
    event_id = _ident(raw_event.get("id"))
    slug = raw_event.get("slug")
    if event_id is None or not isinstance(slug, str) or not slug:
        return []
    category_ids = _category_ids(raw_event.get("tags")) or ()
    tags = _tag_labels(raw_event.get("tags"))
    live_flag = _live_flag(raw_event, None)
    event_title = raw_event.get("title") if isinstance(raw_event.get("title"), str) else None
    start_at = _dt(raw_event.get("startDate") or raw_event.get("start_date"))
    raw_markets = raw_event.get("markets")
    if not isinstance(raw_markets, list):
        return []
    found: list[DiscoveredMarket] = []
    for raw_market in raw_markets:
        if not isinstance(raw_market, dict):
            continue
        if raw_market.get("closed") is True or raw_market.get("resolved") is True:
            continue
        if raw_market.get("active") is False:
            continue
        market_id = _ident(raw_market.get("id"))
        condition_id = _ident(raw_market.get("conditionId"))
        question = raw_market.get("question")
        if market_id is None or condition_id is None:
            continue
        if not isinstance(question, str) or not question.strip():
            continue
        outcomes = _json_list(raw_market.get("outcomes"))
        tokens = _json_list(raw_market.get("clobTokenIds"))
        if not outcomes or not tokens or len(outcomes) != len(tokens):
            continue
        pairs: list[tuple[str, str]] = []
        ok = True
        for name, token in zip(outcomes, tokens, strict=True):
            token_id = _ident(token)
            if not isinstance(name, str) or not name.strip() or token_id is None:
                ok = False
                break
            pairs.append((name.strip(), token_id))
        if not ok or len(pairs) < 2:
            continue
        prices = _json_list(raw_market.get("outcomePrices"))
        hints: list[Decimal | None] = []
        for index, _pair in enumerate(pairs):
            hint = None
            if prices is not None and index < len(prices):
                hint = _decimal_prob(prices[index])
            hints.append(hint)
        market_slug_raw = raw_market.get("slug")
        market_slug = (
            market_slug_raw.strip()
            if isinstance(market_slug_raw, str) and market_slug_raw.strip()
            else None
        )
        found.append(
            DiscoveredMarket(
                event_id=event_id,
                market_id=market_id,
                condition_id=condition_id,
                event_slug=slug,
                question=question.strip(),
                category_ids=category_ids,
                tags=tags,
                live=_live_flag(raw_event, raw_market) if live_flag is not None else _live_flag(raw_event, raw_market),
                liquidity_usd=_decimal_nonneg(raw_market.get("liquidity")) or Decimal("0"),
                volume_24h_usd=_decimal_nonneg(
                    raw_market.get("volume24hr") or raw_market.get("volume")
                )
                or Decimal("0"),
                end_at=_dt(raw_market.get("endDate") or raw_event.get("endDate")),
                outcomes=tuple(pairs),
                price_hints=tuple(hints),
                accepting_orders=raw_market.get("acceptingOrders") is True,
                order_book_enabled=raw_market.get("enableOrderBook") is True,
                event_title=event_title,
                start_at=start_at,
                description=_description(raw_event, raw_market),
                market_slug=market_slug,
            )
        )
    return found


def _live_flag(event: dict[str, Any], market: dict[str, Any] | None) -> bool | None:
    flags = []
    for source in (market or {}, event):
        for key in ("live", "isLive"):
            value = source.get(key)
            if isinstance(value, bool):
                flags.append(value)
    if not flags:
        return None
    return any(flags)


def _ident(value: Any) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value)
    return text if _IDENTIFIER_RE.fullmatch(text) else None


def _category_ids(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    found: list[str] = []
    for tag in value:
        if not isinstance(tag, dict):
            continue
        candidate = tag.get("slug") or tag.get("id")
        if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
            text = str(candidate)
            if _CATEGORY_RE.fullmatch(text):
                found.append(text)
    return tuple(sorted(set(found))) if found else None


def _tag_labels(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    labels: list[str] = []
    for tag in value:
        if not isinstance(tag, dict):
            continue
        label = tag.get("label") or tag.get("slug")
        if isinstance(label, str) and label.strip():
            labels.append(label.strip())
    return tuple(labels)


def _json_list(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _decimal_nonneg(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _decimal_prob(value: Any) -> Decimal | None:
    parsed = _decimal_nonneg(value)
    if parsed is None or parsed > 1:
        return None
    return parsed


def _description(event: dict[str, Any], market: dict[str, Any]) -> str | None:
    for source in (market, event):
        for key in ("description", "resolutionSource", "rules"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:4000]
    return None


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
