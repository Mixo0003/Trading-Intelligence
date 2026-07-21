import logging

from app.config.logging_config import configure_logging
from app.config.settings import load_settings
from app.telegram.menu import build_telegram_application


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level, settings=settings)

    application = build_telegram_application(settings)

    logging.getLogger(__name__).info(
        "Trading Intelligence Telegram bot started (ANALYSIS ONLY)."
    )
    application.run_polling()


if __name__ == "__main__":
    main()
