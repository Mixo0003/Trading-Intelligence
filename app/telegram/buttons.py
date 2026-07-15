from telegram import KeyboardButton, ReplyKeyboardMarkup

from app.region.types import ScanRegion

BTN_WEATHER = "🌤 Weather Markets"
BTN_BEST_OPPORTUNITIES = "📊 Best Opportunities"
BTN_RECENT = "🕘 Recent Reports"
BTN_STATUS = "⚙️ Status"
BTN_HELP = "❓ Help"
BTN_BACK = "⬅️ Back"
BTN_CHANGE_REGION = "⬅️ Change Region"
BTN_TOP_SIGNALS = "📈 Top Signals"
BTN_FORECAST = "📅 Forecasts"

BTN_USA = ScanRegion.USA.menu_button
BTN_EUROPE = ScanRegion.EUROPE.menu_button
BTN_ASIA = ScanRegion.ASIA.menu_button
BTN_ALL_REGIONS = ScanRegion.ALL.menu_button

MAIN_MENU_BUTTONS = (
    BTN_WEATHER,
    BTN_BEST_OPPORTUNITIES,
    BTN_RECENT,
    BTN_STATUS,
    BTN_HELP,
)

REGION_MENU_BUTTONS = (
    BTN_USA,
    BTN_EUROPE,
    BTN_ASIA,
    BTN_ALL_REGIONS,
    BTN_BACK,
)

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


def regional_action_keyboard(region: ScanRegion) -> ReplyKeyboardMarkup:
    scan_btn, top_btn, forecast_btn, change_btn = regional_action_buttons(region)
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(scan_btn), KeyboardButton(top_btn)],
            [KeyboardButton(forecast_btn), KeyboardButton(change_btn)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def region_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_USA), KeyboardButton(BTN_EUROPE)],
            [KeyboardButton(BTN_ASIA), KeyboardButton(BTN_ALL_REGIONS)],
            [KeyboardButton(BTN_BACK)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


ALL_NAV_BUTTONS = MAIN_MENU_BUTTONS + REGION_MENU_BUTTONS + (
    BTN_TOP_SIGNALS,
    BTN_FORECAST,
    BTN_CHANGE_REGION,
    ScanRegion.USA.scan_button,
    ScanRegion.EUROPE.scan_button,
    ScanRegion.ASIA.scan_button,
    ScanRegion.ALL.scan_button,
)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_WEATHER), KeyboardButton(BTN_BEST_OPPORTUNITIES)],
            [KeyboardButton(BTN_RECENT), KeyboardButton(BTN_STATUS)],
            [KeyboardButton(BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def is_regional_action_button(text: str, region: ScanRegion) -> bool:
    scan_btn, top_btn, forecast_btn, change_btn = regional_action_buttons(region)
    return text in {scan_btn, top_btn, forecast_btn, change_btn}
