"""Freshness max-age policies for Market Opportunity Intelligence."""

from __future__ import annotations

from datetime import datetime, timezone

from app.market_opportunity.models import Provenance

# Conservative defaults for V4.1 foundation (no live feeds yet).
MAX_AGE_QUOTE_SECONDS = 120
MAX_AGE_QUOTE_UNKNOWN_EVENT_SECONDS = 60
MAX_AGE_INVENTORY_SECONDS = 7 * 24 * 3600
MAX_AGE_MACRO_SECONDS = 7 * 24 * 3600
MAX_AGE_DEFAULT_SECONDS = 3600


class FreshnessError(ValueError):
    """Stale or unverifiable provenance."""


def max_age_seconds_for(series_kind: str, *, source_event_known: bool) -> int:
    kind = (series_kind or "").strip().lower()
    if kind in {"quote", "price", "mid", "last", "bid", "ask"}:
        return (
            MAX_AGE_QUOTE_SECONDS
            if source_event_known
            else MAX_AGE_QUOTE_UNKNOWN_EVENT_SECONDS
        )
    if kind in {"inventory", "storage", "stocks"}:
        return MAX_AGE_INVENTORY_SECONDS
    if kind in {"macro", "rates", "calendar"}:
        return MAX_AGE_MACRO_SECONDS
    return MAX_AGE_DEFAULT_SECONDS


def assert_fresh(
    provenance: Provenance,
    *,
    now: datetime | None = None,
) -> None:
    if provenance.fetched_at.tzinfo is None:
        raise FreshnessError("fetched_at must be timezone-aware")
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise FreshnessError("now must be timezone-aware")
    age = (clock.astimezone(timezone.utc) - provenance.fetched_at.astimezone(timezone.utc)).total_seconds()
    if age < 0:
        raise FreshnessError("fetched_at is in the future")
    limit = max_age_seconds_for(
        provenance.series_kind,
        source_event_known=provenance.source_event_at is not None,
    )
    if age > limit:
        raise FreshnessError(
            f"stale {provenance.series_kind}: age={age:.0f}s max={limit}s"
        )
