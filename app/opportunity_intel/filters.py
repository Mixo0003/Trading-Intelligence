"""Per-user qualification of persisted global opportunity signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.opportunity_intel.delivery_gates import OpportunityDeliveryGates
from app.opportunity_intel.models import (
    AlertMode,
    ExecutionMetrics,
    LiveMode,
    ObservationMode,
    OpportunityDiagnosticCode,
    OpportunitySignal,
    OpportunitySubscription,
)
from app.opportunity_intel.quiet_hours import is_within_quiet_hours
from app.opportunity_intel.settings import (
    STRICT_LIVE_COOLDOWN_FLOOR_SECONDS,
    OpportunityScannerSettings,
)


@dataclass(frozen=True)
class PersistedSignalContext:
    """Persisted observation + execution metrics for one global signal."""

    signal: OpportunitySignal
    canonical_score: Decimal
    liquidity_usd: Decimal
    volume_24h_usd: Decimal
    absolute_spread: Decimal
    relative_spread: Decimal
    category_ids: tuple[str, ...]
    execution_by_allocation: dict[Decimal, ExecutionMetrics]
    end_at: datetime
    question: str
    outcome: str
    event_slug: str
    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    move_5m: Decimal
    move_15m: Decimal
    move_1h: Decimal
    visible_ask_depth_usd: Decimal
    source_observed_at: datetime


@dataclass(frozen=True)
class RecipientQualification:
    eligible: bool
    reason: OpportunityDiagnosticCode | None = None
    allocation_metric: ExecutionMetrics | None = None


def effective_cooldown_seconds(
    settings: OpportunityScannerSettings,
    *,
    signal_mode: ObservationMode,
) -> int:
    if signal_mode == ObservationMode.LIVE_STRICT:
        return max(int(settings.cooldown_seconds), int(STRICT_LIVE_COOLDOWN_FLOOR_SECONDS))
    return int(settings.cooldown_seconds)


def qualify_recipient(
    *,
    subscription: OpportunitySubscription,
    context: PersistedSignalContext,
    gates: OpportunityDeliveryGates,
    is_admin_recipient: bool,
    now: datetime,
    last_sent_at: datetime | None,
) -> RecipientQualification:
    """Qualify one recipient using persisted data only (no rescore/rescan)."""
    if not subscription.enabled:
        return RecipientQualification(
            False, OpportunityDiagnosticCode.SUBSCRIPTION_DISABLED
        )
    user = subscription.settings
    if user.alert_mode != AlertMode.INSTANT:
        return RecipientQualification(
            False, OpportunityDiagnosticCode.ALERT_MODE_NOT_INSTANT
        )
    if not gates.allows(is_admin_recipient=is_admin_recipient):
        code = (
            OpportunityDiagnosticCode.ADMIN_GATE_DISABLED
            if is_admin_recipient
            else OpportunityDiagnosticCode.USER_GATE_DISABLED
        )
        return RecipientQualification(False, code)

    signal = context.signal
    if signal.mode == ObservationMode.LIVE_STRICT and user.live_mode != LiveMode.STRICT:
        return RecipientQualification(
            False, OpportunityDiagnosticCode.LIVE_MODE_MISMATCH
        )
    if not _categories_allowed(user, context.category_ids):
        return RecipientQualification(
            False, OpportunityDiagnosticCode.CATEGORY_NOT_ALLOWED
        )
    if context.canonical_score < Decimal(user.minimum_score):
        return RecipientQualification(
            False, OpportunityDiagnosticCode.SCORE_BELOW_FLOOR
        )
    if context.liquidity_usd < user.minimum_liquidity_usd:
        return RecipientQualification(
            False, OpportunityDiagnosticCode.LIQUIDITY_TOO_LOW
        )
    if context.volume_24h_usd < user.minimum_24h_volume_usd:
        return RecipientQualification(
            False, OpportunityDiagnosticCode.VOLUME_TOO_LOW
        )
    if context.relative_spread > user.maximum_relative_spread:
        return RecipientQualification(
            False, OpportunityDiagnosticCode.SPREAD_TOO_WIDE
        )
    if context.absolute_spread > user.maximum_absolute_spread:
        return RecipientQualification(
            False, OpportunityDiagnosticCode.SPREAD_TOO_WIDE
        )

    metric = context.execution_by_allocation.get(user.paper_allocation_usd)
    if metric is None:
        return RecipientQualification(
            False, OpportunityDiagnosticCode.ALLOCATION_METRIC_MISSING
        )
    if not metric.fully_executable or metric.slippage is None or metric.execution_vwap is None:
        return RecipientQualification(
            False, OpportunityDiagnosticCode.ALLOCATION_NOT_EXECUTABLE
        )
    if metric.slippage > user.maximum_slippage:
        return RecipientQualification(
            False, OpportunityDiagnosticCode.SLIPPAGE_TOO_HIGH
        )

    if is_within_quiet_hours(user.quiet_hours, user.timezone_name, at=now):
        return RecipientQualification(
            False, OpportunityDiagnosticCode.QUIET_HOURS_ACTIVE
        )

    cooldown = effective_cooldown_seconds(user, signal_mode=signal.mode)
    if last_sent_at is not None:
        sent_at = last_sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        if now < sent_at + timedelta(seconds=cooldown):
            return RecipientQualification(
                False, OpportunityDiagnosticCode.COOLDOWN_ACTIVE
            )

    return RecipientQualification(True, None, metric)


def _categories_allowed(
    user: OpportunityScannerSettings, category_ids: tuple[str, ...]
) -> bool:
    if user.categories.all_categories:
        return True
    return any(category in user.categories.category_ids for category in category_ids)
