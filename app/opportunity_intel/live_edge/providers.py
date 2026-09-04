"""Live-state providers. Missing sources fail closed — never scrape random pages."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol

from app.opportunity_intel.live_edge.matching import normalize_name
from app.opportunity_intel.live_edge.models import (
    DiscoveredMarket,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
)


class LiveStateProvider(Protocol):
    def snapshots_for(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]: ...


class NullLiveStateProvider:
    """Production default when no verified sports/esports feed is configured."""

    def snapshots_for(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        _ = (market, identity, now)
        return ()


class CompositeLiveStateProvider:
    def __init__(self, providers: tuple[LiveStateProvider, ...]) -> None:
        self._providers = providers

    def snapshots_for(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        found: list[LiveStateSnapshot] = []
        for provider in self._providers:
            found.extend(provider.snapshots_for(market, identity, now=now))
        return tuple(found)


class OrderedFallbackProvider:
    """Try providers in order. Conflicting named state is tagged, never merged."""

    def __init__(
        self,
        providers: tuple[LiveStateProvider, ...],
        *,
        categories: tuple[LiveEdgeCategory, ...] | None = None,
    ) -> None:
        self._providers = providers
        self._categories = categories

    def snapshots_for(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        if self._categories is not None and identity.category not in self._categories:
            return ()
        buckets: list[tuple[LiveStateSnapshot, ...]] = []
        for provider in self._providers:
            snaps = provider.snapshots_for(market, identity, now=now)
            if snaps:
                buckets.append(snaps)
        if not buckets:
            return ()
        selected = list(buckets[0])
        for other in buckets[1:]:
            if _has_conflict(tuple(selected), other):
                return tuple(_tag_conflict(item) for item in selected)
            selected = list(_prefer_stronger(tuple(selected), other))
        return tuple(selected)


class FixtureLiveStateProvider:
    """Test-only: inject verified snapshots keyed by market_id or participants."""

    def __init__(self, snapshots: tuple[LiveStateSnapshot, ...] | LiveStateSnapshot) -> None:
        if isinstance(snapshots, LiveStateSnapshot):
            self._snapshots = (snapshots,)
        else:
            self._snapshots = tuple(snapshots)

    def snapshots_for(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        _ = (market, identity, now)
        return self._snapshots


class WeatherEvidenceProvider:
    """Adapts existing Weather Intelligence evidence; does not re-forecast."""

    def __init__(self, snapshots: tuple[LiveStateSnapshot, ...] = ()) -> None:
        self._snapshots = snapshots

    def snapshots_for(
        self,
        market: DiscoveredMarket,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        _ = now
        if identity.category != LiveEdgeCategory.WEATHER:
            return ()
        return self._snapshots


def _prefer_stronger(
    primary: tuple[LiveStateSnapshot, ...], other: tuple[LiveStateSnapshot, ...]
) -> tuple[LiveStateSnapshot, ...]:
    """Keep primary events; replace with a later snapshot only when it adds in-map/round state and does not disagree."""
    upgraded: list[LiveStateSnapshot] = []
    used: set[int] = set()
    for item in primary:
        replacement = None
        for index, candidate in enumerate(other):
            if index in used:
                continue
            if not _same_event(item, candidate):
                continue
            used.add(index)
            if _in_map_strength(candidate) > _in_map_strength(item):
                replacement = candidate
            break
        upgraded.append(replacement or item)
    return tuple(upgraded)


def _in_map_strength(snapshot: LiveStateSnapshot) -> int:
    score = 0
    if snapshot.kills is not None:
        score += 2
    if snapshot.gold_difference is not None:
        score += 2
    if snapshot.towers is not None:
        score += 1
    if snapshot.round_score is not None:
        score += 2
    if snapshot.net_worth_difference is not None:
        score += 2
    return score


def _tag_conflict(snapshot: LiveStateSnapshot) -> LiveStateSnapshot:
    extra = dict(snapshot.extra)
    extra["source_conflict"] = True
    return replace(snapshot, extra=extra)


def _has_conflict(
    left: tuple[LiveStateSnapshot, ...], right: tuple[LiveStateSnapshot, ...]
) -> bool:
    for item in left:
        for other in right:
            if _same_event(item, other) and _state_disagrees(item, other):
                return True
    return False


def _same_event(left: LiveStateSnapshot, right: LiveStateSnapshot) -> bool:
    if len(left.participants) < 2 or len(right.participants) < 2:
        return False
    a = {normalize_name(part) for part in left.participants[:2]}
    b = {normalize_name(part) for part in right.participants[:2]}
    return a == b and bool(a)


def _state_disagrees(left: LiveStateSnapshot, right: LiveStateSnapshot) -> bool:
    right_maps = _aligned_pair(left, right, right.maps_won)
    if left.maps_won is not None and right_maps is not None and left.maps_won != right_maps:
        return True
    if (
        left.current_map is not None
        and right.current_map is not None
        and left.current_map != right.current_map
    ):
        return True
    right_rounds = _aligned_pair(left, right, right.round_score)
    if left.round_score is not None and right_rounds is not None and left.round_score != right_rounds:
        return True
    return False


def _aligned_pair(
    left: LiveStateSnapshot,
    right: LiveStateSnapshot,
    pair: tuple[int, int] | None,
) -> tuple[int, int] | None:
    if pair is None or len(left.participants) < 2 or len(right.participants) < 2:
        return pair
    left0 = normalize_name(left.participants[0])
    right0 = normalize_name(right.participants[0])
    right1 = normalize_name(right.participants[1])
    if left0 == right0:
        return pair
    if left0 == right1:
        return (pair[1], pair[0])
    return pair
