"""Legacy reply-keyboard labels kept for slash-command / test compatibility.

Primary UX uses inline menus in ``inline_menus.py``. Persistent reply keyboards
are removed via ``ReplyKeyboardRemove``.
"""

from telegram import ReplyKeyboardRemove

from app.region.types import ScanRegion
from app.telegram.inline_menus import remove_reply_keyboard

BTN_WEATHER = "🌤 Weather Markets"
BTN_BEST_OPPORTUNITIES = "📊 Best Opportunities"
BTN_RECENT = "🕘 Recent Reports"
BTN_STATUS = "⚙️ Status"
BTN_HELP = "❓ Help"
BTN_BACK = "⬅️ Back"
BTN_CHANGE_REGION = "⬅️ Change Region"
BTN_TOP_SIGNALS = "📈 Top Signals"
BTN_FORECAST = "📅 Forecasts"
BTN_COPY_TRADING = "📋 Copy Trading"

BTN_USA = ScanRegion.USA.menu_button
BTN_EUROPE = ScanRegion.EUROPE.menu_button
BTN_ASIA = ScanRegion.ASIA.menu_button
BTN_ALL_REGIONS = ScanRegion.ALL.menu_button

# No persistent reply-keyboard navigation.
MAIN_MENU_BUTTONS: tuple[str, ...] = ()
REGION_MENU_BUTTONS: tuple[str, ...] = ()
ALL_NAV_BUTTONS: tuple[str, ...] = ()

REGION_BUTTON_TO_SCAN_REGION: dict[str, ScanRegion] = {
    BTN_USA: ScanRegion.USA,
    BTN_EUROPE: ScanRegion.EUROPE,
    BTN_ASIA: ScanRegion.ASIA,
    BTN_ALL_REGIONS: ScanRegion.ALL,
}


def regional_action_buttons(region: ScanRegion) -> tuple[str, str, str, str]:
    return (
        region.scan_button,
        BTN_TOP_SIGNALS,
        BTN_FORECAST,
        BTN_CHANGE_REGION,
    )


def regional_action_keyboard(region: ScanRegion) -> ReplyKeyboardRemove:
    return remove_reply_keyboard()


def region_menu_keyboard() -> ReplyKeyboardRemove:
    return remove_reply_keyboard()


def main_menu_keyboard() -> ReplyKeyboardRemove:
    return remove_reply_keyboard()


def is_regional_action_button(text: str, region: ScanRegion) -> bool:
    scan_btn, top_btn, forecast_btn, change_btn = regional_action_buttons(region)
    return text in {scan_btn, top_btn, forecast_btn, change_btn}
