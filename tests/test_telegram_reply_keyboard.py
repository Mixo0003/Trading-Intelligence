import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import InlineKeyboardMarkup, ReplyKeyboardRemove

from app.telegram.buttons import ALL_NAV_BUTTONS, MAIN_MENU_BUTTONS, main_menu_keyboard
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.handlers import menu_command, start_command
from app.telegram.inline_menus import (
    CB_COPY,
    CB_PM_MENU,
    copy_inline_keyboard,
    discovery_inline_keyboard,
    main_inline_keyboard,
    polymarket_inline_keyboard,
    remove_reply_keyboard,
    weather_inline_keyboard,
)
from tests.test_telegram_commands import _context, _update_with_message


def _inline_labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


class TestReplyKeyboardRemoved:
    def test_no_persistent_reply_keyboard(self):
        assert isinstance(main_menu_keyboard(), ReplyKeyboardRemove)
        assert isinstance(remove_reply_keyboard(), ReplyKeyboardRemove)
        assert MAIN_MENU_BUTTONS == ()
        assert ALL_NAV_BUTTONS == ()

    def test_obsolete_large_buttons_absent_from_nav(self):
        obsolete = {
            "Find weather markets",
            "Analyze market",
            "Recent weather markets",
            "🔎 Find weather markets",
            "📡 Scan Now",
            "🔍 Scan Now",
            "📅 Today's Forecast",
            "☰ Menu",
        }
        assert set(ALL_NAV_BUTTONS).isdisjoint(obsolete)


@pytest.mark.asyncio
async def test_start_removes_reply_keyboard_and_shows_inline_menu():
    update = _update_with_message()
    context = _context()
    with patch("app.telegram.handlers.upsert_user"):
        await start_command(update, context)
    first = update.message.reply_text.call_args_list[0]
    assert isinstance(first.kwargs["reply_markup"], ReplyKeyboardRemove)
    second = update.message.reply_text.call_args_list[1]
    assert isinstance(second.kwargs["reply_markup"], InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_menu_removes_reply_keyboard_and_shows_inline_main():
    update = _update_with_message()
    context = _context()
    await menu_command(update, context)
    assert update.message.reply_text.await_count >= 2
    first = update.message.reply_text.call_args_list[0]
    assert isinstance(first.kwargs["reply_markup"], ReplyKeyboardRemove)
    second = update.message.reply_text.call_args_list[1]
    assert isinstance(second.kwargs["reply_markup"], InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_inline_weather_and_copy_menus():
    assert "🔍 Scan USA" in _inline_labels(weather_inline_keyboard())
    assert "🏆 Trader Discovery" in _inline_labels(copy_inline_keyboard())
    assert "🏆 Top Traders" in _inline_labels(discovery_inline_keyboard())
    assert "📈 Polymarket" in _inline_labels(main_inline_keyboard())
    assert "🌤 Weather Intelligence" in _inline_labels(polymarket_inline_keyboard())
    assert CB_PM_MENU in {
        b.callback_data
        for row in weather_inline_keyboard().inline_keyboard
        for b in row
        if b.callback_data
    }


@pytest.mark.asyncio
async def test_copy_callback_opens_copy_menu():
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.data = CB_COPY
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=1)
    update.message = None
    await inline_callback_handler(update, _context())
    update.callback_query.answer.assert_awaited()
    assert "Copy" in update.callback_query.message.edit_text.call_args.args[0]
