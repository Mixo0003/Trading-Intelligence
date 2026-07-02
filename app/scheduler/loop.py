import logging
import time

from app.config.settings import Settings
from app.scheduler.runner import run_single_scan


logger = logging.getLogger(__name__)


def run_scheduler(
    settings: Settings,
    interval_seconds: int = 300,
) -> None:
    logger.info("Scheduler started. Interval: %s seconds", interval_seconds)

    while True:
        try:
            run_single_scan(settings)
        except Exception as exc:
            logger.exception("Scheduler cycle failed: %s", exc)

        time.sleep(interval_seconds)
