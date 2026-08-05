"""Per-user wave selection, auth recheck, claim/retry helpers (no Telegram send)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.config.settings import Settings
from app.database import opportunity_storage
from app.opportunity_intel.filters import (
    PersistedSignalContext,
    qualify_recipient,
)
from app.opportunity_intel.models import (
    AlertMode,
    DeliveryStatus,
    LiveMode,
    ObservationMode,
    OpportunityDelivery,
    OpportunityDiagnosticCode,
)
from app.opportunity_intel.payload import build_delivery_card_payload
from app.telegram.opportunity_intel.formatting import format_opportunity_card
from app.opportunity_intel.quiet_hours import is_within_quiet_hours
from app.opportunity_intel.telegram_errors import (
    TelegramSendClassification,
    classify_telegram_send_outcome,
)
from app.telegram.auth import is_admin

MAX_SEND_ATTEMPTS = 3
CLAIM_LEASE_SECONDS = 120
RETRY_DELAY_SECONDS_AFTER_ATTEMPT = {
    1: 60,
    2: 300,
    3: 900,
}

_RECORD_SUPPRESS_CODES = frozenset(
    {
        OpportunityDiagnosticCode.ADMIN_GATE_DISABLED,
        OpportunityDiagnosticCode.USER_GATE_DISABLED,
        OpportunityDiagnosticCode.QUIET_HOURS_ACTIVE,
        OpportunityDiagnosticCode.COOLDOWN_ACTIVE,
        OpportunityDiagnosticCode.ALLOCATION_METRIC_MISSING,
        OpportunityDiagnosticCode.ALLOCATION_NOT_EXECUTABLE,
    }
)


@dataclass(frozen=True)
class WaveSelectionResult:
    opportunity_uid: str
    signal_at: datetime
    wave_at: datetime
    canonical_score: Decimal
    pending: tuple[OpportunityDelivery, ...]
    suppressed: tuple[OpportunityDelivery, ...]
    inserted_count: int
    ignored_count: int


@dataclass(frozen=True)
class AuthorizedSendReady:
    delivery: OpportunityDelivery
    claim_id: str


@dataclass(frozen=True)
class AuthorizedSendSuppressed:
    delivery: OpportunityDelivery | None
    reason: OpportunityDiagnosticCode


def select_delivery_wave(
    settings: Settings,
    *,
    opportunity_uid: str,
    wave_at: datetime | None = None,
    now: datetime | None = None,
) -> WaveSelectionResult:
    """Qualify recipients from persisted global signal data and insert one wave.

    No HTTP, discovery, book, history, allocation recalculation, or rescoring.
    """
    clock = _aware(now or datetime.now(timezone.utc))
    wave_ts = _aware(wave_at or clock)
    signal = opportunity_storage.load_signal(settings, opportunity_uid)
    if signal is None:
        raise ValueError("opportunity signal not found")
    bundle = opportunity_storage.load_persisted_observation_bundle(
        settings,
        asset_id=signal.asset_id,
        observed_at=signal.source_observed_at,
    )
    if bundle is None:
        raise ValueError("persisted observation bundle not found")
    if bundle.canonical_score != signal.score:
        # Canonical score remains the signal's persisted score; bundle score is diagnostic.
        canonical_score = signal.score
    else:
        canonical_score = signal.score

    context = PersistedSignalContext(
        signal=signal,
        canonical_score=canonical_score,
        liquidity_usd=bundle.liquidity_usd,
        volume_24h_usd=bundle.volume_24h_usd,
        absolute_spread=bundle.absolute_spread,
        relative_spread=bundle.relative_spread,
        category_ids=bundle.category_ids,
        execution_by_allocation={
            metric.allocation_usd: metric for metric in bundle.execution_metrics
        },
        end_at=bundle.end_at,
        question=bundle.question,
        outcome=bundle.outcome,
        event_slug=bundle.event_slug,
        best_bid=bundle.best_bid,
        best_ask=bundle.best_ask,
        midpoint=bundle.midpoint,
        move_5m=bundle.move_5m,
        move_15m=bundle.move_15m,
        move_1h=bundle.move_1h,
        visible_ask_depth_usd=bundle.visible_ask_depth_usd,
        source_observed_at=bundle.observed_at,
    )
    gates = opportunity_storage.load_delivery_gates(settings)
    rows: list[OpportunityDelivery] = []
    pending: list[OpportunityDelivery] = []
    suppressed: list[OpportunityDelivery] = []

    for subscription in opportunity_storage.list_subscriptions(settings):
        # Live Mode OFF blocks new automatic alert delivery (on-demand Top
        # Opportunities remain available via qualify_recipient alone).
        if subscription.settings.live_mode == LiveMode.OFF:
            continue
        admin_recipient = is_admin(settings, subscription.telegram_id)
        last_sent = opportunity_storage.last_sent_at_for_opportunity_key(
            settings,
            telegram_id=subscription.telegram_id,
            opportunity_key=signal.opportunity_key,
        )
        qualification = qualify_recipient(
            subscription=subscription,
            context=context,
            gates=gates,
            is_admin_recipient=admin_recipient,
            now=clock,
            last_sent_at=last_sent,
        )
        if qualification.eligible:
            metric = qualification.allocation_metric
            if (
                metric is None
                or metric.execution_vwap is None
                or metric.slippage is None
            ):
                continue
            payload = build_delivery_card_payload(
                opportunity_uid=signal.opportunity_uid,
                signal_at=signal.signal_at,
                wave_at=wave_ts,
                signal_family=signal.family,
                question=context.question,
                outcome=context.outcome,
                event_slug=context.event_slug,
                mode=signal.mode,
                canonical_score=canonical_score,
                reason_codes=signal.reason_codes,
                direction=signal.direction,
                move_5m=context.move_5m,
                move_15m=context.move_15m,
                move_1h=context.move_1h,
                best_bid=context.best_bid,
                best_ask=context.best_ask,
                midpoint=context.midpoint,
                absolute_spread=context.absolute_spread,
                relative_spread=context.relative_spread,
                allocation_usd=metric.allocation_usd,
                execution_vwap=metric.execution_vwap,
                slippage=metric.slippage,
                executable_ask_depth_usd=context.visible_ask_depth_usd,
                volume_24h_usd=context.volume_24h_usd,
                liquidity_usd=context.liquidity_usd,
                end_at=context.end_at,
                source_observed_at=context.source_observed_at,
            )
            delivery = OpportunityDelivery(
                telegram_id=subscription.telegram_id,
                opportunity_uid=signal.opportunity_uid,
                status=DeliveryStatus.PENDING,
                payload_text=format_opportunity_card(payload),
                signal_at=signal.signal_at,
                wave_at=wave_ts,
                settings_revision=subscription.settings.revision,
            )
            rows.append(delivery)
            pending.append(delivery)
            continue

        reason = qualification.reason
        if reason is None or reason not in _RECORD_SUPPRESS_CODES:
            continue
        delivery = OpportunityDelivery(
            telegram_id=subscription.telegram_id,
            opportunity_uid=signal.opportunity_uid,
            status=DeliveryStatus.SUPPRESSED,
            payload_text=_suppressed_payload_text(signal.opportunity_uid, reason),
            signal_at=signal.signal_at,
            wave_at=wave_ts,
            settings_revision=subscription.settings.revision,
            last_error_code=reason,
        )
        rows.append(delivery)
        suppressed.append(delivery)

    inserted, ignored = opportunity_storage.insert_delivery_wave(settings, rows)
    return WaveSelectionResult(
        opportunity_uid=signal.opportunity_uid,
        signal_at=signal.signal_at,
        wave_at=wave_ts,
        canonical_score=canonical_score,
        pending=tuple(pending),
        suppressed=tuple(suppressed),
        inserted_count=inserted,
        ignored_count=ignored,
    )


def recheck_delivery_authorization(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_uid: str,
    settings_revision: int,
    now: datetime | None = None,
) -> tuple[bool, OpportunityDiagnosticCode | None]:
    """Re-read subscription/gates/policy immediately before send. Never reroutes."""
    clock = _aware(now or datetime.now(timezone.utc))
    subscription = opportunity_storage.load_subscription(settings, telegram_id)
    if subscription is None:
        return False, OpportunityDiagnosticCode.AUTHORIZATION_REVOKED
    if subscription.telegram_id != telegram_id:
        return False, OpportunityDiagnosticCode.AUTHORIZATION_REVOKED
    if not subscription.enabled:
        return False, OpportunityDiagnosticCode.SUBSCRIPTION_DISABLED
    if subscription.settings.alert_mode != AlertMode.INSTANT:
        return False, OpportunityDiagnosticCode.ALERT_MODE_NOT_INSTANT
    if subscription.settings.live_mode == LiveMode.OFF:
        return False, OpportunityDiagnosticCode.LIVE_MODE_OFF
    if subscription.settings.revision != settings_revision:
        return False, OpportunityDiagnosticCode.SETTINGS_REVISION_CHANGED

    gates = opportunity_storage.load_delivery_gates(settings)
    admin_recipient = is_admin(settings, telegram_id)
    if not gates.allows(is_admin_recipient=admin_recipient):
        return False, (
            OpportunityDiagnosticCode.ADMIN_GATE_DISABLED
            if admin_recipient
            else OpportunityDiagnosticCode.USER_GATE_DISABLED
        )

    signal = opportunity_storage.load_signal(settings, opportunity_uid)
    if signal is None:
        return False, OpportunityDiagnosticCode.AUTHORIZATION_REVOKED
    user = subscription.settings
    if signal.mode == ObservationMode.LIVE_STRICT and user.live_mode != LiveMode.STRICT:
        return False, OpportunityDiagnosticCode.LIVE_MODE_MISMATCH

    bundle = opportunity_storage.load_persisted_observation_bundle(
        settings,
        asset_id=signal.asset_id,
        observed_at=signal.source_observed_at,
    )
    if bundle is None:
        return False, OpportunityDiagnosticCode.AUTHORIZATION_REVOKED
    if not user.categories.all_categories and not any(
        category in user.categories.category_ids for category in bundle.category_ids
    ):
        return False, OpportunityDiagnosticCode.CATEGORY_NOT_ALLOWED
    if is_within_quiet_hours(user.quiet_hours, user.timezone_name, at=clock):
        return False, OpportunityDiagnosticCode.QUIET_HOURS_ACTIVE
    return True, None


def prepare_authorized_send(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_uid: str,
    now: datetime | None = None,
    claim_id: str | None = None,
) -> AuthorizedSendReady | AuthorizedSendSuppressed:
    """Claim lease then recheck authorization. Suppress instead of send/reroute."""
    clock = _aware(now or datetime.now(timezone.utc))
    opaque_claim = claim_id or secrets.token_hex(16)
    claimed = opportunity_storage.claim_delivery_lease(
        settings,
        telegram_id=telegram_id,
        opportunity_uid=opportunity_uid,
        claim_id=opaque_claim,
        now=clock,
        lease_seconds=CLAIM_LEASE_SECONDS,
        max_attempts=MAX_SEND_ATTEMPTS,
    )
    if claimed is None:
        existing = opportunity_storage.load_delivery(
            settings, telegram_id, opportunity_uid
        )
        if existing is not None and existing.status in {
            DeliveryStatus.SENT,
            DeliveryStatus.DEAD_LETTER,
            DeliveryStatus.SUPPRESSED,
        }:
            return AuthorizedSendSuppressed(
                existing, existing.last_error_code or OpportunityDiagnosticCode.DELIVERY_FAILURE
            )
        return AuthorizedSendSuppressed(
            existing, OpportunityDiagnosticCode.DELIVERY_FAILURE
        )

    ok, reason = recheck_delivery_authorization(
        settings,
        telegram_id=telegram_id,
        opportunity_uid=opportunity_uid,
        settings_revision=claimed.settings_revision,
        now=clock,
    )
    if not ok:
        assert reason is not None
        opportunity_storage.mark_delivery_terminal(
            settings,
            telegram_id=telegram_id,
            opportunity_uid=opportunity_uid,
            claim_id=opaque_claim,
            status=DeliveryStatus.SUPPRESSED,
            error_code=reason,
        )
        suppressed = opportunity_storage.load_delivery(
            settings, telegram_id, opportunity_uid
        )
        return AuthorizedSendSuppressed(suppressed, reason)
    return AuthorizedSendReady(claimed, opaque_claim)


def mark_send_success(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_uid: str,
    claim_id: str,
) -> bool:
    return opportunity_storage.mark_delivery_terminal(
        settings,
        telegram_id=telegram_id,
        opportunity_uid=opportunity_uid,
        claim_id=claim_id,
        status=DeliveryStatus.SENT,
    )


def mark_send_failure(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_uid: str,
    claim_id: str,
    classification: TelegramSendClassification,
    now: datetime | None = None,
) -> DeliveryStatus:
    """Apply retry backoff or dead-letter from a bounded Telegram classification."""
    clock = _aware(now or datetime.now(timezone.utc))
    delivery = opportunity_storage.load_delivery(
        settings, telegram_id, opportunity_uid
    )
    if delivery is None:
        return DeliveryStatus.DEAD_LETTER
    attempts = delivery.attempt_count
    if not classification.retryable:
        opportunity_storage.mark_delivery_terminal(
            settings,
            telegram_id=telegram_id,
            opportunity_uid=opportunity_uid,
            claim_id=claim_id,
            status=DeliveryStatus.DEAD_LETTER,
            error_code=classification.code,
        )
        return DeliveryStatus.DEAD_LETTER
    if attempts >= MAX_SEND_ATTEMPTS:
        opportunity_storage.mark_delivery_terminal(
            settings,
            telegram_id=telegram_id,
            opportunity_uid=opportunity_uid,
            claim_id=claim_id,
            status=DeliveryStatus.DEAD_LETTER,
            error_code=OpportunityDiagnosticCode.RETRY_EXHAUSTED,
        )
        return DeliveryStatus.DEAD_LETTER
    delay = float(RETRY_DELAY_SECONDS_AFTER_ATTEMPT.get(attempts, 900))
    if classification.retry_after_seconds is not None:
        delay = max(1.0, float(classification.retry_after_seconds))
    next_at = clock + timedelta(seconds=delay)
    opportunity_storage.mark_delivery_terminal(
        settings,
        telegram_id=telegram_id,
        opportunity_uid=opportunity_uid,
        claim_id=claim_id,
        status=DeliveryStatus.RETRY_WAIT,
        error_code=classification.code,
        next_attempt_at=next_at,
    )
    return DeliveryStatus.RETRY_WAIT


def classify_and_mark_send_failure(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_uid: str,
    claim_id: str,
    exc: BaseException,
    now: datetime | None = None,
) -> DeliveryStatus:
    classification = classify_telegram_send_outcome(exc)
    return mark_send_failure(
        settings,
        telegram_id=telegram_id,
        opportunity_uid=opportunity_uid,
        claim_id=claim_id,
        classification=classification,
        now=now,
    )


def _suppressed_payload_text(
    opportunity_uid: str, reason: OpportunityDiagnosticCode
) -> str:
    del opportunity_uid  # Keep out of user-facing / stored alert text.
    label = reason.value.replace("_", " ").title()
    return f"Opportunity alert suppressed: {label}"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
