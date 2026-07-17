"""Compact inline keyboards for Trading Intelligence navigation."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

# Callback data prefixes (keep under Telegram 64-byte limit).
CB_MAIN = "nav:main"
CB_WEATHER = "nav:weather"
CB_COPY = "nav:copy"
CB_TOP = "nav:top"
CB_SETTINGS = "nav:settings"
CB_HEALTH = "nav:health"

CB_WX_SCAN_USA = "wx:scan:usa"
CB_WX_SCAN_EUROPE = "wx:scan:europe"
CB_WX_SCAN_ASIA = "wx:scan:asia"
CB_WX_SCAN_ALL = "wx:scan:all"
CB_WX_TOP = "wx:top"
CB_WX_FORECAST = "wx:forecast"

CB_COPY_DISCOVER = "copy:discover"
CB_COPY_WATCHLIST = "copy:watchlist"
CB_COPY_STATUS = "copy:status"
CB_COPY_ALERTS = "copy:alerts"
CB_COPY_PAPER = "copy:paper"
CB_COPY_SETTINGS = "copy:settings"

CB_DISC_TOP = "disc:top"
CB_DISC_FIND = "disc:find"
CB_DISC_CAT = "disc:cat"
CB_DISC_RISING = "disc:rising"
CB_DISC_RISK = "disc:risk"
CB_DISC_PREV = "disc:prev"
CB_DISC_NEXT = "disc:next"
CB_DISC_BACK = "disc:back"

CB_SET_CONSERVATIVE = "cset:cons"
CB_SET_BALANCED = "cset:bal"
CB_SET_EXPLORATORY = "cset:exp"
CB_SET_ALERT_FOLLOW = "cset:af"
CB_SET_ALERT_BOTH = "cset:ab"

# Dynamic: disc:add:<wallet> (wallet = 42 chars → ~50 total)


def remove_reply_keyboard() -> ReplyKeyboardRemove:
    """Clear any leftover persistent reply keyboard in existing chats."""
    return ReplyKeyboardRemove()


def main_inline_keyboard(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🌤 Weather Intelligence", callback_data=CB_WEATHER)],
        [InlineKeyboardButton("📋 Copy Trading Intelligence", callback_data=CB_COPY)],
        [InlineKeyboardButton("📊 Top Opportunities", callback_data=CB_TOP)],
        [InlineKeyboardButton("⚙️ Settings", callback_data=CB_SETTINGS)],
    ]
    if is_admin:
        rows.insert(
            -1,
            [InlineKeyboardButton("🩺 System Health", callback_data=CB_HEALTH)],
        )
    return InlineKeyboardMarkup(rows)


def weather_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔍 Scan USA", callback_data=CB_WX_SCAN_USA),
                InlineKeyboardButton("🌍 Scan Europe", callback_data=CB_WX_SCAN_EUROPE),
            ],
            [
                InlineKeyboardButton("🌏 Scan Asia", callback_data=CB_WX_SCAN_ASIA),
                InlineKeyboardButton("🗺 All Regions", callback_data=CB_WX_SCAN_ALL),
            ],
            [
                InlineKeyboardButton("📈 Top Signals", callback_data=CB_WX_TOP),
                InlineKeyboardButton("📅 Forecasts", callback_data=CB_WX_FORECAST),
            ],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data=CB_MAIN)],
        ]
    )


def copy_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏆 Trader Discovery", callback_data=CB_COPY_DISCOVER)],
            [
                InlineKeyboardButton("👀 Watchlist", callback_data=CB_COPY_WATCHLIST),
                InlineKeyboardButton("📡 Copy Status", callback_data=CB_COPY_STATUS),
            ],
            [
                InlineKeyboardButton("🔔 Copy Alerts", callback_data=CB_COPY_ALERTS),
                InlineKeyboardButton("📒 Paper Portfolio", callback_data=CB_COPY_PAPER),
            ],
            [InlineKeyboardButton("⚙️ Copy Settings", callback_data=CB_COPY_SETTINGS)],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data=CB_MAIN)],
        ]
    )


def discovery_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏆 Top Traders", callback_data=CB_DISC_TOP),
                InlineKeyboardButton("🔎 Find Traders", callback_data=CB_DISC_FIND),
            ],
            [
                InlineKeyboardButton("📂 Category Leaders", callback_data=CB_DISC_CAT),
                InlineKeyboardButton("🆕 Rising Traders", callback_data=CB_DISC_RISING),
            ],
            [
                InlineKeyboardButton("⚠️ High Risk", callback_data=CB_DISC_RISK),
                InlineKeyboardButton("⚙️ Copy Settings", callback_data=CB_COPY_SETTINGS),
            ],
            [
                InlineKeyboardButton("👀 Watchlist", callback_data=CB_COPY_WATCHLIST),
                InlineKeyboardButton("⬅️ Copy Menu", callback_data=CB_COPY),
            ],
        ]
    )


def copy_settings_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🛡 Conservative", callback_data=CB_SET_CONSERVATIVE),
                InlineKeyboardButton("⚖ Balanced", callback_data=CB_SET_BALANCED),
            ],
            [InlineKeyboardButton("🚀 Exploratory", callback_data=CB_SET_EXPLORATORY)],
            [
                InlineKeyboardButton("Alerts: FOLLOW only", callback_data=CB_SET_ALERT_FOLLOW),
                InlineKeyboardButton("Alerts: FOLLOW+WATCH", callback_data=CB_SET_ALERT_BOTH),
            ],
            [InlineKeyboardButton("⬅️ Copy Menu", callback_data=CB_COPY)],
        ]
    )


def trader_actions_keyboard(
    wallet: str,
    *,
    index: int,
    total: int,
    profile_url: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("➕ Add to Watchlist", callback_data=f"disc:add:{wallet}"),
            InlineKeyboardButton("🔗 Open Profile", url=profile_url),
        ]
    ]
    nav: list[InlineKeyboardButton] = []
    if index > 0:
        nav.append(InlineKeyboardButton("◀️ Previous", callback_data=CB_DISC_PREV))
    if index < total - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=CB_DISC_NEXT))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=CB_DISC_BACK)])
    return InlineKeyboardMarkup(rows)


MAIN_MENU_TEXT = "Trading Intelligence\nChoose a section:"
WEATHER_MENU_TEXT = "🌤 Weather Intelligence\nChoose an action:"
COPY_MENU_TEXT = "📋 Copy Trading\nMode: analysis only (read-only)"
DISCOVERY_MENU_TEXT = "🏆 Trader Discovery\nPublic Polymarket data"
SETTINGS_MENU_TEXT = (
    "⚙️ Settings\n"
    "Mode: ANALYSIS ONLY\n"
    "Copy Intel: read-only\n"
    "Secrets are never shown."
)
