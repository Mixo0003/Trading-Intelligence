import logging

from app.config.settings import Settings
from app.signals.scanner import scan_polymarket_weather_markets


logger = logging.getLogger(__name__)


def run_single_scan(settings: Settings) -> None:
    logger.info("Starting Polymarket weather scan...")

    result = scan_polymarket_weather_markets(
        settings=settings,
        limit=50,
    )

    logger.info(
        "Scan finished. Markets checked: %s. Signals found: %s",
        result.total_markets,
        len(result.signals),
    )

    for signal in result.signals:
        logger.info("Signal found: %s", signal.decision)
        logger.info("\n%s", signal.message)
