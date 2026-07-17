"""Copy-intel polling scheduler (single JobQueue job)."""

from __future__ import annotations

import logging
import time
import traceback
import uuid
from datetime import datetime, timezone

from app.config.settings import Settings
from app.copy_intel.data_client import CopyDataClient
from app.copy_intel.event_identity import build_event_uid
from app.copy_intel.freshness import (
    baseline_is_complete,
    ensure_baseline_current,
    filter_alertable_events,
    run_baseline,
)
from app.copy_intel.ingestion import ingest_wallet
from app.copy_intel.recommendation import build_recommendation
from app.copy_intel.risk import assert_read_only, record_paper_copy
from app.copy_intel.watchlist import list_enabled, list_entries
from app.database import copy_storage
from app.telegram.copy_intel.formatting import format_recommendation_card

logger = logging.getLogger(__name__)

COPY_POLL_REGISTERED_KEY = "copy_intel_poll_registered"
_COPY_POLL_LOCK = "copy_intel_poll"


async def copy_intel_poll_job(context) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not settings.copy_intel_enabled:
        return
    job_started = time.monotonic()
    owner = f"poll-{uuid.uuid4().hex[:12]}"
    # Prevent overlapping poll workers from double-sending.
    lock_ttl = max(60, settings.copy_poll_interval_seconds * 2)
    if not copy_storage.try_acquire_job_lock(
        settings,
        job_name=_COPY_POLL_LOCK,
        owner=owner,
        ttl_seconds=lock_ttl,
    ):
        logger.info("Copy intel poll skipped: another poll is still running")
        return

    from app.health.jobs import finish_job_execution, record_job_execution

    record_job_execution(
        settings,
        job_name="copy_intel_poll",
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        expected_interval_seconds=settings.copy_poll_interval_seconds,
    )

    try:
        assert_read_only(settings)
        client = CopyDataClient(settings)
        labels = {e.wallet: (e.label or e.wallet[:10]) for e in list_entries(settings)}
        # (event_uid, wallet, recommendation) claimed at wallet level
        pending_recs: list[tuple[str, str, object]] = []
        last_error = None
        stale_rejects = 0
        for wallet in list_enabled(settings):
            try:
                # Outdated/missing baseline_version → MIGRATING; first cycle is baseline-only.
                ensure_baseline_current(settings, wallet)
                if not baseline_is_complete(settings, wallet):
                    status = run_baseline(settings, wallet, client=client)
                    copy_storage.touch_wallet_last_polled(settings, wallet)
                    if status != "COMPLETE":
                        last_error = f"baseline {status} for {wallet[:10]}"
                    continue

                events = ingest_wallet(settings, wallet, client=client)
                copy_storage.touch_wallet_last_polled(settings, wallet)
                alertable = filter_alertable_events(settings, wallet, events)
                stale_rejects += max(0, len(events) - len(alertable))
                for event in alertable:
                    if event.event_type in {"open", "increase"}:
                        rec = build_recommendation(
                            settings,
                            event,
                            trader_label=labels.get(wallet),
                            client=client,
                        )
                        uid = event.event_uid or build_event_uid(event)
                        pending_recs.append((uid, wallet, rec))
                        if rec.action == "COPY" and rec.max_alloc > 0:
                            record_paper_copy(
                                settings,
                                wallet=rec.wallet,
                                condition_id=event.condition_id or "",
                                market_slug=rec.market_slug,
                                outcome=rec.outcome,
                                notional=rec.max_alloc,
                                entry_price=rec.current_price or rec.entry_price or 0.5,
                            )
            except Exception as exc:
                last_error = str(exc)
                logger.exception("Copy intel poll failed for %s", wallet)
        copy_storage.set_poll_state(
            settings,
            "last_poll_at",
            datetime.now(timezone.utc).isoformat(),
        )
        copy_storage.set_poll_state(settings, "last_event_count", str(len(pending_recs)))
        copy_storage.set_poll_state(settings, "last_stale_rejects", str(stale_rejects))
        if pending_recs:
            copy_storage.set_poll_state(
                settings,
                "last_new_event_at",
                datetime.now(timezone.utc).isoformat(),
            )
        if last_error:
            copy_storage.set_poll_state(settings, "last_error", last_error)
        else:
            copy_storage.set_poll_state(settings, "last_error", "")

        await _deliver_pending_alerts(context, settings, pending_recs)
        await _retry_failed_alert_deliveries(context, settings)

        finish_job_execution(
            settings,
            job_name="copy_intel_poll",
            status="ok",
            duration_ms=int((time.monotonic() - job_started) * 1000),
        )
    except Exception:
        copy_storage.set_poll_state(settings, "last_error", traceback.format_exc()[-500:])
        logger.exception("Copy intel poll job failed")
        finish_job_execution(
            settings,
            job_name="copy_intel_poll",
            status="error",
            duration_ms=int((time.monotonic() - job_started) * 1000),
            error=traceback.format_exc()[-500:],
        )
    finally:
        copy_storage.release_job_lock(settings, job_name=_COPY_POLL_LOCK, owner=owner)


