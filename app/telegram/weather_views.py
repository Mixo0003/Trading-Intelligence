from __future__ import annotations

from datetime import datetime

from app.weather.market_forecasts import (
    ForecastResult,
    ForecastScan,
    VALIDATED_OPPORTUNITY,
    WATCH,
    is_target_today,
)


def _number(value: float | None, unit: str = "") -> str:
    return "unavailable" if value is None else f"{value:.1f}{unit}"


def _market_name(market_type: str | None) -> str:
    return "Daily high" if market_type == "DAILY_HIGH" else "Daily low"


def _price(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.1%}"


def _probability(value: float | None) -> str:
    return "not validated" if value is None else f"{value:.1%}"


def _clip(value: str | None, limit: int = 120) -> str:
    text = value or "unavailable"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _scan_item(item: ForecastResult) -> str:
    return (
        f"🌤 {item.city.upper()} — {item.target_date or 'date unavailable'}\n\n"
        f"Market: {_market_name(item.market_type)}\n"
        f"Resolution station: {item.station or 'unverified'}\n"
        f"Forecast: {_number(item.forecast_value, item.native_unit)}\n"
        f"Expected range: {_number(item.expected_low, item.native_unit)}–"
        f"{_number(item.expected_high, item.native_unit)}\n"
        f"Most likely outcome: {_clip(item.selected_bucket)}\n"
        f"Market price: {_price(item.live_price)}\n"
        f"Model probability: {_probability(item.model_probability)}\n"
        f"Status: {item.status}\n"
        f"Reason: {_clip(item.validation_reason)}\n"
        f"Forecast gateway: {item.forecast_gateway or 'unavailable'}\n"
        f"Forecast models: {', '.join(item.forecast_models) or 'none'}"
    )


def format_scan(scan: ForecastScan) -> str:
    header = (
        "🌦 SCAN COMPLETED\n\n"
        f"Markets found: {scan.discovered_count}\n"
        f"Markets analyzed: {scan.analyzed_count}\n"
        f"Forecasts available: {scan.forecast_result_count}\n"
        f"Validated opportunities: {scan.actionable_count}"
    )
    displayed = scan.results[:10]
    body = "\n\n".join(_scan_item(item) for item in displayed)
    failure = (
        f"\n\nPartial failures: {', '.join(scan.partial_failures)}"
        if scan.partial_failures else ""
    )
    return f"{header}\n\n{body}{failure}" if body else f"{header}\n\nNo analyzed results were available.{failure}"


def format_top_signals(scan: ForecastScan) -> str:
    watch_count = sum(item.status == WATCH for item in scan.results)
    stamp = datetime.fromisoformat(scan.scan_timestamp).strftime("%Y-%m-%d %H:%M UTC")
    if scan.actionable_count == 0:
        return (
            "📈 TOP SIGNALS\n\n"
            "No validated betting opportunities in the latest scan.\n\n"
            "Forecasts are still available under:\n"
            "📅 Today’s Forecast\n\n"
            f"Last scan: {stamp}\n"
            f"Markets analyzed: {scan.analyzed_count}\n"
            f"Watchlist items: {watch_count}\n\n"
            "Use 🔍 Scan Now to refresh."
        )
    items = [
        item for item in scan.results
        if item.status in {VALIDATED_OPPORTUNITY, WATCH}
    ]
    return "📈 TOP SIGNALS\n\n" + "\n\n".join(_scan_item(item) for item in items[:10])


def _today_item(item: ForecastResult) -> str:
    return (
        f"🌤 {item.city.upper()}\n"
        f"Market: {_market_name(item.market_type)}\n"
        f"Station: {item.station}\n"
        f"Forecast: {_number(item.forecast_value, item.native_unit)}\n"
        f"Expected range: {_number(item.expected_low, item.native_unit)}–"
        f"{_number(item.expected_high, item.native_unit)}\n"
        f"Most likely market outcome: {_clip(item.selected_bucket)}\n"
        f"Forecast gateway: {item.forecast_gateway}\n"
        f"Forecast models: {', '.join(item.forecast_models)}\n"
        f"Confidence: {item.confidence}\n"
        f"Trade status: {item.status}"
    )


def format_today_forecasts(scan: ForecastScan, now: datetime | None = None) -> str:
    items = [
        item for item in scan.results
        if item.has_forecast and is_target_today(item, now=now)
    ]
    if not items:
        return (
            "📅 TODAY’S MARKET FORECASTS\n\n"
            "No station-verified forecasts in the latest completed scan target "
            "today in each station’s local timezone."
        )
    return "📅 TODAY’S MARKET FORECASTS\n\n" + "\n\n".join(
        _today_item(item) for item in items
    )
