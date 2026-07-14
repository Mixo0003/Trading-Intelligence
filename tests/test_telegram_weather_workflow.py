import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.config.settings import Settings
from app.database.storage import (
    load_latest_completed_weather_scan,
    save_completed_weather_scan,
)
from app.polymarket.models import PolymarketEvent, PolymarketMarket
from app.telegram.router import callback_router
from app.telegram.weather_views import format_today_forecasts
from app.weather.market_forecasts import (
    FORECAST_ONLY,
    INSUFFICIENT_DATA,
    ForecastResult,
    ForecastScan,
    ModelValue,
    _unit,
    analyze_temperature_event,
)


def _settings(tmp_path) -> Settings:
    return Settings(
        project_name="test",
        project_version="1",
        telegram_token="test-token",
        anthropic_api_key="unused",
        admin_ids=[],
        guest_ids=[],
        default_language="en",
        database_path=str(tmp_path / "weather.sqlite3"),
        log_level="INFO",
        polymarket_gamma_base_url="https://example.test",
        polymarket_clob_base_url="https://example.test",
        weather_gov_base_url="https://example.test",
    )


def _result(**overrides) -> ForecastResult:
    values = {
        "event_id": "event-1",
        "child_market_id": "child-80",
        "market_url": "https://polymarket.com/event/miami",
        "city": "Miami",
        "station": "KMIA",
        "station_timezone": "America/New_York",
        "target_date": "2026-07-15",
        "market_type": "DAILY_LOW",
        "native_unit": "°F",
        "forecast_value": 80.0,
        "expected_low": 78.0,
        "expected_high": 82.0,
        "selected_bucket": "Will the lowest temperature in Miami be 80°F?",
        "model_probability": None,
        "live_price": 0.25,
        "status": FORECAST_ONLY,
        "validation_reason": "One station model is usable; no betting recommendation is validated.",
        "forecast_models": ["GFS"],
        "forecast_gateway": "Open-Meteo",
        "confidence": "LOW",
        "source_divergence": 5.0,
        "provider_spread": None,
        "station_verified": True,
    }
    values.update(overrides)
    return ForecastResult(**values)


def _scan(result=None) -> ForecastScan:
    item = result or _result()
    return ForecastScan(
        scan_timestamp="2026-07-14T17:00:00+00:00",
        discovered_count=132,
        analyzed_count=1,
        forecast_result_count=1,
        actionable_count=0,
        skip_reason_counts={},
        results=[item],
        partial_failures=[],
    )


def _update(callback_data: str):
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


def _market(question: str, market_id: str, price: str = "0.25") -> PolymarketMarket:
    return PolymarketMarket(
        id=market_id,
        slug=market_id,
        question=question,
        description="",
        rules="",
        outcomes=["Yes", "No"],
        outcome_prices=[price, str(1 - float(price))],
        volume=10,
        liquidity=10,
        end_date=None,
        raw={},
    )


def test_scan_handler_saves_forecast_only_and_shows_station_and_real_outcome(tmp_path):
    settings = _settings(tmp_path)
    scan = _scan()
    update, query = _update("scan_now")
    with (
        patch("app.telegram.router.load_settings", return_value=settings),
        patch("app.telegram.router.scan_temperature_forecasts", return_value=scan),
    ):
        asyncio.run(callback_router(update, MagicMock()))

    stored = load_latest_completed_weather_scan(settings)
    assert stored is not None
    assert stored.results[0].status == FORECAST_ONLY
    final_text = query.edit_message_text.await_args_list[-1].args[0]
    assert "Forecasts available: 1" in final_text
    assert "Validated opportunities: 0" in final_text
    assert "Resolution station: KMIA" in final_text
    assert scan.results[0].selected_bucket in final_text
    assert "VALIDATED OPPORTUNITY" not in final_text


def test_top_signals_reads_snapshot_without_scan_or_network(tmp_path):
    settings = _settings(tmp_path)
    save_completed_weather_scan(settings, _scan())
    update, query = _update("top_signals")
    with (
        patch("app.telegram.router.load_settings", return_value=settings),
        patch("app.telegram.router.scan_temperature_forecasts") as scanner,
        patch("requests.get") as network,
    ):
        asyncio.run(callback_router(update, MagicMock()))

    scanner.assert_not_called()
    network.assert_not_called()
    final_text = query.edit_message_text.await_args_list[-1].args[0]
    assert "No validated betting opportunities" in final_text
    assert "Watchlist items: 0" in final_text
    assert final_text != "Loading top signals..."


