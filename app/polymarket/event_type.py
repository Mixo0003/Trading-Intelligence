"""Event-level temperature type detection with parent-title precedence."""

from __future__ import annotations

import logging

from app.polymarket.market_type import NormalizedTemperatureType

logger = logging.getLogger(__name__)


def _event_title_text(event_title: str | None, event_description: str | None = None) -> str:
    return "\n".join(part for part in [event_title or "", event_description or ""] if part).lower()


def detect_event_temperature_type(
    event_title: str | None,
    event_description: str | None = None,
) -> NormalizedTemperatureType | None:
    text = _event_title_text(event_title, event_description)
    if not text:
        return None
    if "highest temperature" in text or "maximum temperature" in text or "high temperature" in text:
        return NormalizedTemperatureType.DAILY_HIGH
    if "lowest temperature" in text or "minimum temperature" in text or "low temperature" in text:
        return NormalizedTemperatureType.DAILY_LOW
    return None


def validate_event_type_consistency(
    event_title: str | None,
    normalized_type: NormalizedTemperatureType,
    event_description: str | None = None,
) -> tuple[bool, str | None]:
    expected = detect_event_temperature_type(event_title, event_description)
    if expected is None:
        return True, None
    if expected != normalized_type:
        logger.warning(
            "MARKET_TYPE_MISMATCH event=%s expected=%s got=%s",
            event_title,
            expected.value,
            normalized_type.value,
        )
        return False, "MARKET_TYPE_MISMATCH"
    return True, None
