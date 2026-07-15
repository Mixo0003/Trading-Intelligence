from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.polymarket.market_type import NormalizedTemperatureType, TemperatureMarketType
from app.signals.forecast_display import DISPLAY_FORECAST_ONLY, DISPLAY_VALIDATED, DISPLAY_WATCH
from app.signals.market_analysis import MarketAnalysis
from app.telegram.formatting import (
    format_concise_opportunity,
    format_empty_report,
    format_opportunities_bundle,
    format_scan_completed_summary,
    format_scan_report,
    format_scan_summary,
    format_today_forecast_report,
    format_top_opportunities,
)
from app.signals.scan_service import ScanProgress


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
        recommendation_tier="WATCH",
        reason="test",
        data_sources=["NWS", "Weather.com", "MET Norway"],
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        message="",
        confidence_score=0.8,
        best_outcome="31°C",
        forecast_display="31°C",
        temperature_type=NormalizedTemperatureType.DAILY_HIGH,
        temperature_type_label="Daily high temperature",
        market_unit="CELSIUS",
        provider_unit="CELSIUS",
        displayed_unit="CELSIUS",
        conversion_applied=False,
        market_unit_source="question",
        raw_yes_price=0.25,
        price_valid=True,
        expected_range_display="29–32°C",
        forecast_source="ensemble",
        selected_statistic="daily_max",
        resolution_station="LFPB",
        forecast_gateway="NWS, Weather.com, MET Norway",
        forecast_models="NWS, Weather.com, MET Norway",
        forecast_sources_line="NWS, Weather.com, MET Norway",
        display_status=DISPLAY_WATCH,
        forecast_confidence="MEDIUM",
        display_reason="Meets watch criteria but not full validated betting thresholds.",
        station_timezone="UTC",
        target_date_local=datetime.now(timezone.utc).date().isoformat(),
    )
    defaults.update(kwargs)
    return MarketAnalysis(**defaults)


class TestReportFormatting:
    def test_no_kelly_disclaimer(self):
        text = format_opportunities_bundle([_analysis()])
        assert "Kelly" not in text
        assert "Analysis only" not in text

    def test_no_anthropic_counter_in_scan_summary(self):
        progress = ScanProgress(discovered=1, accepted=1, results=[_analysis()])
        text = format_scan_summary(progress)
        assert "Anthropic" not in text

    def test_concise_valid_report_format(self):
        text = format_concise_opportunity(_analysis())
        assert "Market: Daily high temperature" in text
        assert "consensus 31°C" in text
        assert "Station: LFPB" in text
        assert "Decision: WATCH" in text
        assert "Kelly" not in text

    def test_forecast_only_shows_forecast_not_betting_block(self):
        text = format_concise_opportunity(
            _analysis(
                recommendation_tier="SKIP",
                validation_error="INSUFFICIENT_PROVIDERS",
                display_status=DISPLAY_FORECAST_ONLY,
                display_reason="Only one forecast model available; betting requires broader agreement.",
            )
        )
        assert "consensus 31°C" in text
        assert "Decision: FORECAST ONLY" in text
        assert "Analysis unavailable" not in text

    def test_london_celsius_not_fahrenheit(self):
        text = format_concise_opportunity(_analysis(forecast_display="31°C"))
        assert "°C" in text
        assert "°F" not in text.split("consensus")[1].splitlines()[0]

    def test_bundle_no_duplicate_footer(self):
        bundle = format_opportunities_bundle([_analysis(), _analysis(city_location="London")])
        assert "Kelly" not in bundle

    def test_empty_report_includes_scan_summary(self):
        text = format_empty_report(3)
        assert "SCAN COMPLETED" in text
        assert "Parent events discovered globally: 3" in text
        assert "Child markets discovered globally: 3" in text

    def test_scan_completed_summary_counts(self):
        progress = ScanProgress(
            discovered=12,
            worldwide_discovered=12,
            regional_count=3,
            parent_events_discovered=4,
            child_markets_discovered=12,
            parent_events_in_region=3,
            child_markets_loaded_for_analysis=9,
            parent_events_analyzed=3,
            scan_region="EUROPE",
            all_analyses=[
                _analysis(display_status=DISPLAY_FORECAST_ONLY),
                _analysis(city_location="Miami", display_status=DISPLAY_FORECAST_ONLY),
                _analysis(city_location="London", display_status=DISPLAY_VALIDATED, recommendation_tier="NORMAL BET"),
            ],
        )
        text = format_scan_completed_summary(progress)
        assert "EUROPE SCAN COMPLETED" in text
        assert "Parent events discovered globally: 4" in text
        assert "Child markets discovered globally: 12" in text
        assert "Parent events retained in region: 3" in text
        assert "Child markets loaded for analysis: 9" in text
        assert "Parent event analyses completed: 3" in text
        assert "Forecasts available: 3" in text
        assert "Validated opportunities: 1" in text

    def test_scan_report_includes_forecast_only_cards(self):
        progress = ScanProgress(
            discovered=12,
            all_analyses=[
                _analysis(display_status=DISPLAY_FORECAST_ONLY, recommendation_tier="SKIP"),
            ],
        )
        text = format_scan_report(progress)
        assert "Results:" in text
        assert "Decision: FORECAST ONLY" in text

    def test_today_forecast_uses_regional_header(self):
        progress = ScanProgress(
            discovered=5,
            scan_region="USA",
            all_analyses=[
                _analysis(
                    display_status=DISPLAY_FORECAST_ONLY,
                    station_timezone="UTC",
                    target_date_local=datetime.now(timezone.utc).date().isoformat(),
                ),
            ],
        )
        text = format_today_forecast_report(progress)
        assert "USA FORECASTS" in text
        assert "Decision: FORECAST ONLY" in text

    def test_forecast_not_model_probability(self):
        text = format_concise_opportunity(
            _analysis(
                forecast_display="31°C",
                model_probability=0.34,
                display_status=DISPLAY_WATCH,
                band_rows=(("31°C", 0.34, 0.25, 0.09),),
            )
        )
        assert "34%" in text
        assert "Band | Model | Market | Edge" in text

    def test_top_opportunities_concise(self):
        progress = ScanProgress(
            discovered=1,
            accepted=1,
            results=[_analysis(recommendation_tier="NORMAL BET", display_status=DISPLAY_VALIDATED)],
        )
        text = format_top_opportunities(progress)
        assert "📊 Best Opportunities" in text
        assert "Anthropic" not in text
