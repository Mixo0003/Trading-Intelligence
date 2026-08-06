"""V4.2 WTI Stage A (memory) + Stage B (validate/persist) pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence

from app.config.settings import Settings
from app.market_opportunity.blackouts import (
    BlackoutError,
    assert_schedule_coverage,
    is_in_blackout,
)
from app.market_opportunity.detectors.base import (
    WTI_BREAKOUT_DETECTOR_ID,
    WTI_INVENTORY_DETECTOR_ID,
    WTI_STRUCTURE_DETECTOR_IDS,
    WTI_TREND_DETECTOR_ID,
    DetectorCandidate,
    WtiDetectorContext,
)
from app.market_opportunity.detectors.wti_breakout import WtiBreakoutDetector
from app.market_opportunity.detectors.wti_inventory import (
    INVENTORY_Z_CANDIDATE,
    INVENTORY_Z_STRONG,
    WtiInventoryDetector,
)
from app.market_opportunity.detectors.wti_trend import WtiTrendDetector
from app.market_opportunity.geometry import GeometryError, OpportunityGeometry, build_geometry
from app.market_opportunity.indicators import IndicatorError, inventory_wow_zscore
from app.market_opportunity.models import (
    AssetClass,
    ConfidenceBand,
    EvidenceRef,
    InstrumentRef,
    Opportunity,
    OpportunityFamily,
    OpportunitySide,
    OpportunityStatus,
    PriceLevel,
    Provenance,
)
from app.market_opportunity.freshness import FreshnessError, assert_fresh
from app.market_opportunity.paper import assert_paper_only
from app.market_opportunity.sources.eia import (
    WTI_CUSHING_DISPLAY_NAME,
    WTI_CUSHING_MARKET_ID,
    WTI_CUSHING_SOURCE_ID,
    WTI_CUSHING_SYMBOL,
    WTI_CUSHING_VENUE,
    DailyBar,
    InventoryPoint,
    period_to_source_event_at,
)
from app.market_opportunity.sources.fred import FredObservation, lookup_same_period
from app.market_opportunity.validation import ValidationError, validate_opportunity

logger = logging.getLogger(__name__)

TIME_HORIZON = "5 trading days"
FRED_CONFLICT_THRESHOLD = Decimal("0.005")  # 0.5%
SERIES_EIA_DAILY = "eia_daily_spot"
SERIES_EIA_INVENTORY = "eia_weekly_inventory"
SERIES_FRED = "fred_daily_crosscheck"

_FAMILY_BY_DETECTOR = {
    WTI_TREND_DETECTOR_ID: OpportunityFamily.TREND_MOMENTUM,
    WTI_BREAKOUT_DETECTOR_ID: OpportunityFamily.BREAKOUT,
    WTI_INVENTORY_DETECTOR_ID: OpportunityFamily.COMMODITY_SUPPLY_DEMAND,
}

_STRUCTURE_KIND = {
    WTI_TREND_DETECTOR_ID: "trend",
    WTI_BREAKOUT_DETECTOR_ID: "breakout",
}


@dataclass(frozen=True)
class WtiPipelineInput:
    """Inputs for a single WTI scan (already fetched; no network here)."""

    bars: Sequence[DailyBar]
    inventory: Sequence[InventoryPoint] = ()
    fred: Sequence[FredObservation] = ()
    fetched_at: datetime | None = None
    now: datetime | None = None


@dataclass
class StageBResult:
    published: list[Opportunity] = field(default_factory=list)
    suppressed: list[dict[str, Any]] = field(default_factory=list)
    stage_a_count: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)


def wti_instrument() -> InstrumentRef:
    return InstrumentRef(
        asset_class=AssetClass.COMMODITY,
        symbol=WTI_CUSHING_SYMBOL,
        source_id=WTI_CUSHING_SOURCE_ID,
        venue=WTI_CUSHING_VENUE,
        market_id=WTI_CUSHING_MARKET_ID,
        display_name=WTI_CUSHING_DISPLAY_NAME,
    )


def opportunity_uid(detector_id: str, period: date) -> str:
    return f"wti:{detector_id}:{period.isoformat()}"


def add_weekdays(start: date, n: int) -> date:
    """Add ``n`` weekdays (Mon–Fri) after ``start``."""
    if n < 0:
        raise ValueError("n must be >= 0")
    cursor = start
    added = 0
    while added < n:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            added += 1
    return cursor


def run_stage_a(context: WtiDetectorContext | WtiPipelineInput) -> list[DetectorCandidate]:
    """Run the three WTI detectors in memory. No DB writes."""
    if isinstance(context, WtiPipelineInput):
        ctx = WtiDetectorContext(bars=context.bars, inventory=context.inventory)
    else:
        ctx = context
    detectors = (
        WtiTrendDetector(),
        WtiBreakoutDetector(),
        WtiInventoryDetector(),
    )
    candidates: list[DetectorCandidate] = []
    for detector in detectors:
        candidates.extend(detector.detect(ctx))
    return candidates


def apply_inventory_confluence(
    candidates: Sequence[DetectorCandidate],
) -> list[DetectorCandidate]:
    """Drop inventory candidates lacking same-side trend/breakout confluence."""
    structure_sides = {
        c.side for c in candidates if c.detector_id in WTI_STRUCTURE_DETECTOR_IDS
    }
    out: list[DetectorCandidate] = []
    for candidate in candidates:
        if candidate.detector_id == WTI_INVENTORY_DETECTOR_ID:
            if candidate.side in structure_sides:
                out.append(candidate)
            continue
        out.append(candidate)
    return out


def _fred_same_period_value(
    fred: Sequence[FredObservation],
    period: date,
) -> Decimal | None:
    obs = lookup_same_period(list(fred), period)
    return obs.value if obs is not None else None


def fred_conflict(
    eia_price: Decimal,
    fred_price: Decimal | None,
) -> bool:
    """True when same-period FRED differs from EIA by more than 0.5% of mid."""
    if fred_price is None:
        return False
    mid = (eia_price + fred_price) / Decimal("2")
    if mid == 0:
        return True
    return abs(eia_price - fred_price) / mid > FRED_CONFLICT_THRESHOLD


def fred_agrees(
    eia_price: Decimal,
    fred_price: Decimal | None,
) -> bool:
    if fred_price is None:
        return False
    return not fred_conflict(eia_price, fred_price)


def resolve_confidence(
    *,
    has_structure: bool,
    inventory_abs_z: Decimal | None,
    fred_agree: bool,
    fred_present: bool,
) -> ConfidenceBand:
    """LOW structure only; MEDIUM structure+inv|z|>=1 or FRED agree; HIGH all strong.

    Missing FRED caps at MEDIUM.
    """
    if not has_structure:
        return ConfidenceBand.LOW
    inv_confirm = inventory_abs_z is not None and inventory_abs_z >= INVENTORY_Z_CANDIDATE
    inv_strong = inventory_abs_z is not None and inventory_abs_z >= INVENTORY_Z_STRONG
    if inv_strong and fred_agree and fred_present:
        return ConfidenceBand.HIGH
    if inv_confirm or fred_agree:
        return ConfidenceBand.MEDIUM
    if not fred_present:
        return ConfidenceBand.LOW
    return ConfidenceBand.LOW


def _structure_for_candidate(
    candidate: DetectorCandidate,
    confluence_partners: Sequence[DetectorCandidate],
) -> str:
    if candidate.detector_id in _STRUCTURE_KIND:
        return _STRUCTURE_KIND[candidate.detector_id]
    # Inventory publishes using a same-side structure partner's geometry.
    for partner in confluence_partners:
        if (
            partner.side == candidate.side
            and partner.detector_id in _STRUCTURE_KIND
        ):
            return _STRUCTURE_KIND[partner.detector_id]
    raise GeometryError("inventory candidate lacks structure partner for geometry")


def _inventory_abs_z(points: Sequence[InventoryPoint]) -> Decimal | None:
    if len(points) < 27:
        return None
    try:
        return abs(inventory_wow_zscore(points, window=26))
    except IndicatorError:
        return None


def _fresh_inventory_abs_z(
    points: Sequence[InventoryPoint],
    *,
    fetched_at: datetime,
    now: datetime,
) -> Decimal | None:
    """Return |z| only when the latest inventory point passes freshness."""
    if not points:
        return None
    try:
        assert_fresh(
            _provenance(
                source_id=WTI_CUSHING_SOURCE_ID,
                series_kind=SERIES_EIA_INVENTORY,
                period=points[-1].period,
                fetched_at=fetched_at,
            ),
            now=now,
        )
    except (FreshnessError, ValueError):
        return None
    return _inventory_abs_z(points)


def _provenance(
    *,
    source_id: str,
    series_kind: str,
    period: date,
    fetched_at: datetime,
    note: str = "",
) -> Provenance:
    return Provenance(
        source_id=source_id,
        fetched_at=fetched_at,
        source_event_at=period_to_source_event_at(period),
        series_kind=series_kind,
        note=note,
    )


def _build_evidence(
    *,
    candidate: DetectorCandidate,
    bars: Sequence[DailyBar],
    inventory: Sequence[InventoryPoint],
    inventory_abs_z: Decimal | None,
    fred_price: Decimal | None,
    fetched_at: datetime,
    now: datetime,
    geometry: OpportunityGeometry,
) -> tuple[EvidenceRef, ...]:
    period = bars[-1].period
    price_prov = _provenance(
        source_id=WTI_CUSHING_SOURCE_ID,
        series_kind=SERIES_EIA_DAILY,
        period=period,
        fetched_at=fetched_at,
        note="RWTC daily spot",
    )
    items: list[EvidenceRef] = [
        EvidenceRef(
            evidence_id=f"eia-rwtc-{period.isoformat()}",
            summary=(
                f"EIA RWTC close {bars[-1].close} on {period.isoformat()}; "
                f"detector={candidate.detector_id} reason={candidate.reason}"
            ),
            provenance=price_prov,
            is_catalyst=True,
        ),
        EvidenceRef(
            evidence_id=f"geometry-{candidate.detector_id}-{period.isoformat()}",
            summary=(
                f"Paper geometry entry=[{geometry.entry_low}, {geometry.entry_high}] "
                f"inv={geometry.invalidation} T1={geometry.t1} T2={geometry.t2} "
                f"R={geometry.r} ATR={geometry.atr}"
            ),
            provenance=price_prov,
            is_catalyst=False,
        ),
    ]
    # Inventory evidence only when fresh and confirmatory (or inventory detector).
    if (
        inventory
        and inventory_abs_z is not None
        and (
            candidate.detector_id == WTI_INVENTORY_DETECTOR_ID
            or inventory_abs_z >= INVENTORY_Z_CANDIDATE
        )
    ):
        inv_period = inventory[-1].period
        inv_prov = _provenance(
            source_id=WTI_CUSHING_SOURCE_ID,
            series_kind=SERIES_EIA_INVENTORY,
            period=inv_period,
            fetched_at=fetched_at,
            note="WCESTUS1 weekly stocks ex-SPR",
        )
        try:
            assert_fresh(inv_prov, now=now)
        except FreshnessError:
            inv_prov = None
        if inv_prov is not None:
            items.append(
                EvidenceRef(
                    evidence_id=f"eia-inv-{inv_period.isoformat()}",
                    summary=(
                        f"EIA WCESTUS1 inventory {inventory[-1].value} MBBL "
                        f"as-of {inv_period.isoformat()}; WoW |z|={inventory_abs_z}"
                    ),
                    provenance=inv_prov,
                    is_catalyst=candidate.detector_id == WTI_INVENTORY_DETECTOR_ID,
                )
            )
    if fred_price is not None:
        fred_prov = _provenance(
            source_id="FRED",
            series_kind=SERIES_FRED,
            period=period,
            fetched_at=fetched_at,
            note="optional cross-check",
        )
        try:
            assert_fresh(fred_prov, now=now)
        except FreshnessError:
            fred_prov = None
        if fred_prov is not None:
            items.append(
                EvidenceRef(
                    evidence_id=f"fred-dcoilwtico-{period.isoformat()}",
                    summary=(
                        f"FRED DCOILWTICO same-period {fred_price} agrees within 0.5% "
                        f"of EIA {bars[-1].close}"
                    ),
                    provenance=fred_prov,
                    is_catalyst=False,
                )
            )
    return tuple(items)


def _counterarguments(
    *,
    side: OpportunitySide,
    fred_present: bool,
    inventory_abs_z: Decimal | None,
) -> tuple[str, ...]:
    args = [
        "EIA daily print is not a live futures quote; levels are paper structure only.",
        "Macro or inventory surprises can invalidate technical structure quickly.",
    ]
    if side is OpportunitySide.LONG:
        args.append("Downside gap risk: RWTC high/low equal close; intraday path unknown.")
    else:
        args.append("Squeeze/short-cover risk on daily spot proxy with no true high/low.")
    if not fred_present:
        args.append("FRED same-period cross-check missing; confidence capped at MEDIUM.")
    if inventory_abs_z is None:
        args.append("Inventory confirmation unavailable or cold-start incomplete.")
    return tuple(args)


def _price_level(
    label: str,
    price: Decimal,
    provenance: Provenance,
) -> PriceLevel:
    return PriceLevel(label=label, price=price, provenance=provenance)


def build_opportunity_from_candidate(
    candidate: DetectorCandidate,
    *,
    bars: Sequence[DailyBar],
    inventory: Sequence[InventoryPoint],
    fred: Sequence[FredObservation],
    fetched_at: datetime,
    now: datetime,
    confluence_partners: Sequence[DetectorCandidate],
) -> Opportunity:
    period = bars[-1].period
    eia_price = bars[-1].close
    fred_price = _fred_same_period_value(fred, period)
    if fred_conflict(eia_price, fred_price):
        raise ValidationError(
            f"FRED same-period conflict >0.5% EIA={eia_price} FRED={fred_price}"
        )

    structure = _structure_for_candidate(candidate, confluence_partners)
    geometry = build_geometry(list(bars), side=candidate.side, structure=structure)  # type: ignore[arg-type]
    inv_abs_z = _fresh_inventory_abs_z(
        inventory, fetched_at=fetched_at, now=now
    )
    # Inventory detector without fresh inventory cannot publish.
    if candidate.detector_id == WTI_INVENTORY_DETECTOR_ID and inv_abs_z is None:
        raise ValidationError("inventory candidate lacks fresh inventory confirmation")

    fred_present = fred_price is not None
    agree = fred_agrees(eia_price, fred_price)
    confidence = resolve_confidence(
        has_structure=True,
        inventory_abs_z=inv_abs_z,
        fred_agree=agree,
        fred_present=fred_present,
    )

    price_prov = _provenance(
        source_id=WTI_CUSHING_SOURCE_ID,
        series_kind=SERIES_EIA_DAILY,
        period=period,
        fetched_at=fetched_at,
    )
    evidence = _build_evidence(
        candidate=candidate,
        bars=bars,
        inventory=inventory,
        inventory_abs_z=inv_abs_z,
        fred_price=fred_price if agree else None,
        fetched_at=fetched_at,
        now=now,
        geometry=geometry,
    )
    # Evidence for FRED when present but used only for conflict/agree already handled.
    # If FRED present and agrees, evidence includes it; if missing, counterarguments note it.
    if fred_present and not agree:
        # Should have raised on conflict; defensive.
        raise ValidationError("FRED conflict must suppress before build")

    expires_day = add_weekdays(period, 5)
    expires_at = datetime(
        expires_day.year,
        expires_day.month,
        expires_day.day,
        23,
        59,
        59,
        tzinfo=timezone.utc,
    )
    reward_risk = Decimal("1.2")  # T1 = 1.2R by construction
    created = now.astimezone(timezone.utc)

    return Opportunity(
        opportunity_uid=opportunity_uid(candidate.detector_id, period),
        family=_FAMILY_BY_DETECTOR[candidate.detector_id],
        opportunity_type=candidate.detector_id,
        side=candidate.side,
        instrument=wti_instrument(),
        status=OpportunityStatus.PUBLISHED,
        confidence=confidence,
        verified_price=_price_level("verified", eia_price, price_prov),
        entry_zone_low=geometry.entry_low,
        entry_zone_high=geometry.entry_high,
        targets=(
            _price_level("T1", geometry.t1, price_prov),
            _price_level("T2", geometry.t2, price_prov),
        ),
        invalidation=_price_level("invalidation", geometry.invalidation, price_prov),
        time_horizon=TIME_HORIZON,
        reward_risk=reward_risk,
        evidence=evidence,
        counterarguments=_counterarguments(
            side=candidate.side,
            fred_present=fred_present,
            inventory_abs_z=inv_abs_z,
        ),
        paper_only=True,
        created_at=created,
        updated_at=created,
        expires_at=expires_at,
    )


def run_stage_b(
    settings: Settings,
    pipeline_input: WtiPipelineInput,
    *,
    persist: bool = True,
    candidates: Sequence[DetectorCandidate] | None = None,
) -> StageBResult:
    """Validate Stage A candidates and optionally persist published opportunities.

    Stage A candidates are never written. Suppressions are returned in-memory /
    diagnostics only.
    """
    assert_paper_only(settings)
    now = pipeline_input.now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValidationError("now must be timezone-aware")
    fetched_at = pipeline_input.fetched_at or now
    if fetched_at.tzinfo is None:
        raise ValidationError("fetched_at must be timezone-aware")

    bars = list(pipeline_input.bars)
    inventory = list(pipeline_input.inventory)
    fred = list(pipeline_input.fred)
    result = StageBResult()

    if not bars:
        result.suppressed.append({"reason": "no_price_bars"})
        result.diagnostics["suppress"] = "no_price_bars"
        return result

    stage_a = list(candidates) if candidates is not None else run_stage_a(pipeline_input)
    result.stage_a_count = len(stage_a)
    publishable = apply_inventory_confluence(stage_a)
    dropped_inv = result.stage_a_count - len(publishable)
    if dropped_inv:
        result.suppressed.append(
            {"reason": "inventory_without_structure_confluence", "count": dropped_inv}
        )

    try:
        assert_schedule_coverage(now)
    except BlackoutError as exc:
        result.suppressed.append({"reason": "blackout_schedule", "detail": str(exc)})
        result.diagnostics["blackout_schedule"] = str(exc)
        return result

    if is_in_blackout(now):
        result.suppressed.append({"reason": "in_blackout"})
        result.diagnostics["blackout"] = True
        return result

    period = bars[-1].period
    eia_price = bars[-1].close
    fred_price = _fred_same_period_value(fred, period)
    if fred_conflict(eia_price, fred_price):
        result.suppressed.append(
            {
                "reason": "fred_conflict",
                "eia": str(eia_price),
                "fred": str(fred_price),
            }
        )
        result.diagnostics["fred_conflict"] = True
        return result

    result.diagnostics["fred_present"] = fred_price is not None
    result.diagnostics["period"] = period.isoformat()

    for candidate in publishable:
        try:
            opportunity = build_opportunity_from_candidate(
                candidate,
                bars=bars,
                inventory=inventory,
                fred=fred,
                fetched_at=fetched_at,
                now=now,
                confluence_partners=publishable,
            )
            validate_opportunity(opportunity, now=now)
        except (ValidationError, GeometryError, BlackoutError, ValueError, TypeError) as exc:
            result.suppressed.append(
                {
                    "reason": "stage_b_reject",
                    "detector_id": candidate.detector_id,
                    "detail": str(exc),
                }
            )
            logger.info(
                "MOI Stage B suppressed %s: %s",
                candidate.detector_id,
                exc,
            )
            continue

        if persist:
            from app.database import market_opportunity_storage as moi_storage

            moi_storage.upsert_opportunity(settings, opportunity)
        result.published.append(opportunity)

    result.diagnostics["published_count"] = len(result.published)
    result.diagnostics["suppressed_count"] = len(result.suppressed)
    return result


def run_wti_scan(
    settings: Settings,
    pipeline_input: WtiPipelineInput,
    *,
    persist: bool = True,
) -> StageBResult:
    """Full Stage A → confluence → Stage B scan (no network)."""
    return run_stage_b(settings, pipeline_input, persist=persist)
