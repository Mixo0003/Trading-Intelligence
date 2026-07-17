"""End-to-end tests for Telegram Scan Now wiring through scan_weather_markets."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.polymarket.market_type import NormalizedTemperatureType
from app.polymarket.models import PolymarketMarket
from app.signals.scan_service import scan_weather_markets
from app.telegram.formatting import format_concise_opportunity, format_scan_report


def _market(**kwargs) -> PolymarketMarket:
    raw = kwargs.pop("raw", {})
    return PolymarketMarket(
        id=kwargs.get("id", "1"),
        slug=kwargs.get("slug", "test"),
        question=kwargs.get("question", ""),
        description=kwargs.get("description", ""),
        rules=kwargs.get("rules", ""),
        outcomes=["Yes", "No"],
        outcome_prices=kwargs.get("outcome_prices", [0.25, 0.75]),
        volume=None,
        liquidity=None,
        end_date=kwargs.get("end_date"),
        raw=raw,
    )


PARIS_RULES = (
    "Resolved using Weather Underground at LFPB station. "
    "https://www.wunderground.com/history/daily/fr/paris/LFPB"
)


def _horizon_day(*, days_ahead: int = 2) -> date:
    """UTC calendar day inside the default 7-day forecast eligibility window."""
    return datetime.now(timezone.utc).date() + timedelta(days=days_ahead)


def _paris_day_label(day: date) -> str:
    return f"{day.strftime('%B')} {day.day}"


def _paris_slug_date(day: date) -> str:
    return f"{day.strftime('%B').lower()}-{day.day}-{day.year}"


def _paris_high_children(day: date | None = None) -> list[PolymarketMarket]:
    day = day or _horizon_day()
    label = _paris_day_label(day)
    slug = f"highest-temperature-in-paris-on-{_paris_slug_date(day)}"
    end = f"{day.isoformat()}T12:00:00Z"
    return [
        _market(
            id="h_tail",
            question=f"Will the highest temperature in Paris be 29°C or below on {label}?",
            rules=PARIS_RULES,
            end_date=end,
            raw={
                "groupItemTitle": "29°C or below",
                "endDate": end,
                "events": [{
                    "id": "701548",
                    "title": f"Highest temperature in Paris on {label}?",
                    "slug": slug,
                }],
            },
            outcome_prices=[0.008, 0.992],
        ),
        _market(
            id="h31",
            question=f"Will the highest temperature in Paris be 31°C on {label}?",
            rules=PARIS_RULES,
            end_date=end,
            raw={
                "groupItemTitle": "31°C",
                "endDate": end,
                "events": [{
                    "id": "701548",
                    "title": f"Highest temperature in Paris on {label}?",
                    "slug": slug,
                }],
            },
            outcome_prices=[0.25, 0.75],
        ),
        _market(
            id="h36",
            question=f"Will the highest temperature in Paris be 36°C or higher on {label}?",
            rules=PARIS_RULES,
            end_date=end,
            raw={
                "groupItemTitle": "36°C or higher",
                "endDate": end,
                "events": [{
                    "id": "701548",
                    "title": f"Highest temperature in Paris on {label}?",
                    "slug": slug,
                }],
            },
            outcome_prices=[0.01, 0.99],
        ),
    ]


def _paris_low_single_child(day: date | None = None) -> PolymarketMarket:
    day = day or _horizon_day()
    label = _paris_day_label(day)
    end = f"{day.isoformat()}T12:00:00Z"
    return _market(
        id="low24",
        question=f"Will the lowest temperature in Paris be 24°C on {label}?",
        rules=PARIS_RULES,
        end_date=end,
        raw={
            "groupItemTitle": "24°C",
            "endDate": end,
            "events": [{
                "id": "701640",
                "title": f"Lowest temperature in Paris on {label}?",
                "slug": f"lowest-temperature-in-paris-on-{_paris_slug_date(day)}",
            }],
        },
        outcome_prices=[0.003, 0.997],
    )


# Built at import with a horizon-safe UTC day (eligibility-mocked tests reuse these).
PARIS_HIGH_CHILDREN = _paris_high_children()
PARIS_LOW_SINGLE_CHILD = _paris_low_single_child()


class TestScanPathWiring:
    def test_single_discovered_child_expands_to_event_analysis(self):
        settings = MagicMock()
        settings.max_markets_per_scan = 50
        settings.max_results_per_report = 5
        settings.priority_weather_cities = []
        settings.weather_scan_concurrency = 6
        settings.weather_scan_deadline_seconds = 600
        settings.weather_discovery_cache_ttl_seconds = 300

        expanded = PARIS_HIGH_CHILDREN

        with (
            patch("app.signals.scan_service.PolymarketClient"),
            patch("app.signals.scan_service.get_weather_markets", return_value=(expanded, 1, 0)),
            patch("app.signals.scan_service.expand_group_children", return_value=expanded) as expand_mock,
            patch("app.signals.scan_service.analyze_temperature_event") as event_analysis,
            patch("app.signals.scan_service.analyze_market") as legacy_analysis,
            patch("app.signals.scan_service.event_matches_scan_region", return_value=(True, None)),
            patch("app.signals.scan_service._classify_parent_event", return_value=MagicMock()),
            patch("app.signals.scan_service.evaluate_market_eligibility", return_value=(True, None, None)),
            patch("app.signals.scan_service.sort_analyses", side_effect=lambda items: items),
        ):
            event_analysis.return_value = MagicMock(
                recommendation_tier="WATCH",
                edge=0.05,
                market_title="Highest temperature in Paris on July 16?",
                city_location="Paris",
                target_date="July 16",
                temperature_type=NormalizedTemperatureType.DAILY_HIGH,
                temperature_type_label="Daily high temperature",
                forecast_display="31°C",
                best_outcome="31°C",
                implied_probability=0.25,
                model_probability=0.34,
                confidence="MEDIUM",
                station_code="LFPB",
                validation_error=None,
            )
            progress = scan_weather_markets(settings)

        expand_mock.assert_called()
        event_analysis.assert_called()
        legacy_analysis.assert_not_called()
        assert len(progress.results) == 1

    def test_legacy_analyzer_not_used_when_siblings_present(self):
        settings = MagicMock()
        settings.max_markets_per_scan = 50
        settings.max_results_per_report = 5
        settings.priority_weather_cities = []
        settings.weather_scan_concurrency = 6
        settings.weather_scan_deadline_seconds = 600
        settings.weather_discovery_cache_ttl_seconds = 300

        with (
            patch("app.signals.scan_service.PolymarketClient"),
            patch(
                "app.signals.scan_service.get_weather_markets",
                return_value=([PARIS_LOW_SINGLE_CHILD], 1, 0),
            ),
            patch(
                "app.signals.scan_service.expand_group_children",
                return_value=PARIS_HIGH_CHILDREN,
            ),
            patch("app.signals.scan_service.analyze_temperature_event") as event_analysis,
            patch("app.signals.scan_service.analyze_market") as legacy_analysis,
            patch("app.signals.scan_service.event_matches_scan_region", return_value=(True, None)),
            patch("app.signals.scan_service._classify_parent_event", return_value=MagicMock()),
            patch("app.signals.scan_service.evaluate_market_eligibility", return_value=(True, None, None)),
            patch("app.signals.scan_service.sort_analyses", side_effect=lambda items: items),
        ):
            event_analysis.return_value = MagicMock(
                recommendation_tier="SKIP",
                edge=None,
                market_title="Highest temperature in Paris on July 16?",
                city_location="Paris",
                target_date="July 16",
                temperature_type=NormalizedTemperatureType.DAILY_HIGH,
                temperature_type_label="Daily high temperature",
                forecast_display="31°C",
                best_outcome="31°C",
                implied_probability=0.25,
                model_probability=0.2,
                confidence="MEDIUM",
                station_code="LFPB",
                validation_error=None,
            )
            scan_weather_markets(settings)

        legacy_analysis.assert_not_called()
        event_analysis.assert_called_once()


class TestParisTelegramScanIntegration:
    @patch("app.signals.event_analysis.fetch_daily_forecast_stats")
    @patch("app.signals.event_analysis.resolve_verified_station_from_text")
    @patch("app.signals.scan_service.get_weather_markets")
    @patch("app.signals.scan_service.PolymarketClient")
    def test_scan_now_path_renders_daily_high_paris_block(
        self,
        mock_client_cls,
        mock_get_markets,
        mock_resolve_station,
        mock_forecast_stats,
    ):
        from app.weather.daily_forecast import DailyForecastStats
        from app.weather.stations import WeatherStation
        from tests.conftest import make_test_settings

        settings = make_test_settings(priority_weather_cities=[])
        day = _horizon_day()
        children = _paris_high_children(day)
        label = _paris_day_label(day)

        mock_get_markets.return_value = (children, 3, 0)
        mock_client_cls.return_value = MagicMock()
        mock_resolve_station.return_value = (
            WeatherStation(
                code="LFPB",
                name="Paris Le Bourget",
                latitude=48.9694,
                longitude=2.4414,
                verified=True,
            ),
            None,
        )
        mock_forecast_stats.return_value = DailyForecastStats(
            provider="GFS,NWS",
            station_code="LFPB",
            target_date_local=day,
            station_timezone="Europe/Paris",
            forecast_window_start_local=f"{day.isoformat()}T00:00",
            forecast_window_end_local=f"{day.isoformat()}T23:00",
            daily_min_c=19.9,
            daily_max_c=31.0,
            selected_statistic="daily_max",
            selected_value_c=31.0,
            hourly_samples=[],
            ensemble_provider_count=2,
            forecast_provider_label="Open-Meteo (GFS); reference: NWS",
        )

        progress = scan_weather_markets(settings)
        paris = next(
            r for r in progress.results
            if "Highest temperature in Paris" in (r.market_title or "")
        )

        assert paris.temperature_type == NormalizedTemperatureType.DAILY_HIGH
        assert paris.selected_statistic == "daily_max"
        assert paris.forecast_display != "19.9°C"
        assert paris.best_outcome != "24°C"
        assert "24°c" not in paris.best_outcome.lower()
        assert label.split()[0] in (paris.market_title or "")

        if paris.validation_error:
            assert paris.forecast_display not in {"", "N/A"}
        text = format_concise_opportunity(paris)
        if not paris.validation_error:
            assert "Forecast:" in text
            assert "Decision:" in text
        assert "Minimum" not in text
        assert "Kelly" not in text
        assert "Anthropic" not in text
        assert "Station:" not in text
        assert "Band | Model" not in text

        bundle = format_scan_report(progress)
        assert len(progress.results) >= 1
        assert (
            "Decision:" in bundle
            or "No current temperature markets found." in bundle
            or paris.validation_error is not None
        )

    def test_paired_slug_for_high_and_low(self):
        from app.polymarket.event_expansion import paired_temperature_event_slug

        low = "lowest-temperature-in-paris-on-july-16-2026"
        high = paired_temperature_event_slug(low)
        assert high == "highest-temperature-in-paris-on-july-16-2026"
        assert paired_temperature_event_slug(high) == low
