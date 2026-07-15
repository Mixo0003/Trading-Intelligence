import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import ReplyKeyboardMarkup

from app.region.types import ScanRegion
from app.telegram.buttons import (
    ALL_NAV_BUTTONS,
    BTN_BACK,
    BTN_BEST_OPPORTUNITIES,
    BTN_CHANGE_REGION,
    BTN_FORECAST,
    BTN_HELP,
    BTN_RECENT,
    BTN_STATUS,
    BTN_TOP_SIGNALS,
    BTN_USA,
    BTN_WEATHER,
    MAIN_MENU_BUTTONS,
    REGION_MENU_BUTTONS,
    main_menu_keyboard,
    region_menu_keyboard,
    regional_action_keyboard,
)
from app.telegram.handlers import button_message_handler, menu_command, start_command
from tests.test_telegram_commands import _context, _update_with_message


def _keyboard_labels(markup: ReplyKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


class TestReplyKeyboardLayout:
    def test_main_keyboard_has_only_current_buttons(self):
        labels = _keyboard_labels(main_menu_keyboard())
        assert labels == list(MAIN_MENU_BUTTONS)

    def test_region_menu_layout(self):
        labels = _keyboard_labels(region_menu_keyboard())
        assert labels == list(REGION_MENU_BUTTONS)

    def test_regional_action_layout(self):
        labels = _keyboard_labels(regional_action_keyboard(ScanRegion.USA))
        assert ScanRegion.USA.scan_button in labels
        assert BTN_TOP_SIGNALS in labels
        assert BTN_FORECAST in labels
        assert BTN_CHANGE_REGION in labels

    def test_obsolete_buttons_absent(self):
        obsolete = {
            "Find weather markets",
            "Analyze market",
            "Recent weather markets",
            "🔎 Find weather markets",
            "📡 Scan Now",
            "🔍 Scan Now",
            "📅 Today's Forecast",
        }
        labels = set(ALL_NAV_BUTTONS)
        assert labels.isdisjoint(obsolete)

    def test_manual_analyze_not_on_keyboard(self):
        labels = set(ALL_NAV_BUTTONS)
        assert "/analyze" not in labels
        assert "Analyze" not in labels


@pytest.mark.asyncio
async def test_start_shows_main_bottom_keyboard():
    update = _update_with_message()
    context = _context()
    with patch("app.telegram.handlers.upsert_user"):
        await start_command(update, context)
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert _keyboard_labels(markup) == list(MAIN_MENU_BUTTONS)


@pytest.mark.asyncio
async def test_menu_restores_main_bottom_keyboard():
    update = _update_with_message()
    context = _context()
    await menu_command(update, context)
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert _keyboard_labels(markup) == list(MAIN_MENU_BUTTONS)


@pytest.mark.asyncio
@pytest.mark.parametrize("button", MAIN_MENU_BUTTONS)
async def test_main_menu_button_routes(button):
    update = _update_with_message()
    update.message.text = button
    context = _context()
    context.user_data = {"selected_scan_region": ScanRegion.ALL}
    progress = MagicMock(discovered=0, accepted=0, skipped=0, skip_reasons={}, results=[])

    with patch("app.telegram.handlers._run_scan", new=AsyncMock(return_value=progress)), patch(
        "app.telegram.handlers.get_recent_analyses",
        return_value=[],
    ), patch("app.telegram.handlers.upsert_user"), patch(
        "app.telegram.handlers.PolymarketClient"
    ) as client_cls:
        client_cls.return_value.get_json.return_value = []
        await button_message_handler(update, context)

    assert update.message.reply_text.await_count >= 1


@pytest.mark.asyncio
async def test_weather_markets_opens_region_menu():
    update = _update_with_message()
    update.message.text = BTN_WEATHER
    context = _context()
    await button_message_handler(update, context)
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert _keyboard_labels(markup) == list(REGION_MENU_BUTTONS)


@pytest.mark.asyncio
async def test_usa_selection_opens_regional_actions():
    update = _update_with_message()
    update.message.text = BTN_USA
    context = _context()
    context.user_data = {}
    await button_message_handler(update, context)
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert ScanRegion.USA.scan_button in _keyboard_labels(markup)


@pytest.mark.asyncio
async def test_back_restores_main_keyboard():
    update = _update_with_message()
    update.message.text = BTN_BACK
    context = _context()
    await button_message_handler(update, context)
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert _keyboard_labels(markup) == list(MAIN_MENU_BUTTONS)
