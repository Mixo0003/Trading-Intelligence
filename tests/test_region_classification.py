"""Region classification tests."""

import pytest

from app.region.classifier import classify_market_region, event_matches_scan_region
from app.region.types import ScanRegion


class TestStationClassification:
    @pytest.mark.parametrize(
        ("station", "expected"),
        [
            ("KMIA", ScanRegion.USA),
            ("KORD", ScanRegion.USA),
            ("LFPB", ScanRegion.EUROPE),
            ("EGLL", ScanRegion.EUROPE),
            ("ZGGG", ScanRegion.ASIA),
            ("RCTP", ScanRegion.ASIA),
        ],
    )
    def test_known_stations(self, station, expected):
        text = f"Will the daily high temperature at {station} exceed 90°F on July 15, 2026?"
        result = classify_market_region(text, station_code=station)
        assert result.region is expected
        assert result.source == "station"

    def test_unknown_country_is_unresolved(self):
        text = "Will the daily high temperature at XXXX exceed 90°F on July 15, 2026?"
        result = classify_market_region(text, station_code="XXXX")
        assert result.region is None
        assert result.reason == "REGION_UNRESOLVED"


class TestEventMatching:
    def test_usa_scan_rejects_paris(self):
        text = "Highest temperature in Paris on July 16? Station LFPB."
        matches, reason = event_matches_scan_region(text, ScanRegion.USA)
        assert matches is False
        assert reason == "REGION_MISMATCH"

    def test_europe_scan_rejects_miami(self):
        text = "Highest temperature in Miami on July 16? Station KMIA."
        matches, reason = event_matches_scan_region(text, ScanRegion.EUROPE)
        assert matches is False
        assert reason == "REGION_MISMATCH"

    def test_asia_scan_rejects_london(self):
        text = "Highest temperature in London on July 16? Station EGLL."
        matches, reason = event_matches_scan_region(text, ScanRegion.ASIA)
        assert matches is False
        assert reason == "REGION_MISMATCH"

    def test_unknown_excluded_from_regional_scans(self):
        text = "Highest temperature in Unknownville on July 16?"
        matches, reason = event_matches_scan_region(text, ScanRegion.USA)
        assert matches is False
        assert reason == "REGION_UNRESOLVED"

    def test_all_region_accepts_unknown_for_later_verification(self):
        text = "Highest temperature in Unknownville on July 16?"
        matches, reason = event_matches_scan_region(text, ScanRegion.ALL)
        assert matches is True
        assert reason is None
