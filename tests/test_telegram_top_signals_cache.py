"""Integration tests: Top Signals reads cached scan results without re-scanning."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.polymarket.market_type import NormalizedTemperatureType, TemperatureMarketType
from app.signals.forecast_display import DISPLAY_VALIDATED
from app.signals.market_analysis import MarketAnalysis, ScanProgress
from app.region.types import ScanRegion
from app.telegram.buttons import BTN_TOP_SIGNALS
from app.telegram.handlers import (
    _load_top_signals_response,
    button_message_handler,
    scan_command,
)
from app.telegram.scan_cache import CachedScanSnapshot, load_latest_scan_snapshot, save_scan_snapshot
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
        recommendation_tier="NORMAL BET",
        reason="test",
        display_status=DISPLAY_VALIDATED,
        forecast_confidence="HIGH",
        display_reason="Passed conservative betting validation.",
        forecast_gateway="NWS, Weather.com, MET Norway",
        forecast_models="NWS",
        data_sources=["NWS", "Weather.com", "MET Norway"],
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        message="",
        confidence_score=0.8,
        best_outcome="31°C",
        forecast_display="31°C",
        temperature_type=NormalizedTemperatureType.DAILY_HIGH,
        temperature_type_label="Daily high temperature",
        resolution_station="LFPB",
        forecast_providers="Open-Meteo (GFS)",
    )
    defaults.update(kwargs)
    return MarketAnalysis(**defaults)


def _progress(*, actionable: bool = True) -> ScanProgress:
    analyses = [
        _analysis(
            recommendation_tier="NORMAL BET" if actionable else "SKIP",
            validation_error=None if actionable else "FORECAST_UNAVAILABLE",
        ),
    ]
    return ScanProgress(
        discovered=132,
        accepted=132,
        skipped=0,
        skip_reasons={},
        all_analyses=analyses,
        results=analyses[:1],
    )


def _context(settings, bot_data=None):
    context = MagicMock()
    context.application.bot_data = bot_data or {"settings": settings}
    context.user_data = {"selected_scan_region": ScanRegion.ALL}
    return context


def _update_with_message(*, text: str | None = None, user_id: int = 42):
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id, username="u", first_name="Test")
    message = AsyncMock()
    message.chat.id = user_id
    if text is not None:
        message.text = text
    update.message = message
    update.effective_message = message
    return update


@pytest.fixture
def db_settings(tmp_path):
    return make_test_settings(database_path=str(tmp_path / "test.db"))


class TestTopSignalsCache:
    @pytest.mark.asyncio
    async def test_top_signals_does_not_call_scan_weather_markets(self, db_settings):
        update = _update_with_message(text=BTN_TOP_SIGNALS)
        context = _context(db_settings)
        save_scan_snapshot(
            db_settings,
            42,
            _progress(actionable=False),
            bot_data=context.application.bot_data,
            region=ScanRegion.ALL,
        )

        with patch("app.telegram.handlers.scan_weather_markets") as scan_mock, patch(
            "app.telegram.handlers._run_scan",
            new=AsyncMock(),
        ) as run_scan_mock:
            await button_message_handler(update, context)

        scan_mock.assert_not_called()
        run_scan_mock.assert_not_called()
        assert update.message.reply_text.await_count == 1
        update.message.reply_text.return_value.edit_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_top_signals_makes_zero_external_network_calls(self, db_settings):
        update = _update_with_message(text=BTN_TOP_SIGNALS)
        context = _context(db_settings)
        save_scan_snapshot(
            db_settings,
            42,
            _progress(actionable=False),
            bot_data=context.application.bot_data,
            region=ScanRegion.ALL,
        )

        with patch("requests.get") as requests_get, patch(
            "app.telegram.handlers.PolymarketClient"
        ) as client_cls, patch(
            "app.telegram.handlers.scan_weather_markets"
        ) as scan_mock:
            await button_message_handler(update, context)

        requests_get.assert_not_called()
        client_cls.assert_not_called()
        scan_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_actionable_returns_immediately(self, db_settings):
        context = _context(db_settings)
        progress = ScanProgress(
            discovered=132,
            accepted=132,
            all_analyses=[
                _analysis(
                    recommendation_tier="SKIP",
                    validation_error="FORECAST_SOURCE_DIVERGENCE",
                    display_status="FORECAST ONLY",
                    betting_skip_reason="FORECAST_SOURCE_DIVERGENCE",
                ),
                _analysis(
                    city_location="Miami",
                    recommendation_tier="SKIP",
                    validation_error="FORECAST_UNAVAILABLE",
                    display_status="SKIP — INSUFFICIENT DATA",
                    forecast_display="",
                ),
            ],
            results=[],
        )
        snapshot = save_scan_snapshot(
            db_settings,
            42,
            progress,
            bot_data=context.application.bot_data,
            region=ScanRegion.ALL,
        )
        snapshot = CachedScanSnapshot("2026-07-14 20:15 UTC", snapshot.progress, region=ScanRegion.ALL)
        context.application.bot_data["latest_scan_snapshots"] = {
            42: {"latest_scan:ALL": snapshot},
        }

        start = time.perf_counter()
        text = await _load_top_signals_response(
            db_settings, context, telegram_id=42, region=ScanRegion.ALL
        )
        elapsed = time.perf_counter() - start

        assert elapsed < 1.0
        assert "ALL REGIONS TOP SIGNALS" in text
        assert "No validated opportunities" in text

    @pytest.mark.asyncio
    async def test_displays_previous_actionable_results(self, db_settings):
        context = _context(db_settings)
        save_scan_snapshot(
            db_settings,
            42,
            _progress(actionable=True),
            bot_data=context.application.bot_data,
            region=ScanRegion.ALL,
        )

        text = await _load_top_signals_response(
            db_settings, context, telegram_id=42, region=ScanRegion.ALL
        )

        assert "ALL REGIONS TOP SIGNALS" in text
        assert "PARIS" in text
        assert "NORMAL BET" in text or "ACTION" in text

    @pytest.mark.asyncio
    async def test_no_previous_scan_message(self, db_settings):
        context = _context(db_settings)
        text = await _load_top_signals_response(
            db_settings, context, telegram_id=99, region=ScanRegion.ALL
        )
        assert "No completed All Regions scan is available yet." in text
        assert "Scan All Regions" in text

    @pytest.mark.asyncio
    async def test_loading_message_replaced_on_error(self, db_settings):
        update = _update_with_message(text=BTN_TOP_SIGNALS)
        context = _context(db_settings)

        with patch(
            "app.telegram.handlers.load_latest_scan_snapshot",
            side_effect=RuntimeError("db read failed"),
        ):
            await button_message_handler(update, context)

        assert update.message.reply_text.await_count == 1
        loading_message = update.message.reply_text.return_value
        loading_message.edit_text.assert_awaited_once()
        final_text = loading_message.edit_text.await_args.args[0]
        assert "Loading top signals" not in final_text
        assert "Could not load cached results" in final_text

    @pytest.mark.asyncio
    async def test_scan_now_still_performs_live_scan(self, db_settings):
        update = _update_with_message()
        context = _context(db_settings)
        progress = _progress(actionable=False)

        with patch("app.telegram.handlers.upsert_user"), patch(
            "app.telegram.handlers._run_scan",
            new=AsyncMock(return_value=progress),
        ) as run_scan_mock, patch(
            "app.telegram.handlers.persist_scan_results"
        ) as persist_mock, patch(
            "app.telegram.handlers.save_scan_snapshot",
            return_value=CachedScanSnapshot("now", progress),
        ) as save_mock:
            await scan_command(update, context)

        run_scan_mock.assert_awaited_once()
        persist_mock.assert_called_once()
        save_mock.assert_called_once()
        assert update.message.reply_text.await_count == 1
        update.message.reply_text.return_value.edit_text.assert_awaited_once()


class TestScanSnapshotPersistence:
    def test_round_trip_snapshot(self, db_settings):
        progress = _progress(actionable=True)
        save_scan_snapshot(db_settings, 7, progress, bot_data={}, region=ScanRegion.ALL)

        loaded = load_latest_scan_snapshot(db_settings, 7, bot_data={}, region=ScanRegion.ALL)
        assert loaded is not None
        assert loaded.analyzed_count == 1
        assert loaded.actionable_analyses[0].city_location == "Paris"
