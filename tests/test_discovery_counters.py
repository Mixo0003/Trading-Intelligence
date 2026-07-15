from types import SimpleNamespace

from app.polymarket.models import PolymarketMarket
from app.region.types import ScanRegion
from app.signals.scan_service import scan_weather_markets
from app.telegram.formatting import format_scan_completed_summary
from tests.conftest import make_test_settings


def _child(market_id: str, label: str) -> PolymarketMarket:
    return PolymarketMarket(
        id=market_id,
        slug=f"atlanta-{market_id}",
        question=f"Will the highest temperature at KATL be {label} on July 16, 2026?",
        description="Resolved using Weather Underground station KATL.",
        rules="Weather Underground KATL daily high temperature.",
        outcomes=["Yes", "No"],
        outcome_prices=[0.2, 0.8],
        volume=1000.0,
        liquidity=500.0,
        end_date="2099-01-01T00:00:00Z",
        raw={
            "closed": False,
            "events": [
                {
                    "id": "parent-1",
                    "title": "Highest temperature in Atlanta on July 16?",
                }
            ],
        },
    )


def _analysis():
    return SimpleNamespace(
        station_code="KATL",
        resolution_source="weather_underground",
        forecast_display="90°F",
        yes_price=0.2,
        ev=None,
        liquidity=500.0,
        is_priority_us_city=True,
        confidence_score=0.5,
        display_status="FORECAST ONLY",
    )


def _run(monkeypatch, region: ScanRegion):
    children = [
        _child("one", "89°F or below"),
        _child("two", "90°F"),
        _child("three", "91°F or above"),
    ]
    monkeypatch.setattr(
        "app.signals.scan_service.get_weather_markets",
        lambda *args, **kwargs: (children, 1, 0),
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
    return scan_weather_markets(
        make_test_settings(max_results_per_report=10),
        region=region,
    )


def test_one_parent_multiple_children_regional_scan(monkeypatch):
    progress = _run(monkeypatch, ScanRegion.USA)
    assert progress.parent_events_discovered == 1
    assert progress.child_markets_discovered == 3
    assert progress.parent_events_in_region == 1
    assert progress.child_markets_loaded_for_analysis == 3
    assert progress.parent_events_analyzed == 1


def test_one_parent_multiple_children_all_scan(monkeypatch):
    progress = _run(monkeypatch, ScanRegion.ALL)
    assert progress.parent_events_discovered == 1
    assert progress.child_markets_discovered == 3
    assert progress.parent_events_in_region == 1
    assert progress.parent_events_analyzed == 1

    text = format_scan_completed_summary(progress)
    assert "Parent events discovered globally: 1" in text
    assert "Child markets discovered globally: 3" in text
    assert "Parent events retained in region: 1" in text
    assert "Child markets loaded for analysis: 3" in text
    assert "Parent event analyses completed: 1" in text
