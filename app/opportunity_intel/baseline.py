"""Per-asset two-cycle baseline advancement and reset rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.database.opportunity_storage import MarketStateRecord
from app.opportunity_intel.models import (
    ObservationMode,
    OpportunityBaseline,
    OpportunityDiagnosticCode,
)
from app.opportunity_intel.versions import (
    BASELINE_VERSION,
    MARKET_ABSENCE_RESET_SECONDS,
    OPPORTUNITY_SCORE_VERSION,
    SAFETY_POLICY_VERSION,
)


@dataclass(frozen=True)
class BaselineDecision:
    baseline: OpportunityBaseline
    reset_code: OpportunityDiagnosticCode | None
    advanced: bool
    became_ready: bool


def evaluate_baseline_reset(
    *,
    prior: MarketStateRecord | None,
    mode: ObservationMode,
    identity_fingerprint: str,
    now: datetime,
    present_in_discovery: bool,
) -> OpportunityDiagnosticCode | None:
    if prior is None:
        return None
    if prior.identity_fingerprint and prior.identity_fingerprint != identity_fingerprint:
        return OpportunityDiagnosticCode.BASELINE_RESET_IDENTITY
    if prior.mode != mode:
        return OpportunityDiagnosticCode.BASELINE_RESET_MODE
    if (
        prior.baseline.baseline_version != BASELINE_VERSION
        or prior.score_version != OPPORTUNITY_SCORE_VERSION
        or prior.safety_policy_version != SAFETY_POLICY_VERSION
    ):
        return OpportunityDiagnosticCode.BASELINE_RESET_VERSION
    if not present_in_discovery and prior.baseline.last_observed_at is not None:
        age = (
            now.astimezone(timezone.utc)
            - prior.baseline.last_observed_at.astimezone(timezone.utc)
        ).total_seconds()
        if age > MARKET_ABSENCE_RESET_SECONDS:
            return OpportunityDiagnosticCode.BASELINE_RESET_STALE
    return None


def advance_baseline(
    *,
    asset_id: str,
    mode: ObservationMode,
    prior: MarketStateRecord | None,
    cycle_id: str,
    observed_at: datetime,
    observation_valid: bool,
    identity_fingerprint: str,
    now: datetime,
    present_in_discovery: bool = True,
) -> BaselineDecision:
    """Advance baseline only on a complete valid observation for this asset."""
    reset = evaluate_baseline_reset(
        prior=prior,
        mode=mode,
        identity_fingerprint=identity_fingerprint,
        now=now,
        present_in_discovery=present_in_discovery,
    )
    if reset is not None or prior is None:
        count = 1 if observation_valid else 0
        baseline = OpportunityBaseline(
            asset_id=asset_id,
            mode=mode,
            baseline_version=BASELINE_VERSION,
            consecutive_complete_cycles=count,
            last_complete_cycle_id=cycle_id if observation_valid else None,
            last_observed_at=observed_at if observation_valid else None,
            ready=False,
        )
        return BaselineDecision(
            baseline=baseline,
            reset_code=reset,
            advanced=observation_valid,
            became_ready=False,
        )

    if not observation_valid:
        # Failed cycle: preserve prior baseline unchanged.
        return BaselineDecision(
            baseline=prior.baseline,
            reset_code=None,
            advanced=False,
            became_ready=False,
        )

    if prior.baseline.ready:
        baseline = OpportunityBaseline(
            asset_id=asset_id,
            mode=mode,
            baseline_version=BASELINE_VERSION,
            consecutive_complete_cycles=2,
            last_complete_cycle_id=cycle_id,
            last_observed_at=observed_at,
            ready=True,
        )
        return BaselineDecision(
            baseline=baseline,
            reset_code=None,
            advanced=False,
            became_ready=False,
        )

    next_count = min(2, prior.baseline.consecutive_complete_cycles + 1)
    became_ready = next_count == 2
    baseline = OpportunityBaseline(
        asset_id=asset_id,
        mode=mode,
        baseline_version=BASELINE_VERSION,
        consecutive_complete_cycles=next_count,
        last_complete_cycle_id=cycle_id,
        last_observed_at=observed_at,
        ready=became_ready,
    )
    return BaselineDecision(
        baseline=baseline,
        reset_code=None,
        advanced=True,
        became_ready=became_ready,
    )
