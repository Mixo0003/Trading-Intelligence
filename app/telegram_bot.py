import logging

from app.config.settings import load_settings
from app.telegram.menu import build_telegram_application


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )


def main() -> None:
    setup_logging()

    settings = load_settings()
    application = build_telegram_application(settings)

    logging.info("Telegram bot started.")
    application.run_polling()


if __name__ == "__main__":
    main()
