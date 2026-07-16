"""Tests for multi-source weather workflow without Open-Meteo."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from app.polymarket.market_type import NormalizedTemperatureType
from app.polymarket.models import PolymarketMarket
from app.polymarket.sibling_outcomes import OutcomeKind, SiblingOutcome
from app.signals.forecast_display import DISPLAY_FORECAST_ONLY, DISPLAY_SKIP_INSUFFICIENT
from app.signals.probability_distribution import DistributionContext, distribute_market_probabilities
from app.signals.bucket_probability import default_sigma
from app.telegram.formatting import format_actionable_weather_card
from app.weather.forecast_ensemble import fetch_forecast_ensemble
from app.weather.stations import WeatherStation
from app.weather.temperature_units import TemperatureUnit
from tests.conftest import make_test_settings


def _bracket(label: str, low: float, high: float, price: float) -> SiblingOutcome:
    market = PolymarketMarket(
        id=f"id-{label}",
        slug=label,
        question=label,
        description="",
        rules="",
        outcomes=["Yes", "No"],
        outcome_prices=[price, 1.0 - price],
        volume=None,
        liquidity=None,
        end_date=None,
        raw={"groupItemTitle": label},
    )
    return SiblingOutcome(
        market_id=market.id,
        market=market,
        label=label,
        kind=OutcomeKind.BRACKET,
        unit=TemperatureUnit.FAHRENHEIT,
        lower_bound=low,
        upper_bound=high,
        yes_price=price,
    )


class TestOpenMeteoRemoval:
    def test_forecast_ensemble_module_has_no_open_meteo_fetch(self):
        import app.weather.forecast_ensemble as mod

        assert not hasattr(mod, "_fetch_open_meteo_provider")
        assert not hasattr(mod, "fetch_open_meteo")

    def test_collect_providers_uses_nws_weather_com_met_norway(self):
        settings = make_test_settings()
        station = WeatherStation(
            code="KORD",
            name="Chicago O'Hare",
            latitude=41.9786,
            longitude=-87.9048,
            elevation_m=200.0,
            timezone="America/Chicago",
            verified=True,
        )
        with (
            patch("app.weather.forecast_ensemble.fetch_nws_hourly") as mock_nws,
            patch("app.weather.forecast_ensemble.fetch_weather_com_daily") as mock_wx,
            patch("app.weather.forecast_ensemble.fetch_met_norway") as mock_met,
        ):
            from app.weather.provider_forecast import ProviderForecast

            def _prov(name: str, val: float) -> ProviderForecast:
                return ProviderForecast(
                    provider_name=name,
                    model_name=name,
                    requested_latitude=station.latitude,
                    requested_longitude=station.longitude,
                    requested_elevation_m=200.0,
                    requested_timezone="America/Chicago",
                    grid_latitude=station.latitude,
                    grid_longitude=station.longitude,
                    grid_elevation_m=200.0,
                    grid_distance_km=0.5,
                    target_date_local=date(2026, 7, 17),
                    forecast_window_start_local="2026-07-17T00:00",
                    forecast_window_end_local="2026-07-17T23:00",
                    daily_min_c=20.0,
                    daily_max_c=30.0,
                    selected_value_c=val,
                    selected_statistic="daily_max",
                    hourly_samples=[],
                    min_hour_local=None,
                    max_hour_local=None,
                    init_time=None,
                )

            mock_nws.return_value = _prov("NWS", 29.0)
            mock_wx.return_value = _prov("Weather.com", 30.0)
            mock_met.return_value = _prov("MET Norway", 28.5)

            result = fetch_forecast_ensemble(
                settings,
                station,
                "July 17, 2026",
                NormalizedTemperatureType.DAILY_HIGH,
            )
            assert result is not None
            assert result.betting_skip_reason is None
            assert len(result.provider_names) == 3
            assert "Open-Meteo" not in result.forecast_provider_label


class TestProbabilityDistribution:
    def test_probabilities_sum_to_one(self):
        outcomes = [
            _bracket("80-81°F", 80, 81, 0.12),
            _bracket("82-83°F", 82, 83, 0.18),
            _bracket("84-85°F", 84, 85, 0.31),
        ]
        ctx = DistributionContext(
            consensus_native=83.0,
            spread_native=1.0,
            unit=TemperatureUnit.FAHRENHEIT,
        )
        dist = distribute_market_probabilities(
            outcomes, ctx, sigma=default_sigma(TemperatureUnit.FAHRENHEIT)
        )
        total = sum(item.probability for item in dist)
        assert total == pytest.approx(1.0, abs=0.01)

    def test_neighboring_bands_nonzero(self):
        outcomes = [
            _bracket("80-81°F", 80, 81, 0.12),
            _bracket("82-83°F", 82, 83, 0.18),
            _bracket("84-85°F", 84, 85, 0.31),
        ]
        ctx = DistributionContext(
            consensus_native=83.0,
            spread_native=5.0,
            unit=TemperatureUnit.FAHRENHEIT,
        )
        dist = distribute_market_probabilities(
            outcomes, ctx, sigma=default_sigma(TemperatureUnit.FAHRENHEIT) + 2.0
        )
        for item in dist:
            assert item.probability >= 0.03

    def test_high_spread_caps_favorite(self):
        outcomes = [
            _bracket("80-81°F", 80, 81, 0.12),
            _bracket("82-83°F", 82, 83, 0.18),
            _bracket("84-85°F", 84, 85, 0.31),
        ]
        ctx = DistributionContext(
            consensus_native=83.0,
            spread_native=5.0,
            unit=TemperatureUnit.FAHRENHEIT,
        )
        dist = distribute_market_probabilities(
            outcomes, ctx, sigma=default_sigma(TemperatureUnit.FAHRENHEIT) + 2.0
        )
        assert max(item.probability for item in dist) <= 0.35


class TestTelegramFormatting:
    def test_concise_card_format(self):
        analysis = MagicMock()
        analysis.city_location = "Chicago"
        analysis.target_date = "July 17"
        analysis.resolution_station = "KORD"
        analysis.station_code = "KORD"
        analysis.temperature_type_label = "Daily high temperature"
        analysis.temperature_type = NormalizedTemperatureType.DAILY_HIGH
        analysis.forecast_display = "84°F"
        analysis.expected_range_display = "82–86°F"
        analysis.provider_spread_display = "2°F"
        analysis.forecast_sources_line = "NWS, Weather.com, MET Norway"
        analysis.forecast_providers = None
        analysis.band_rows = (("84-85°F", 0.38, 0.18, 0.20),)
        analysis.display_status = "FORECAST ONLY"
        analysis.implied_probability = 0.18
        analysis.best_outcome = "84-85°F"
        analysis.ev = 0.20
        analysis.display_reason = "Fewer than three independent forecast sources available."
        analysis.reason = ""
        analysis.live_triggers = ("Temperature must reach 82°F by 13:00 local.",)

        text = format_actionable_weather_card(analysis)
        assert "🌡 CHICAGO — JULY 17" in text
        assert "Forecast: 84°F" in text
        assert "Best result: 84-85°F" in text
        assert "Price: 18%" in text
        assert "Decision: FORECAST ONLY" in text
        assert len(text.splitlines()) <= 8
        for forbidden in ("Open-Meteo", "Station:", "Band | Model", "EV:"):
            assert forbidden not in text
        assert "Market:" in text
        assert "Reason: Insufficient sources." in text
