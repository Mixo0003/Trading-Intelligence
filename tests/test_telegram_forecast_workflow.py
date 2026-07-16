"""Telegram handler integration tests for forecast-first workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.polymarket.market_type import NormalizedTemperatureType, TemperatureMarketType
from app.region.types import ScanRegion
from app.signals.forecast_display import DISPLAY_FORECAST_ONLY, DISPLAY_VALIDATED
from app.signals.market_analysis import MarketAnalysis, ScanProgress
from app.telegram.buttons import BTN_FORECAST
from app.telegram.handlers import button_message_handler, forecast_command, scan_command
from app.telegram.scan_cache import save_scan_snapshot
from tests.conftest import make_test_settings


def _analysis(**kwargs) -> MarketAnalysis:
    defaults = dict(
        market_title="Highest temperature in Paris on July 16?",
        market_url="https://polymarket.com/event/paris",
        city_location="Paris",
        station_code="LFPB",
        resolution_source="weather_underground",
        target_date="July 16",
        market_type=TemperatureMarketType.MAXIMUM_TEMPERATURE,
        threshold_f=87.8,
        threshold_unit="CELSIUS",
        yes_price=0.25,
        implied_probability=0.25,
        model_probability=0.34,
        edge=0.09,
        ev=0.09,
        kelly_fraction=0.02,
        kelly_cap=0.02,
        confidence="MEDIUM",
        recommendation_tier="SKIP",
        reason="test",
        data_sources=["open_meteo"],
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        message="",
        confidence_score=0.8,
        best_outcome="31°C",
        forecast_display="31°C",
        temperature_type=NormalizedTemperatureType.DAILY_HIGH,
        temperature_type_label="Daily high temperature",
        resolution_station="LFPB",
        forecast_gateway="Open-Meteo",
        forecast_models="GFS",
        display_status=DISPLAY_FORECAST_ONLY,
        forecast_confidence="LOW",
        display_reason="Only one forecast model available; betting requires broader agreement.",
        station_timezone="UTC",
        target_date_local=datetime.now(timezone.utc).date().isoformat(),
        validation_error="INSUFFICIENT_PROVIDERS",
        betting_skip_reason="INSUFFICIENT_PROVIDERS",
    )
    defaults.update(kwargs)
    return MarketAnalysis(**defaults)


def _context(settings):
    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    context.user_data = {"selected_scan_region": ScanRegion.ALL}
    context.bot = AsyncMock()
    return context


def _update(*, text: str | None = None, user_id: int = 42):
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id, username="u", first_name="Test")
    message = AsyncMock()
    message.chat.id = user_id
    message.chat_id = user_id
    if text is not None:
        message.text = text
    update.message = message
    update.effective_message = message
    return update


def _edited_text(update) -> str:
    loading_message = update.message.reply_text.return_value
    loading_message.edit_text.assert_awaited_once()
    return loading_message.edit_text.await_args.args[0]


def _scan_delivered_text(context) -> str:
    texts = []
    for call in context.bot.send_message.await_args_list:
        texts.append(str(call.kwargs.get("text", call.args[0] if call.args else "")))
    return "\n\n".join(texts)


@pytest.fixture
def db_settings(tmp_path):
    return make_test_settings(database_path=str(tmp_path / "workflow.db"))


class TestForecastFirstWorkflow:
    @pytest.mark.asyncio
    async def test_scan_now_saves_snapshot_and_shows_forecast_only(self, db_settings):
        import asyncio

        update = _update()
        context = _context(db_settings)
        analyses = [_analysis()]
        progress = ScanProgress(
            discovered=132,
            accepted=132,
            worldwide_discovered=132,
            regional_count=12,
            scan_region="ALL",
            all_analyses=analyses,
            results=analyses,
        )

        with patch("app.telegram.handlers.upsert_user"), patch(
            "app.telegram.handlers._run_scan",
            new=AsyncMock(return_value=progress),
        ), patch("app.telegram.handlers.persist_scan_results"):
            await scan_command(update, context)
            await asyncio.sleep(0.05)

        final_text = _scan_delivered_text(context)
        assert "Decision: FORECAST ONLY" in final_text
        assert "Forecasts available" not in final_text
        assert "Validated opportunities" not in final_text

    @pytest.mark.asyncio
    async def test_forecasts_reads_cache_without_scan(self, db_settings):
        update = _update(text=BTN_FORECAST)
        context = _context(db_settings)
        progress = ScanProgress(
            discovered=10,
            scan_region="ALL",
            all_analyses=[_analysis()],
            results=[_analysis()],
        )
        save_scan_snapshot(
            db_settings,
            42,
            progress,
            bot_data=context.application.bot_data,
            region=ScanRegion.ALL,
        )

        with patch("app.telegram.handlers.scan_weather_markets") as scan_mock, patch(
            "app.telegram.handlers._run_scan",
            new=AsyncMock(),
        ):
            await button_message_handler(update, context)

        scan_mock.assert_not_called()
        final_text = _edited_text(update)
        assert "ALL REGIONS FORECASTS" in final_text
        assert "Decision: FORECAST ONLY" in final_text

    @pytest.mark.asyncio
    async def test_forecast_empty_cache_message(self, db_settings):
        update = _update()
        context = _context(db_settings)
        await forecast_command(update, context)
        final_text = _edited_text(update)
        assert "No completed All Regions scan is available yet" in final_text

    @pytest.mark.asyncio
    async def test_forecast_only_without_reference(self, db_settings):
        update = _update()
        context = _context(db_settings)
        item = _analysis(
            betting_skip_reason="INSUFFICIENT_PROVIDERS",
            validation_error="INSUFFICIENT_PROVIDERS",
            display_status=DISPLAY_FORECAST_ONLY,
        )
        progress = ScanProgress(discovered=5, scan_region="ALL", all_analyses=[item], results=[item])
        save_scan_snapshot(
            db_settings,
            42,
            progress,
            bot_data=context.application.bot_data,
            region=ScanRegion.ALL,
        )
        await forecast_command(update, context)
        final_text = _edited_text(update)
        assert "Forecast: 31°C" in final_text
        assert "Decision: FORECAST ONLY" in final_text
