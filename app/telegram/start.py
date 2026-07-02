from telegram import Update
from telegram.ext import ContextTypes

from app.telegram.menu import show_main_menu


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(update, context)
