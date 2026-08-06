"""Freshness max-age policies for Market Opportunity Intelligence."""

from __future__ import annotations

from datetime import datetime, timezone

from app.market_opportunity.models import Provenance

# Tick / quote ceilings (V4.1) — fetched_at based. Do NOT use for EIA RWTC.
MAX_AGE_QUOTE_SECONDS = 120
MAX_AGE_QUOTE_UNKNOWN_EVENT_SECONDS = 60
MAX_AGE_INVENTORY_SECONDS = 7 * 24 * 3600
MAX_AGE_MACRO_SECONDS = 7 * 24 * 3600
MAX_AGE_DEFAULT_SECONDS = 3600

# Official daily/weekly series — as-of age via source_event_at (calendar days).
MAX_AGE_EIA_DAILY_SPOT_DAYS = 3
MAX_AGE_FRED_DAILY_CROSSCHECK_DAYS = 4
MAX_AGE_EIA_WEEKLY_INVENTORY_DAYS = 9
MAX_FETCH_AGE_OFFICIAL_SECONDS = 24 * 3600

_OFFICIAL_SERIES_KINDS = frozenset(
    {
        "eia_daily_spot",
        "fred_daily_crosscheck",
        "eia_weekly_inventory",
    }
)


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


def max_asof_calendar_days_for(series_kind: str) -> int | None:
    """Return calendar-day as-of ceiling for official series kinds, else None."""
    kind = (series_kind or "").strip().lower()
    if kind == "eia_daily_spot":
        return MAX_AGE_EIA_DAILY_SPOT_DAYS
    if kind == "fred_daily_crosscheck":
        return MAX_AGE_FRED_DAILY_CROSSCHECK_DAYS
    if kind == "eia_weekly_inventory":
        return MAX_AGE_EIA_WEEKLY_INVENTORY_DAYS
    return None


def _calendar_days_between(earlier: datetime, later: datetime) -> int:
    return (later.astimezone(timezone.utc).date() - earlier.astimezone(timezone.utc).date()).days


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
    clock = clock.astimezone(timezone.utc)
    fetched = provenance.fetched_at.astimezone(timezone.utc)
    fetch_age = (clock - fetched).total_seconds()
    if fetch_age < 0:
        raise FreshnessError("fetched_at is in the future")

    kind = (provenance.series_kind or "").strip().lower()
    if kind in _OFFICIAL_SERIES_KINDS:
        if provenance.source_event_at is None:
            raise FreshnessError(f"{kind} requires source_event_at")
        if provenance.source_event_at.tzinfo is None:
            raise FreshnessError("source_event_at must be timezone-aware")
        if fetch_age > MAX_FETCH_AGE_OFFICIAL_SECONDS:
            raise FreshnessError(
                f"stale fetch for {kind}: age={fetch_age:.0f}s "
                f"max={MAX_FETCH_AGE_OFFICIAL_SECONDS}s"
            )
        asof_days = _calendar_days_between(provenance.source_event_at, clock)
        if asof_days < 0:
            raise FreshnessError("source_event_at is in the future")
        limit_days = max_asof_calendar_days_for(kind)
        assert limit_days is not None
        if asof_days > limit_days:
            raise FreshnessError(
                f"stale {kind}: as_of_age={asof_days}d max={limit_days}d"
            )
        return

    # Tick quote/price and legacy kinds — fetched_at age (unchanged).
    limit = max_age_seconds_for(
        provenance.series_kind,
        source_event_known=provenance.source_event_at is not None,
    )
    if fetch_age > limit:
        raise FreshnessError(
            f"stale {provenance.series_kind}: age={fetch_age:.0f}s max={limit}s"
        )
