"""Copy status clarity + discovery async/diagnostics regressions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.copy_intel.discovery import (
    DIAG_API_UNAVAILABLE,
    DIAG_REJECTED_BY_SCORING,
    DiscoveryError,
    DiscoveryRunResult,
    format_discovery_diagnostic,
)
from app.copy_intel.scheduler import COPY_POLL_REGISTERED_KEY, COPY_POLL_SKIP_REASON_KEY
from app.database.storage import initialize_database
from app.telegram.copy_intel.formatting import format_copy_status
from app.telegram.copy_intel.handlers import _ensure_discovery, show_discovery_list
from tests.conftest import make_test_settings


def test_status_shows_polling_disabled_when_flag_false():
    settings = make_test_settings(copy_intel_enabled=False)
    text = format_copy_status(
        settings,
        {},
        scheduler_registered=False,
        scheduler_skip_reason="COPY_INTEL_ENABLED is false",
    )
    assert "COPY_INTEL_ENABLED: False" in text
    assert "DISABLED" in text
    assert "COPY_INTEL_ENABLED is false" in text
    assert "ACTIVE" not in text


def test_status_shows_active_when_registered():
    settings = make_test_settings(copy_intel_enabled=True)
    text = format_copy_status(
        settings,
        {"last_poll_at": "2026-08-05T12:00:00+00:00", "last_event_count": "2"},
        scheduler_registered=True,
        watchlist_total=3,
        watchlist_enabled=2,
        watchlist_paused=1,
        baseline_complete=1,
        baseline_pending=2,
        delivery_pending=0,
    )
    assert "Polling: ACTIVE" in text
    assert "Watchlist: 3 total (2 enabled, 1 paused)" in text
    assert "Baseline: 1 complete, 2 pending/other" in text
    assert "Last event count: 2" in text


@pytest.mark.asyncio
async def test_discovery_runs_in_to_thread(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "disc.db"),
        copy_intel_enabled=True,
        copy_intel_read_only=True,
    )
    initialize_database(settings)
    update = MagicMock()
    update.effective_user = MagicMock(id=1)
    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    context.user_data = {}

    result = DiscoveryRunResult(candidates=[], scores=[], diagnostic_code="ok")

    async def _fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch(
            "app.telegram.copy_intel.handlers.edit_or_reply",
            new_callable=AsyncMock,
        ),
        patch(
            "app.telegram.copy_intel.handlers.run_discovery_detailed",
            return_value=result,
        ) as run_mock,
        patch(
            "app.telegram.copy_intel.handlers.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ) as to_thread,
    ):
        scores = await _ensure_discovery(update, context)
    assert scores == []
    to_thread.assert_called_once()
    run_mock.assert_called_once()


@pytest.mark.asyncio
async def test_discovery_api_unavailable_message(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "disc2.db"),
        copy_intel_enabled=True,
        copy_intel_read_only=True,
        admin_ids=[1],
    )
    initialize_database(settings)
    update = MagicMock()
    update.effective_user = MagicMock(id=1)
    update.callback_query = MagicMock()
    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    context.user_data = {}

    async def _boom(*_a, **_k):
        raise DiscoveryError(DIAG_API_UNAVAILABLE, "Leaderboard API unavailable.")

    with (
        patch(
            "app.telegram.copy_intel.handlers._require_copy_admin",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.telegram.copy_intel.handlers._ensure_discovery",
            side_effect=_boom,
        ),
        patch(
            "app.telegram.copy_intel.handlers.edit_or_reply",
            new_callable=AsyncMock,
        ) as edit,
    ):
        await show_discovery_list(update, context, title="Top Traders")
    text = edit.await_args.args[1]
    assert "API unavailable" in text
    assert "empty trader list" not in text.lower() or "cannot continue" in text.lower()


def test_discovery_diagnostic_rejected_by_scoring():
    text = format_discovery_diagnostic(
        DIAG_REJECTED_BY_SCORING,
        "Found 8 candidate wallet(s); analyzed 8 — none passed FOLLOW/WATCH scoring.",
    )
    assert "rejected by scoring" in text.lower()
    assert "Found 8" in text


def test_skip_reason_key_set_when_disabled():
    from app.copy_intel.scheduler import schedule_copy_intel_jobs

    settings = make_test_settings(copy_intel_enabled=False)
    app = MagicMock()
    app.bot_data = {}
    app.job_queue = MagicMock()
    schedule_copy_intel_jobs(app, settings)
    assert app.bot_data.get(COPY_POLL_REGISTERED_KEY) is not True
    assert COPY_POLL_SKIP_REASON_KEY in app.bot_data
