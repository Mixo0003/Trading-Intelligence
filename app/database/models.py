from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class User:
    telegram_id: int
    username: str | None
    first_name: str | None
    language: str
    created_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class UserFilters:
    telegram_id: int
    market_source: str
    category: str
    min_edge: float
    notifications_enabled: bool


@dataclass(frozen=True)
class AnalysisRecord:
    id: int | None
    telegram_id: int
    market_url: str
    market_title: str | None
    decision: str
    confidence: float | None
    created_at: datetime
