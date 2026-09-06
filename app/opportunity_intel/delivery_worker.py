"""Bounded delivery worker for Market Opportunity Scanner alerts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config.settings import Settings
from app.database import opportunity_storage
from app.opportunity_intel.delivery import (
    MAX_SEND_ATTEMPTS,
    AuthorizedSendReady,
    AuthorizedSendSuppressed,
    mark_send_failure,
    mark_send_success,
    prepare_authorized_send,
)
from app.opportunity_intel.models import DeliveryStatus, OpportunityDiagnosticCode
from app.opportunity_intel.telegram_errors import (
    TelegramSendClassification,
    classify_telegram_send_outcome,
)
from app.opportunity_intel.versions import (
    DELIVERY_TELEGRAM_CONCURRENCY,
    DELIVERY_WORKER_BATCH_SIZE,
)

logger = logging.getLogger(__name__)

TelegramSendFn = Callable[..., Awaitable[Any]]


class FreshTradeCandidateUnavailable(RuntimeError):
    """Sanitized fail-closed result for a fresh pre-send analysis."""

    def __init__(self, code) -> None:
        super().__init__("fresh trade-candidate analysis unavailable")
        self.code = code


@dataclass(frozen=True)
class DeliveryWorkerResult:
    processed: int = 0
    sent: int = 0
    retry_wait: int = 0
    suppressed: int = 0
    dead_letter: int = 0
    skipped: int = 0
    failure_categories: dict[str, int] = field(default_factory=dict)


async def process_opportunity_deliveries(
    settings: Settings,
    *,
    send_message: TelegramSendFn,
    now: datetime | None = None,
    batch_size: int = DELIVERY_WORKER_BATCH_SIZE,
    concurrency: int = DELIVERY_TELEGRAM_CONCURRENCY,
    auto_alerts_enabled: bool | None = None,
    fresh_trade_candidate_cards: bool = False,
) -> DeliveryWorkerResult:
    """Process due delivery rows with atomic claim + auth recheck.

    Never starts real Telegram traffic unless ``send_message`` is provided by
    the caller. When automatic alerts are disabled, returns without sending.
    """
    from app.opportunity_intel.scanner_control import auto_alerts_armed

    alerts_on = (
        auto_alerts_armed(settings)
        if auto_alerts_enabled is None
        else bool(auto_alerts_enabled)
    )
    if not alerts_on:
        return DeliveryWorkerResult()

    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    batch = opportunity_storage.list_delivery_worker_batch(
        settings,
        now=clock,
        limit=min(int(batch_size), DELIVERY_WORKER_BATCH_SIZE),
        max_attempts=MAX_SEND_ATTEMPTS,
    )
    if not batch:
        return DeliveryWorkerResult()

    sem = asyncio.Semaphore(max(1, min(int(concurrency), DELIVERY_TELEGRAM_CONCURRENCY)))
    counters = {
        "processed": 0,
        "sent": 0,
        "retry_wait": 0,
        "suppressed": 0,
        "dead_letter": 0,
        "skipped": 0,
    }
    failure_categories: dict[str, int] = {}
    lock = asyncio.Lock()

    async def _one(telegram_id: int, opportunity_uid: str) -> None:
        async with sem:
            prepared = await asyncio.to_thread(
                prepare_authorized_send,
                settings,
                telegram_id=telegram_id,
                opportunity_uid=opportunity_uid,
                now=clock,
            )
            async with lock:
                counters["processed"] += 1
            if isinstance(prepared, AuthorizedSendSuppressed):
                async with lock:
                    counters["suppressed"] += 1
                    code = (
                        prepared.reason.value
                        if prepared.reason is not None
                        else "suppressed"
                    )
                    failure_categories[code] = failure_categories.get(code, 0) + 1
                return
            if not isinstance(prepared, AuthorizedSendReady):
                async with lock:
                    counters["skipped"] += 1
                return

            delivery = prepared.delivery
            payload_text = delivery.payload_text
            if fresh_trade_candidate_cards:
                try:
                    payload_text = await asyncio.to_thread(
                        render_fresh_trade_candidate_card,
                        settings,
                        telegram_id=delivery.telegram_id,
                        opportunity_uid=delivery.opportunity_uid,
                        now=clock,
                    )
                except FreshTradeCandidateUnavailable as exc:
                    status = await asyncio.to_thread(
                        mark_send_failure,
                        settings,
                        telegram_id=delivery.telegram_id,
                        opportunity_uid=delivery.opportunity_uid,
                        claim_id=prepared.claim_id,
                        classification=TelegramSendClassification(
                            code=exc.code,
                            retryable=True,
                        ),
                        now=clock,
                    )
                    async with lock:
                        if status == DeliveryStatus.RETRY_WAIT:
                            counters["retry_wait"] += 1
                        else:
                            counters["dead_letter"] += 1
                        failure_categories[exc.code.value] = (
                            failure_categories.get(exc.code.value, 0) + 1
                        )
                    return
                except Exception:
                    logger.exception("Fresh trade-candidate rendering failed")
                    status = await asyncio.to_thread(
                        mark_send_failure,
                        settings,
                        telegram_id=delivery.telegram_id,
                        opportunity_uid=delivery.opportunity_uid,
                        claim_id=prepared.claim_id,
                        classification=TelegramSendClassification(
                            code=OpportunityDiagnosticCode.DELIVERY_FAILURE,
                            retryable=True,
                        ),
                        now=clock,
                    )
                    async with lock:
                        if status == DeliveryStatus.RETRY_WAIT:
                            counters["retry_wait"] += 1
                        else:
                            counters["dead_letter"] += 1
                        failure_categories["fresh_card_render"] = (
                            failure_categories.get("fresh_card_render", 0) + 1
                        )
                    return
            try:
                await send_message(
                    chat_id=delivery.telegram_id,
                    text=payload_text,
                )
            except Exception as exc:
                classification = classify_telegram_send_outcome(exc)
                status = await asyncio.to_thread(
                    mark_send_failure,
                    settings,
                    telegram_id=delivery.telegram_id,
                    opportunity_uid=delivery.opportunity_uid,
                    claim_id=prepared.claim_id,
                    classification=classification,
                    now=clock,
                )
                async with lock:
                    if status == DeliveryStatus.RETRY_WAIT:
                        counters["retry_wait"] += 1
                    else:
                        counters["dead_letter"] += 1
                    failure_categories[classification.code.value] = (
                        failure_categories.get(classification.code.value, 0) + 1
                    )
                return

            ok = await asyncio.to_thread(
                mark_send_success,
                settings,
                telegram_id=delivery.telegram_id,
                opportunity_uid=delivery.opportunity_uid,
                claim_id=prepared.claim_id,
            )
            async with lock:
                if ok:
                    counters["sent"] += 1
                else:
                    counters["skipped"] += 1
                    failure_categories["mark_sent_failed"] = (
                        failure_categories.get("mark_sent_failed", 0) + 1
                    )

    await asyncio.gather(
        *(_one(row.telegram_id, row.opportunity_uid) for row in batch)
    )
    return DeliveryWorkerResult(
        processed=counters["processed"],
        sent=counters["sent"],
        retry_wait=counters["retry_wait"],
        suppressed=counters["suppressed"],
        dead_letter=counters["dead_letter"],
        skipped=counters["skipped"],
        failure_categories=dict(failure_categories),
    )


def render_fresh_trade_candidate_card(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_uid: str,
    now: datetime,
) -> str:
    """Refresh Gamma, history, and both sides of the book before Telegram send."""
    from app.opportunity_intel.live_trade_analysis import (
        LiveTradeAnalysisFailure,
        analyze_live_trade_snapshot,
        fetch_live_trade_snapshot,
    )
    from app.opportunity_intel.models import OpportunityDiagnosticCode
    from app.telegram.opportunity_intel.formatting import format_trade_candidate_card

    signal = opportunity_storage.load_signal(settings, opportunity_uid)
    subscription = opportunity_storage.load_subscription(settings, telegram_id)
    if signal is None or subscription is None:
        raise FreshTradeCandidateUnavailable(
            OpportunityDiagnosticCode.AUTHORIZATION_REVOKED
        )
    bundle = opportunity_storage.load_persisted_observation_bundle(
        settings,
        asset_id=signal.asset_id,
        observed_at=signal.source_observed_at,
    )
    if bundle is None:
        raise FreshTradeCandidateUnavailable(
            OpportunityDiagnosticCode.LIVE_DATA_INCONSISTENT
        )
    snapshot = fetch_live_trade_snapshot(
        settings,
        signal=signal,
        bundle=bundle,
        now=now,
    )
    if isinstance(snapshot, LiveTradeAnalysisFailure):
        raise FreshTradeCandidateUnavailable(snapshot.diagnostic_code)
    analysis = analyze_live_trade_snapshot(
        signal=signal,
        bundle=bundle,
        snapshot=snapshot,
        allocation_usd=subscription.settings.paper_allocation_usd,
        maximum_slippage=subscription.settings.maximum_slippage,
        now=now,
    )
    if isinstance(analysis, LiveTradeAnalysisFailure):
        raise FreshTradeCandidateUnavailable(analysis.diagnostic_code)
    return format_trade_candidate_card(analysis.payload, analysis.decision)
