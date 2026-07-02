from telegram import Update
from telegram.ext import ContextTypes

from app.telegram.menu import (
    show_main_menu,
    show_markets_menu,
    show_polymarket_menu,
    show_placeholder,
)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "menu_main":
        await query.edit_message_text(
            "🚀 TI — Trading Intelligence\n\nSelect an option below to begin.",
            reply_markup=__import__("app.telegram.buttons", fromlist=["main_menu_keyboard"]).main_menu_keyboard(),
        )

    elif data == "menu_markets":
        await show_markets_menu(update, context)

    elif data == "market_polymarket":
        await show_polymarket_menu(update, context)

    elif data == "polymarket_weather":
        await show_placeholder(
            update,
            "🌦️ Polymarket Weather",
            "Send a Polymarket weather market URL.\n\nThe bot will read description, rules, and resolution source first.",
        )

    elif data == "menu_filters":
        await show_placeholder(update, "⚙️ Filters", "Filters will be added after the database module.")

    elif data == "menu_reports":
        await show_placeholder(update, "📈 Reports", "Reports will be added after the signal engine.")

    elif data == "menu_assistant":
        await show_placeholder(update, "🤖 AI Assistant", "Claude integration will be added after verified data collection.")

    elif data == "menu_language":
        await show_placeholder(update, "🌍 Language", "V1.0 supports English only. More languages later.")

    elif data == "menu_community":
        await show_placeholder(update, "👥 Community", "Community link will be added later.")

    elif data == "menu_support":
        await show_placeholder(update, "🆘 Support", "Support flow will be added later.")

    else:
        await show_placeholder(update, "Unknown option", "This button is not implemented yet.")
