import re
from datetime import date, datetime

from app.polymarket.market_type import TemperatureMarketType, ThresholdDirection
from app.weather.forecast import WeatherForecast


def _parse_target_date(date_text: str | None) -> date | None:
    if not date_text:
        return None
    iso = re.search(r"(\d{4}-\d{2}-\d{2})", date_text)
    if iso:
        return datetime.strptime(iso.group(1), "%Y-%m-%d").date()
    cleaned = re.sub(r"(st|nd|rd|th)", "", (date_text or "").strip().lower())
    for fmt in ("%B %d, %Y", "%B %d %Y", "%B %d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            if parsed.year == 1900:
                parsed = parsed.replace(year=datetime.now().year)
            return parsed.date()
        except ValueError:
            continue
    return None


def _index_for_date(target: date, dates: list[str]) -> int | None:
    target_iso = target.isoformat()
    for i, day in enumerate(dates):
        if day == target_iso:
            return i
    return None


def extract_forecast_temperature_f(
    forecast: WeatherForecast,
    target_date_text: str | None,
    market_type: TemperatureMarketType,
    direction: ThresholdDirection,
) -> float | None:
    target = _parse_target_date(target_date_text)
    if target is None:
        return None

    if forecast.source == "open_meteo":
        return None

    periods = forecast.raw.get("properties", {}).get("periods", [])
    target_label = target.strftime("%A").lower()
    temps: list[float] = []
    for period in periods:
        name = str(period.get("name", "")).lower()
        temp = period.get("temperature")
        if temp is None:
            continue
        if target_label in name or target.isoformat() in name:
            temps.append(float(temp))
    if temps:
        return max(temps) if direction == ThresholdDirection.ABOVE else min(temps)

    match = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*F", forecast.forecast_text, re.I)
    if match:
        return float(match.group(1))
    return None
