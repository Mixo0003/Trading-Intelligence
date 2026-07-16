"""Focused tests for station-first evidence layer (WU history, METAR, NWS AFD, local TV)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.polymarket.market_type import NormalizedTemperatureType
from app.polymarket.sibling_outcomes import OutcomeKind, SiblingOutcome
from app.signals.forecast_display import short_display_reason
from app.telegram.formatting import format_concise_card
from app.weather.evidence.asos_daily import fetch_asos_daily_range
from app.weather.evidence.local_tv import _extract_high_f, fetch_local_tv_evidence
from app.weather.evidence.metar import fetch_metar_evidence
from app.weather.evidence.nws_discussion import extract_discussion_signals
from app.weather.evidence.station_evidence import (
    gather_station_evidence,
    qualitative_provider_count,
)
from app.weather.evidence.types import (
    DailyObservation,
    LocalTvEvidence,
    MetarEvidence,
    StationEvidence,
    WundergroundHistoryEvidence,
)
from app.weather.evidence.wunderground_history import (
    build_wunderground_history_url,
    compute_station_bias_f,
    map_observed_to_band,
    polymarket_round_temp_f,
    probe_wunderground_history_page,
)
from app.weather.forecast_ensemble import INDEPENDENT_MIN_PROVIDERS, _collect_independent_providers
from app.weather.stations import WeatherStation, get_station
from app.weather.temperature_units import TemperatureUnit
from tests.conftest import make_test_settings


KORD = WeatherStation(
    "KORD",
    "Chicago O'Hare",
    41.9786,
    -87.9048,
    timezone="America/Chicago",
    verified=True,
)


def test_exact_station_enforcement_city_is_not_station():
    assert get_station("KORD") is not None
    assert get_station("Chicago") is None
    url = build_wunderground_history_url("KORD", date(2026, 7, 14))
    assert "KORD" in url
    assert "chicago" not in url.lower() or "KORD" in url


def test_wunderground_history_url_and_rounding():
    url = build_wunderground_history_url(
        "KMIA",
        date(2026, 7, 10),
        resolution_url="https://www.wunderground.com/history/airport/KMIA",
    )
    assert "wunderground.com" in url
    assert "KMIA" in url
    assert polymarket_round_temp_f(84.4) == 84
    assert polymarket_round_temp_f(84.5) == 85
    assert polymarket_round_temp_f(84.6) == 85


def test_final_observed_high_low_band_mapping():
    outcomes = [
        SiblingOutcome(
            market_id="1",
            market=MagicMock(),
            label="84-85°F",
            kind=OutcomeKind.BRACKET,
            unit=TemperatureUnit.FAHRENHEIT,
            lower_bound=84,
            upper_bound=85,
        ),
        SiblingOutcome(
            market_id="2",
            market=MagicMock(),
            label="86°F or higher",
            kind=OutcomeKind.ABOVE_TAIL,
            unit=TemperatureUnit.FAHRENHEIT,
            lower_bound=86,
        ),
    ]
    rounded = polymarket_round_temp_f(84.6)
    matched = map_observed_to_band(rounded, outcomes)
    assert matched is not None
    assert matched.label == "84-85°F"
    assert map_observed_to_band(90, outcomes).kind == OutcomeKind.ABOVE_TAIL


def test_no_scraped_weather_com_key_use_in_history_probe():
    html = (
        'apiKey=' + ('x' * 32) + ' '
        '"calendarDayTemperatureMax":[90,91] lib-city-history'
    )
    with patch("app.weather.evidence.wunderground_history.requests.get") as mock_get:
        resp = MagicMock()
        resp.status_code = 200
        resp.text = html
        mock_get.return_value = resp
        ok, note = probe_wunderground_history_page(
            "https://www.wunderground.com/history/daily/KORD/date/2026-07-14"
        )
    assert ok is True
    assert note == "FORECAST_BLOB_ONLY"
    # Ensure we never issued api.weather.com from this module path
    called_url = mock_get.call_args[0][0]
    assert "api.weather.com" not in called_url


def test_metar_parsing_and_stale_rejection():
    fresh = {
        "temp": 28.0,
        "dewp": 18.0,
        "wdir": 180,
        "wspd": 10,
        "wxString": "TSRA",
        "clouds": [{"cover": "BKN", "base": 4000}],
        "reportTime": datetime.now(timezone.utc).isoformat(),
    }
    stale = dict(fresh)
    stale["reportTime"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()

    with patch("app.weather.evidence.metar.requests.get") as mock_get, patch(
        "app.weather.evidence.metar._daily_max_from_nws_obs", return_value=29.0
    ):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [fresh],
            raise_for_status=lambda: None,
        )
        evidence = fetch_metar_evidence(KORD)
    assert evidence.stale is False
    assert evidence.temperature_c == 28.0
    assert evidence.weather_code == "TSRA"

    with patch("app.weather.evidence.metar.requests.get") as mock_get, patch(
        "app.weather.evidence.metar._daily_max_from_nws_obs", return_value=None
    ):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [stale],
            raise_for_status=lambda: None,
        )
        stale_ev = fetch_metar_evidence(KORD)
    assert stale_ev.stale is True
    assert stale_ev.daily_max_c is None


def test_nws_discussion_extraction():
    text = (
        "Thunderstorms are expected this afternoon near peak heating. "
        "A cold front will pass tonight. Clouds clearing by midday. "
        "Marine layer may linger along the coast. Low confidence in storm timing."
    )
    signals = extract_discussion_signals(text)
    assert signals["thunderstorm_timing"]
    assert signals["frontal_timing"]
    assert signals["cloud_clearing"]
    assert signals["marine_layer"]
    assert signals["confidence_language"]


def test_local_tv_stale_and_ambiguous_rejection():
    old = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
    stale_html = f'''
    <html><head><meta property="article:modified_time" content="{old}"/></head>
    <body>Chicago high near 88°F with storms. O'Hare looks warm.</body></html>
    '''
    ambiguous_html = """
    <html><head><meta property="article:modified_time" content="2026-07-15T12:00:00+00:00"/></head>
    <body>Chicago weather outlook for the weekend across the region.</body></html>
    """
    assert _extract_high_f(ambiguous_html) is None

    with patch("app.weather.evidence.local_tv.requests.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, text=stale_html)
        items = fetch_local_tv_evidence("KORD")
    assert items
    assert all(not item.accepted for item in items)
    assert any(item.reject_reason == "STALE" for item in items)


def test_qualitative_sources_not_counted_as_providers():
    evidence = StationEvidence(
        station_code="KORD",
        nws_discussion=MagicMock(),
        local_tv=(
            LocalTvEvidence(
                station_code="KORD",
                source_name="ABC7",
                source_url="https://example.com",
                published_at=datetime.now(timezone.utc),
                high_f=88.0,
                narrative="Sunny",
                accepted=True,
            ),
        ),
    )
    assert qualitative_provider_count(evidence) == 0
    assert INDEPENDENT_MIN_PROVIDERS == 3


def test_station_bias_from_3_to_5_days():
    days = [
        DailyObservation("KORD", date(2026, 7, 10), 90.0, 70.0, "asos_iem"),
        DailyObservation("KORD", date(2026, 7, 11), 91.0, 71.0, "asos_iem"),
        DailyObservation("KORD", date(2026, 7, 12), 92.0, 72.0, "asos_iem"),
        DailyObservation("KORD", date(2026, 7, 13), 93.0, 73.0, "asos_iem"),
    ]
    bias, n = compute_station_bias_f(days, forecast_highs_f=[89, 90, 91, 92], min_days=3)
    assert n == 4
    assert bias == pytest.approx(1.0)
    bias2, n2 = compute_station_bias_f(days[:2], min_days=3)
    assert bias2 is None
    assert n2 == 2


def test_high_low_market_separation_in_evidence_reason():
    settings = make_test_settings()
    with patch(
        "app.weather.evidence.station_evidence.fetch_wunderground_history_evidence",
        return_value=WundergroundHistoryEvidence(
            station_code="KORD",
            history_url="https://www.wunderground.com/history/daily/KORD",
            recent_days=(),
        ),
    ), patch(
        "app.weather.evidence.station_evidence.fetch_metar_evidence",
        return_value=MetarEvidence(
            station_code="KORD",
            observed_at=datetime.now(timezone.utc),
            temperature_c=25.0,
            dewpoint_c=18.0,
            wind_dir_deg=180,
            wind_speed_kt=8,
            cloud_cover="BKN",
            weather_code="TSRA",
            daily_max_c=26.0,
            stale=False,
        ),
    ), patch(
        "app.weather.evidence.station_evidence.fetch_nws_discussion",
        return_value=None,
    ), patch(
        "app.weather.evidence.station_evidence.fetch_local_tv_evidence",
        return_value=(),
    ), patch(
        "app.weather.evidence.station_evidence._local_hour",
        return_value=11,
    ):
        high_ev = gather_station_evidence(
            settings,
            KORD,
            target=date(2026, 7, 16),
            event_type=NormalizedTemperatureType.DAILY_HIGH,
            resolution_url=None,
            forecast_stats=None,
        )
        low_ev = gather_station_evidence(
            settings,
            KORD,
            target=date(2026, 7, 16),
            event_type=NormalizedTemperatureType.DAILY_LOW,
            resolution_url=None,
            forecast_stats=None,
        )
    assert high_ev.short_reason == "Morning storms may cap the high"
    assert low_ev.short_reason != "Morning storms may cap the high"


def test_telegram_card_unchanged_structure():
    analysis = MagicMock()
    analysis.city_location = "Chicago"
    analysis.target_date = "July 15"
    analysis.temperature_type = NormalizedTemperatureType.DAILY_HIGH
    analysis.temperature_type_label = "Daily high temperature"
    analysis.forecast_display = "88°F"
    analysis.expected_range_display = "86–90°F"
    analysis.best_outcome = "88-89°F"
    analysis.implied_probability = 0.22
    analysis.display_status = "ACTION"
    analysis.display_reason = "Strong value."
    analysis.wunderground_history_url = "https://www.wunderground.com/history/daily/KORD"
    analysis.station_bias_f = 1.2
    analysis.live_triggers = ("ignored",)
    analysis.forecast_sources_line = "NWS, Weather.com, MET Norway"
    analysis.metar_stale = False

    text = format_concise_card(analysis)
    assert "Market: Daily high temperature" in text
    assert "Forecast: 88°F" in text
    assert "Decision: ACTION" in text
    assert "Reason: Strong value." in text
    for forbidden in ("Wunderground", "METAR", "ABC7", "Station bias", "Band |"):
        assert forbidden not in text


def test_station_unresolved_reason():
    assert (
        short_display_reason(
            display_status="SKIP",
            validation_error="STATION_UNRESOLVED",
            betting_skip_reason=None,
            station_error="STATION_UNRESOLVED",
        )
        == "Station unresolved."
    )


def test_collect_providers_still_numerical_only():
    settings = make_test_settings()
    with patch("app.weather.forecast_ensemble.fetch_nws_hourly", return_value=MagicMock(provider_name="NWS")), patch(
        "app.weather.forecast_ensemble.fetch_weather_com_daily",
        return_value=MagicMock(provider_name="Weather.com"),
    ), patch(
        "app.weather.forecast_ensemble.fetch_met_norway",
        return_value=MagicMock(provider_name="MET Norway"),
    ), patch(
        "app.weather.forecast_ensemble.fetch_ecmwf_ifs",
        return_value=MagicMock(provider_name="ECMWF IFS"),
    ), patch(
        "app.weather.forecast_ensemble.fetch_dwd_icon_global",
        return_value=MagicMock(provider_name="DWD ICON Global"),
    ):
        providers = _collect_independent_providers(
            settings,
            KORD,
            date(2026, 7, 16),
            NormalizedTemperatureType.DAILY_HIGH,
            "America/Chicago",
        )
    names = {p.provider_name for p in providers}
    assert names == {
        "NWS",
        "Weather.com",
        "MET Norway",
        "ECMWF IFS",
        "DWD ICON Global",
    }
    assert "Local TV" not in names
    assert "METAR" not in names
    assert "NWS Discussion" not in names
    assert "Open-Meteo" not in names


def test_asos_daily_parse_shape():
    payload = {
        "data": [
            {"date": "2026-07-14", "max_tmpf": 96.0, "min_tmpf": 74.0},
            {"date": "2026-07-15", "max_tmpf": 95.0, "min_tmpf": 76.0},
        ]
    }
    with patch("app.weather.evidence.asos_daily.requests.get") as mock_get:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: payload,
            raise_for_status=lambda: None,
        )
        rows = fetch_asos_daily_range("KORD", end=date(2026, 7, 15), days=5)
    assert len(rows) == 2
    assert rows[-1].high_f == 95.0
    assert rows[-1].source == "asos_iem"
