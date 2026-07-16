"""Regional scan snapshot cache isolation tests."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.polymarket.market_type import NormalizedTemperatureType, TemperatureMarketType
from app.region.types import ScanRegion
from app.signals.market_analysis import MarketAnalysis, ScanProgress
from app.telegram.buttons import BTN_FORECAST, BTN_TOP_SIGNALS
from app.telegram.handlers import _load_top_signals_response, button_message_handler
from app.telegram.scan_cache import (
    load_latest_scan_snapshot,
    save_scan_snapshot,
    snapshot_cache_key,
)
from tests.conftest import make_test_settings


def _analysis(city: str) -> MarketAnalysis:
    return MarketAnalysis(
        market_title=f"Highest temperature in {city}?",
        market_url=f"https://polymarket.com/event/{city.lower()}",
        city_location=city,
        station_code="KORD" if city == "Chicago" else "LFPB",
        resolution_source="weather_underground",
        target_date="July 16",
        market_type=TemperatureMarketType.MAXIMUM_TEMPERATURE,
        threshold_f=90.0,
        threshold_unit="FAHRENHEIT",
        yes_price=0.4,
        implied_probability=0.4,
        model_probability=0.5,
        edge=0.1,
        ev=0.1,
        kelly_fraction=0.02,
        kelly_cap=0.02,
        confidence="MEDIUM",
        recommendation_tier="SKIP",
        reason="test",
        data_sources=["test"],
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        message="",
        confidence_score=0.7,
        best_outcome="88°F",
        forecast_display="88°F",
        temperature_type=NormalizedTemperatureType.DAILY_HIGH,
        temperature_type_label="Daily high",
        display_status="FORECAST ONLY",
    )


def _progress(city: str, region: ScanRegion) -> ScanProgress:
    analyses = [_analysis(city)]
    return ScanProgress(
        discovered=10,
        worldwide_discovered=10,
        regional_count=1,
        all_analyses=analyses,
        results=analyses,
        scan_region=region.value,
    )


@pytest.fixture
def db_settings(tmp_path):
    return make_test_settings(database_path=str(tmp_path / "region_cache.db"))


class TestRegionalCacheIsolation:
    def test_snapshots_isolated_by_region(self, db_settings):
        save_scan_snapshot(
            db_settings, 42, _progress("Chicago", ScanRegion.USA), bot_data={}, region=ScanRegion.USA
        )
        save_scan_snapshot(
            db_settings, 42, _progress("Paris", ScanRegion.EUROPE), bot_data={}, region=ScanRegion.EUROPE
        )

        usa = load_latest_scan_snapshot(db_settings, 42, region=ScanRegion.USA)
        europe = load_latest_scan_snapshot(db_settings, 42, region=ScanRegion.EUROPE)

        assert usa is not None
        assert europe is not None
        assert usa.progress.all_analyses[0].city_location == "Chicago"
        assert europe.progress.all_analyses[0].city_location == "Paris"
        assert snapshot_cache_key(ScanRegion.USA) == "latest_scan:USA"

    @pytest.mark.asyncio
    async def test_top_signals_reads_regional_snapshot(self, db_settings):
        context = MagicMock()
        context.application.bot_data = {"settings": db_settings}
        context.user_data = {"selected_scan_region": ScanRegion.EUROPE}
        save_scan_snapshot(
            db_settings,
            42,
            _progress("Paris", ScanRegion.EUROPE),
            bot_data=context.application.bot_data,
            region=ScanRegion.EUROPE,
        )
        save_scan_snapshot(
            db_settings,
            42,
            _progress("Chicago", ScanRegion.USA),
            bot_data=context.application.bot_data,
            region=ScanRegion.USA,
        )

        text = await _load_top_signals_response(
            db_settings, context, telegram_id=42, region=ScanRegion.EUROPE
        )
        assert "EUROPE TOP SIGNALS" in text
        assert "No validated opportunities" in text
        assert "Forecasts available" not in text
        assert "Watchlist items" not in text

    @pytest.mark.asyncio
    async def test_forecasts_does_not_live_scan(self, db_settings):
        update = MagicMock()
        update.effective_user = MagicMock(id=42)
        message = AsyncMock()
        message.chat.id = 42
        message.text = BTN_FORECAST
        update.message = message
        update.effective_message = message

        context = MagicMock()
        context.application.bot_data = {"settings": db_settings}
        context.user_data = {"selected_scan_region": ScanRegion.USA}
        save_scan_snapshot(
            db_settings,
            42,
            _progress("Chicago", ScanRegion.USA),
            bot_data=context.application.bot_data,
            region=ScanRegion.USA,
        )

        with patch("app.telegram.handlers.scan_weather_markets") as scan_mock, patch(
            "app.telegram.handlers._run_scan",
            new=AsyncMock(),
        ):
            from app.telegram.handlers import forecast_command
            await forecast_command(update, context)

        scan_mock.assert_not_called()
        assert message.reply_text.await_count == 1
        message.reply_text.return_value.edit_text.assert_awaited_once()
