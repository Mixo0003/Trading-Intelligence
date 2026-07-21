"""Health monitor domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

HealthStatus = Literal["HEALTHY", "DEGRADED", "DOWN", "DISABLED"]
StorageHealthLevel = Literal["HEALTHY", "DEGRADED", "CRITICAL"]
IncidentState = Literal["open", "updated", "acknowledged", "suppressed", "resolved"]


@dataclass(frozen=True)
class ComponentStatus:
    component: str
    status: HealthStatus
    detail: str
    checked_at: str
    metrics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OverallHealth:
    status: HealthStatus
    components: tuple[ComponentStatus, ...]
    checked_at: str
    summary: str


@dataclass(frozen=True)
class StoragePathMetric:
    """Sanitized path metric — logical label only, never absolute private paths."""

    label: str
    configured: bool
    exists: bool
    size_bytes: int


@dataclass(frozen=True)
class StorageMetrics:
    """Admin-readable storage summary (Phase 1: metrics only)."""

    level: StorageHealthLevel
    detail: str
    database_size_bytes: int | None
    wal_size_bytes: int | None
    shm_size_bytes: int | None
    journal_size_bytes: int | None
    logs: StoragePathMetric
    cache: StoragePathMetric
    disk_free_bytes: int | None
    disk_total_bytes: int | None
    disk_free_percent: float | None
    journal_mode: str | None
    table_row_counts: dict[str, int]
    db_growth_bytes: int | None
    prior_database_size_bytes: int | None
    path_labels: dict[str, str]

    def as_sanitized_dict(self) -> dict:
        return {
            "storage_level": self.level,
            "storage_detail": self.detail,
            "database_size_bytes": self.database_size_bytes,
            "wal_size_bytes": self.wal_size_bytes,
            "shm_size_bytes": self.shm_size_bytes,
            "journal_size_bytes": self.journal_size_bytes,
            "logs_size_bytes": self.logs.size_bytes if self.logs.configured else None,
            "logs_configured": self.logs.configured,
            "logs_exists": self.logs.exists,
            "cache_size_bytes": self.cache.size_bytes if self.cache.configured else None,
            "cache_configured": self.cache.configured,
            "cache_exists": self.cache.exists,
            "disk_free_bytes": self.disk_free_bytes,
            "disk_total_bytes": self.disk_total_bytes,
            "disk_free_percent": self.disk_free_percent,
            "disk_free_mb": (
                round(self.disk_free_bytes / (1024 * 1024), 1)
                if self.disk_free_bytes is not None
                else None
            ),
            "journal_mode": self.journal_mode,
            "table_row_counts": dict(self.table_row_counts),
            "table_count": len(self.table_row_counts),
            "db_growth_bytes": self.db_growth_bytes,
            "prior_database_size_bytes": self.prior_database_size_bytes,
            "path_labels": dict(self.path_labels),
        }
