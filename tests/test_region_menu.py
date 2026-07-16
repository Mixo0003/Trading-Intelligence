"""Telegram region / weather menu tests (inline UX)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import InlineKeyboardMarkup, ReplyKeyboardRemove

from app.region.session import USER_REGION_KEY, get_user_scan_region, set_user_scan_region
from app.region.types import ScanRegion
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.handlers import weather_command
from app.telegram.inline_menus import (
    CB_WEATHER,
    CB_WX_SCAN_EUROPE,
    CB_WX_SCAN_USA,
    remove_reply_keyboard,
    weather_inline_keyboard,
)
from tests.test_telegram_commands import _context, _update_with_message


def _inline_labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


class TestRegionKeyboards:
    def test_weather_inline_layout(self):
        labels = _inline_labels(weather_inline_keyboard())
        assert "🔍 Scan USA" in labels
        assert "🌍 Scan Europe" in labels
        assert "📈 Top Signals" in labels
        assert "⬅️ Main Menu" in labels

    def test_no_persistent_reply_keyboard(self):
        assert isinstance(remove_reply_keyboard(), ReplyKeyboardRemove)


@pytest.mark.asyncio
async def test_weather_command_shows_inline_weather_menu():
    update = _update_with_message()
    context = _context()
    await weather_command(update, context)
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert "🔍 Scan USA" in _inline_labels(markup)


@pytest.mark.asyncio
async def test_region_selection_via_callback_persists():
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.data = CB_WX_SCAN_EUROPE
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=1, username="u", first_name="T")
    update.message = None
    context = _context()
    context.user_data = {}
    with patch("app.telegram.handlers.try_start_scoped_task", new=AsyncMock(return_value=(True, None))), patch(
        "app.telegram.handlers.upsert_user"
    ):
        await inline_callback_handler(update, context)
    assert get_user_scan_region(context) is ScanRegion.EUROPE


@pytest.mark.asyncio
async def test_weather_callback_opens_weather_menu():
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.data = CB_WEATHER
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=1)
    update.message = None
    await inline_callback_handler(update, _context())
    text = update.callback_query.message.edit_text.call_args.args[0]
    assert "Weather" in text


@pytest.mark.asyncio
async def test_default_region_is_all():
    context = _context()
    context.user_data = {}
    assert get_user_scan_region(context) is ScanRegion.ALL


@pytest.mark.asyncio
async def test_usa_scan_callback():
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.data = CB_WX_SCAN_USA
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=1)
    update.message = None
    context = _context()
    context.user_data = {USER_REGION_KEY: ScanRegion.ALL}
    with patch("app.telegram.handlers.try_start_scoped_task", new=AsyncMock(return_value=(True, None))), patch(
        "app.telegram.handlers.upsert_user"
    ):
        await inline_callback_handler(update, context)
    assert get_user_scan_region(context) is ScanRegion.USA
