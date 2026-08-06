"""JobQueue registration for Market Opportunity Intelligence (idle when disabled)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config.settings import Settings
from app.market_opportunity.detectors.registry import enabled_detectors
from app.market_opportunity.paper import assert_paper_only
from app.market_opportunity.settings import module_enabled

logger = logging.getLogger(__name__)

MOI_JOBS_REGISTERED_KEY = "market_opportunity_jobs_registered"
MOI_IDLE_JOB_NAME = "market_opportunity_idle"


async def market_opportunity_idle_job(context) -> None:
    """
    Idle heartbeat when module is enabled.
    Never calls detectors, never performs external HTTP, never sends alerts.
    """
    settings: Settings = context.application.bot_data["settings"]
    if not module_enabled(settings):
        return
    assert_paper_only(settings)
    if enabled_detectors():
        logger.error(
            "MOI idle job refused: enabled detectors present in V4.1 foundation"
        )
        return
    try:
        from app.database import market_opportunity_storage as moi_storage

        moi_storage.upsert_runtime_status(
            settings,
            status="idle",
            detail="no live detectors; no external HTTP",
            checked_at=datetime.now(timezone.utc),
        )
    except Exception:
        logger.debug("MOI runtime status update skipped", exc_info=True)


def schedule_market_opportunity_jobs(application, settings: Settings) -> None:
    """Idempotent. Disabled module registers nothing."""
    if not module_enabled(settings):
        logger.info("Market Opportunity Intelligence disabled; jobs not registered")
        return
    if application.bot_data.get(MOI_JOBS_REGISTERED_KEY):
        logger.info("Market Opportunity jobs already registered; skipping")
        return
    if application.job_queue is None:
        logger.warning("Job queue unavailable; Market Opportunity jobs not scheduled")
        return
    # Enabled but V4.1 has no live detectors: idle heartbeat only.
    application.job_queue.run_repeating(
        market_opportunity_idle_job,
        interval=3600,
        first=60,
        name=MOI_IDLE_JOB_NAME,
    )
    application.bot_data[MOI_JOBS_REGISTERED_KEY] = True
    logger.info("Market Opportunity idle job scheduled (no detectors, no HTTP)")
