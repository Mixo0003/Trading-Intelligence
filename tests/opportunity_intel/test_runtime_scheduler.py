"""Checkpoint 7 — scheduler registration and overlap/lock behavior."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database import copy_storage
from app.database.opportunity_storage import initialize_opportunity_schema
from app.database.storage import initialize_database
from app.opportunity_intel.runtime import run_opportunity_scan_cycle
from app.opportunity_intel.scheduler import (
    OPPORTUNITY_SCAN_JOB_NAME,
    OPPORTUNITY_SCAN_REGISTERED_KEY,
    market_opportunity_scan_job,
    schedule_opportunity_jobs,
    shutdown_opportunity_tasks,
)
from tests.conftest import make_test_settings


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "opp_sched.db"),
        opportunity_scanner_enabled=True,
        opportunity_scan_interval_seconds=120,
        opportunity_first_run_delay_seconds=30,
        copy_intel_enabled=True,
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    return value


def test_disabled_means_no_registration():
    settings = make_test_settings(opportunity_scanner_enabled=False)
    app = MagicMock()
    app.bot_data = {}
    app.job_queue = MagicMock()
    schedule_opportunity_jobs(app, settings)
    app.job_queue.run_repeating.assert_not_called()
    assert OPPORTUNITY_SCAN_REGISTERED_KEY not in app.bot_data


def test_idempotent_registration_and_interval(settings):
    app = MagicMock()
    app.bot_data = {}
    app.job_queue = MagicMock()
    schedule_opportunity_jobs(app, settings)
    assert app.bot_data[OPPORTUNITY_SCAN_REGISTERED_KEY] is True
    app.job_queue.run_repeating.assert_called_once()
    kwargs = app.job_queue.run_repeating.call_args.kwargs
    assert kwargs["interval"] == 120
    assert kwargs["first"] == 30
    assert kwargs["name"] == OPPORTUNITY_SCAN_JOB_NAME
    schedule_opportunity_jobs(app, settings)
    app.job_queue.run_repeating.assert_called_once()


def test_db_lock_acquire_release_and_stale_recovery(settings):
    assert copy_storage.try_acquire_job_lock(
        settings, job_name="market_opportunity_scan", owner="a", ttl_seconds=1
    )
    assert not copy_storage.try_acquire_job_lock(
        settings, job_name="market_opportunity_scan", owner="b", ttl_seconds=60
    )
    copy_storage.release_job_lock(
        settings, job_name="market_opportunity_scan", owner="a"
    )
    assert copy_storage.try_acquire_job_lock(
        settings, job_name="market_opportunity_scan", owner="b", ttl_seconds=60
    )
    copy_storage.release_job_lock(
        settings, job_name="market_opportunity_scan", owner="b"
    )
    # Stale lock recovery: force expired row then steal.
    assert copy_storage.try_acquire_job_lock(
        settings, job_name="market_opportunity_scan", owner="stale", ttl_seconds=60
    )
    from app.database.storage import get_connection

    with get_connection(settings) as connection:
        connection.execute(
            """
            UPDATE copy_job_locks
            SET expires_at = ?
            WHERE job_name = ?
            """,
            (
                datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(),
                "market_opportunity_scan",
            ),
        )
    assert copy_storage.try_acquire_job_lock(
        settings, job_name="market_opportunity_scan", owner="fresh", ttl_seconds=60
    )
    copy_storage.release_job_lock(
        settings, job_name="market_opportunity_scan", owner="fresh"
    )


@pytest.mark.asyncio
async def test_overlap_skip_records_and_returns_quickly(settings):
    app = MagicMock()
    app.bot_data = {"settings": settings}
    context = MagicMock()
    context.application = app
    context.bot = MagicMock()
    assert copy_storage.try_acquire_job_lock(
        settings,
        job_name="market_opportunity_scan",
        owner="holder",
        ttl_seconds=120,
    )
    with patch(
        "app.opportunity_intel.scheduler.record_skipped_overlap",
        wraps=__import__(
            "app.opportunity_intel.runtime", fromlist=["record_skipped_overlap"]
        ).record_skipped_overlap,
    ) as skipped:
        await market_opportunity_scan_job(context)
        skipped.assert_called_once()
    copy_storage.release_job_lock(
        settings, job_name="market_opportunity_scan", owner="holder"
    )


@pytest.mark.asyncio
async def test_job_callback_spawns_background_work(settings):
    app = MagicMock()
    app.bot_data = {"settings": settings}
    context = MagicMock()
    context.application = app
    context.bot.send_message = MagicMock()

    async def _fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    done_future = MagicMock()
    done_future.done.return_value = True
    handle = MagicMock()
    handle.future = done_future
    handle.deadline_token = MagicMock()

    start_mock = MagicMock(return_value=handle)
    await_mock = MagicMock(
        return_value=MagicMock(
            enabled=True,
            timed_out=False,
            cycle=MagicMock(storage_ok=True),
        )
    )
    deliver = AsyncMock()

    with patch(
        "app.opportunity_intel.scheduler.has_inflight_cycle",
        return_value=False,
    ), patch(
        "app.opportunity_intel.scheduler.copy_storage.try_acquire_job_lock",
        return_value=True,
    ), patch(
        "app.opportunity_intel.scheduler.copy_storage.release_job_lock",
    ), patch(
        "app.opportunity_intel.scheduler.copy_storage.renew_job_lock",
        return_value=True,
    ), patch(
        "app.opportunity_intel.scheduler.start_managed_cycle",
        start_mock,
    ), patch(
        "app.opportunity_intel.scheduler.await_managed_cycle",
        await_mock,
    ), patch(
        "app.opportunity_intel.scheduler.process_opportunity_deliveries",
        deliver,
    ), patch(
        "app.opportunity_intel.scheduler.asyncio.to_thread",
        side_effect=_fake_to_thread,
    ), patch(
        "app.health.jobs.record_job_execution"
    ), patch(
        "app.health.jobs.finish_job_execution"
    ):
        settings_local = make_test_settings(
            database_path=settings.database_path,
            opportunity_scanner_enabled=True,
            opportunity_auto_alerts_enabled=True,
            copy_intel_enabled=True,
        )
        context.application.bot_data["settings"] = settings_local
        await market_opportunity_scan_job(context)
        pending = set(context.application.bot_data.get("opportunity_scan_background_tasks") or ())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert start_mock.called
        assert await_mock.called
        deliver.assert_awaited()
        assert deliver.await_args.kwargs["fresh_trade_candidate_cards"] is True


@pytest.mark.asyncio
async def test_clean_shutdown_cancels_tasks(settings):
    app = MagicMock()
    app.bot_data = {"settings": settings, "opportunity_scan_background_tasks": set()}

    async def _hang():
        await asyncio.sleep(60)

    task = asyncio.create_task(_hang())
    app.bot_data["opportunity_scan_background_tasks"].add(task)
    await shutdown_opportunity_tasks(app)
    assert task.cancelled() or task.done()


def test_disabled_scan_cycle_is_noop(settings):
    disabled = make_test_settings(
        database_path=settings.database_path,
        opportunity_scanner_enabled=False,
    )
    result = run_opportunity_scan_cycle(disabled)
    assert result.enabled is False
    assert result.cycle is None


def test_deadline_timeout_recorded(settings, monkeypatch):
    def _hang(*_args, **_kwargs):
        import time

        time.sleep(2)
        from app.opportunity_intel.discovery import ResultStatus
        from app.opportunity_intel.scanner import ScannerCycleResult

        return ScannerCycleResult(
            cycle_id="late",
            discovery_status=ResultStatus.COMPLETE,
            markets_discovered=0,
            outcomes_shortlisted=0,
            histories_valid=0,
            histories_failed=0,
            books_valid=0,
            books_failed=0,
            observations_eligible=0,
            observations_rejected=0,
            baseline_advanced=0,
            signals_created=0,
            signals_materially_updated=0,
            signals_expired=0,
            storage_ok=True,
        )

    monkeypatch.setattr(
        "app.opportunity_intel.runtime._execute_cycle",
        _hang,
    )
    from app.opportunity_intel.runtime import (
        ManagedCycleHandle,
        await_managed_cycle,
        has_inflight_cycle,
        start_managed_cycle,
    )

    handle = start_managed_cycle(
        make_test_settings(
            database_path=settings.database_path,
            opportunity_scanner_enabled=True,
            opportunity_cycle_deadline_seconds=1,
        ),
        now=datetime.now(timezone.utc),
    )
    assert isinstance(handle, ManagedCycleHandle)
    result = await_managed_cycle(handle, timeout_seconds=0.4)
    assert result.timed_out is True
    assert result.selection_ran is False
    assert has_inflight_cycle() is True
    handle.future.result(timeout=5)
