from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.polymarket.market_type import TemperatureMarketType
from app.polymarket.markets import _limit_parent_events
from app.polymarket.models import PolymarketMarket
from app.region.classifier import (
    RegionClassification,
    classify_market_region,
    event_matches_scan_region,
)
from app.region.types import ScanRegion
from app.signals.market_analysis import MarketAnalysis
from app.signals.scan_service import scan_weather_markets
from app.telegram.handlers import scan_command
from app.weather.station_resolver import extract_station_code_from_text
from app.weather.stations import WeatherStation
from tests.conftest import make_test_settings


LIVE_ORIGINAL_EVENTS = (
    ("Busan", "RKPK", "kr", 35.179, 128.938, "KR", ScanRegion.ASIA),
    ("Helsinki", "EFHK", "fi", 60.327, 24.957, "FI", ScanRegion.EUROPE),
    ("Guangzhou", "ZGGG", "cn", 23.392, 113.307, "CN", ScanRegion.ASIA),
    ("Jeddah", "OEJN", "sa", 21.685, 39.166, "SA", ScanRegion.ASIA),
    ("Lucknow", "VILK", "in", 26.761, 80.889, "IN", ScanRegion.ASIA),
    ("Panama City", "MPMG", "pa", 8.967, -79.555, "PA", None),
)


def _event_text(city: str, station: str, country_path: str) -> str:
    return (
        f"Highest temperature in {city} on July 16?\n"
        f"Resolved at {station} Airport Station using "
        f"https://www.wunderground.com/history/daily/{country_path}/{city.lower().replace(' ', '-')}/{station}."
    )


def _market(
    market_id: str,
    *,
    event_id: str,
    city: str,
    station: str,
    country_path: str = "us/wa",
) -> PolymarketMarket:
    text = _event_text(city, station, country_path)
    event_slug = f"highest-temperature-in-{city.lower().replace(' ', '-')}-on-july-16-2026"
    return PolymarketMarket(
        id=market_id,
        slug=f"{event_slug}-{market_id}",
        question=f"Will the highest temperature in {city} be 80°F?",
        description=text,
        rules=text,
        outcomes=["Yes", "No"],
        outcome_prices=[0.2, 0.8],
        volume=1000.0,
        liquidity=500.0,
        end_date="2099-01-01T00:00:00Z",
        raw={
            "closed": False,
            "events": [
                {
                    "id": event_id,
                    "slug": event_slug,
                    "title": f"Highest temperature in {city} on July 16?",
                    "description": text,
                }
            ],
        },
    )


def _analysis(city: str = "Seattle", station: str = "KSEA") -> MarketAnalysis:
    return MarketAnalysis(
        market_title=f"Highest temperature in {city}",
        market_url="https://polymarket.com/event/test",
        city_location=city,
        station_code=station,
        resolution_source="weather_underground",
        target_date="July 16, 2026",
        market_type=TemperatureMarketType.MAXIMUM_TEMPERATURE,
        threshold_f=80.0,
        threshold_unit="FAHRENHEIT",
        yes_price=0.2,
        implied_probability=0.2,
        model_probability=0.35,
        edge=0.15,
        ev=0.1,
        kelly_fraction=0.01,
        kelly_cap=0.02,
        confidence="MEDIUM",
        recommendation_tier="WATCH",
        reason="Verified station forecast; actionable threshold not met.",
        data_sources=["open_meteo"],
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        message="watch",
        confidence_score=0.6,
        best_outcome="80°F",
        forecast_display="79°F",
        expected_range_display="76–82°F",
        display_status="WATCH",
        display_reason="Verified station forecast; actionable threshold not met.",
    )


@pytest.mark.parametrize(
    "city,code,path,lat,lon,country,expected_region",
    LIVE_ORIGINAL_EVENTS,
)
def test_original_live_parent_event_evidence(
    city,
    code,
    path,
    lat,
    lon,
    country,
    expected_region,
):
    text = _event_text(city, code, path)
    station = WeatherStation(code, city, lat, lon, verified=True)
    classification = classify_market_region(
        text,
        station_code=code,
        station=station,
        event_metadata={"title": f"Highest temperature in {city}"},
    )
    assert classification.country_code == country
    assert classification.region == expected_region
    assert classification.conflict is False
    matches, reason = event_matches_scan_region(
        text,
        ScanRegion.USA,
        classification=classification,
    )
    assert matches is False
    assert reason == "REGION_MISMATCH"


@pytest.mark.parametrize(
    "city,code,lat,lon",
    (
        ("Miami", "KMIA", 25.7959, -80.2870),
        ("Chicago", "KORD", 41.9786, -87.9048),
        ("New York", "KJFK", 40.6413, -73.7781),
        ("Los Angeles", "KLAX", 33.9416, -118.4085),
        ("Seattle", "KSEA", 47.4502, -122.3088),
        ("Honolulu", "PHNL", 21.3187, -157.9225),
    ),
)
def test_verified_usa_station_cannot_be_region_mismatch(city, code, lat, lon):
    text = _event_text(city, code, "us")
    station = WeatherStation(code, city, lat, lon, verified=True)
    classification = classify_market_region(text, station_code=code, station=station)
    assert classification.region is ScanRegion.USA
    assert classification.source == "coordinates"
    assert event_matches_scan_region(
        text,
        ScanRegion.USA,
        classification=classification,
    ) == (True, None)


