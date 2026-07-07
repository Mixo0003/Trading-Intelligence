import logging

from telegram import Bot

from app.config.settings import Settings


logger = logging.getLogger(__name__)


def send_telegram_message(
    settings: Settings,
    chat_id: int,
    text: str,
) -> bool:
    try:
        bot = Bot(token=settings.telegram_token)
        bot.send_message(
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=True,
        )
        return True

    except Exception as exc:
        logger.exception("Failed to send Telegram message: %s", exc)
        return False
