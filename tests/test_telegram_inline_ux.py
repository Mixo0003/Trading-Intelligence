"""Focused tests for compact inline Telegram UX."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.region.types import ScanRegion
from app.telegram.callback_handler import inline_callback_handler
from app.telegram.copy_intel.handlers import add_watchlist_from_callback
from app.telegram.handlers import help_command, menu_command, start_command
from app.telegram.inline_menus import (
    CB_COPY,
    CB_COPY_DISCOVER,
    CB_MAIN,
    CB_WEATHER,
    CB_WX_SCAN_USA,
    COPY_MENU_TEXT,
    DISCOVERY_MENU_TEXT,
    MAIN_MENU_TEXT,
    SETTINGS_MENU_TEXT,
    WEATHER_MENU_TEXT,
    copy_inline_keyboard,
    discovery_inline_keyboard,
    main_inline_keyboard,
    weather_inline_keyboard,
)
from app.telegram.menu import build_telegram_application
from app.telegram.messages import HELP_TEXT, START_TEXT, WEATHER_REGION_TEXT
from tests.conftest import make_test_settings
from tests.test_telegram_commands import _context, _update_with_message

_CYRILLIC = re.compile(r"[а-яА-ЯіІїЇєЄґҐ]")


def _callback_labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _callback_datas(markup: InlineKeyboardMarkup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]


def _callback_update(data: str):
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.data = data
    update.callback_query.message = AsyncMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = update.callback_query.message
    update.effective_user = MagicMock(id=1, username="u", first_name="T")
    update.message = None
    return update


class TestEnglishOnlyMenus:
    def test_menu_strings_english_only(self):
        for text in (
            MAIN_MENU_TEXT,
            WEATHER_MENU_TEXT,
            COPY_MENU_TEXT,
            DISCOVERY_MENU_TEXT,
            SETTINGS_MENU_TEXT,
            START_TEXT,
            HELP_TEXT,
            WEATHER_REGION_TEXT,
        ):
            assert not _CYRILLIC.search(text), text

    def test_inline_button_labels_english_only(self):
        labels = (
            _callback_labels(main_inline_keyboard())
            + _callback_labels(weather_inline_keyboard())
            + _callback_labels(copy_inline_keyboard())
            + _callback_labels(discovery_inline_keyboard())
        )
        for label in labels:
            assert not _CYRILLIC.search(label), label


class TestNoPersistentKeyboard:
    def test_no_reply_keyboard_markup_from_menu_builders(self):
        from app.telegram.buttons import main_menu_keyboard, region_menu_keyboard
        from app.telegram.copy_intel.buttons import copy_menu_keyboard
        from app.telegram.inline_menus import remove_reply_keyboard

        for markup in (
            main_menu_keyboard(),
            region_menu_keyboard(),
            copy_menu_keyboard(),
            remove_reply_keyboard(),
        ):
            assert isinstance(markup, ReplyKeyboardRemove)
            assert not isinstance(markup, ReplyKeyboardMarkup)

    def test_main_inline_menu(self):
        labels = _callback_labels(main_inline_keyboard())
        assert "🌤 Weather Intelligence" in labels
        assert "📋 Copy Trading Intelligence" in labels
        assert "📊 Top Opportunities" in labels
        assert "⚙️ Settings" in labels

    def test_weather_inline_menu(self):
        labels = _callback_labels(weather_inline_keyboard())
        assert "🔍 Scan USA" in labels
        assert "🌍 Scan Europe" in labels
        assert "📈 Top Signals" in labels
        assert "⬅️ Main Menu" in labels

    def test_copy_inline_menu(self):
        labels = _callback_labels(copy_inline_keyboard())
        assert "🏆 Trader Discovery" in labels
        assert "👀 Watchlist" in labels
        assert "⬅️ Main Menu" in labels

    def test_discovery_inline_menu(self):
        labels = _callback_labels(discovery_inline_keyboard())
        assert "🏆 Top Traders" in labels
        assert "🔎 Find Traders" in labels
        assert "⬅️ Copy Menu" in labels


@pytest.mark.asyncio
async def test_help_has_no_slash_command_dump():
    update = _update_with_message()
    await help_command(update, _context())
    text = update.message.reply_text.call_args.args[0]
    assert "/scan" not in text
    assert "/copy_list" not in text
    assert "/copy_add" not in text


@pytest.mark.asyncio
async def test_start_uses_keyboard_remove_and_inline_main():
    update = _update_with_message()
    context = _context()
    with patch("app.telegram.handlers.upsert_user"):
        await start_command(update, context)
    assert update.message.reply_text.await_count >= 2
    first_kwargs = update.message.reply_text.call_args_list[0].kwargs
    assert isinstance(first_kwargs["reply_markup"], ReplyKeyboardRemove)
    assert not _CYRILLIC.search(update.message.reply_text.call_args_list[0].args[0])
    second = update.message.reply_text.call_args_list[1]
    assert isinstance(second.kwargs["reply_markup"], InlineKeyboardMarkup)
    assert "Choose a section" in second.args[0]


@pytest.mark.asyncio
async def test_menu_uses_keyboard_remove():
    update = _update_with_message()
    await menu_command(update, _context())
    first = update.message.reply_text.call_args_list[0]
    assert isinstance(first.kwargs["reply_markup"], ReplyKeyboardRemove)


@pytest.mark.asyncio
async def test_callback_answered_and_edit_in_place():
    update = _callback_update(CB_WEATHER)
    context = _context()
    await inline_callback_handler(update, context)
    update.callback_query.answer.assert_awaited()
    update.callback_query.message.edit_text.assert_awaited()
    args, kwargs = update.callback_query.message.edit_text.call_args
    assert "Weather" in args[0]
    assert not _CYRILLIC.search(args[0])
    assert isinstance(kwargs["reply_markup"], InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_back_to_main_and_copy_menus():
    update = _callback_update(CB_MAIN)
    context = _context()
    await inline_callback_handler(update, context)
    update.callback_query.message.edit_text.assert_awaited()
    text = update.callback_query.message.edit_text.call_args.args[0]
    assert "Choose a section" in text

    update = _callback_update(CB_COPY)
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.call_args.args[0]
    assert "Copy" in text
    assert not _CYRILLIC.search(text)

    update = _callback_update(CB_COPY_DISCOVER)
    await inline_callback_handler(update, context)
    text = update.callback_query.message.edit_text.call_args.args[0]
    assert "Discovery" in text


@pytest.mark.asyncio
async def test_weather_scan_callback_starts_scan():
    update = _callback_update(CB_WX_SCAN_USA)
    context = _context()
    context.user_data = {}
    with patch("app.telegram.handlers.try_start_scoped_task", new=AsyncMock(return_value=(True, None))), patch(
        "app.telegram.handlers.upsert_user"
    ):
        await inline_callback_handler(update, context)
    update.callback_query.answer.assert_awaited()
    edited = update.callback_query.message.edit_text.call_args.args[0]
    assert "Scan started" in edited
    from app.region.session import get_user_scan_region

    assert get_user_scan_region(context) == ScanRegion.USA


@pytest.mark.asyncio
async def test_add_to_watchlist_callback(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "ux.db"))
    from app.database.storage import initialize_database

    initialize_database(settings)
    update = _callback_update("disc:add:0xee00ba338c59557141789b127927a55f5cc5cea1")
    context = _context()
    context.application.bot_data = {"settings": settings}
    context.user_data = {}
    await add_watchlist_from_callback(
        update,
        context,
        "0xee00ba338c59557141789b127927a55f5cc5cea1",
    )
    update.callback_query.message.edit_text.assert_awaited()
    text = update.callback_query.message.edit_text.call_args.args[0]
    assert "watchlist" in text.lower()
    assert not _CYRILLIC.search(text)


def test_single_callback_handler_registered(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "bot.db"),
        telegram_token="123456:ABCDEF",
    )
    with patch("app.telegram.menu.initialize_database"), patch(
        "app.telegram.menu.schedule_report_jobs"
    ), patch("app.telegram.menu.schedule_copy_intel_jobs"):
        app = build_telegram_application(settings)
    callback_handlers = [
        h for h in app.handlers[0] if h.__class__.__name__ == "CallbackQueryHandler"
    ]
    assert len(callback_handlers) == 1
    message_handlers = [
        h for h in app.handlers[0] if h.__class__.__name__ == "MessageHandler"
    ]
    assert message_handlers == []


def test_no_duplicate_callback_data_in_menus():
    all_data = (
        _callback_datas(main_inline_keyboard())
        + _callback_datas(weather_inline_keyboard())
        + _callback_datas(copy_inline_keyboard())
        + _callback_datas(discovery_inline_keyboard())
    )
    assert CB_WEATHER in all_data
    assert CB_WX_SCAN_USA in all_data
