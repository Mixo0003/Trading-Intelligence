"""Read-only Top Opportunities catalog from persisted scanner data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.config.settings import Settings
from app.database import opportunity_storage
from app.opportunity_intel.filters import PersistedSignalContext, qualify_recipient
from app.opportunity_intel.delivery_gates import OpportunityDeliveryGates
from app.opportunity_intel.models import (
    AlertMode,
    LiveMode,
    ObservationMode,
    OpportunitySignal,
    OpportunitySubscription,
    ScannerStatus,
    SignalStatus,
)
from app.opportunity_intel.payload import DeliveryCardPayload, build_delivery_card_payload
from app.opportunity_intel.settings import (
    NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS,
    OpportunityScannerSettings,
    balanced_settings,
)

TOP_OPPORTUNITIES_LIMIT = 3


@dataclass(frozen=True)
class CatalogEntry:
    signal: OpportunitySignal
    payload: DeliveryCardPayload
    score: Decimal


@dataclass(frozen=True)
class TopOpportunitiesResult:
    status: str
    entries: tuple[CatalogEntry, ...]
    message: str


def resolve_user_settings(
    settings: Settings, telegram_id: int
) -> OpportunityScannerSettings:
    sub = opportunity_storage.load_subscription(settings, telegram_id)
    if sub is None:
        return balanced_settings()
    return sub.settings


def load_top_opportunities(
    settings: Settings,
    *,
    telegram_id: int,
    now: datetime | None = None,
    limit: int = TOP_OPPORTUNITIES_LIMIT,
) -> TopOpportunitiesResult:
    """Return filtered cached opportunities. No HTTP, rescoring, or delivery."""
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = opportunity_storage.load_scanner_state(settings)
    if state is None:
        return TopOpportunitiesResult(
            status="not_initialized",
            entries=(),
            message="Scanner not initialized. No cached opportunities yet.",
        )
    if state.status == ScannerStatus.DISABLED:
        return TopOpportunitiesResult(
            status="disabled",
            entries=(),
            message="Market Opportunity Scanner is disabled.",
        )
    if state.status == ScannerStatus.BASELINING or (
        not state.discovery_complete and state.global_baseline_cycles < 2
    ):
        # Per-asset baseline gates signals; global cycles are diagnostic.
        if state.status == ScannerStatus.BASELINING:
            return TopOpportunitiesResult(
                status="baselining",
                entries=(),
                message="Baseline in progress. Cached results are not ready yet.",
            )
    if state.last_complete_cycle_at is None:
        return TopOpportunitiesResult(
            status="not_initialized",
            entries=(),
            message="Scanner not initialized. No complete cycle yet.",
        )

    age = (clock - state.last_complete_cycle_at.astimezone(timezone.utc)).total_seconds()
    if age > float(NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS) * 30:
        # Bound: treat multi-hour-old cycles as stale for on-demand display.
        return TopOpportunitiesResult(
            status="stale",
            entries=(),
            message="Cached opportunity data is stale and is not shown as current.",
        )

    user_settings = resolve_user_settings(settings, telegram_id)
    # Synthetic subscription for filter evaluation (Top view ignores delivery gates).
    subscription = OpportunitySubscription(
        telegram_id=telegram_id,
        enabled=True,
        settings=OpportunityScannerSettings(
            revision=user_settings.revision,
            minimum_score=user_settings.minimum_score,
            minimum_liquidity_usd=user_settings.minimum_liquidity_usd,
            minimum_24h_volume_usd=user_settings.minimum_24h_volume_usd,
            maximum_relative_spread=user_settings.maximum_relative_spread,
            maximum_absolute_spread=user_settings.maximum_absolute_spread,
            maximum_slippage=user_settings.maximum_slippage,
            paper_allocation_usd=user_settings.paper_allocation_usd,
            categories=user_settings.categories,
            live_mode=user_settings.live_mode,
            cooldown_seconds=user_settings.cooldown_seconds,
            alert_mode=AlertMode.INSTANT,
            quiet_hours=None,
            timezone_name=user_settings.timezone_name,
        ),
    )
    gates = OpportunityDeliveryGates(
        admin_delivery_enabled=True, user_delivery_enabled=True
    )

    candidates: list[CatalogEntry] = []
    for signal in opportunity_storage.list_active_signals(settings, limit=50):
        if signal.status != SignalStatus.ACTIVE:
            continue
        if (
            signal.mode == ObservationMode.LIVE_STRICT
            and user_settings.live_mode != LiveMode.STRICT
        ):
            continue
        bundle = opportunity_storage.load_persisted_observation_bundle(
            settings,
            asset_id=signal.asset_id,
            observed_at=signal.source_observed_at,
        )
        if bundle is None:
            continue
        context = PersistedSignalContext(
            signal=signal,
            canonical_score=signal.score,
            liquidity_usd=bundle.liquidity_usd,
            volume_24h_usd=bundle.volume_24h_usd,
            absolute_spread=bundle.absolute_spread,
            relative_spread=bundle.relative_spread,
            category_ids=bundle.category_ids,
            execution_by_allocation={
                m.allocation_usd: m for m in bundle.execution_metrics
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
        qualification = qualify_recipient(
            subscription=subscription,
            context=context,
            gates=gates,
            is_admin_recipient=True,
            now=clock,
            last_sent_at=None,
        )
        if not qualification.eligible or qualification.allocation_metric is None:
            continue
        metric = qualification.allocation_metric
        if metric.execution_vwap is None or metric.slippage is None:
            continue
        payload = build_delivery_card_payload(
            opportunity_uid=signal.opportunity_uid,
            signal_at=signal.signal_at,
            wave_at=signal.signal_at,
            signal_family=signal.family,
            question=bundle.question,
            outcome=bundle.outcome,
            event_slug=bundle.event_slug,
            mode=signal.mode,
            canonical_score=signal.score,
            reason_codes=signal.reason_codes,
            direction=signal.direction,
            move_5m=bundle.move_5m,
            move_15m=bundle.move_15m,
            move_1h=bundle.move_1h,
            best_bid=bundle.best_bid,
            best_ask=bundle.best_ask,
            midpoint=bundle.midpoint,
            absolute_spread=bundle.absolute_spread,
            relative_spread=bundle.relative_spread,
            allocation_usd=metric.allocation_usd,
            execution_vwap=metric.execution_vwap,
            slippage=metric.slippage,
            executable_ask_depth_usd=bundle.visible_ask_depth_usd,
            volume_24h_usd=bundle.volume_24h_usd,
            liquidity_usd=bundle.liquidity_usd,
            end_at=bundle.end_at,
            source_observed_at=bundle.observed_at,
        )
        candidates.append(
            CatalogEntry(signal=signal, payload=payload, score=signal.score)
        )

    candidates.sort(key=lambda item: item.score, reverse=True)
    selected = tuple(candidates[: max(1, min(limit, TOP_OPPORTUNITIES_LIMIT))])
    if not selected:
        return TopOpportunitiesResult(
            status="empty",
            entries=(),
            message="No qualifying opportunities for your current filters.",
        )
    return TopOpportunitiesResult(
        status="ready",
        entries=selected,
        message=f"Top {len(selected)} cached opportunities",
    )
