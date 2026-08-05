"""Copy scheduler to_thread offload and overlap safety."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.copy_intel.scheduler import (
    COPY_POLL_REGISTERED_KEY,
    _poll_wallets_sync,
    copy_intel_poll_job,
    schedule_copy_intel_jobs,
)
from app.database import copy_storage
from app.database.storage import initialize_database
from tests.conftest import make_test_settings


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "copy_sched.db"),
        copy_intel_enabled=True,
    )
    initialize_database(value)
    return value


def test_job_registered_once():
    settings = make_test_settings(copy_intel_enabled=True)
    app = MagicMock()
    app.bot_data = {}
    app.job_queue = MagicMock()
    schedule_copy_intel_jobs(app, settings)
    assert app.bot_data[COPY_POLL_REGISTERED_KEY] is True
    app.job_queue.run_repeating.assert_called_once()
    schedule_copy_intel_jobs(app, settings)
    app.job_queue.run_repeating.assert_called_once()


def test_disabled_skips_registration():
    from app.copy_intel.scheduler import COPY_POLL_SKIP_REASON_KEY

    settings = make_test_settings(copy_intel_enabled=False)
    app = MagicMock()
    app.bot_data = {}
    app.job_queue = MagicMock()
    schedule_copy_intel_jobs(app, settings)
    app.job_queue.run_repeating.assert_not_called()
    assert "COPY_INTEL_ENABLED is false" in app.bot_data[COPY_POLL_SKIP_REASON_KEY]


def test_menu_registers_copy_jobs_exactly_once():
    """build_telegram_application path must call schedule_copy_intel_jobs once."""
    from app.telegram.menu import build_telegram_application

    settings = make_test_settings(copy_intel_enabled=True, telegram_token="1:test")
    with (
        patch("app.telegram.menu.initialize_database"),
        patch("app.telegram.menu.schedule_report_jobs"),
        patch("app.telegram.menu.schedule_opportunity_jobs"),
        patch("app.telegram.menu.schedule_health_jobs"),
        patch("app.telegram.menu.schedule_copy_intel_jobs") as copy_sched,
        patch("app.telegram.menu._build_application") as build_app,
    ):
        app = MagicMock()
        app.bot_data = {}
        build_app.return_value = app
        build_telegram_application(settings)
        copy_sched.assert_called_once_with(app, settings)


@pytest.mark.asyncio
async def test_overlap_lock_skips_second_poll(settings):
    owned = copy_storage.try_acquire_job_lock(
        settings, job_name="copy_intel_poll", owner="holder", ttl_seconds=120
    )
    assert owned is True
    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    with patch("app.copy_intel.scheduler.asyncio.to_thread") as to_thread:
        await copy_intel_poll_job(context)
        to_thread.assert_not_called()
    copy_storage.release_job_lock(
        settings, job_name="copy_intel_poll", owner="holder"
    )


@pytest.mark.asyncio
async def test_poll_uses_to_thread_for_wallet_body(settings):
    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    context.bot = MagicMock()

    async def _fake_to_thread(fn, *args, **kwargs):
        assert fn is _poll_wallets_sync
        return [], None, 0

    with (
        patch("app.copy_intel.scheduler.asyncio.to_thread", side_effect=_fake_to_thread),
        patch(
            "app.copy_intel.scheduler.deliver_pending_alerts",
            new_callable=AsyncMock,
        ) as deliver,
        patch(
            "app.copy_intel.scheduler.retry_due_alert_deliveries",
            new_callable=AsyncMock,
        ) as retry,
        patch("app.health.jobs.record_job_execution"),
        patch("app.health.jobs.finish_job_execution"),
    ):
        await copy_intel_poll_job(context)
        deliver.assert_awaited_once()
        retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_releases_lock_after_completion(settings):
    context = MagicMock()
    context.application.bot_data = {"settings": settings}

    async def _fake_to_thread(fn, *args, **kwargs):
        return [], None, 0

    with (
        patch("app.copy_intel.scheduler.asyncio.to_thread", side_effect=_fake_to_thread),
        patch(
            "app.copy_intel.scheduler.deliver_pending_alerts",
            new_callable=AsyncMock,
        ),
        patch(
            "app.copy_intel.scheduler.retry_due_alert_deliveries",
            new_callable=AsyncMock,
        ),
        patch("app.health.jobs.record_job_execution"),
        patch("app.health.jobs.finish_job_execution"),
    ):
        await copy_intel_poll_job(context)

    # Lock must be free for a subsequent acquire.
    assert (
        copy_storage.try_acquire_job_lock(
            settings, job_name="copy_intel_poll", owner="next", ttl_seconds=60
        )
        is True
    )
    copy_storage.release_job_lock(settings, job_name="copy_intel_poll", owner="next")


@pytest.mark.asyncio
async def test_event_loop_not_blocked_by_sync_body(settings):
    """While wallet body runs in a thread, the event loop can still schedule work."""
    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    progressed = asyncio.Event()

    def _slow_body(_settings):
        import time

        deadline = time.time() + 0.05
        while time.time() < deadline:
            if progressed.is_set():
                break
            time.sleep(0.005)
        return [], None, 0

    async def _progress():
        await asyncio.sleep(0)
        progressed.set()

    with (
        patch("app.copy_intel.scheduler._poll_wallets_sync", side_effect=_slow_body),
        patch(
            "app.copy_intel.scheduler.deliver_pending_alerts",
            new_callable=AsyncMock,
        ),
        patch(
            "app.copy_intel.scheduler.retry_due_alert_deliveries",
            new_callable=AsyncMock,
        ),
        patch("app.health.jobs.record_job_execution"),
        patch("app.health.jobs.finish_job_execution"),
    ):
        progress_task = asyncio.create_task(_progress())
        await copy_intel_poll_job(context)
        await progress_task
    assert progressed.is_set()
