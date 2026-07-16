import pytest

from app.database.storage import initialize_database, mark_report_sent, was_report_sent
from app.config.settings import Settings
from app.region.types import ScanRegion


@pytest.fixture
def settings(tmp_path):
    return Settings(
        project_name="TI",
        project_version="0.1.0",
        telegram_token="token",
        anthropic_api_key="",
        admin_ids=[1],
        guest_ids=[],
        default_language="en",
        database_path=str(tmp_path / "test.db"),
        log_level="INFO",
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_clob_base_url="https://clob.polymarket.com",
        weather_gov_base_url="https://api.weather.gov",
        report_timezone="America/New_York",
        report_times=["08:00"],
        max_markets_per_scan=50,
        max_results_per_report=10,
        auto_reports_enabled=True,
        priority_weather_cities=["Chicago"],
        anthropic_rule_fallback_enabled=False,
        forecast_divergence_threshold_f=3.0,
        forecast_divergence_threshold_c=2.0,
        forecast_grid_max_distance_km=15.0,
        forecast_min_providers=2,
        auto_report_regions=[ScanRegion.USA],
        weather_discovery_cache_ttl_seconds=300,
        weather_scan_concurrency=6,
        weather_scan_deadline_seconds=600,
    )


def test_report_deduplication(settings):
    initialize_database(settings)
    assert was_report_sent(settings, "2026-07-15 08:00", "https://polymarket.com/market/a") is False
    mark_report_sent(settings, "2026-07-15 08:00", "https://polymarket.com/market/a", "Market A", "WATCH")
    assert was_report_sent(settings, "2026-07-15 08:00", "https://polymarket.com/market/a") is True
