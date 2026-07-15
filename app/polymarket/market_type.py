import re
from enum import Enum

from app.signals.bracket import is_bracket_market


class NormalizedTemperatureType(str, Enum):
    DAILY_HIGH = "DAILY_HIGH"
    DAILY_LOW = "DAILY_LOW"
    ABOVE_THRESHOLD = "ABOVE_THRESHOLD"
    BELOW_THRESHOLD = "BELOW_THRESHOLD"
    BRACKET_RANGE = "BRACKET_RANGE"
    EXACT_TEMPERATURE = "EXACT_TEMPERATURE"


NORMALIZED_TYPE_LABELS = {
    NormalizedTemperatureType.DAILY_HIGH: "Daily high temperature",
    NormalizedTemperatureType.DAILY_LOW: "Daily low temperature",
    NormalizedTemperatureType.ABOVE_THRESHOLD: "Temperature above threshold",
    NormalizedTemperatureType.BELOW_THRESHOLD: "Temperature below threshold",
    NormalizedTemperatureType.BRACKET_RANGE: "Temperature range",
    NormalizedTemperatureType.EXACT_TEMPERATURE: "Exact temperature",
}


class TemperatureMarketType(str, Enum):
    MAXIMUM_TEMPERATURE = "maximum temperature"
    MINIMUM_TEMPERATURE = "minimum temperature"
    ABOVE_THRESHOLD = "above threshold"
    BELOW_THRESHOLD = "below threshold"
    BRACKET = "bracket/range"
    UNKNOWN = "unknown"


class ThresholdDirection(str, Enum):
    ABOVE = "above"
    BELOW = "below"


def _has_range_pattern(text: str) -> bool:
    lower = (text or "").lower()
    if "temperature range" in lower:
        return True
    return bool(re.search(r"\d+\s*[\-–—]\s*\d+\s*°?\s*[cf]\b", lower))


def _is_exact_bucket(text: str) -> bool:
    lower = (text or "").lower()
    return bool(re.search(r"\bbe\s+\d{1,3}(?:\.\d+)?\s*°?\s*[cf]?\b", lower)) and not _has_range_pattern(text)


def _threshold_below(text: str) -> bool:
    lower = (text or "").lower().replace("wunderground", "")
    if "or lower" in lower:
        return True
    if any(w in lower for w in ["below", "less than", "lower than"]):
        return True
    return re.search(r"\bunder\s+\d", lower) is not None


def _threshold_above(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        w in lower
        for w in ["above", "over", "exceed", "greater than", "higher than", "or higher"]
    )


def normalize_temperature_type(text: str) -> NormalizedTemperatureType:
    lower = (text or "").lower()
    is_below = _threshold_below(text)
    is_above = _threshold_above(text)

    if is_above and not is_below:
        return NormalizedTemperatureType.ABOVE_THRESHOLD
    if is_below and not is_above:
        return NormalizedTemperatureType.BELOW_THRESHOLD

    is_daily_high = any(
        w in lower
        for w in [
            "maximum temperature",
            "max temperature",
            "daily high",
            "high temperature",
            "highest temperature",
        ]
    )
    is_daily_low = any(
        w in lower
        for w in [
            "minimum temperature",
            "min temperature",
            "daily low",
            "low temperature",
            "lowest temperature",
        ]
    )

    if _has_range_pattern(text) and not is_daily_high and not is_daily_low:
        return NormalizedTemperatureType.BRACKET_RANGE

    if is_daily_high:
        return NormalizedTemperatureType.DAILY_HIGH
    if is_daily_low:
        return NormalizedTemperatureType.DAILY_LOW

    if _has_range_pattern(text):
        return NormalizedTemperatureType.BRACKET_RANGE

    if is_bracket_market(text) or _is_exact_bucket(text):
        return NormalizedTemperatureType.EXACT_TEMPERATURE

    if "temperature" in lower:
        if is_below:
            return NormalizedTemperatureType.BELOW_THRESHOLD
        return NormalizedTemperatureType.ABOVE_THRESHOLD

    return NormalizedTemperatureType.EXACT_TEMPERATURE


def normalized_type_label(normalized_type: NormalizedTemperatureType) -> str:
    return NORMALIZED_TYPE_LABELS[normalized_type]


def classify_temperature_market(text: str) -> tuple[TemperatureMarketType, ThresholdDirection]:
    lower = (text or "").lower()

    is_below = _threshold_below(text)
    is_above = _threshold_above(text)

    if is_bracket_market(text):
        if any(w in lower for w in ["minimum", "lowest", "low temperature"]):
            return TemperatureMarketType.BRACKET, ThresholdDirection.BELOW
        return TemperatureMarketType.BRACKET, ThresholdDirection.ABOVE

    if any(w in lower for w in ["maximum temperature", "max temperature", "daily high", "high temperature", "highest temperature"]):
        return TemperatureMarketType.MAXIMUM_TEMPERATURE, ThresholdDirection.ABOVE

    if any(w in lower for w in ["minimum temperature", "min temperature", "daily low", "low temperature", "lowest temperature"]):
        return TemperatureMarketType.MINIMUM_TEMPERATURE, ThresholdDirection.BELOW

    if "temperature" in lower:
        if is_below and not is_above:
            return TemperatureMarketType.BELOW_THRESHOLD, ThresholdDirection.BELOW
        if is_above or not is_below:
            return TemperatureMarketType.ABOVE_THRESHOLD, ThresholdDirection.ABOVE

    if is_below:
        return TemperatureMarketType.BELOW_THRESHOLD, ThresholdDirection.BELOW
    if is_above:
        return TemperatureMarketType.ABOVE_THRESHOLD, ThresholdDirection.ABOVE

    return TemperatureMarketType.UNKNOWN, ThresholdDirection.ABOVE
