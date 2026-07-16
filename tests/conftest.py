from app.region.types import ScanRegion

import pytest

from app.config.settings import Settings


def make_test_settings(**overrides) -> Settings:
    base = dict(
        project_name="TI",
        project_version="0.1.0",
        telegram_token="token",
        anthropic_api_key="",
        admin_ids=[1],
        guest_ids=[],
        default_language="en",
        database_path=":memory:",
        log_level="INFO",
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_data_base_url="https://data-api.polymarket.com",
        polymarket_clob_base_url="https://clob.polymarket.com",
        weather_gov_base_url="https://api.weather.gov",
        report_timezone="America/Chicago",
        report_times=["08:00", "12:00", "20:00"],
        max_markets_per_scan=50,
        max_results_per_report=5,
        auto_reports_enabled=False,
        priority_weather_cities=[
            "Chicago",
            "New York City",
            "Miami",
            "Atlanta",
            "Seattle",
            "Los Angeles",
        ],
        anthropic_rule_fallback_enabled=False,
        forecast_divergence_threshold_f=3.0,
        forecast_divergence_threshold_c=2.0,
        forecast_grid_max_distance_km=15.0,
        forecast_min_providers=2,
        auto_report_regions=[ScanRegion.USA, ScanRegion.EUROPE, ScanRegion.ASIA],
        weather_discovery_cache_ttl_seconds=300,
        weather_scan_concurrency=6,
        weather_scan_deadline_seconds=600,
        copy_intel_enabled=False,
        copy_intel_read_only=True,
        copy_paper_bankroll_usd=10000.0,
        copy_poll_interval_seconds=120,
        copy_max_trade_age_min=30,
        copy_min_resolved_trades=20,
        copy_min_liquidity_usd=500.0,
        copy_max_slippage_pct=0.02,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def test_settings() -> Settings:
    return make_test_settings()