def test_today_handler_reads_low_confidence_forecast_without_reference(tmp_path):
    settings = _settings(tmp_path)
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    # UTC is safely converted to the station timezone by the formatter; use UTC
    # here to avoid coupling this test to the host timezone.
    now = datetime.now(timezone.utc)
    from zoneinfo import ZoneInfo
    target = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    scan = _scan(_result(target_date=target))
    save_completed_weather_scan(settings, scan)
    update, query = _update("today_forecast")
    with (
        patch("app.telegram.router.load_settings", return_value=settings),
        patch("app.telegram.router.scan_temperature_forecasts") as scanner,
    ):
        asyncio.run(callback_router(update, MagicMock()))

    scanner.assert_not_called()
    final_text = query.edit_message_text.await_args_list[-1].args[0]
    assert "KMIA" in final_text
    assert "Confidence: LOW" in final_text
    assert "Trade status: FORECAST ONLY" in final_text
    assert final_text != "Loading today’s market forecast..."


def test_station_local_date_not_utc_date():
    scan = _scan(_result(target_date="2026-07-14"))
    just_after_midnight_utc = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
    text = format_today_forecasts(scan, now=just_after_midnight_utc)
    assert "KMIA" in text


def test_nyc_abbreviation_does_not_turn_fahrenheit_market_into_celsius():
    assert _unit("Highest temperature in NYC: 88°F") == "°F"


def test_missing_exact_station_remains_skip():
    event = PolymarketEvent(
        id="missing-station",
        slug="missing-station",
        title="Lowest temperature in Miami on July 15",
        description="No resolution station is named.",
        markets=[_market("Will the lowest temperature in Miami be 80°F?", "80")],
        raw={},
    )
    result = analyze_temperature_event(MagicMock(), event)
    assert result.status == INSUFFICIENT_DATA
    assert result.station_verified is False
    assert result.forecast_value is None


def test_miami_one_model_keeps_forecast_but_never_bet():
    event = PolymarketEvent(
        id="miami",
        slug="miami-low",
        title="Lowest temperature in Miami on July 15",
        description="Resolution station: KMIA. Reference may also report °C.",
        markets=[
            _market("Will the lowest temperature in Miami be 79°F or below?", "79"),
            _market("Will the lowest temperature in Miami be 80°F?", "80"),
            _market("Will the lowest temperature in Miami be 81°F or higher?", "81"),
        ],
        raw={},
    )
    with patch(
        "app.weather.market_forecasts.fetch_station_forecasts",
        return_value=([ModelValue("GFS", 80.0, "America/New_York")], 74.0, []),
    ):
        result = analyze_temperature_event(MagicMock(), event)
    assert result.station == "KMIA"
    assert result.market_type == "DAILY_LOW"
    assert result.native_unit == "°F"
    assert result.status == FORECAST_ONLY
    assert result.confidence == "LOW"
    assert result.child_market_id in {market.id for market in event.markets}
    assert result.selected_bucket in {market.question for market in event.markets}


def test_paris_high_never_invents_missing_24c_bucket():
    event = PolymarketEvent(
        id="paris",
        slug="paris-high",
        title="Highest temperature in Paris on July 16",
        description="Resolution station: LFPG",
        markets=[
            _market("Will the highest temperature in Paris be 23°C or below?", "23"),
            _market("Will the highest temperature in Paris be 25°C?", "25"),
            _market("Will the highest temperature in Paris be 26°C or higher?", "26"),
        ],
        raw={},
    )
    with patch(
        "app.weather.market_forecasts.fetch_station_forecasts",
        return_value=(
            [
                ModelValue("GFS", 75.2, "Europe/Paris"),
                ModelValue("ECMWF", 76.1, "Europe/Paris"),
            ],
            None,
            [],
        ),
    ):
        result = analyze_temperature_event(MagicMock(), event)
    assert result.market_type == "DAILY_HIGH"
    assert result.station == "LFPG"
    assert result.selected_bucket in {market.question for market in event.markets}
    assert "24°C" not in result.selected_bucket


def test_temperature_range_bucket_uses_both_positive_endpoints():
    event = PolymarketEvent(
        id="miami-ranges",
        slug="miami-ranges",
        title="Lowest temperature in Miami on July 15",
        description="Resolution station: KMIA",
        markets=[
            _market("Will the lowest temperature in Miami be 77°F or below?", "low"),
            _market("Will the lowest temperature in Miami be between 78-79°F?", "range"),
            _market("Will the lowest temperature in Miami be 80°F or higher?", "high"),
        ],
        raw={},
    )
    with patch(
        "app.weather.market_forecasts.fetch_station_forecasts",
        return_value=([ModelValue("GFS", 78.5, "America/New_York")], None, []),
    ):
        result = analyze_temperature_event(MagicMock(), event)
    assert result.child_market_id == "range"
