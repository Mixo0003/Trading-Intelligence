"""User-facing labels for Opportunity Live Mode and Alert Mode.

Internal enum values remain OFF/STRICT and ON_DEMAND_ONLY/INSTANT for storage
and validation. Telegram surfaces must use these display labels only.
"""

from __future__ import annotations

from app.opportunity_intel.models import AlertMode, LiveMode

# User-facing Live Mode
LIVE_MODE_OFF_LABEL = "OFF"
LIVE_MODE_ON_LABEL = "ON"

LIVE_MODE_OFF_DESCRIPTION = "Live/in-play markets are excluded."
LIVE_MODE_ON_DESCRIPTION = (
    "Live/in-play markets may be included after the normal safety checks."
)

# User-facing Alert Mode
ALERT_MODE_MANUAL_LABEL = "MANUAL"
ALERT_MODE_AUTO_LABEL = "AUTO"

ALERT_MODE_MANUAL_DESCRIPTION = "Alerts are shown only when you request them."
ALERT_MODE_AUTO_DESCRIPTION = "Valid alerts are sent automatically."

_LIVE_MODE_FROM_TEXT = {
    "OFF": LiveMode.OFF,
    "ON": LiveMode.STRICT,
    "STRICT": LiveMode.STRICT,  # legacy typed input
}

_ALERT_MODE_FROM_TEXT = {
    "MANUAL": AlertMode.ON_DEMAND_ONLY,
    "AUTO": AlertMode.INSTANT,
    "ON_DEMAND_ONLY": AlertMode.ON_DEMAND_ONLY,  # legacy typed input
    "INSTANT": AlertMode.INSTANT,  # legacy typed input
}


def live_mode_label(mode: LiveMode) -> str:
    if mode == LiveMode.STRICT:
        return LIVE_MODE_ON_LABEL
    return LIVE_MODE_OFF_LABEL


def alert_mode_label(mode: AlertMode) -> str:
    if mode == AlertMode.INSTANT:
        return ALERT_MODE_AUTO_LABEL
    return ALERT_MODE_MANUAL_LABEL


def parse_live_mode_text(raw: str) -> LiveMode:
    key = raw.strip().upper()
    try:
        return _LIVE_MODE_FROM_TEXT[key]
    except KeyError as exc:
        raise ValueError("live_mode must be OFF or ON") from exc


def parse_alert_mode_text(raw: str) -> AlertMode:
    key = raw.strip().upper()
    try:
        return _ALERT_MODE_FROM_TEXT[key]
    except KeyError as exc:
        raise ValueError("alert_mode must be MANUAL or AUTO") from exc
