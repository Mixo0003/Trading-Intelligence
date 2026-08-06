"""Lifecycle transition guards for Market Opportunity Intelligence."""

from __future__ import annotations

from app.market_opportunity.models import OpportunityStatus

_ALLOWED: dict[OpportunityStatus, frozenset[OpportunityStatus]] = {
    OpportunityStatus.DISCOVERED: frozenset(
        {OpportunityStatus.VALIDATED, OpportunityStatus.EXPIRED}
    ),
    OpportunityStatus.VALIDATED: frozenset(
        {
            OpportunityStatus.SCORED,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.INVALIDATED,
        }
    ),
    OpportunityStatus.SCORED: frozenset(
        {
            OpportunityStatus.PUBLISHED,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.INVALIDATED,
        }
    ),
    OpportunityStatus.PUBLISHED: frozenset(
        {
            OpportunityStatus.EXPIRED,
            OpportunityStatus.INVALIDATED,
            OpportunityStatus.MEASURED,
        }
    ),
    OpportunityStatus.EXPIRED: frozenset({OpportunityStatus.MEASURED}),
    OpportunityStatus.INVALIDATED: frozenset({OpportunityStatus.MEASURED}),
    OpportunityStatus.MEASURED: frozenset({OpportunityStatus.CALIBRATED}),
    OpportunityStatus.CALIBRATED: frozenset(),
}


class LifecycleError(ValueError):
    """Illegal opportunity status transition."""


def can_transition(current: OpportunityStatus, nxt: OpportunityStatus) -> bool:
    if current == nxt:
        return True
    return nxt in _ALLOWED.get(current, frozenset())


def assert_transition(current: OpportunityStatus, nxt: OpportunityStatus) -> None:
    if not can_transition(current, nxt):
        raise LifecycleError(f"illegal transition {current.value} -> {nxt.value}")
