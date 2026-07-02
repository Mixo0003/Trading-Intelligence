from telegram import Update
from telegram.ext import ContextTypes

from app.telegram.buttons import (
    main_menu_keyboard,
    markets_keyboard,
    polymarket_keyboard,
    back_keyboard,
)


MAIN_MENU_TEXT = """
🚀 TI — Trading Intelligence

Real-time market intelligence for prediction traders.

V1.0 scope:
• Polymarket only
• Weather markets only
• Resolution-first analysis

Select an option below to begin.
"""


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            MAIN_MENU_TEXT,
            reply_markup=main_menu_keyboard(),
        )


async def show_markets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.edit_message_text(
        "📊 Markets\n\nFor V1.0 we only support Polymarket.",
        reply_markup=markets_keyboard(),
    )


async def show_polymarket_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.edit_message_text(
        "✅ Polymarket\n\nFor now: Weather markets only.",
        reply_markup=polymarket_keyboard(),
    )


async def show_placeholder(update: Update, title: str, text: str) -> None:
    query = update.callback_query
    await query.edit_message_text(
        f"{title}\n\n{text}",
        reply_markup=back_keyboard(),
    )
