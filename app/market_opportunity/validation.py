"""Fail-closed validation for Market Opportunity candidates."""

from __future__ import annotations

from decimal import Decimal

from app.market_opportunity.freshness import FreshnessError, assert_fresh
from app.market_opportunity.models import (
    ConfidenceBand,
    Opportunity,
    OpportunitySide,
    PriceLevel,
)


class ValidationError(ValueError):
    """Opportunity failed deterministic validation."""


def _reject(message: str) -> None:
    raise ValidationError(message)


def assert_no_invented_confidence_percent(value: object) -> None:
    """Confidence must be LOW/MEDIUM/HIGH — never a free-form percent."""
    if isinstance(value, ConfidenceBand):
        return
    if isinstance(value, str) and value in {c.value for c in ConfidenceBand}:
        return
    _reject("confidence must be LOW, MEDIUM, or HIGH (not a percent)")


def assert_price_levels_consistent(
    *,
    side: OpportunitySide,
    verified: PriceLevel,
    entry_low: Decimal,
    entry_high: Decimal,
    targets: tuple[PriceLevel, ...],
    invalidation: PriceLevel,
) -> None:
    if entry_low > entry_high:
        _reject("entry zone inverted")
    if not targets:
        _reject("targets required")
    # Contradictory unresolved prices: same label, different prices, different sources.
    by_label: dict[str, list[PriceLevel]] = {}
    for level in (verified, invalidation, *targets):
        by_label.setdefault(level.label, []).append(level)
    for label, levels in by_label.items():
        prices = {level.price for level in levels}
        sources = {level.provenance.source_id for level in levels}
        if len(prices) > 1 and len(sources) > 1:
            _reject(f"contradictory unresolved prices for label={label}")

    if side == OpportunitySide.WAIT:
        return
    if side == OpportunitySide.LONG:
        for target in targets:
            if target.price <= entry_high:
                _reject("LONG targets must be above entry zone")
        if invalidation.price >= entry_low:
            _reject("LONG invalidation must be below entry zone")
    elif side == OpportunitySide.SHORT:
        for target in targets:
            if target.price >= entry_low:
                _reject("SHORT targets must be below entry zone")
        if invalidation.price <= entry_high:
            _reject("SHORT invalidation must be above entry zone")


def validate_opportunity(opportunity: Opportunity, *, now=None) -> None:
    """Fail closed: any issue raises ValidationError / FreshnessError."""
    if not opportunity.paper_only:
        _reject("paper_only must be true")
    assert_no_invented_confidence_percent(opportunity.confidence)

    # Instrument ambiguity already enforced in InstrumentRef; re-check emptiness.
    inst = opportunity.instrument
    if not inst.source_id or not inst.venue or not inst.market_id:
        _reject("instrument requires source_id, venue, and market_id")

    levels = (
        opportunity.verified_price,
        opportunity.invalidation,
        *opportunity.targets,
    )
    for level in levels:
        try:
            assert_fresh(level.provenance, now=now)
        except FreshnessError as exc:
            raise ValidationError(str(exc)) from exc

    for evidence in opportunity.evidence:
        try:
            assert_fresh(evidence.provenance, now=now)
        except FreshnessError as exc:
            raise ValidationError(str(exc)) from exc

    assert_price_levels_consistent(
        side=opportunity.side,
        verified=opportunity.verified_price,
        entry_low=opportunity.entry_zone_low,
        entry_high=opportunity.entry_zone_high,
        targets=opportunity.targets,
        invalidation=opportunity.invalidation,
    )

    if opportunity.side != OpportunitySide.WAIT and not opportunity.evidence:
        _reject("directional opportunities require evidence")
    if not opportunity.counterarguments:
        _reject("counterarguments/risks required")
