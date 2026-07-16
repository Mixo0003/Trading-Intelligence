import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.region.types import ScanRegion
from app.signals.market_analysis import ScanProgress
from app.telegram.handlers import (
    SCAN_STARTED_MESSAGE,
    scan_command,
    top_command,
)
from tests.conftest import make_test_settings


def _context():
    context = MagicMock()
    context.application.bot_data = {"settings": make_test_settings()}
    context.user_data = {"selected_scan_region": ScanRegion.USA}
    context.bot = AsyncMock()
    return context


def _update():
    update = MagicMock()
    update.effective_user = MagicMock(id=42, username="u", first_name="Test")
    message = AsyncMock()
    message.chat.id = 42
    message.chat_id = 42
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
async def test_scan_success_acknowledges_and_delivers_background():
    update = _update()
    context = _context()
    done = asyncio.Event()

    async def quick_scan(*_a, **_k):
        done.set()
        return _empty_progress()

    with patch("app.telegram.handlers.upsert_user"), patch(
        "app.telegram.handlers._run_scan",
        new=quick_scan,
    ), patch("app.telegram.handlers.persist_scan_results"), patch(
        "app.telegram.handlers.save_scan_snapshot"
    ):
        await scan_command(update, context)
        assert update.message.reply_text.await_args.args[0] == SCAN_STARTED_MESSAGE
        await asyncio.wait_for(done.wait(), timeout=1.0)
        await asyncio.sleep(0.05)

    assert context.bot.send_message.await_count >= 1
    texts = []
    for call in context.bot.send_message.await_args_list:
        if call.args:
            texts.append(str(call.args[0]))
        texts.append(str(call.kwargs.get("text", "")))
    assert any("No current temperature markets found." in text for text in texts)


@pytest.mark.asyncio
async def test_zero_result_top_response_edits_loading_message():
    update = _update()
    with patch("app.telegram.handlers.load_latest_scan_snapshot", return_value=None):
        await top_command(update, _context())

    loading = update.message.reply_text.return_value
    loading.edit_text.assert_awaited_once()
    assert "No completed USA scan" in loading.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_scan_exception_sends_failure_from_background():
    update = _update()
    context = _context()
    done = asyncio.Event()

    async def failing_scan(*_a, **_k):
        done.set()
        raise RuntimeError("provider failed")

    with patch("app.telegram.handlers.upsert_user"), patch(
        "app.telegram.handlers._run_scan",
        new=failing_scan,
    ):
        await scan_command(update, context)
        await asyncio.wait_for(done.wait(), timeout=1.0)
        await asyncio.sleep(0.05)

    assert any(
        "Scan failed" in str(c.kwargs.get("text", c.args[0] if c.args else ""))
        for c in context.bot.send_message.await_args_list
    )


@pytest.mark.asyncio
async def test_scan_partial_deadline_delivers_partial_line():
    update = _update()
    context = _context()
    progress = _empty_progress()
    progress.partial = True
    progress.parent_events_discovered = 1

    async def partial_scan(*_a, **_k):
        return progress

    with patch("app.telegram.handlers.upsert_user"), patch(
        "app.telegram.handlers._run_scan",
        new=partial_scan,
    ), patch("app.telegram.handlers.persist_scan_results"), patch(
        "app.telegram.handlers.save_scan_snapshot"
    ):
        await scan_command(update, context)
        await asyncio.sleep(0.05)

    texts = [
        str(c.kwargs.get("text", c.args[0] if c.args else ""))
        for c in context.bot.send_message.await_args_list
    ]
    assert any("partial results" in text for text in texts)
