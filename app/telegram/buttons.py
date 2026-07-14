from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📡 Scan Now", callback_data="scan_now"),
            InlineKeyboardButton("📈 Top Signals", callback_data="top_signals"),
        ],
        [
            InlineKeyboardButton("📅 Today’s Forecast", callback_data="today_forecast"),
            InlineKeyboardButton("📊 Status", callback_data="status"),
        ],
        [
            InlineKeyboardButton("⚙️ Filters", callback_data="filters"),
            InlineKeyboardButton("🔔 Notifications", callback_data="notifications"),
        ],
        [
            InlineKeyboardButton("🆘 Support", callback_data="support"),
        ],
    ])


def filters_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➖ Min Edge", callback_data="filters:min_edge_down"),
            InlineKeyboardButton("➕ Min Edge", callback_data="filters:min_edge_up"),
        ],
        [
            InlineKeyboardButton("🔔 Toggle Notifications", callback_data="filters:toggle_notifications"),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="main:back"),
        ],
    ])
