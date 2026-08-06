"""Paper outcome measurement for V4.2 WTI opportunities."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from app.config.settings import Settings
from app.market_opportunity.models import (
    OpportunityOutcome,
    OpportunitySide,
    OpportunityStatus,
)
from app.market_opportunity.sources.eia import DailyBar

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MeasurableOpportunity:
    """Minimal opportunity fields required for measurement."""

    opportunity_uid: str
    side: OpportunitySide
    entry_zone_low: Decimal
    entry_zone_high: Decimal
    targets: tuple[Decimal, ...]
    invalidation: Decimal
    signal_period: date
    status: str = OpportunityStatus.PUBLISHED.value


def zone_touched(bar: DailyBar, low: Decimal, high: Decimal) -> bool:
    """True when bar range overlaps [low, high] (RWTC: high=low=close=value)."""
    return bar.low <= high and bar.high >= low


def level_touched_long(bar: DailyBar, level: Decimal) -> bool:
    return bar.high >= level


def level_touched_short(bar: DailyBar, level: Decimal) -> bool:
    return bar.low <= level


def level_touched(bar: DailyBar, *, side: OpportunitySide, level: Decimal) -> bool:
    if side is OpportunitySide.LONG:
        return level_touched_long(bar, level)
    if side is OpportunitySide.SHORT:
        return level_touched_short(bar, level)
    return False


def invalidation_touched(
    bar: DailyBar, *, side: OpportunitySide, invalidation: Decimal
) -> bool:
    if side is OpportunitySide.LONG:
        return bar.low <= invalidation
    if side is OpportunitySide.SHORT:
        return bar.high >= invalidation
    return False


def _bars_after(
    bars: Sequence[DailyBar],
    *,
    on_or_after: date,
) -> list[DailyBar]:
    return [b for b in bars if b.period >= on_or_after]


def measure_opportunity(
    opportunity: MeasurableOpportunity,
    bars: Sequence[DailyBar],
    *,
    measured_at: datetime | None = None,
) -> OpportunityOutcome:
    """Measure paper outcome after new daily bars.

    - Entry: zone touch (low≤entry_high and high≥entry_low)
    - Same-bar target + invalidation → ambiguous (do not set win/loss)
    - MFE/MAE after entry vs entry_mid; returns only after entry
    - Gaps remain null; never invent fills
    """
    clock = measured_at or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)

    post = _bars_after(bars, on_or_after=opportunity.signal_period)
    if not post:
        return OpportunityOutcome(
            opportunity_uid=opportunity.opportunity_uid,
            measured_at=clock,
        )

    entry_mid = (opportunity.entry_zone_low + opportunity.entry_zone_high) / Decimal("2")
    entry_idx: int | None = None
    for i, bar in enumerate(post):
        if zone_touched(bar, opportunity.entry_zone_low, opportunity.entry_zone_high):
            entry_idx = i
            break

    entry_reached = entry_idx is not None
    entry_missed: bool | None = None
    if not entry_reached:
        # Only mark missed when opportunity is expired/invalidated.
        if opportunity.status in {
            OpportunityStatus.EXPIRED.value,
            OpportunityStatus.INVALIDATED.value,
        }:
            entry_missed = True
        return OpportunityOutcome(
            opportunity_uid=opportunity.opportunity_uid,
            entry_reached=False,
            entry_missed=entry_missed,
            targets_reached=tuple(False for _ in opportunity.targets),
            invalidation_reached=False,
            measured_at=clock,
        )

    assert entry_idx is not None
    path = post[entry_idx:]
    targets_reached: list[bool | None] = [False for _ in opportunity.targets]
    invalidation_reached: bool | None = False
    ambiguous = False

    # Walk bars after entry; same-bar target+invalidation → ambiguous.
    for bar in path:
        inv_hit = invalidation_touched(
            bar, side=opportunity.side, invalidation=opportunity.invalidation
        )
        target_hits = [
            level_touched(bar, side=opportunity.side, level=level)
            for level in opportunity.targets
        ]
        if inv_hit and any(target_hits):
            ambiguous = True
            invalidation_reached = None
            targets_reached = [None for _ in opportunity.targets]
            break
        if inv_hit:
            invalidation_reached = True
            break
        for i, hit in enumerate(target_hits):
            if hit:
                targets_reached[i] = True

    mfe: Decimal | None = None
    mae: Decimal | None = None
    if opportunity.side is OpportunitySide.LONG:
        mfe = max((b.high - entry_mid) for b in path)
        mae = min((b.low - entry_mid) for b in path)
    elif opportunity.side is OpportunitySide.SHORT:
        mfe = max((entry_mid - b.low) for b in path)
        mae = min((entry_mid - b.high) for b in path)

    return_1d = _horizon_return(path, entry_mid=entry_mid, side=opportunity.side, sessions=1)
    return_5d = _horizon_return(path, entry_mid=entry_mid, side=opportunity.side, sessions=5)

    # Normalize optional bools for dataclass (targets_reached is tuple[bool, ...])
    # Ambiguous: store empty targets_reached and null invalidation.
    if ambiguous:
        targets_tuple: tuple[bool, ...] = ()
    else:
        targets_tuple = tuple(bool(x) for x in targets_reached)

    return OpportunityOutcome(
        opportunity_uid=opportunity.opportunity_uid,
        entry_reached=True,
        entry_missed=False,
        targets_reached=targets_tuple,
        invalidation_reached=invalidation_reached if not ambiguous else None,
        mfe=mfe,
        mae=mae,
        return_1d=return_1d,
        return_5d=return_5d,
        return_20d=None,
        measured_at=clock,
    )


def _horizon_return(
    path_from_entry: Sequence[DailyBar],
    *,
    entry_mid: Decimal,
    side: OpportunitySide,
    sessions: int,
) -> Decimal | None:
    """Return from entry_mid to close ``sessions`` trading sessions after entry bar.

    Entry bar is index 0; 1d uses index 1, 5d uses index 5. Gaps → null.
    """
    if sessions < 1:
        return None
    if len(path_from_entry) <= sessions:
        return None
    close = path_from_entry[sessions].close
    if entry_mid == 0:
        return None
    raw = (close - entry_mid) / entry_mid
    if side is OpportunitySide.SHORT:
        return -raw
    return raw


def measurable_from_row(row: dict[str, Any]) -> MeasurableOpportunity:
    """Build MeasurableOpportunity from a storage dict row."""
    side = OpportunitySide(str(row["side"]))
    targets_raw = row.get("targets") or row.get("target_prices") or []
    targets: list[Decimal] = []
    for item in targets_raw:
        if isinstance(item, dict):
            targets.append(Decimal(str(item["price"])))
        else:
            targets.append(Decimal(str(item)))
    signal_period = row.get("signal_period")
    if isinstance(signal_period, str):
        signal_period = date.fromisoformat(signal_period[:10])
    elif signal_period is None:
        # Fall back to created_at date or verified provenance period.
        created = row.get("created_at") or row.get("period")
        if isinstance(created, datetime):
            signal_period = created.date()
        elif isinstance(created, str):
            signal_period = date.fromisoformat(created[:10])
        else:
            raise ValueError("signal_period required for measurement")
    inv = row.get("invalidation_price", row.get("invalidation"))
    if isinstance(inv, dict):
        inv = inv["price"]
    return MeasurableOpportunity(
        opportunity_uid=str(row["opportunity_uid"]),
        side=side,
        entry_zone_low=Decimal(str(row["entry_zone_low"])),
        entry_zone_high=Decimal(str(row["entry_zone_high"])),
        targets=tuple(targets),
        invalidation=Decimal(str(inv)),
        signal_period=signal_period,
        status=str(row.get("status") or OpportunityStatus.PUBLISHED.value),
    )


def measure_open_opportunities(
    settings: Settings,
    bars: Sequence[DailyBar],
    *,
    measured_at: datetime | None = None,
    persist: bool = True,
) -> list[OpportunityOutcome]:
    """Load open opportunities, measure against bars, optionally persist."""
    from app.database import market_opportunity_storage as moi_storage

    if not bars:
        return []
    # Require a new daily bar relative to last known measurement path — caller
    # supplies bars including the newest observation.
    rows = moi_storage.list_open_for_measurement(settings)
    outcomes: list[OpportunityOutcome] = []
    for row in rows:
        try:
            measurable = measurable_from_row(row)
        except (KeyError, ValueError, TypeError) as exc:
            logger.info("MOI outcome skip uid=%s: %s", row.get("opportunity_uid"), exc)
            continue
        outcome = measure_opportunity(measurable, bars, measured_at=measured_at)
        if persist:
            moi_storage.upsert_outcome(settings, outcome)
            # Transition to measured when horizons complete or expired resolved.
            if _measurement_complete(outcome, measurable):
                moi_storage.update_opportunity_status(
                    settings,
                    measurable.opportunity_uid,
                    OpportunityStatus.MEASURED.value,
                )
        outcomes.append(outcome)
    return outcomes


def _measurement_complete(
    outcome: OpportunityOutcome,
    opportunity: MeasurableOpportunity,
) -> bool:
    if outcome.entry_reached and outcome.return_5d is not None:
        return True
    if outcome.entry_missed:
        return True
    if outcome.invalidation_reached is True:
        return True
    if opportunity.status in {
        OpportunityStatus.EXPIRED.value,
        OpportunityStatus.INVALIDATED.value,
    }:
        return True
    return False
