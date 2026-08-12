"""Crypto opportunity delivery worker (uses existing bot send_message; no new poller)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from app.config.settings import Settings
from app.crypto.opportunity.delivery import recheck_delivery_authorization
from app.database import crypto_opportunity_storage as storage

logger = logging.getLogger(__name__)

MAX_SEND_ATTEMPTS = 3
RETRY_DELAY_SECONDS = {1: 60, 2: 300, 3: 900}

SendMessage = Callable[[int, str], Awaitable[None]]


async def process_crypto_opportunity_deliveries(
    settings: Settings,
    *,
    send_message: SendMessage,
    limit: int = 20,
) -> int:
    """Process pending crypto deliveries. Abort when global delivery flag is off."""
    if not getattr(settings, "crypto_opportunity_delivery_enabled", False):
        return 0

    batch = storage.list_pending_deliveries(settings, limit=limit)
    sent = 0
    for row in batch:
        telegram_id = int(row["telegram_id"])
        opportunity_uid = str(row["opportunity_uid"])
        opportunity_key = str(row["opportunity_key"])
        attempts = int(row["attempts"])
        ok, reason = recheck_delivery_authorization(
            settings,
            telegram_id=telegram_id,
            opportunity_key=opportunity_key,
        )
        if not ok:
            storage.mark_delivery_status(
                settings,
                telegram_id=telegram_id,
                opportunity_uid=opportunity_uid,
                status="SUPPRESSED",
                last_error=reason,
            )
            continue
        try:
            await send_message(telegram_id, str(row["payload_text"]))
        except Exception as exc:  # noqa: BLE001 — classify retry vs dead letter
            attempts += 1
            if attempts >= MAX_SEND_ATTEMPTS:
                storage.mark_delivery_status(
                    settings,
                    telegram_id=telegram_id,
                    opportunity_uid=opportunity_uid,
                    status="DEAD_LETTER",
                    attempts=attempts,
                    last_error=type(exc).__name__,
                )
            else:
                delay = RETRY_DELAY_SECONDS.get(attempts, 900)
                nxt = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat()
                storage.mark_delivery_status(
                    settings,
                    telegram_id=telegram_id,
                    opportunity_uid=opportunity_uid,
                    status="RETRY_WAIT",
                    attempts=attempts,
                    last_error=type(exc).__name__,
                    next_attempt_at=nxt,
                )
            logger.warning(
                "Crypto delivery failed for %s: %s",
                opportunity_uid,
                type(exc).__name__,
            )
            continue

        storage.mark_delivery_status(
            settings,
            telegram_id=telegram_id,
            opportunity_uid=opportunity_uid,
            status="SENT",
            attempts=attempts + 1,
        )
        storage.record_sent(
            settings,
            telegram_id=telegram_id,
            opportunity_key=opportunity_key,
            opportunity_uid=opportunity_uid,
        )
        sent += 1
    return sent
