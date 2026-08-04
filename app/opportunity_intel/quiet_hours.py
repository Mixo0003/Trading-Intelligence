"""Quiet-hours evaluation for instant opportunity delivery."""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.opportunity_intel.settings import QuietHours


def is_within_quiet_hours(
    quiet_hours: QuietHours | None,
    timezone_name: str,
    *,
    at: datetime,
) -> bool:
    """Return True when ``at`` falls inside validated local quiet hours.

    Overnight ranges (e.g. 22:00–06:00) are supported. Fail-closed on an
    invalid IANA timezone by treating quiet hours as active.
    """
    if quiet_hours is None:
        return False
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("quiet-hours evaluation requires a timezone-aware timestamp")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return True
    local = at.astimezone(zone)
    local_clock = local.timetz().replace(tzinfo=None)
    return _clock_within(quiet_hours.start, quiet_hours.end, local_clock)


def _clock_within(start: time, end: time, value: time) -> bool:
    if start < end:
        return start <= value < end
    # Overnight: active from start through midnight and from midnight to end.
    return value >= start or value < end
