import logging

from app.config.settings import load_settings
from app.scheduler.loop import run_scheduler


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )


def main() -> None:
    setup_logging()

    settings = load_settings()

    run_scheduler(
        settings=settings,
        interval_seconds=300,
    )


if __name__ == "__main__":
    main()
