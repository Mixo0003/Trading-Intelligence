"""Market Opportunity Scanner JobQueue registration and tick."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from app.config.settings import Settings
from app.database import copy_storage
from app.opportunity_intel.delivery_worker import process_opportunity_deliveries
from app.opportunity_intel.runtime import (
    CycleRuntimeResult,
    ManagedCycleHandle,
    await_managed_cycle,
    finish_late_managed_cycle,
    has_inflight_cycle,
    record_skipped_overlap,
    start_managed_cycle,
)

logger = logging.getLogger(__name__)

OPPORTUNITY_SCAN_REGISTERED_KEY = "opportunity_scan_registered"
OPPORTUNITY_SCAN_JOB_NAME = "market_opportunity_scan_job"
_OPPORTUNITY_SCAN_LOCK = "market_opportunity_scan"
_SHUTDOWN_TASKS_KEY = "opportunity_scan_background_tasks"
_ACTIVE_HANDLE_KEY = "opportunity_scan_active_handle"


def _track_task(application, task: asyncio.Task) -> None:
    tasks: set[asyncio.Task] = application.bot_data.setdefault(_SHUTDOWN_TASKS_KEY, set())
    tasks.add(task)

    def _done(done: asyncio.Task) -> None:
        tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Opportunity background task failed")

    task.add_done_callback(_done)


def _lock_ttl(settings: Settings) -> int:
    return max(
        60,
        int(settings.opportunity_scan_interval_seconds) * 2,
        int(settings.opportunity_cycle_deadline_seconds) * 2,
    )


async def _renew_lock_loop(
    settings: Settings,
    *,
    owner: str,
    stop: asyncio.Event,
) -> None:
    ttl = _lock_ttl(settings)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(5.0, ttl / 3))
            return
        except asyncio.TimeoutError:
            copy_storage.renew_job_lock(
                settings,
                job_name=_OPPORTUNITY_SCAN_LOCK,
                owner=owner,
                ttl_seconds=ttl,
            )


async def market_opportunity_scan_job(context) -> None:
    """Return quickly; blocking scan/delivery work runs off the event loop."""
    settings: Settings = context.application.bot_data["settings"]
    if not settings.opportunity_scanner_enabled:
        return

    active = context.application.bot_data.get(_ACTIVE_HANDLE_KEY)
    if isinstance(active, ManagedCycleHandle) and not active.future.done():
        logger.info("Opportunity scan skipped: in-flight worker still running")
        record_skipped_overlap(settings, bot_data=context.application.bot_data)
        return
    if has_inflight_cycle():
        logger.info("Opportunity scan skipped: executor still has in-flight cycle")
        record_skipped_overlap(settings, bot_data=context.application.bot_data)
        return

    owner = f"opp-{uuid.uuid4().hex[:12]}"
    lock_ttl = _lock_ttl(settings)
    if not copy_storage.try_acquire_job_lock(
        settings,
        job_name=_OPPORTUNITY_SCAN_LOCK,
        owner=owner,
        ttl_seconds=lock_ttl,
    ):
        logger.info("Opportunity scan skipped: overlap protection")
        record_skipped_overlap(settings, bot_data=context.application.bot_data)
        return

    from app.health.jobs import finish_job_execution, record_job_execution

    job_started = time.monotonic()
    record_job_execution(
        settings,
        job_name=OPPORTUNITY_SCAN_JOB_NAME,
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        expected_interval_seconds=settings.opportunity_scan_interval_seconds,
    )

    async def _work() -> None:
        stop_renew = asyncio.Event()
        renew_task = asyncio.create_task(
            _renew_lock_loop(settings, owner=owner, stop=stop_renew),
            name="opportunity_scan_lock_renew",
        )
        handle: ManagedCycleHandle | None = None
        lock_released = False
        try:
            started = await asyncio.to_thread(
                start_managed_cycle,
                settings,
                now=datetime.now(timezone.utc),
                bot_data=context.application.bot_data,
            )
            if isinstance(started, CycleRuntimeResult):
                finish_job_execution(
                    settings,
                    job_name=OPPORTUNITY_SCAN_JOB_NAME,
                    status="ok",
                    duration_ms=int((time.monotonic() - job_started) * 1000),
                )
                return
            handle = started
            context.application.bot_data[_ACTIVE_HANDLE_KEY] = handle

            result = await asyncio.to_thread(await_managed_cycle, handle)
            if result.timed_out and not handle.future.done():
                def _on_late(_fut) -> None:
                    try:
                        finish_late_managed_cycle(handle)
                    finally:
                        copy_storage.release_job_lock(
                            settings, job_name=_OPPORTUNITY_SCAN_LOCK, owner=owner
                        )
                        if context.application.bot_data.get(_ACTIVE_HANDLE_KEY) is handle:
                            context.application.bot_data[_ACTIVE_HANDLE_KEY] = None

                handle.future.add_done_callback(_on_late)
                finish_job_execution(
                    settings,
                    job_name=OPPORTUNITY_SCAN_JOB_NAME,
                    status="error",
                    duration_ms=int((time.monotonic() - job_started) * 1000),
                    error="cycle_deadline",
                )
                # Late callback owns lock release.
                lock_released = True
                handle = None
                return

            if (
                settings.opportunity_auto_alerts_enabled
                and result.enabled
                and not result.timed_out
                and result.cycle is not None
                and result.cycle.storage_ok
            ):
                await process_opportunity_deliveries(
                    settings,
                    send_message=context.bot.send_message,
                    now=datetime.now(timezone.utc),
                )
            finish_job_execution(
                settings,
                job_name=OPPORTUNITY_SCAN_JOB_NAME,
                status="ok" if not result.timed_out else "error",
                duration_ms=int((time.monotonic() - job_started) * 1000),
                error="cycle_deadline" if result.timed_out else None,
            )
        except Exception as exc:
            logger.exception("Opportunity scan job failed")
            finish_job_execution(
                settings,
                job_name=OPPORTUNITY_SCAN_JOB_NAME,
                status="error",
                duration_ms=int((time.monotonic() - job_started) * 1000),
                error=str(type(exc).__name__)[:120],
            )
        finally:
            stop_renew.set()
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass
            if lock_released:
                pass
            elif handle is not None and not handle.future.done():
                def _release(_fut) -> None:
                    copy_storage.release_job_lock(
                        settings, job_name=_OPPORTUNITY_SCAN_LOCK, owner=owner
                    )
                    if context.application.bot_data.get(_ACTIVE_HANDLE_KEY) is handle:
                        context.application.bot_data[_ACTIVE_HANDLE_KEY] = None

                handle.future.add_done_callback(_release)
            else:
                copy_storage.release_job_lock(
                    settings, job_name=_OPPORTUNITY_SCAN_LOCK, owner=owner
                )
                if handle is not None and (
                    context.application.bot_data.get(_ACTIVE_HANDLE_KEY) is handle
                ):
                    context.application.bot_data[_ACTIVE_HANDLE_KEY] = None

    task = asyncio.create_task(_work(), name=OPPORTUNITY_SCAN_JOB_NAME)
    _track_task(context.application, task)


def schedule_opportunity_jobs(application, settings: Settings) -> None:
    """Idempotent registration. Disabled scanner registers nothing."""
    if not settings.opportunity_scanner_enabled:
        logger.info("Opportunity scanner disabled; scan job not registered")
        return
    if application.bot_data.get(OPPORTUNITY_SCAN_REGISTERED_KEY):
        logger.info("Opportunity scan job already registered; skipping.")
        return
    if application.job_queue is None:
        logger.warning("Job queue unavailable; opportunity scanner disabled.")
        return

    interval = int(settings.opportunity_scan_interval_seconds)
    first = int(settings.opportunity_first_run_delay_seconds)
    application.job_queue.run_repeating(
        market_opportunity_scan_job,
        interval=interval,
        first=first,
        name=OPPORTUNITY_SCAN_JOB_NAME,
    )
    application.bot_data[OPPORTUNITY_SCAN_REGISTERED_KEY] = True
    application.bot_data.setdefault(_SHUTDOWN_TASKS_KEY, set())
    logger.info(
        "Opportunity scan job scheduled every %ss (first=%ss)",
        interval,
        first,
    )


async def shutdown_opportunity_tasks(application) -> None:
    """Cancel awaiting tasks; track non-cancellable executor futures safely."""
    tasks: set[asyncio.Task] = set(application.bot_data.get(_SHUTDOWN_TASKS_KEY) or ())
    if tasks:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        application.bot_data[_SHUTDOWN_TASKS_KEY] = set()
    handle = application.bot_data.get(_ACTIVE_HANDLE_KEY)
    if isinstance(handle, ManagedCycleHandle) and not handle.future.done():
        handle.deadline_token.expire()
        logger.info(
            "Shutdown: opportunity executor future still running; token expired"
        )
