"""Telegram region menu tests."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import ReplyKeyboardMarkup

from app.region.session import USER_REGION_KEY, get_user_scan_region, set_user_scan_region
from app.region.types import ScanRegion
from app.telegram.buttons import (
    BTN_ASIA,
    BTN_BACK,
    BTN_CHANGE_REGION,
    BTN_EUROPE,
    BTN_FORECAST,
    BTN_TOP_SIGNALS,
    BTN_USA,
    BTN_WEATHER,
    REGION_MENU_BUTTONS,
    region_menu_keyboard,
    regional_action_keyboard,
)
from app.telegram.handlers import button_message_handler, weather_command
from tests.test_telegram_commands import _context, _update_with_message


def _keyboard_labels(markup: ReplyKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


class TestRegionKeyboards:
    def test_region_selection_layout(self):
        labels = _keyboard_labels(region_menu_keyboard())
        assert labels == list(REGION_MENU_BUTTONS)

    def test_usa_action_layout(self):
        labels = _keyboard_labels(regional_action_keyboard(ScanRegion.USA))
        assert labels == [
            ScanRegion.USA.scan_button,
            BTN_TOP_SIGNALS,
            BTN_FORECAST,
            BTN_CHANGE_REGION,
        ]


@pytest.mark.asyncio
async def test_weather_markets_shows_region_keyboard():
    update = _update_with_message()
    update.message.text = BTN_WEATHER
    context = _context()
    await button_message_handler(update, context)
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert _keyboard_labels(markup) == list(REGION_MENU_BUTTONS)


@pytest.mark.asyncio
async def test_region_selection_persists_per_chat():
    update = _update_with_message()
    update.message.text = BTN_EUROPE
    context = _context()
    context.user_data = {}

    await button_message_handler(update, context)

    assert get_user_scan_region(context) is ScanRegion.EUROPE
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert ScanRegion.EUROPE.scan_button in _keyboard_labels(markup)


@pytest.mark.asyncio
async def test_change_region_returns_to_selection():
    update = _update_with_message()
    context = _context()
    context.user_data = {USER_REGION_KEY: ScanRegion.USA}
    update.message.text = BTN_CHANGE_REGION

    await button_message_handler(update, context)

    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert _keyboard_labels(markup) == list(REGION_MENU_BUTTONS)


@pytest.mark.asyncio
async def test_default_region_is_all():
    context = _context()
    context.user_data = {}
    assert get_user_scan_region(context) is ScanRegion.ALL
