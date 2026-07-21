PROJECT_NAME = "Trading Intelligence"
PROJECT_VERSION = "0.1.0"

DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ["en"]

DEFAULT_DB_PATH = "data/trading_intelligence.db"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_STORAGE_LOGS_DIR = "logs"
DEFAULT_STORAGE_CACHE_DIR = ""

# Storage disk / growth thresholds (Phase 1 metrics only; no cleanup).
STORAGE_DEGRADED_FREE_BYTES = 1024 * 1024 * 1024  # 1 GiB
STORAGE_CRITICAL_FREE_BYTES = 256 * 1024 * 1024  # 256 MiB
STORAGE_DEGRADED_FREE_PERCENT = 10.0
STORAGE_CRITICAL_FREE_PERCENT = 5.0
STORAGE_DEGRADED_DB_GROWTH_BYTES = 512 * 1024 * 1024  # 512 MiB between samples
STORAGE_CRITICAL_DB_GROWTH_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB between samples
STORAGE_DEGRADED_WAL_BYTES = 64 * 1024 * 1024  # 64 MiB
STORAGE_CRITICAL_WAL_BYTES = 256 * 1024 * 1024  # 256 MiB

POLYMARKET_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
POLYMARKET_DATA_BASE_URL = "https://data-api.polymarket.com"
POLYMARKET_CLOB_BASE_URL = "https://clob.polymarket.com"

# Copy-intel exposure caps (fraction of paper bankroll).
COPY_MAX_ALLOC_PER_MARKET = 0.05
COPY_MAX_ALLOC_PER_WALLET = 0.03
COPY_MAX_ALLOC_TOTAL = 0.15

WEATHER_GOV_BASE_URL = "https://api.weather.gov"

MAIN_MENU_IMAGE_PATH = "assets/ti_banner.jpg"
