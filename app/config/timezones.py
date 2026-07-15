import logging
from datetime import timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


def load_timezone(name: str | None) -> tzinfo:
    """Load an IANA timezone, falling back to UTC without crashing handlers."""
    configured_name = (name or "").strip()
    if not configured_name:
        logger.error("Report timezone is empty; falling back to UTC.")
        return timezone.utc

    try:
        return ZoneInfo(configured_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        logger.error(
            "Invalid report timezone %r (%s); falling back to UTC.",
            configured_name,
            exc,
        )
        return timezone.utc
