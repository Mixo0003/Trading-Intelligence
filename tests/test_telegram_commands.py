import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.signals.market_analysis import ScanProgress
from app.telegram.handlers import (
    analyze_command,
    help_command,
    menu_command,
    recent_command,
    scan_command,
    start_command,
    status_command,
    top_command,
    weather_command,
)
from tests.conftest import make_test_settings


def _settings():
    return make_test_settings()


def _update_with_message():
    update = MagicMock()
    update.effective_user = MagicMock(id=123, username="u", first_name="Test")
    message = AsyncMock()
    message.chat.id = 123
    update.message = message
    update.effective_message = message
    return update


def _context():
    context = MagicMock()
    context.application.bot_data = {"settings": _settings()}
    context.args = []
    return context


@pytest.mark.asyncio
async def test_start_command():
    update = _update_with_message()
    context = _context()
    with patch("app.telegram.handlers.upsert_user"):
        await start_command(update, context)
    assert update.message.reply_text.await_count >= 2
    assert "ANALYSIS ONLY" in update.message.reply_text.call_args_list[0].args[0]


@pytest.mark.asyncio
async def test_menu_command():
    update = _update_with_message()
    context = _context()
    with patch("app.telegram.handlers.upsert_user"):
        await menu_command(update, context)
    assert update.message.reply_text.await_count >= 1


@pytest.mark.asyncio
async def test_weather_command():
    update = _update_with_message()
    await weather_command(update, _context())
    assert "Weather" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_help_command():
    update = _update_with_message()
    await help_command(update, _context())
    text = update.message.reply_text.call_args[0][0]
    assert "Weather" in text or "Menu" in text or "menu" in text.lower()
    assert "/scan" not in text


@pytest.mark.asyncio
async def test_top_command():
    update = _update_with_message()
    context = _context()
    progress = ScanProgress(discovered=1, accepted=1, skipped=0, results=[], all_analyses=[])
    with patch("app.telegram.handlers.load_latest_scan_snapshot") as load_mock:
        load_mock.return_value = MagicMock(progress=progress)
        await top_command(update, context)
    assert update.message.reply_text.await_count == 1
    loading_message = update.message.reply_text.return_value
    loading_message.edit_text.assert_awaited_once()
    final_text = loading_message.edit_text.await_args.args[0]
    assert "TOP SIGNALS" in final_text or "SCAN COMPLETED" in final_text or "No completed scan" in final_text


@pytest.mark.asyncio
async def test_status_command():
    update = _update_with_message()
    with patch("app.telegram.handlers.PolymarketClient") as client_cls:
        client_cls.return_value.get_json.return_value = []
        await status_command(update, _context())
    assert "ANALYSIS ONLY" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_recent_command_empty():
    update = _update_with_message()
    with patch("app.telegram.handlers.get_recent_analyses", return_value=[]):
        await recent_command(update, _context())
    assert "No recent" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_scan_command():
    update = _update_with_message()
    context = _context()
    context.bot = AsyncMock()
    update.message.chat_id = 42
    progress = ScanProgress(
        discovered=1,
        child_markets_discovered=1,
        parent_events_discovered=1,
        parent_events_in_region=1,
    )
    with patch("app.telegram.handlers.upsert_user"), patch(
        "app.telegram.handlers._run_scan",
        new=AsyncMock(return_value=progress),
    ), patch("app.telegram.handlers.persist_scan_results"), patch(
        "app.telegram.handlers.save_scan_snapshot"
    ):
        await scan_command(update, context)
        await asyncio.sleep(0.05)
    assert update.message.reply_text.await_count == 1
    assert "Scan started" in update.message.reply_text.await_args.args[0]
    assert context.bot.send_message.await_count >= 1


@pytest.mark.asyncio
async def test_analyze_command_requires_url():
    update = _update_with_message()
    await analyze_command(update, _context())
    assert "Usage" in update.message.reply_text.call_args[0][0]
