import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.region.types import ScanRegion
from app.signals.market_analysis import ScanProgress
from app.telegram.handlers import scan_command, top_command
from tests.conftest import make_test_settings


def _context():
    context = MagicMock()
    context.application.bot_data = {"settings": make_test_settings()}
    context.user_data = {"selected_scan_region": ScanRegion.USA}
    return context


def _update():
    update = MagicMock()
    update.effective_user = MagicMock(id=42, username="u", first_name="Test")
    message = AsyncMock()
    message.chat.id = 42
    update.message = message
    update.effective_message = message
    return update


def _empty_progress() -> ScanProgress:
    return ScanProgress(
        scan_region=ScanRegion.USA.value,
        parent_events_discovered=0,
        child_markets_discovered=0,
        parent_events_in_region=0,
    )


@pytest.mark.asyncio
async def test_scan_success_edits_loading_message():
    update = _update()
    with patch("app.telegram.handlers.upsert_user"), patch(
        "app.telegram.handlers._run_scan",
        new=AsyncMock(return_value=_empty_progress()),
    ), patch("app.telegram.handlers.persist_scan_results"), patch(
        "app.telegram.handlers.save_scan_snapshot"
    ):
        await scan_command(update, _context())

    loading = update.message.reply_text.return_value
    loading.edit_text.assert_awaited_once()
    assert "SCAN COMPLETED" in loading.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_zero_result_top_response_edits_loading_message():
    update = _update()
    with patch("app.telegram.handlers.load_latest_scan_snapshot", return_value=None):
        await top_command(update, _context())

    loading = update.message.reply_text.return_value
    loading.edit_text.assert_awaited_once()
    assert "No completed USA scan" in loading.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_scan_exception_edits_loading_message():
    update = _update()
    with patch("app.telegram.handlers.upsert_user"), patch(
        "app.telegram.handlers._run_scan",
        new=AsyncMock(side_effect=RuntimeError("provider failed")),
    ):
        await scan_command(update, _context())

    loading = update.message.reply_text.return_value
    loading.edit_text.assert_awaited_once()
    assert "Scan failed" in loading.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_scan_timeout_edits_loading_message():
    update = _update()
    with patch("app.telegram.handlers.upsert_user"), patch(
        "app.telegram.handlers._run_scan",
        new=AsyncMock(side_effect=asyncio.TimeoutError),
    ):
        await scan_command(update, _context())

    loading = update.message.reply_text.return_value
    loading.edit_text.assert_awaited_once()
    assert "timed out" in loading.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_edit_failure_deletes_loading_then_sends_final():
    update = _update()
    loading = AsyncMock()
    loading.edit_text.side_effect = RuntimeError("message cannot be edited")
    update.message.reply_text.side_effect = [loading, AsyncMock()]

    with patch("app.telegram.handlers.upsert_user"), patch(
        "app.telegram.handlers._run_scan",
        new=AsyncMock(return_value=_empty_progress()),
    ), patch("app.telegram.handlers.persist_scan_results"), patch(
        "app.telegram.handlers.save_scan_snapshot"
    ):
        await scan_command(update, _context())

    loading.edit_text.assert_awaited_once()
    loading.delete.assert_awaited_once()
    assert update.message.reply_text.await_count == 2
    assert "SCAN COMPLETED" in update.message.reply_text.await_args_list[-1].args[0]
