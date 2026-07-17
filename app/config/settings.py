import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from app.config.constants import (
    DEFAULT_DB_PATH,
    DEFAULT_LANGUAGE,
    DEFAULT_LOG_LEVEL,
    POLYMARKET_CLOB_BASE_URL,
    POLYMARKET_DATA_BASE_URL,
    POLYMARKET_GAMMA_BASE_URL,
    PROJECT_NAME,
    PROJECT_VERSION,
    WEATHER_GOV_BASE_URL,
)


load_dotenv()

if TYPE_CHECKING:
    from app.region.types import ScanRegion

DEFAULT_PRIORITY_WEATHER_CITIES = (
    "Chicago,New York City,Miami,Atlanta,Seattle,Los Angeles"
)


def _split_ints(value: str) -> list[int]:
    if not value:
        return []
    result: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if item.isdigit():
            result.append(int(item))
    return result


def _split_times(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _split_cities(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _split_regions(value: str) -> list:
    from app.region.types import ScanRegion, parse_scan_region

    if not value:
        return []
    regions: list[ScanRegion] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        region = parse_scan_region(item)
        if region not in regions:
            regions.append(region)
    return regions


@dataclass(frozen=True)
class Settings:
    project_name: str
    project_version: str

    telegram_token: str
    anthropic_api_key: str

    admin_ids: list[int]
    guest_ids: list[int]

    default_language: str
    database_path: str
    log_level: str

    polymarket_gamma_base_url: str
    polymarket_data_base_url: str
    polymarket_clob_base_url: str
    weather_gov_base_url: str

    copy_intel_enabled: bool
    copy_intel_read_only: bool
    copy_paper_bankroll_usd: float
    copy_poll_interval_seconds: int
    copy_max_trade_age_min: int
    copy_max_event_age_seconds: int
    copy_min_resolved_trades: int
    copy_min_liquidity_usd: float
    copy_max_slippage_pct: float

    health_monitor_enabled: bool
    health_check_interval_seconds: int
    health_alerts_enabled: bool
    health_failure_threshold: int
    health_recovery_threshold: int
    health_job_stale_multiplier: float
    health_ai_explainer_enabled: bool

    report_timezone: str
    report_times: list[str]
    max_markets_per_scan: int
    max_results_per_report: int
    auto_reports_enabled: bool
    priority_weather_cities: list[str]
    anthropic_rule_fallback_enabled: bool
    forecast_divergence_threshold_f: float
    forecast_divergence_threshold_c: float
    forecast_grid_max_distance_km: float
    forecast_min_providers: int
    auto_report_regions: list
    weather_discovery_cache_ttl_seconds: int
    weather_scan_concurrency: int
    weather_scan_deadline_seconds: int

    @property
    def scheduled_reports_configured(self) -> bool:
        return bool(
            self.report_timezone
            and self.report_times
            and self.auto_reports_enabled
            and self.auto_report_regions
        )


def load_settings() -> Settings:
    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if not telegram_token:
        raise RuntimeError("Missing TELEGRAM_TOKEN in .env")

    # Default raised from 50 to 200: worldwide keyset discovery now finds ~135
    # active daily-temperature parent events, and the old cap silently
    # truncated Scan All before region classification ever ran.
    max_markets = int(os.getenv("MAX_MARKETS_PER_SCAN", "200"))
    max_results = int(os.getenv("MAX_RESULTS_PER_REPORT", "5"))
    priority_cities = _split_cities(os.getenv("PRIORITY_WEATHER_CITIES", ""))
    if not priority_cities:
        priority_cities = _split_cities(DEFAULT_PRIORITY_WEATHER_CITIES)

    return Settings(
        project_name=PROJECT_NAME,
        project_version=PROJECT_VERSION,
        telegram_token=telegram_token,
        anthropic_api_key=anthropic_api_key,
        admin_ids=_split_ints(os.getenv("ADMIN_IDS", "")),
        guest_ids=_split_ints(os.getenv("GUEST_IDS", "")),
        default_language=os.getenv("DEFAULT_LANGUAGE", DEFAULT_LANGUAGE),
        database_path=os.getenv("DATABASE_PATH", DEFAULT_DB_PATH),
        log_level=os.getenv("LOG_LEVEL", DEFAULT_LOG_LEVEL),
        polymarket_gamma_base_url=os.getenv(
            "POLYMARKET_GAMMA_BASE_URL",
            POLYMARKET_GAMMA_BASE_URL,
        ),
        polymarket_data_base_url=os.getenv(
            "POLYMARKET_DATA_BASE_URL",
            POLYMARKET_DATA_BASE_URL,
        ),
        polymarket_clob_base_url=os.getenv(
            "POLYMARKET_CLOB_BASE_URL",
            POLYMARKET_CLOB_BASE_URL,
        ),
        weather_gov_base_url=os.getenv(
            "WEATHER_GOV_BASE_URL",
            WEATHER_GOV_BASE_URL,
        ),
        report_timezone=os.getenv("REPORT_TIMEZONE", "").strip(),
        report_times=_split_times(os.getenv("REPORT_TIMES", "")),
        max_markets_per_scan=max_markets,
        max_results_per_report=max_results,
        auto_reports_enabled=_parse_bool(os.getenv("AUTO_REPORTS_ENABLED", "false")),
        priority_weather_cities=priority_cities,
        anthropic_rule_fallback_enabled=_parse_bool(
            os.getenv("ANTHROPIC_RULE_FALLBACK_ENABLED", "false")
        ),
        forecast_divergence_threshold_f=float(
            os.getenv("FORECAST_DIVERGENCE_THRESHOLD_F", "3.0")
        ),
        forecast_divergence_threshold_c=float(
            os.getenv("FORECAST_DIVERGENCE_THRESHOLD_C", "2.0")
        ),
        forecast_grid_max_distance_km=float(
            os.getenv("FORECAST_GRID_MAX_DISTANCE_KM", "15.0")
        ),
        forecast_min_providers=int(os.getenv("FORECAST_MIN_PROVIDERS", "2")),
        auto_report_regions=_split_regions(
            os.getenv("AUTO_REPORT_REGIONS", "USA,EUROPE,ASIA")
        ),
        weather_discovery_cache_ttl_seconds=int(
            os.getenv("WEATHER_DISCOVERY_CACHE_TTL_SECONDS", "300")
        ),
        weather_scan_concurrency=max(
            1, int(os.getenv("WEATHER_SCAN_CONCURRENCY", "6"))
        ),
        weather_scan_deadline_seconds=max(
            30, int(os.getenv("WEATHER_SCAN_DEADLINE_SECONDS", "600"))
        ),
        copy_intel_enabled=_parse_bool(os.getenv("COPY_INTEL_ENABLED", "false")),
        copy_intel_read_only=_parse_bool(os.getenv("COPY_INTEL_READ_ONLY", "true"), default=True),
        copy_paper_bankroll_usd=float(os.getenv("COPY_PAPER_BANKROLL_USD", "10000")),
        copy_poll_interval_seconds=max(60, int(os.getenv("COPY_POLL_INTERVAL_SECONDS", "120"))),
        copy_max_trade_age_min=max(1, int(os.getenv("COPY_MAX_TRADE_AGE_MIN", "30"))),
        copy_max_event_age_seconds=max(1, int(os.getenv("COPY_MAX_EVENT_AGE_SECONDS", "600"))),
        copy_min_resolved_trades=max(1, int(os.getenv("COPY_MIN_RESOLVED_TRADES", "20"))),
        copy_min_liquidity_usd=float(os.getenv("COPY_MIN_LIQUIDITY_USD", "500")),
        copy_max_slippage_pct=float(os.getenv("COPY_MAX_SLIPPAGE_PCT", "0.02")),
        health_monitor_enabled=_parse_bool(os.getenv("HEALTH_MONITOR_ENABLED", "true"), default=True),
        health_check_interval_seconds=max(30, int(os.getenv("HEALTH_CHECK_INTERVAL_SECONDS", "300"))),
        health_alerts_enabled=_parse_bool(os.getenv("HEALTH_ALERTS_ENABLED", "true"), default=True),
        health_failure_threshold=max(1, int(os.getenv("HEALTH_FAILURE_THRESHOLD", "3"))),
        health_recovery_threshold=max(1, int(os.getenv("HEALTH_RECOVERY_THRESHOLD", "2"))),
        health_job_stale_multiplier=max(1.0, float(os.getenv("HEALTH_JOB_STALE_MULTIPLIER", "2"))),
        health_ai_explainer_enabled=_parse_bool(
            os.getenv("HEALTH_AI_EXPLAINER_ENABLED", "false")
        ),
    )
