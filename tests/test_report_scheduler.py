import pytest

from app.database.storage import initialize_database, mark_report_sent, was_report_sent
from tests.conftest import make_test_settings
from app.region.types import ScanRegion


@pytest.fixture
def settings(tmp_path):
    return make_test_settings(
        database_path=str(tmp_path / "test.db"),
        report_timezone="America/New_York",
        report_times=["08:00"],
        auto_reports_enabled=True,
        auto_report_regions=[ScanRegion.USA],
    )


def test_report_deduplication(settings):
    initialize_database(settings)
    assert was_report_sent(settings, "2026-07-15 08:00", "https://polymarket.com/market/a") is False
    mark_report_sent(settings, "2026-07-15 08:00", "https://polymarket.com/market/a", "Market A", "WATCH")
    assert was_report_sent(settings, "2026-07-15 08:00", "https://polymarket.com/market/a") is True
