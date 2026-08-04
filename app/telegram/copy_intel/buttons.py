"""Copy-trading button labels (legacy reply keyboards deprecated).

Inline menus live in ``app.telegram.inline_menus``. Symbols kept for tests
and command fallbacks.
"""

from telegram import ReplyKeyboardRemove

from app.telegram.inline_menus import (
    copy_inline_keyboard,
    discovery_inline_keyboard,
    remove_reply_keyboard,
)

BTN_COPY_TRADING = "📋 Copy Trading"
BTN_COPY_LIST = "👀 Watchlist"
BTN_COPY_STATUS = "📡 Copy Status"
BTN_COPY_ALERTS = "🔔 Copy Alerts"
BTN_COPY_PAPER = "📒 Paper Portfolio"
BTN_COPY_BACK = "⬅️ Polymarket"
BTN_TRADER_DISCOVERY = "🏆 Trader Discovery"
BTN_TOP_TRADERS = "🏆 Top Traders"
BTN_FIND_TRADERS = "🔎 Find Traders"
BTN_CATEGORY_LEADERS = "📂 Category Leaders"
BTN_RISING_TRADERS = "🆕 Rising Traders"
BTN_HIGH_RISK = "⚠️ High Risk"
BTN_COPY_SETTINGS = "⚙️ Copy Settings"
BTN_BACK_COPY_MENU = "⬅️ Copy Menu"
BTN_SETTINGS_CONSERVATIVE = "🛡 Conservative"
BTN_SETTINGS_BALANCED = "⚖ Balanced"
BTN_SETTINGS_EXPLORATORY = "🚀 Exploratory"
BTN_SETTINGS_ALERTS_FOLLOW = "Alerts: FOLLOW only"
BTN_SETTINGS_ALERTS_BOTH = "Alerts: FOLLOW+WATCH"

COPY_MENU_BUTTONS = ()
DISCOVERY_MENU_BUTTONS = ()
SETTINGS_MENU_BUTTONS = ()
ALL_COPY_NAV_BUTTONS = ()


def copy_menu_keyboard() -> ReplyKeyboardRemove:
    return remove_reply_keyboard()


def discovery_menu_keyboard() -> ReplyKeyboardRemove:
    return remove_reply_keyboard()


def settings_menu_keyboard() -> ReplyKeyboardRemove:
    return remove_reply_keyboard()


def add_watchlist_button_label(wallet: str) -> str:
    return f"➕ Add {wallet[:8]}"


# Re-export inline builders for convenience.
__all__ = [
    "BTN_COPY_TRADING",
    "BTN_TRADER_DISCOVERY",
    "BTN_TOP_TRADERS",
    "ALL_COPY_NAV_BUTTONS",
    "copy_menu_keyboard",
    "discovery_menu_keyboard",
    "settings_menu_keyboard",
    "copy_inline_keyboard",
    "discovery_inline_keyboard",
]
