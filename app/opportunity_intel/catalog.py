"""Top Opportunities catalog from persisted scanner data + live eligibility."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

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
    OpportunityScannerSettings,
    balanced_settings,
)
from app.opportunity_intel.top_eligibility import (
    TOP_DISPLAY_FRESHNESS_SECONDS,
    active_catalog_asset_ids,
    evaluate_top_opportunity_eligibility,
)
from app.polymarket.client import PolymarketClient

TOP_OPPORTUNITIES_LIMIT = 3

DISCOVERING_MESSAGE = "Scanner is building the complete market catalog."
BASELINING_MESSAGE = (
    "Market catalog is ready. Baseline observations are still being built."
)
READY_EMPTY_MESSAGE = "Scanner is ready. No markets currently pass your filters."


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
    gamma_client: Any | None = None,
) -> TopOpportunitiesResult:
    """Return live-eligible Top Opportunities from persisted signals.

    Applies a final fail-closed live eligibility filter before formatting.
    Does not rescore, authorize delivery, or touch Signal History.
    """
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = opportunity_storage.load_scanner_state(settings)
    if state is None:
        progress = opportunity_storage.load_discovery_progress(settings)
        from app.opportunity_intel.scanner_control import scanner_armed

        if progress is not None or scanner_armed(settings):
            return TopOpportunitiesResult(
                status="discovering",
                entries=(),
                message=DISCOVERING_MESSAGE,
            )
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
    if not state.discovery_complete or state.last_complete_discovery_at is None:
        # Prefer persisted ACTIVE catalog when scanner_state lags a restart.
        active = opportunity_storage.load_active_catalog_version(settings)
        if active is None:
            return TopOpportunitiesResult(
                status="discovering",
                entries=(),
                message=DISCOVERING_MESSAGE,
            )
    if opportunity_storage.count_baseline_ready_assets(settings) == 0:
        return TopOpportunitiesResult(
            status="baselining",
            entries=(),
            message=BASELINING_MESSAGE,
        )
    if state.last_complete_cycle_at is None:
        return TopOpportunitiesResult(
            status="not_initialized",
            entries=(),
            message="Scanner not initialized. No complete cycle yet.",
        )

    age = (clock - state.last_complete_cycle_at.astimezone(timezone.utc)).total_seconds()
    if age > TOP_DISPLAY_FRESHNESS_SECONDS:
        # Bound: treat multi-hour-old cycles as stale for on-demand display.
        return TopOpportunitiesResult(
            status="stale",
            entries=(),
            message="Cached opportunity data is stale and is not shown as current.",
        )

    active_catalog = opportunity_storage.load_active_catalog(settings)
    if active_catalog is None:
        return TopOpportunitiesResult(
            status="discovering",
            entries=(),
            message=DISCOVERING_MESSAGE,
        )
    catalog_asset_ids = active_catalog_asset_ids(active_catalog[1])
    client = gamma_client if gamma_client is not None else PolymarketClient(settings)

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

    ranked: list[tuple[OpportunitySignal, Any, Any]] = []
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
        ranked.append((signal, bundle, metric))

    ranked.sort(key=lambda item: item[0].score, reverse=True)
    max_entries = max(1, min(limit, TOP_OPPORTUNITIES_LIMIT))
    selected: list[CatalogEntry] = []
    for signal, bundle, metric in ranked:
        eligibility = evaluate_top_opportunity_eligibility(
            client,
            signal=signal,
            bundle=bundle,
            catalog_asset_ids=catalog_asset_ids,
            now=clock,
        )
        if not eligibility.eligible:
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
        selected.append(
            CatalogEntry(signal=signal, payload=payload, score=signal.score)
        )
        if len(selected) >= max_entries:
            break

    if not selected:
        return TopOpportunitiesResult(
            status="empty",
            entries=(),
            message=READY_EMPTY_MESSAGE,
        )
    return TopOpportunitiesResult(
        status="ready",
        entries=tuple(selected),
        message=f"Top {len(selected)} cached opportunities",
    )
