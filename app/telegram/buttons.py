from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📡 Scan Now", callback_data="scan_now"),
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
