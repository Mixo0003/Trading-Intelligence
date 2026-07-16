"""Copy-intel polling scheduler (single JobQueue job)."""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone

from app.config.settings import Settings
from app.copy_intel.data_client import CopyDataClient
from app.copy_intel.ingestion import ingest_wallet
from app.copy_intel.recommendation import build_recommendation
from app.copy_intel.risk import assert_read_only, record_paper_copy
from app.copy_intel.watchlist import list_enabled, list_entries
from app.database import copy_storage

logger = logging.getLogger(__name__)

COPY_POLL_REGISTERED_KEY = "copy_intel_poll_registered"


async def copy_intel_poll_job(context) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not settings.copy_intel_enabled:
        return
    try:
        assert_read_only(settings)
        client = CopyDataClient(settings)
        labels = {e.wallet: (e.label or e.wallet[:10]) for e in list_entries(settings)}
        all_events = []
        last_error = None
        for wallet in list_enabled(settings):
            try:
                events = ingest_wallet(settings, wallet, client=client)
                for event in events:
                    if event.event_type in {"open", "increase"}:
                        rec = build_recommendation(
                            settings,
                            event,
                            trader_label=labels.get(wallet),
                            client=client,
                        )
                        all_events.append(rec)
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
        copy_storage.set_poll_state(settings, "last_event_count", str(len(all_events)))
        if last_error:
            copy_storage.set_poll_state(settings, "last_error", last_error)
        else:
            copy_storage.set_poll_state(settings, "last_error", "")

        if all_events and settings.admin_ids:
            from app.telegram.copy_intel.formatting import format_recommendation_messages

            messages = format_recommendation_messages(all_events)
            for admin_id in settings.admin_ids:
                for text in messages:
                    try:
                        await context.bot.send_message(chat_id=admin_id, text=text)
                    except Exception:
                        logger.exception("Failed to send copy alert to %s", admin_id)
    except Exception:
        copy_storage.set_poll_state(settings, "last_error", traceback.format_exc()[-500:])
        logger.exception("Copy intel poll job failed")


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
