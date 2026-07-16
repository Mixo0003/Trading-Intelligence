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
        assert "🌡 PARIS — JULY 16" in text
        assert "Market: Daily high temperature" in text
        assert "Forecast: 31°C" in text
        assert "Expected: 29–32°C" in text
        assert "Best result: 31°C" in text
        assert "Price: 25%" in text
        assert "Decision: WATCH" in text
        assert "Reason: Potential value." in text
        assert "Kelly" not in text
        assert len(text.splitlines()) <= 8

    def test_forecast_only_shows_forecast_not_betting_block(self):
        text = format_concise_opportunity(
            _analysis(
                recommendation_tier="SKIP",
                validation_error="INSUFFICIENT_PROVIDERS",
                display_status=DISPLAY_FORECAST_ONLY,
                display_reason="Only one forecast model available; betting requires broader agreement.",
            )
        )
        assert "Forecast: 31°C" in text
        assert "Decision: FORECAST ONLY" in text
        assert "Reason: Insufficient sources." in text
        assert "Analysis unavailable" not in text

    def test_daily_high_market_type_from_event_classification(self):
        text = format_concise_opportunity(
            _analysis(
                market_title="Highest temperature in Paris on July 16?",
                temperature_type=NormalizedTemperatureType.DAILY_HIGH,
                temperature_type_label="Daily high temperature",
            )
        )
        assert "Market: Daily high temperature" in text

    def test_daily_low_market_type_from_event_classification(self):
        text = format_concise_opportunity(
            _analysis(
                market_title="Lowest temperature in Miami on July 16?",
                city_location="Miami",
                temperature_type=NormalizedTemperatureType.DAILY_LOW,
                temperature_type_label="Daily low temperature",
                forecast_display="80°F",
                expected_range_display="78–82°F",
                best_outcome="80-81°F",
                implied_probability=0.37,
            )
        )
        assert "Market: Daily low temperature" in text
        assert "°F" in text
        assert "Best result: 80-81°F" in text
        assert "Price: 37%" in text

    def test_unknown_type_shows_temperature_market(self):
        text = format_concise_opportunity(
            _analysis(
                temperature_type=NormalizedTemperatureType.EXACT_TEMPERATURE,
                temperature_type_label="Exact temperature",
            )
        )
        assert "Market: Temperature market" in text
        assert "Daily high" not in text
        assert "Daily low" not in text

    def test_long_reasons_map_to_short_labels(self):
        text = format_concise_opportunity(
            _analysis(
                display_status=DISPLAY_WATCH,
                display_reason="Potentially interesting edge, but insufficient confidence or agreement.",
            )
        )
        assert "Reason: Potential value." in text
        reason_line = [line for line in text.splitlines() if line.startswith("Reason:")][0]
        assert len(reason_line.removeprefix("Reason: ").split()) <= 4

    def test_card_max_eight_lines_no_diagnostics(self):
        text = format_concise_opportunity(_analysis())
        assert len(text.splitlines()) <= 8
        for forbidden in (
            "Station:",
            "Band | Model",
            "EV:",
            "Skip reasons",
            "discovered globally",
            "Open-Meteo",
        ):
            assert forbidden not in text

    def test_london_celsius_not_fahrenheit(self):
        text = format_concise_opportunity(_analysis(forecast_display="31°C"))
        assert "°C" in text
        assert "°F" not in text.splitlines()[1]

    def test_bundle_no_duplicate_footer(self):
        bundle = format_opportunities_bundle([_analysis(), _analysis(city_location="London")])
        assert "Kelly" not in bundle

    def test_empty_report_shows_no_markets_message(self):
        text = format_empty_report(3)
        assert text == "No current temperature markets found."

    def test_scan_completed_summary_has_no_counters_or_skip_reasons(self):
        progress = ScanProgress(
            discovered=12,
            worldwide_discovered=12,
            regional_count=3,
            parent_events_discovered=4,
            child_markets_discovered=12,
            parent_events_in_region=3,
            child_markets_loaded_for_analysis=9,
            parent_events_analyzed=3,
            skip_reasons={"NOT_WEATHER": 5},
            scan_region="EUROPE",
            all_analyses=[
                _analysis(display_status=DISPLAY_FORECAST_ONLY),
                _analysis(city_location="Miami", display_status=DISPLAY_FORECAST_ONLY),
                _analysis(city_location="London", display_status=DISPLAY_VALIDATED, recommendation_tier="NORMAL BET"),
            ],
        )
        text = format_scan_completed_summary(progress)
        assert text == "🌦 EUROPE SCAN COMPLETED"
        for forbidden in (
            "discovered globally",
            "retained in region",
            "loaded for analysis",
            "analyses completed",
            "Forecasts available",
            "Validated opportunities",
            "Skip reasons",
            "NOT_WEATHER",
        ):
            assert forbidden not in text

    def test_scan_report_includes_forecast_only_cards_without_diagnostics(self):
        progress = ScanProgress(
            discovered=12,
            skip_reasons={"NOT_WEATHER": 2},
            all_analyses=[
                _analysis(display_status=DISPLAY_FORECAST_ONLY, recommendation_tier="SKIP"),
            ],
        )
        text = format_scan_report(progress)
        assert "Decision: FORECAST ONLY" in text
        for forbidden in ("Results:", "Skip reasons", "Band | Model", "Station:", "EV:"):
            assert forbidden not in text

    def test_scan_report_empty_shows_no_markets_message(self):
        progress = ScanProgress(discovered=0, all_analyses=[])
        text = format_scan_report(progress)
        assert text == "No current temperature markets found."

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

    def test_card_uses_model_probability_band_not_market_price_band(self):
        text = format_concise_opportunity(
            _analysis(
                forecast_display="31°C",
                model_probability=0.34,
                display_status=DISPLAY_WATCH,
                best_outcome="31°C",
                band_rows=(("31°C", 0.34, 0.25, 0.09),),
            )
        )
        assert "Best result: 31°C" in text
        assert "Price: 25%" in text
        assert "Band | Model | Market | Edge" not in text

    def test_top_opportunities_concise(self):
        progress = ScanProgress(
            discovered=1,
            accepted=1,
            results=[_analysis(recommendation_tier="NORMAL BET", display_status=DISPLAY_VALIDATED)],
        )
        text = format_top_opportunities(progress)
        assert "📊 Best Opportunities" in text
        assert "Anthropic" not in text