def test_coordinates_can_classify_usa_without_city_or_prefix():
    station = WeatherStation("XXXX", "Verified location", 39.0, -98.0, verified=True)
    result = classify_market_region("Temperature event", station=station)
    assert result.region is ScanRegion.USA
    assert result.source == "coordinates"


def test_non_us_station_remains_excluded():
    station = WeatherStation("EGLL", "London Heathrow", 51.47, -0.4543, verified=True)
    result = classify_market_region(
        _event_text("London", "EGLL", "gb"),
        station=station,
    )
    assert result.region is ScanRegion.EUROPE
    assert event_matches_scan_region(
        "London temperature",
        ScanRegion.USA,
        classification=result,
    ) == (False, "REGION_MISMATCH")


def test_unresolved_when_no_geographic_evidence_exists():
    result = classify_market_region("Temperature market with no location")
    assert result.is_unknown
    assert result.reason == "REGION_UNRESOLVED"


def test_hong_kong_text_is_not_misread_as_usa_station():
    text = "Highest temperature in Hong Kong on July 15?"
    assert extract_station_code_from_text(text) is None
    result = classify_market_region(text)
    assert result.region is ScanRegion.ASIA
    assert result.country_code == "HK"


def test_conflicting_station_and_coordinates_are_explicit():
    station = WeatherStation("KJFK", "Conflicting station", 48.8566, 2.3522, verified=True)
    result = classify_market_region("Temperature at KJFK", station=station)
    assert result.is_conflict
    assert result.reason.startswith("REGION_CONFLICT")
    assert event_matches_scan_region(
        "Temperature at KJFK",
        ScanRegion.USA,
        classification=result,
    ) == (False, "REGION_CONFLICT")


def test_parent_limit_keeps_complete_child_sets():
    children = [
        _market(
            f"{parent}-{child}",
            event_id=f"parent-{parent}",
            city=f"City {parent}",
            station="KSEA",
        )
        for parent in range(6)
        for child in range(11)
    ]
    assert len(_limit_parent_events(children, 6)) == 66
    assert len(_limit_parent_events(children, 5)) == 55


def test_parent_classification_is_inherited_by_all_children(monkeypatch):
    children = [
        _market(str(index), event_id="usa-parent", city="Seattle", station="KSEA")
        for index in range(3)
    ]
    calls = 0

    def classify_once(group):
        nonlocal calls
        calls += 1
        return RegionClassification(
            region=ScanRegion.USA,
            country_code="US",
            source="coordinates",
            reason="verified parent station coordinates",
            station_code="KSEA",
            latitude=47.4502,
            longitude=-122.3088,
        )

    monkeypatch.setattr(
        "app.signals.scan_service.get_weather_markets",
        lambda *args, **kwargs: (children, 3, 0),
    )
    monkeypatch.setattr("app.signals.scan_service._classify_parent_event", classify_once)
    monkeypatch.setattr(
        "app.signals.scan_service.evaluate_market_eligibility",
        lambda market, seen: (True, None, ""),
    )
    monkeypatch.setattr(
        "app.signals.scan_service.expand_group_children",
        lambda client, eligible: children,
    )
    monkeypatch.setattr(
        "app.signals.scan_service.analyze_temperature_event",
        lambda settings, expanded: _analysis(),
    )

    progress = scan_weather_markets(
        make_test_settings(max_results_per_report=10),
        region=ScanRegion.USA,
    )
    assert calls == 1
    assert progress.parent_events_in_region == 1
    assert progress.child_markets_analyzed == 3
    assert progress.parent_events_analyzed == 1


@pytest.mark.asyncio
async def test_production_telegram_usa_scan_reaches_analysis(monkeypatch):
    children = [
        _market(str(index), event_id="usa-parent", city="Seattle", station="KSEA")
        for index in range(3)
    ]
    monkeypatch.setattr(
        "app.signals.scan_service.get_weather_markets",
        lambda *args, **kwargs: (children, 3, 0),
    )
    monkeypatch.setattr(
        "app.signals.scan_service.evaluate_market_eligibility",
        lambda market, seen: (True, None, ""),
    )
    monkeypatch.setattr(
        "app.signals.scan_service.expand_group_children",
        lambda client, eligible: children,
    )
    monkeypatch.setattr(
        "app.signals.scan_service.analyze_temperature_event",
        lambda settings, expanded: _analysis(),
    )
    monkeypatch.setattr("app.telegram.handlers.upsert_user", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.telegram.handlers.persist_scan_results",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.telegram.handlers.save_scan_snapshot",
        lambda *args, **kwargs: None,
    )

    settings = make_test_settings(max_results_per_report=10)
    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    context.user_data = {"selected_scan_region": ScanRegion.USA}
    update = MagicMock()
    update.effective_user = MagicMock(id=42, username="u", first_name="Test")
    update.message = AsyncMock()
    update.message.chat.id = 42
    update.effective_message = update.message

    await scan_command(update, context)

    loading = update.message.reply_text.return_value
    loading.edit_text.assert_awaited_once()
    final_text = loading.edit_text.await_args.args[0]
    assert "Parent events retained in region: 1" in final_text
    assert "Child markets analyzed: 3" in final_text
    assert "Forecasts available: 1" in final_text
    assert "SEATTLE" in final_text
    assert "Status: WATCH" in final_text
