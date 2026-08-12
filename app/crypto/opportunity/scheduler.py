"""Idempotent JobQueue registration for crypto opportunity delivery (no poller)."""

from __future__ import annotations

import logging

from app.config.settings import Settings
from app.crypto.opportunity.delivery_worker import process_crypto_opportunity_deliveries

logger = logging.getLogger(__name__)

CRYPTO_DELIVERY_JOBS_REGISTERED_KEY = "crypto_opportunity_delivery_jobs_registered"
CRYPTO_DELIVERY_JOB_NAME = "crypto_opportunity_delivery"


async def crypto_opportunity_delivery_job(context) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not settings.crypto_opportunity_delivery_enabled:
        return
    if not settings.crypto_intel_enabled:
        return

    async def _send(telegram_id: int, text: str) -> None:
        await context.bot.send_message(chat_id=telegram_id, text=text)

    await process_crypto_opportunity_deliveries(settings, send_message=_send)


def schedule_crypto_opportunity_jobs(application, settings: Settings) -> None:
    """Register delivery job once. Does not start a second Telegram poller."""
    bot_data = application.bot_data
    if bot_data.get(CRYPTO_DELIVERY_JOBS_REGISTERED_KEY):
        return
    if not settings.crypto_opportunity_delivery_enabled:
        return
    job_queue = getattr(application, "job_queue", None)
    if job_queue is None:
        logger.warning("Crypto delivery job skipped: no job_queue")
        return
    job_queue.run_repeating(
        crypto_opportunity_delivery_job,
        interval=60,
        first=30,
        name=CRYPTO_DELIVERY_JOB_NAME,
    )
    bot_data[CRYPTO_DELIVERY_JOBS_REGISTERED_KEY] = True
