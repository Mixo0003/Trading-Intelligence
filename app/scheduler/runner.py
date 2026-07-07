import logging

from app.config.settings import Settings
from app.signals.dedup import mark_signal_sent, was_signal_sent
from app.signals.scanner import scan_polymarket_weather_markets
from app.telegram.notifier import send_telegram_message


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

    if not settings.admin_ids:
        logger.warning("No ADMIN_IDS configured. Signals will not be sent.")
        return

    for signal in result.signals:
        if was_signal_sent(signal):
            logger.info("Duplicate signal skipped: %s", signal.decision)
            continue

        sent_anywhere = False

        for admin_id in settings.admin_ids:
            sent = send_telegram_message(
                settings=settings,
                chat_id=admin_id,
                text=signal.message,
            )

            if sent:
                sent_anywhere = True
                logger.info("Signal sent to admin_id=%s", admin_id)
            else:
                logger.warning("Failed to send signal to admin_id=%s", admin_id)

        if sent_anywhere:
            mark_signal_sent(signal)
