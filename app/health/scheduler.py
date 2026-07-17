"""Health monitor JobQueue registration."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from app.config.settings import Settings
from app.health.alerts import dispatch_health_alerts
from app.health.jobs import finish_job_execution, record_job_execution
from app.health.monitor import run_health_check

logger = logging.getLogger(__name__)

HEALTH_POLL_REGISTERED_KEY = "health_monitor_poll_registered"


async def health_monitor_tick(context) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not settings.health_monitor_enabled:
        return
    started = time.monotonic()
    record_job_execution(
        settings,
        job_name="health_monitor_tick",
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        expected_interval_seconds=settings.health_check_interval_seconds,
    )
    try:
        overall = run_health_check(
            settings,
            bot_data=context.application.bot_data,
        )
        context.application.bot_data["health_last_overall"] = overall.status
        context.application.bot_data["health_last_checked_at"] = overall.checked_at

        async def _send(admin_id: int, text: str) -> None:
            await context.bot.send_message(chat_id=admin_id, text=text)

        await dispatch_health_alerts(settings, overall, _send)
        finish_job_execution(
            settings,
            job_name="health_monitor_tick",
            status="ok",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        logger.exception("Health monitor tick failed")
        finish_job_execution(
            settings,
            job_name="health_monitor_tick",
            status="error",
            duration_ms=int((time.monotonic() - started) * 1000),
            error=str(exc)[:500],
        )


def schedule_health_jobs(application, settings: Settings) -> None:
    if not settings.health_monitor_enabled:
        return
    if application.bot_data.get(HEALTH_POLL_REGISTERED_KEY):
        logger.info("Health monitor job already registered; skipping.")
        return
    if application.job_queue is None:
        logger.warning("Job queue unavailable; health monitor disabled.")
        return
    application.job_queue.run_repeating(
        health_monitor_tick,
        interval=settings.health_check_interval_seconds,
        first=15,
        name="health_monitor_tick",
    )
    application.bot_data[HEALTH_POLL_REGISTERED_KEY] = True
    logger.info(
        "Health monitor scheduled every %ss",
        settings.health_check_interval_seconds,
    )
