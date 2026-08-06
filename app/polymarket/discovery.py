import re
from datetime import date, datetime, timedelta
from enum import Enum

from app.polymarket.market_text import build_market_context_text, is_temperature_market_candidate
from app.polymarket.market_type import (
    TemperatureMarketType,
    ThresholdDirection,
    classify_temperature_market,
)
from app.polymarket.models import PolymarketMarket
from app.polymarket.pricing import extract_yes_price
from app.weather.resolution import parse_resolution_details


class SkipReason(str, Enum):
    NOT_WEATHER = "not weather-related"
    NOT_TEMPERATURE = "not temperature-related"
    CLOSED = "market closed"
    EXPIRED = "market expired"
    MISSING_PRICE = "missing market price"
    MISSING_TARGET_DATE = "missing target date"
    MISSING_RESOLUTION_SOURCE = "missing resolution source"
    MISSING_STATION = "missing verifiable station"
    MISSING_THRESHOLD = "missing threshold"
    DUPLICATE = "duplicate outcome"
    OUTSIDE_FORECAST_HORIZON = "outside forecast horizon"
    MALFORMED_METADATA = "malformed market metadata"
    UNSUPPORTED_FORMAT = "unsupported market format"


TEMPERATURE_KEYWORDS = [
    "temperature",
    "high temperature",
    "low temperature",
    "daily high",
    "daily low",
    "maximum temperature",
    "minimum temperature",
    "hottest",
    "coldest",
    "°f",
    " degrees f",
]

WEATHER_KEYWORDS = TEMPERATURE_KEYWORDS + [
    "weather",
    "noaa",
    "nws",
    "weather underground",
    "wunderground",
]


def _market_text(market: PolymarketMarket) -> str:
    return build_market_context_text(market)


def _is_confirmed_non_weather(text: str) -> bool:
    lower = text.lower()
    non_weather_markers = (
        "fifa world cup",
        "golden glove",
        "nfl supplemental draft",
        "netflix, inc.",
        "stock split",
        "pyth",
        "1-minute candle",
    )
    return any(marker in lower for marker in non_weather_markers)


def _parse_target_date(date_text: str | None) -> date | None:
    if not date_text:
        return None
    iso = re.search(r"(\d{4}-\d{2}-\d{2})", date_text)
    if iso:
        return datetime.strptime(iso.group(1), "%Y-%m-%d").date()
    abbr = re.search(
        r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\.?\s+'?(\d{2})\b",
        date_text.strip().lower(),
    )
    if abbr:
        month_map = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        year = 2000 + int(abbr.group(3))
        return date(year, month_map[abbr.group(2)], int(abbr.group(1)))
    # Strip day ordinals only (1st/2nd/3rd/4th). Do not strip substrings inside
    # month names — e.g. "august" must not become "augu".
    cleaned = re.sub(
        r"\b(\d{1,2})(st|nd|rd|th)\b",
        r"\1",
        date_text.strip().lower(),
    )
    for fmt in ("%B %d, %Y", "%B %d %Y", "%B %d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            if parsed.year == 1900:
                parsed = parsed.replace(year=datetime.now().year)
            return parsed.date()
        except ValueError:
            continue
    return None


def _is_within_forecast_horizon(target: date | None, horizon_days: int = 7) -> bool:
    if target is None:
        return False
    today = date.today()
    return today <= target <= today + timedelta(days=horizon_days)


def evaluate_market_eligibility(
    market: PolymarketMarket,
    seen_keys: set[str],
) -> tuple[bool, SkipReason | None, str]:
    text = _market_text(market)
    lower = text.lower()

    if market.raw.get("closed") is True:
        return False, SkipReason.CLOSED, text

    if _is_confirmed_non_weather(text):
        return False, SkipReason.NOT_WEATHER, text

    if not is_temperature_market_candidate(market):
        if any(k in lower for k in ("weather", "noaa", "nws", "wunderground")):
            return False, SkipReason.NOT_TEMPERATURE, text
        return False, SkipReason.NOT_WEATHER, text

    if not any(k in lower for k in TEMPERATURE_KEYWORDS):
        return False, SkipReason.NOT_TEMPERATURE, text

    market_type, _ = classify_temperature_market(text)
    if market_type == TemperatureMarketType.UNKNOWN and "temperature" not in lower:
        return False, SkipReason.UNSUPPORTED_FORMAT, text

    if extract_yes_price(market) is None:
        return False, SkipReason.MISSING_PRICE, text

    resolution = parse_resolution_details(text)
    if not resolution.source:
        return False, SkipReason.MISSING_RESOLUTION_SOURCE, text
    if not resolution.station_code:
        return False, SkipReason.MISSING_STATION, text
    if resolution.threshold_f is None:
        return False, SkipReason.MISSING_THRESHOLD, text
    if not resolution.target_date:
        return False, SkipReason.MISSING_TARGET_DATE, text

    target = _parse_target_date(resolution.target_date)
    if not _is_within_forecast_horizon(target):
        return False, SkipReason.OUTSIDE_FORECAST_HORIZON, text

    dedupe_key = market.id or market.slug or text[:120]
    if dedupe_key in seen_keys:
        return False, SkipReason.DUPLICATE, text

    if market.end_date:
        try:
            end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00")).date()
            if end < date.today():
                return False, SkipReason.EXPIRED, text
        except ValueError:
            pass

    seen_keys.add(dedupe_key)
    return True, None, text
