import os
from dataclasses import dataclass
from dotenv import load_dotenv

from app.config.constants import (
    DEFAULT_DB_PATH,
    DEFAULT_LANGUAGE,
    DEFAULT_LOG_LEVEL,
    POLYMARKET_CLOB_BASE_URL,
    POLYMARKET_GAMMA_BASE_URL,
    PROJECT_NAME,
    PROJECT_VERSION,
    WEATHER_GOV_BASE_URL,
)


load_dotenv()


def _split_ints(value: str) -> list[int]:
    if not value:
        return []
    result: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if item.isdigit():
            result.append(int(item))
    return result


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
    polymarket_clob_base_url: str
    weather_gov_base_url: str


def load_settings() -> Settings:
    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if not telegram_token:
        raise RuntimeError("Missing TELEGRAM_TOKEN in .env")

    if not anthropic_api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY in .env")

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
        polymarket_clob_base_url=os.getenv(
            "POLYMARKET_CLOB_BASE_URL",
            POLYMARKET_CLOB_BASE_URL,
        ),
        weather_gov_base_url=os.getenv(
            "WEATHER_GOV_BASE_URL",
            WEATHER_GOV_BASE_URL,
        ),
    )
