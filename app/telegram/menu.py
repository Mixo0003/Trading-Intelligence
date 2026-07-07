from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from app.config.settings import Settings
from app.telegram.router import callback_router
from app.telegram.start import start_command


def build_telegram_application(settings: Settings) -> Application:
    app = Application.builder().token(settings.telegram_token).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(callback_router))

    return app