async def _deliver_pending_alerts(
    context,
    settings: Settings,
    pending_recs: list[tuple[str, str, object]],
) -> None:
    if not pending_recs or not settings.admin_ids:
        return
    # Group cards by subscriber after per-subscriber atomic claim.
    for admin_id in settings.admin_ids:
        claimed_cards: list[str] = []
        claimed_keys: list[tuple[str, str]] = []
        for event_uid, wallet, rec in pending_recs:
            card = format_recommendation_card(rec)  # type: ignore[arg-type]
            if not copy_storage.claim_copy_alert_delivery(
                settings,
                subscriber_id=admin_id,
                wallet=wallet,
                event_uid=event_uid,
                payload_text=card,
            ):
                continue
            claimed_cards.append(card)
            claimed_keys.append((wallet, event_uid))
        if not claimed_cards:
            continue
        # Rebuild message batches from claimed cards only.
        messages = _cards_to_messages(claimed_cards)
        send_ok = True
        for text in messages:
            try:
                await context.bot.send_message(chat_id=admin_id, text=text)
            except Exception:
                send_ok = False
                logger.exception("Failed to send copy alert to %s", admin_id)
                break
        status = "sent" if send_ok else "failed"
        for wallet, event_uid in claimed_keys:
            copy_storage.mark_copy_alert_delivery(
                settings,
                subscriber_id=admin_id,
                wallet=wallet,
                event_uid=event_uid,
                status=status,
            )


async def _retry_failed_alert_deliveries(context, settings: Settings) -> None:
    if not settings.admin_ids:
        return
    rows = copy_storage.list_failed_copy_alert_deliveries(settings, limit=50)
    for row in rows:
        subscriber_id = int(row["subscriber_id"])
        wallet = row["wallet"]
        event_uid = row["event_uid"]
        payload = row["payload_text"]
        if not payload:
            continue
        if not copy_storage.claim_copy_alert_delivery(
            settings,
            subscriber_id=subscriber_id,
            wallet=wallet,
            event_uid=event_uid,
            payload_text=payload,
        ):
            continue
        try:
            await context.bot.send_message(chat_id=subscriber_id, text=payload)
            copy_storage.mark_copy_alert_delivery(
                settings,
                subscriber_id=subscriber_id,
                wallet=wallet,
                event_uid=event_uid,
                status="sent",
            )
        except Exception:
            logger.exception("Retry send failed for subscriber %s", subscriber_id)
            copy_storage.mark_copy_alert_delivery(
                settings,
                subscriber_id=subscriber_id,
                wallet=wallet,
                event_uid=event_uid,
                status="failed",
            )


def _cards_to_messages(cards: list[str]) -> list[str]:
    from app.telegram.copy_intel.formatting import TELEGRAM_MAX_MESSAGE_LENGTH

    if not cards:
        return []
    chunks: list[str] = []
    current = "Copy Trading Alerts\n"
    for card in cards:
        piece = card + "\n\n"
        if len(current) + len(piece) > TELEGRAM_MAX_MESSAGE_LENGTH:
            chunks.append(current.rstrip())
            current = "Copy Trading Alerts\n" + piece
        else:
            current += piece
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def schedule_copy_intel_jobs(application, settings: Settings) -> None:
    if not settings.copy_intel_enabled:
        return
    if application.bot_data.get(COPY_POLL_REGISTERED_KEY):
        logger.info("Copy intel poll job already registered; skipping.")
        return
    if application.job_queue is None:
        logger.warning("Job queue unavailable; copy intel polling disabled.")
        return
    interval = settings.copy_poll_interval_seconds
    application.job_queue.run_repeating(
        copy_intel_poll_job,
        interval=interval,
        first=10,
        name="copy_intel_poll",
    )
    application.bot_data[COPY_POLL_REGISTERED_KEY] = True
    logger.info("Copy intel poll job scheduled every %ss", interval)
