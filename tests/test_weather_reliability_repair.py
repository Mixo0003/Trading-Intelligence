"""Weather scan / Forecasts reliability regressions."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.polymarket.discovery import SkipReason, evaluate_market_eligibility
from app.polymarket.market_type import NormalizedTemperatureType, TemperatureMarketType
from app.polymarket.models import PolymarketMarket
from app.region.types import ScanRegion
from app.signals.forecast_display import (
    DISPLAY_FORECAST_ONLY,
    DISPLAY_SKIP_INSUFFICIENT,
    DISPLAY_VALIDATED,
    compute_display_status,
    has_forecast_result,
)
from app.signals.market_analysis import MarketAnalysis, ScanProgress
from app.telegram.formatting import (
    format_scan_delivery_messages,
    format_today_forecast_from_snapshot,
    format_today_forecast_report,
)
from app.telegram.handlers import forecast_from_callback, scan_command
from app.telegram.scan_cache import load_latest_scan_snapshot, save_scan_snapshot
from app.weather.daily_forecast import DailyForecastStats
from tests.conftest import make_test_settings


def _analysis(**kwargs) -> MarketAnalysis:
    today = datetime.now(timezone.utc).date().isoformat()
    defaults = dict(
        market_title="Highest temperature in Austin?",
        market_url="https://polymarket.com/event/austin",
        city_location="Austin",
        station_code="KAUS",
        resolution_source="weather_underground",
        target_date="August 5",
        market_type=TemperatureMarketType.MAXIMUM_TEMPERATURE,
        threshold_f=95.0,
        threshold_unit="FAHRENHEIT",
        yes_price=None,
        implied_probability=None,
        model_probability=0.34,
        edge=None,
        ev=None,
        kelly_fraction=None,
        kelly_cap=0.02,
        confidence="MEDIUM",
        recommendation_tier="SKIP",
        reason="MISSING_PRICE",
        data_sources=["open_meteo"],
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        message="",
        confidence_score=0.7,
        best_outcome="95°F",
        forecast_display="96°F",
        expected_range_display="94–98°F",
        temperature_type=NormalizedTemperatureType.DAILY_HIGH,
        temperature_type_label="Daily high temperature",
        display_status=DISPLAY_FORECAST_ONLY,
        forecast_confidence="MEDIUM",
        display_reason="Price unavailable.",
        station_timezone="UTC",
        target_date_local=today,
        validation_error="MISSING_PRICE",
    )
    defaults.update(kwargs)
    return MarketAnalysis(**defaults)


def _forecast_stats() -> DailyForecastStats:
    from datetime import date

    return DailyForecastStats(
        provider="open_meteo",
        station_code="KAUS",
        target_date_local=date.today(),
        station_timezone="UTC",
        forecast_window_start_local="00:00",
        forecast_window_end_local="23:00",
        daily_min_c=24.0,
        daily_max_c=35.5,
        selected_statistic="daily_max",
        selected_value_c=35.5,
        hourly_samples=[],
        provider_names=("Open-Meteo GFS", "ECMWF IFS"),
        provider_spread_c=0.8,
        ensemble_provider_count=2,
        betting_skip_reason="INSUFFICIENT_PROVIDERS",
        forecast_provider_label="Open-Meteo",
    )


class TestTargetDateParsing:
    def test_lowercase_month_within_horizon(self):
        from datetime import date, timedelta

        from app.polymarket.discovery import _is_within_forecast_horizon, _parse_target_date

        target = date.today() + timedelta(days=2)
        text = target.strftime("%B %d, %Y").lower()
        parsed = _parse_target_date(text)
        assert parsed == target
        assert _is_within_forecast_horizon(parsed) is True


class TestMissingPriceDoesNotHideForecast:
    def test_eligibility_allows_missing_price(self):
        from datetime import date, timedelta

        target = (date.today() + timedelta(days=2)).strftime("%B %d, %Y")
        market = PolymarketMarket(
            id="1",
            slug="kaus-high",
            question=f"Will the daily high temperature at KAUS exceed 90°F on {target}?",
            description="Resolved using Weather Underground for station KAUS.",
            rules="Weather Underground KAUS daily high temperature.",
            outcomes=["Yes", "No"],
            outcome_prices=[],
            volume=100.0,
            liquidity=50.0,
            end_date="2099-01-01T00:00:00Z",
            raw={"closed": False},
        )
        ok, reason, _ = evaluate_market_eligibility(market, set())
        assert ok is True
        assert reason is None
        assert reason is not SkipReason.MISSING_PRICE

    def test_compute_display_status_forecast_only_without_mapping(self):
        status = compute_display_status(
            station_verified=True,
            station_error=None,
            forecast_stats=_forecast_stats(),
            forecast_native=96.0,
            mapping_ok=False,
            selected_in_event=False,
            betting_valid=False,
            recommendation_tier="SKIP",
        )
        assert status == DISPLAY_FORECAST_ONLY
        assert has_forecast_result(status, "96°F")

    def test_scan_delivery_includes_forecast_only_missing_price(self):
        progress = ScanProgress(
            discovered=5,
            parent_events_discovered=2,
            parent_events_in_region=2,
            parent_events_analyzed=1,
            scan_region="USA",
            all_analyses=[_analysis()],
            results=[_analysis()],
        )
        messages = format_scan_delivery_messages(progress)
        text = "\n\n".join(messages)
        assert "Decision: FORECAST ONLY" in text
        assert "Forecast: 96°F" in text
        assert "Price: N/A" in text
        assert "No forecast cards available" not in text


class TestEmptyScanDiagnostic:
    def test_empty_scan_shows_diagnostic_counts(self):
        progress = ScanProgress(
            discovered=40,
            child_markets_discovered=40,
            parent_events_discovered=12,
            parent_events_in_region=8,
            parent_events_analyzed=0,
            skipped=8,
            skip_reasons={
                "outside forecast horizon": 3,
                "missing verifiable station": 2,
                "FORECAST_UNAVAILABLE": 1,
                "missing market price": 0,
            },
            scan_region="USA",
            all_analyses=[],
            results=[],
        )
        messages = format_scan_delivery_messages(progress)
        text = "\n\n".join(messages)
        assert "USA SCAN COMPLETED" in text
        assert "No forecast cards available" in text
        assert "Markets discovered: 40" in text
        assert "Markets eligible (in region): 8" in text
        assert "Outside forecast horizon: 3" in text
        assert "Station unresolved: 2" in text

    def test_skip_only_analyses_without_forecast_use_diagnostic(self):
        progress = ScanProgress(
            discovered=3,
            parent_events_discovered=3,
            parent_events_in_region=3,
            parent_events_analyzed=1,
            scan_region="EUROPE",
            all_analyses=[
                _analysis(
                    forecast_display="",
                    display_status=DISPLAY_SKIP_INSUFFICIENT,
                    validation_error="STATION_UNRESOLVED",
                    display_reason="Station unresolved.",
                )
            ],
            results=[],
        )
        text = "\n\n".join(format_scan_delivery_messages(progress))
        assert "No forecast cards available" in text
        assert "EUROPE SCAN COMPLETED" in text


class TestSnapshotAndForecastsCallback:
    def test_snapshot_persistence_and_forecasts_output(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "wx.db"))
        progress = ScanProgress(
            discovered=2,
            scan_region="USA",
            all_analyses=[_analysis()],
            results=[_analysis()],
        )
        bot_data: dict = {}
        save_scan_snapshot(
            settings, 7, progress, bot_data=bot_data, region=ScanRegion.USA
        )
        loaded = load_latest_scan_snapshot(
            settings, 7, bot_data=bot_data, region=ScanRegion.USA
        )
        assert loaded is not None
        text = format_today_forecast_from_snapshot(loaded)
        assert "USA FORECASTS" in text
        assert "Decision: FORECAST ONLY" in text
        assert "Forecast: 96°F" in text

    def test_regional_scan_isolation(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "wx2.db"))
        bot_data: dict = {}
        usa = ScanProgress(
            discovered=1,
            scan_region="USA",
            all_analyses=[_analysis(city_location="Austin")],
            results=[_analysis(city_location="Austin")],
        )
        europe = ScanProgress(
            discovered=1,
            scan_region="EUROPE",
            all_analyses=[_analysis(city_location="Paris", station_code="LFPB")],
            results=[_analysis(city_location="Paris", station_code="LFPB")],
        )
        save_scan_snapshot(settings, 9, usa, bot_data=bot_data, region=ScanRegion.USA)
        save_scan_snapshot(
            settings, 9, europe, bot_data=bot_data, region=ScanRegion.EUROPE
        )
        usa_snap = load_latest_scan_snapshot(
            settings, 9, bot_data=bot_data, region=ScanRegion.USA
        )
        eu_snap = load_latest_scan_snapshot(
            settings, 9, bot_data=bot_data, region=ScanRegion.EUROPE
        )
        assert usa_snap is not None and eu_snap is not None
        assert "AUSTIN" in format_today_forecast_from_snapshot(usa_snap)
        assert "PARIS" in format_today_forecast_from_snapshot(eu_snap)
        assert "PARIS" not in format_today_forecast_from_snapshot(usa_snap)

    @pytest.mark.asyncio
    async def test_forecasts_callback_reads_latest_snapshot(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "wx3.db"))
        context = MagicMock()
        context.application.bot_data = {"settings": settings}
        context.user_data = {"selected_scan_region": ScanRegion.USA}
        update = MagicMock()
        update.effective_user = MagicMock(id=11)
        progress = ScanProgress(
            discovered=1,
            scan_region="USA",
            all_analyses=[_analysis()],
            results=[_analysis()],
        )
        save_scan_snapshot(
            settings,
            11,
            progress,
            bot_data=context.application.bot_data,
            region=ScanRegion.USA,
        )
        with patch(
            "app.telegram.handlers.edit_or_reply", new_callable=AsyncMock
        ) as edit:
            await forecast_from_callback(update, context)
        text = edit.await_args.args[1]
        assert "USA FORECASTS" in text
        assert "Decision: FORECAST ONLY" in text

    @pytest.mark.asyncio
    async def test_interactive_scan_persists_snapshot_for_forecasts(self, tmp_path):
        settings = make_test_settings(database_path=str(tmp_path / "wx4.db"))
        context = MagicMock()
        context.application.bot_data = {"settings": settings}
        context.user_data = {"selected_scan_region": ScanRegion.USA}
        context.bot = AsyncMock()
        update = MagicMock()
        update.effective_user = MagicMock(id=12, username="u", first_name="T")
        message = AsyncMock()
        message.chat.id = 12
        message.chat_id = 12
        update.message = message
        update.effective_message = message
        update.callback_query = None
        analyses = [_analysis()]
        progress = ScanProgress(
            discovered=4,
            parent_events_discovered=2,
            parent_events_in_region=2,
            parent_events_analyzed=1,
            scan_region="USA",
            all_analyses=analyses,
            results=analyses,
        )
        with (
            patch("app.telegram.handlers.upsert_user"),
            patch(
                "app.telegram.handlers._run_scan",
                new=AsyncMock(return_value=progress),
            ),
            patch("app.telegram.handlers.persist_scan_results"),
        ):
            await scan_command(update, context)
            await asyncio_sleep_brief()

        loaded = load_latest_scan_snapshot(
            settings, 12, bot_data=context.application.bot_data, region=ScanRegion.USA
        )
        assert loaded is not None
        assert "FORECAST ONLY" in format_today_forecast_report(loaded.progress)


async def asyncio_sleep_brief():
    import asyncio

    await asyncio.sleep(0.05)


def test_validated_still_preferred_over_forecast_only():
    progress = ScanProgress(
        discovered=2,
        scan_region="ALL",
        all_analyses=[
            _analysis(display_status=DISPLAY_FORECAST_ONLY, city_location="A"),
            _analysis(
                display_status=DISPLAY_VALIDATED,
                recommendation_tier="NORMAL BET",
                yes_price=0.25,
                implied_probability=0.25,
                city_location="B",
            ),
        ],
    )
    text = "\n\n".join(format_scan_delivery_messages(progress))
    # Validated card should appear (sorted first).
    assert "Decision: ACTION" in text or "Decision: FORECAST ONLY" in text
