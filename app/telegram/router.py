from telegram import Update
from telegram.ext import ContextTypes

from app.telegram.buttons import main_menu_keyboard


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if query is None:
        return

    await query.answer()

    data = query.data

    if data == "scan_now":
        await query.edit_message_text(
            "📡 Scan Now\n\nСкоро підключимо ручний запуск scanner.",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "status":
        await query.edit_message_text(
            "📊 Status\n\n✅ Bot online\n✅ Telegram notifier works\n✅ Scheduler ready\n✅ Scanner ready",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "filters":
        await query.edit_message_text(
            "⚙️ Filters\n\nMVP:\n- Polymarket Weather: ON\n- Min edge: 5%\n\nДетальні фільтри підключимо наступним кроком.",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "notifications":
        await query.edit_message_text(
            "🔔 Notifications\n\nMVP: notifications enabled for admin.",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "support":
        await query.edit_message_text(
            "🆘 Support\n\nНапиши адміну або повернись у головне меню.",
            reply_markup=main_menu_keyboard(),
        )

    else:
        await query.edit_message_text(
            "Unknown option.",
            reply_markup=main_menu_keyboard(),
        )
