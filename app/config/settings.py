import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from app.config.constants import (
    DEFAULT_DB_PATH,
    DEFAULT_LANGUAGE,
    DEFAULT_LOG_LEVEL,
    DEFAULT_STORAGE_ACTIVE_LOG_NAME,
    DEFAULT_STORAGE_BACKUP_DIR,
    DEFAULT_STORAGE_CACHE_DIR,
    DEFAULT_STORAGE_LOGS_DIR,
    DEFAULT_STORAGE_PROOF_DIR,
    POLYMARKET_CLOB_BASE_URL,
    POLYMARKET_DATA_BASE_URL,
    POLYMARKET_GAMMA_BASE_URL,
    PROJECT_NAME,
    PROJECT_VERSION,
    STORAGE_BACKUP_CONFIRM_TTL_SECONDS,
    STORAGE_BACKUP_ENABLED,
    STORAGE_BACKUP_LABEL,
    STORAGE_BACKUP_LOCK_TTL_SECONDS,
    STORAGE_BACKUP_MAX_AGE_DAYS,
    STORAGE_BACKUP_MAX_COUNT,
    STORAGE_BACKUP_MIN_FREE_BYTES,
    STORAGE_CACHE_MIN_AGE_SECONDS,
    STORAGE_CLEANUP_CONFIRM_TTL_SECONDS,
    STORAGE_CLEANUP_DRY_RUN,
    STORAGE_CLEANUP_ENABLED,
    STORAGE_CLEANUP_LOCK_TTL_SECONDS,
    STORAGE_CLEANUP_MAX_BYTES_PER_RUN,
    STORAGE_CLEANUP_MAX_FILES_PER_RUN,
    STORAGE_CLEANUP_MIN_AGE_SECONDS,
    STORAGE_CRITICAL_DB_GROWTH_BYTES,
    STORAGE_CRITICAL_FREE_BYTES,
    STORAGE_CRITICAL_FREE_PERCENT,
    STORAGE_CRITICAL_WAL_BYTES,
    STORAGE_DEGRADED_DB_GROWTH_BYTES,
    STORAGE_DEGRADED_FREE_BYTES,
    STORAGE_DEGRADED_FREE_PERCENT,
    STORAGE_DEGRADED_WAL_BYTES,
    STORAGE_FILE_LOGGING_ENABLED,
    STORAGE_LOG_BACKUP_COUNT,
    STORAGE_LOG_MAX_BYTES,
    STORAGE_LOG_RETENTION_DAYS,
    STORAGE_PROOF_MIN_AGE_SECONDS,
    STORAGE_RETENTION_PRUNE_CONFIRM_TTL_SECONDS,
    STORAGE_RETENTION_PRUNE_DRY_RUN,
    STORAGE_RETENTION_PRUNE_ENABLED,
    STORAGE_RETENTION_PRUNE_LOCK_TTL_SECONDS,
    STORAGE_RETENTION_PRUNE_MAX_BACKUP_AGE_SECONDS,
    STORAGE_RETENTION_PRUNE_MAX_ROWS_PER_POLICY,
    STORAGE_RETENTION_PRUNE_MAX_ROWS_PER_RUN,
    STORAGE_RETENTION_PRUNE_STOP_ON_POLICY_FAILURE,
    STORAGE_RETENTION_PRUNE_TIMEOUT_SECONDS,
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


def _parse_bounded_int(
    raw: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
    name: str,
) -> int:
    text = default if raw is None or str(raw).strip() == "" else str(raw).strip()
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


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

    storage_logs_dir: str
    storage_cache_dir: str
    storage_proof_dir: str
    storage_active_log_name: str
    storage_file_logging_enabled: bool
    storage_cleanup_enabled: bool
    storage_cleanup_dry_run: bool
    storage_log_max_bytes: int
    storage_log_backup_count: int
    storage_log_retention_days: int
    storage_cleanup_min_age_seconds: int
    storage_cache_min_age_seconds: int
    storage_proof_min_age_seconds: int
    storage_cleanup_max_files_per_run: int
    storage_cleanup_max_bytes_per_run: int
    storage_cleanup_lock_ttl_seconds: int
    storage_degraded_free_bytes: int
    storage_critical_free_bytes: int
    storage_degraded_free_percent: float
    storage_critical_free_percent: float
    storage_degraded_db_growth_bytes: int
    storage_critical_db_growth_bytes: int
    storage_degraded_wal_bytes: int
    storage_critical_wal_bytes: int
    storage_backup_dir: str
    storage_backup_enabled: bool
    storage_backup_max_count: int
    storage_backup_max_age_days: int
    storage_backup_lock_ttl_seconds: int
    storage_backup_min_free_bytes: int
    storage_backup_label: str
    storage_backup_confirm_ttl_seconds: int
    storage_cleanup_confirm_ttl_seconds: int
    storage_retention_prune_enabled: bool
    storage_retention_prune_dry_run: bool
    storage_retention_prune_max_rows_per_run: int
    storage_retention_prune_max_rows_per_policy: int
    storage_retention_prune_timeout_seconds: int
    storage_retention_prune_max_backup_age_seconds: int
    storage_retention_prune_lock_ttl_seconds: int
    storage_retention_prune_confirm_ttl_seconds: int
    storage_retention_prune_stop_on_policy_failure: bool

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

    opportunity_scanner_enabled: bool
    opportunity_auto_alerts_enabled: bool
    opportunity_user_delivery_enabled: bool
    opportunity_scan_interval_seconds: int
    opportunity_discovery_ttl_seconds: int
    opportunity_max_markets: int
    opportunity_max_books_per_cycle: int
    opportunity_max_live_books_per_cycle: int
    opportunity_concurrency: int
    opportunity_cycle_deadline_seconds: int
    opportunity_first_run_delay_seconds: int

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
        storage_logs_dir=os.getenv("STORAGE_LOGS_DIR", DEFAULT_STORAGE_LOGS_DIR).strip(),
        storage_cache_dir=os.getenv("STORAGE_CACHE_DIR", DEFAULT_STORAGE_CACHE_DIR).strip(),
        storage_proof_dir=os.getenv("STORAGE_PROOF_DIR", DEFAULT_STORAGE_PROOF_DIR).strip(),
        storage_active_log_name=os.getenv(
            "STORAGE_ACTIVE_LOG_NAME", DEFAULT_STORAGE_ACTIVE_LOG_NAME
        ).strip()
        or DEFAULT_STORAGE_ACTIVE_LOG_NAME,
        storage_file_logging_enabled=_parse_bool(
            os.getenv("STORAGE_FILE_LOGGING_ENABLED", str(STORAGE_FILE_LOGGING_ENABLED).lower()),
            default=STORAGE_FILE_LOGGING_ENABLED,
        ),
        storage_cleanup_enabled=_parse_bool(
            os.getenv("STORAGE_CLEANUP_ENABLED", str(STORAGE_CLEANUP_ENABLED).lower()),
            default=STORAGE_CLEANUP_ENABLED,
        ),
        storage_cleanup_dry_run=_parse_bool(
            os.getenv("STORAGE_CLEANUP_DRY_RUN", str(STORAGE_CLEANUP_DRY_RUN).lower()),
            default=STORAGE_CLEANUP_DRY_RUN,
        ),
        storage_log_max_bytes=max(
            1024, int(os.getenv("STORAGE_LOG_MAX_BYTES", str(STORAGE_LOG_MAX_BYTES)))
        ),
        storage_log_backup_count=max(
            1, int(os.getenv("STORAGE_LOG_BACKUP_COUNT", str(STORAGE_LOG_BACKUP_COUNT)))
        ),
        storage_log_retention_days=max(
            1, int(os.getenv("STORAGE_LOG_RETENTION_DAYS", str(STORAGE_LOG_RETENTION_DAYS)))
        ),
        storage_cleanup_min_age_seconds=max(
            0,
            int(os.getenv("STORAGE_CLEANUP_MIN_AGE_SECONDS", str(STORAGE_CLEANUP_MIN_AGE_SECONDS))),
        ),
        storage_cache_min_age_seconds=max(
            0,
            int(os.getenv("STORAGE_CACHE_MIN_AGE_SECONDS", str(STORAGE_CACHE_MIN_AGE_SECONDS))),
        ),
        storage_proof_min_age_seconds=max(
            0,
            int(os.getenv("STORAGE_PROOF_MIN_AGE_SECONDS", str(STORAGE_PROOF_MIN_AGE_SECONDS))),
        ),
        storage_cleanup_max_files_per_run=max(
            1,
            int(
                os.getenv(
                    "STORAGE_CLEANUP_MAX_FILES_PER_RUN",
                    str(STORAGE_CLEANUP_MAX_FILES_PER_RUN),
                )
            ),
        ),
        storage_cleanup_max_bytes_per_run=max(
            1,
            int(
                os.getenv(
                    "STORAGE_CLEANUP_MAX_BYTES_PER_RUN",
                    str(STORAGE_CLEANUP_MAX_BYTES_PER_RUN),
                )
            ),
        ),
        storage_cleanup_lock_ttl_seconds=max(
            1,
            int(
                os.getenv(
                    "STORAGE_CLEANUP_LOCK_TTL_SECONDS",
                    str(STORAGE_CLEANUP_LOCK_TTL_SECONDS),
                )
            ),
        ),
        storage_degraded_free_bytes=max(
            1, int(os.getenv("STORAGE_DEGRADED_FREE_BYTES", str(STORAGE_DEGRADED_FREE_BYTES)))
        ),
        storage_critical_free_bytes=max(
            1, int(os.getenv("STORAGE_CRITICAL_FREE_BYTES", str(STORAGE_CRITICAL_FREE_BYTES)))
        ),
        storage_degraded_free_percent=max(
            0.0,
            float(os.getenv("STORAGE_DEGRADED_FREE_PERCENT", str(STORAGE_DEGRADED_FREE_PERCENT))),
        ),
        storage_critical_free_percent=max(
            0.0,
            float(os.getenv("STORAGE_CRITICAL_FREE_PERCENT", str(STORAGE_CRITICAL_FREE_PERCENT))),
        ),
        storage_degraded_db_growth_bytes=max(
            1,
            int(
                os.getenv(
                    "STORAGE_DEGRADED_DB_GROWTH_BYTES",
                    str(STORAGE_DEGRADED_DB_GROWTH_BYTES),
                )
            ),
        ),
        storage_critical_db_growth_bytes=max(
            1,
            int(
                os.getenv(
                    "STORAGE_CRITICAL_DB_GROWTH_BYTES",
                    str(STORAGE_CRITICAL_DB_GROWTH_BYTES),
                )
            ),
        ),
        storage_degraded_wal_bytes=max(
            1, int(os.getenv("STORAGE_DEGRADED_WAL_BYTES", str(STORAGE_DEGRADED_WAL_BYTES)))
        ),
        storage_critical_wal_bytes=max(
            1, int(os.getenv("STORAGE_CRITICAL_WAL_BYTES", str(STORAGE_CRITICAL_WAL_BYTES)))
        ),
        storage_backup_dir=os.getenv("STORAGE_BACKUP_DIR", DEFAULT_STORAGE_BACKUP_DIR).strip(),
        storage_backup_enabled=_parse_bool(
            os.getenv("STORAGE_BACKUP_ENABLED", str(STORAGE_BACKUP_ENABLED).lower()),
            default=STORAGE_BACKUP_ENABLED,
        ),
        storage_backup_max_count=max(
            1, int(os.getenv("STORAGE_BACKUP_MAX_COUNT", str(STORAGE_BACKUP_MAX_COUNT)))
        ),
        storage_backup_max_age_days=max(
            1, int(os.getenv("STORAGE_BACKUP_MAX_AGE_DAYS", str(STORAGE_BACKUP_MAX_AGE_DAYS)))
        ),
        storage_backup_lock_ttl_seconds=max(
            1,
            int(
                os.getenv(
                    "STORAGE_BACKUP_LOCK_TTL_SECONDS",
                    str(STORAGE_BACKUP_LOCK_TTL_SECONDS),
                )
            ),
        ),
        storage_backup_min_free_bytes=max(
            1,
            int(
                os.getenv(
                    "STORAGE_BACKUP_MIN_FREE_BYTES",
                    str(STORAGE_BACKUP_MIN_FREE_BYTES),
                )
            ),
        ),
        storage_backup_label=(
            os.getenv("STORAGE_BACKUP_LABEL", STORAGE_BACKUP_LABEL).strip() or STORAGE_BACKUP_LABEL
        ),
        storage_backup_confirm_ttl_seconds=max(
            1,
            int(
                os.getenv(
                    "STORAGE_BACKUP_CONFIRM_TTL_SECONDS",
                    str(STORAGE_BACKUP_CONFIRM_TTL_SECONDS),
                )
            ),
        ),
        storage_cleanup_confirm_ttl_seconds=max(
            1,
            int(
                os.getenv(
                    "STORAGE_CLEANUP_CONFIRM_TTL_SECONDS",
                    str(STORAGE_CLEANUP_CONFIRM_TTL_SECONDS),
                )
            ),
        ),
        storage_retention_prune_enabled=_parse_bool(
            os.getenv(
                "STORAGE_RETENTION_PRUNE_ENABLED",
                str(STORAGE_RETENTION_PRUNE_ENABLED).lower(),
            ),
            default=STORAGE_RETENTION_PRUNE_ENABLED,
        ),
        storage_retention_prune_dry_run=_parse_bool(
            os.getenv(
                "STORAGE_RETENTION_PRUNE_DRY_RUN",
                str(STORAGE_RETENTION_PRUNE_DRY_RUN).lower(),
            ),
            default=STORAGE_RETENTION_PRUNE_DRY_RUN,
        ),
        storage_retention_prune_max_rows_per_run=max(
            1,
            int(
                os.getenv(
                    "STORAGE_RETENTION_PRUNE_MAX_ROWS_PER_RUN",
                    str(STORAGE_RETENTION_PRUNE_MAX_ROWS_PER_RUN),
                )
            ),
        ),
        storage_retention_prune_max_rows_per_policy=max(
            1,
            int(
                os.getenv(
                    "STORAGE_RETENTION_PRUNE_MAX_ROWS_PER_POLICY",
                    str(STORAGE_RETENTION_PRUNE_MAX_ROWS_PER_POLICY),
                )
            ),
        ),
        storage_retention_prune_timeout_seconds=max(
            1,
            int(
                os.getenv(
                    "STORAGE_RETENTION_PRUNE_TIMEOUT_SECONDS",
                    str(STORAGE_RETENTION_PRUNE_TIMEOUT_SECONDS),
                )
            ),
        ),
        storage_retention_prune_max_backup_age_seconds=max(
            1,
            int(
                os.getenv(
                    "STORAGE_RETENTION_PRUNE_MAX_BACKUP_AGE_SECONDS",
                    str(STORAGE_RETENTION_PRUNE_MAX_BACKUP_AGE_SECONDS),
                )
            ),
        ),
        storage_retention_prune_lock_ttl_seconds=max(
            1,
            int(
                os.getenv(
                    "STORAGE_RETENTION_PRUNE_LOCK_TTL_SECONDS",
                    str(STORAGE_RETENTION_PRUNE_LOCK_TTL_SECONDS),
                )
            ),
        ),
        storage_retention_prune_confirm_ttl_seconds=max(
            1,
            int(
                os.getenv(
                    "STORAGE_RETENTION_PRUNE_CONFIRM_TTL_SECONDS",
                    str(STORAGE_RETENTION_PRUNE_CONFIRM_TTL_SECONDS),
                )
            ),
        ),
        storage_retention_prune_stop_on_policy_failure=_parse_bool(
            os.getenv(
                "STORAGE_RETENTION_PRUNE_STOP_ON_POLICY_FAILURE",
                str(STORAGE_RETENTION_PRUNE_STOP_ON_POLICY_FAILURE).lower(),
            ),
            default=STORAGE_RETENTION_PRUNE_STOP_ON_POLICY_FAILURE,
        ),
        opportunity_scanner_enabled=_parse_bool(
            os.getenv("OPPORTUNITY_SCANNER_ENABLED", "false")
        ),
        opportunity_auto_alerts_enabled=_parse_bool(
            os.getenv("OPPORTUNITY_AUTO_ALERTS_ENABLED", "false")
        ),
        opportunity_user_delivery_enabled=_parse_bool(
            os.getenv("OPPORTUNITY_USER_DELIVERY_ENABLED", "false")
        ),
        opportunity_scan_interval_seconds=_parse_bounded_int(
            os.getenv("OPPORTUNITY_SCAN_INTERVAL_SECONDS"),
            default=120,
            minimum=30,
            maximum=3600,
            name="OPPORTUNITY_SCAN_INTERVAL_SECONDS",
        ),
        opportunity_discovery_ttl_seconds=_parse_bounded_int(
            os.getenv("OPPORTUNITY_DISCOVERY_TTL_SECONDS"),
            default=300,
            minimum=30,
            maximum=3600,
            name="OPPORTUNITY_DISCOVERY_TTL_SECONDS",
        ),
        opportunity_max_markets=_parse_bounded_int(
            os.getenv("OPPORTUNITY_MAX_MARKETS"),
            default=100,
            minimum=1,
            maximum=100,
            name="OPPORTUNITY_MAX_MARKETS",
        ),
        opportunity_max_books_per_cycle=_parse_bounded_int(
            os.getenv("OPPORTUNITY_MAX_BOOKS_PER_CYCLE"),
            default=40,
            minimum=1,
            maximum=40,
            name="OPPORTUNITY_MAX_BOOKS_PER_CYCLE",
        ),
        opportunity_max_live_books_per_cycle=_parse_bounded_int(
            os.getenv("OPPORTUNITY_MAX_LIVE_BOOKS_PER_CYCLE"),
            default=10,
            minimum=1,
            maximum=10,
            name="OPPORTUNITY_MAX_LIVE_BOOKS_PER_CYCLE",
        ),
        opportunity_concurrency=_parse_bounded_int(
            os.getenv("OPPORTUNITY_CONCURRENCY"),
            default=5,
            minimum=1,
            maximum=5,
            name="OPPORTUNITY_CONCURRENCY",
        ),
        opportunity_cycle_deadline_seconds=_parse_bounded_int(
            os.getenv("OPPORTUNITY_CYCLE_DEADLINE_SECONDS"),
            default=75,
            minimum=15,
            maximum=300,
            name="OPPORTUNITY_CYCLE_DEADLINE_SECONDS",
        ),
        opportunity_first_run_delay_seconds=_parse_bounded_int(
            os.getenv("OPPORTUNITY_FIRST_RUN_DELAY_SECONDS"),
            default=30,
            minimum=0,
            maximum=600,
            name="OPPORTUNITY_FIRST_RUN_DELAY_SECONDS",
        ),
    )
