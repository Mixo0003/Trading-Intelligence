from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Markets", callback_data="menu_markets"),
            InlineKeyboardButton("🤖 AI Assistant", callback_data="menu_assistant"),
        ],
        [
            InlineKeyboardButton("⚙️ Filters", callback_data="menu_filters"),
            InlineKeyboardButton("📈 Reports", callback_data="menu_reports"),
        ],
        [
            InlineKeyboardButton("🌍 Language", callback_data="menu_language"),
            InlineKeyboardButton("👥 Community", callback_data="menu_community"),
        ],
        [
            InlineKeyboardButton("🆘 Support", callback_data="menu_support"),
        ],
    ])


def markets_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Polymarket", callback_data="market_polymarket")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu_main")],
    ])


def polymarket_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌦️ Weather", callback_data="polymarket_weather")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu_markets")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="menu_main")]
    ])
