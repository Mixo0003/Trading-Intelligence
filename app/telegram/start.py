from telegram import Update
from telegram.ext import ContextTypes

from app.telegram.buttons import main_menu_keyboard


START_TEXT = """🚀 TI — Trading Intelligence

Real-time market intelligence for prediction traders.

MVP mode:
📡 Scan Now
📊 Status
⚙️ Filters
🔔 Notification
🆘 Support
"""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        START_TEXT,
        reply_markup=main_menu_keyboard(),
    )
