"""Read-only storage metrics and disk/growth thresholds (V2.1.1 Phase 1)."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from pathlib import Path

from app.config.settings import Settings
from app.health.models import StorageHealthLevel, StorageMetrics, StoragePathMetric
from app.health.sanitize import logical_path_label

logger = logging.getLogger(__name__)

_DIAGNOSTIC_TABLES = (
    "users",
    "user_filters",
    "analysis_records",
    "report_history",
    "scan_snapshots",
    "copy_watchlist",
    "copy_position_snapshots",
    "copy_trade_events",
    "copy_trader_metrics",
    "copy_recommendations",
    "copy_paper_positions",
    "copy_audit_log",
    "copy_poll_state",
    "copy_user_settings",
    "copy_discovery_scores",
    "wallet_monitor_state",
    "wallet_event_watermarks",
    "processed_copy_events",
    "baseline_runs",
    "copy_alert_deliveries",
    "copy_job_locks",
    "health_checks",
    "component_states",
    "health_incidents",
    "health_alerts",
    "job_executions",
    "provider_metrics",
    "unauthorized_access_log",
)


def _file_size(path: Path) -> int | None:
    try:
        if path.is_file():
            return int(path.stat().st_size)
    except OSError:
        return None
    return None


def _dir_size_bytes(path: Path) -> tuple[bool, int]:
    """Return (exists, total_bytes). Missing or empty dirs are safe (0 bytes)."""
    try:
        if not path.exists():
            return False, 0
        if not path.is_dir():
            size = _file_size(path)
            return True, int(size or 0)
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += int((Path(root) / name).stat().st_size)
                except OSError:
                    continue
        return True, total
    except OSError:
        return False, 0


def _disk_usage(target: Path) -> tuple[int | None, int | None]:
    try:
        probe = target if target.exists() else target.parent
        if not probe.exists():
            probe = Path(".")
        usage = shutil.disk_usage(probe)
        return int(usage.free), int(usage.total)
    except OSError:
        return None, None


def _path_metric(label: str, configured_path: str) -> StoragePathMetric:
    configured = bool(configured_path and configured_path.strip())
    if not configured:
        return StoragePathMetric(label=label, configured=False, exists=False, size_bytes=0)
    exists, size = _dir_size_bytes(Path(configured_path.strip()))
    return StoragePathMetric(label=label, configured=True, exists=exists, size_bytes=size)


def _open_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Open SQLite read-only. Never creates files or changes journal_mode."""
    if str(db_path) == ":memory:":
        return None
    try:
        if not db_path.exists():
            return None
        resolved = db_path.resolve()
        uri = resolved.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except (sqlite3.Error, OSError) as exc:
        logger.debug("Read-only SQLite open failed: %s", exc)
        return None


def _read_journal_mode(connection: sqlite3.Connection) -> str | None:
    try:
        row = connection.execute("PRAGMA journal_mode").fetchone()
        if not row:
            return None
        mode = str(row[0]).strip().lower()
        return mode or None
    except sqlite3.Error:
        return None


def _table_row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    except sqlite3.Error:
        return counts

    for name in _DIAGNOSTIC_TABLES:
        if name not in existing:
            continue
        try:
            row = connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
            counts[name] = int(row[0]) if row else 0
        except sqlite3.Error:
            continue
    return counts


def _prior_database_size(prior_metrics: dict | None) -> int | None:
    if not prior_metrics:
        return None
    value = prior_metrics.get("database_size_bytes")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def evaluate_storage_level(
    settings: Settings,
    *,
    disk_free_bytes: int | None,
    disk_free_percent: float | None,
    wal_size_bytes: int | None,
    journal_size_bytes: int | None,
    db_growth_bytes: int | None,
) -> tuple[StorageHealthLevel, str]:
    """Derive HEALTHY / DEGRADED / CRITICAL from free space and growth signals."""
    reasons_critical: list[str] = []
    reasons_degraded: list[str] = []

    if disk_free_bytes is not None:
        if disk_free_bytes < settings.storage_critical_free_bytes:
            reasons_critical.append("free_bytes_critical")
        elif disk_free_bytes < settings.storage_degraded_free_bytes:
            reasons_degraded.append("free_bytes_degraded")

    if disk_free_percent is not None:
        if disk_free_percent < settings.storage_critical_free_percent:
            reasons_critical.append("free_percent_critical")
        elif disk_free_percent < settings.storage_degraded_free_percent:
            reasons_degraded.append("free_percent_degraded")

    sidecar = max(int(wal_size_bytes or 0), int(journal_size_bytes or 0))
    if sidecar >= settings.storage_critical_wal_bytes:
        reasons_critical.append("wal_journal_critical")
    elif sidecar >= settings.storage_degraded_wal_bytes:
        reasons_degraded.append("wal_journal_degraded")

    if db_growth_bytes is not None:
        if db_growth_bytes >= settings.storage_critical_db_growth_bytes:
            reasons_critical.append("db_growth_critical")
        elif db_growth_bytes >= settings.storage_degraded_db_growth_bytes:
            reasons_degraded.append("db_growth_degraded")

    if reasons_critical:
        return "CRITICAL", "Storage critical: " + ", ".join(reasons_critical)
    if reasons_degraded:
        return "DEGRADED", "Storage degraded: " + ", ".join(reasons_degraded)
    return "HEALTHY", "Storage OK"


def collect_storage_metrics(
    settings: Settings,
    *,
    prior_metrics: dict | None = None,
) -> StorageMetrics:
    """Collect sanitized storage metrics. Read-only; does not mutate SQLite."""
    db_path = Path(settings.database_path)
    is_memory = str(db_path) == ":memory:"

    database_size = None if is_memory else _file_size(db_path)
    wal_size = None if is_memory else _file_size(Path(str(db_path) + "-wal"))
    shm_size = None if is_memory else _file_size(Path(str(db_path) + "-shm"))
    journal_size = None if is_memory else _file_size(Path(str(db_path) + "-journal"))

    logs = _path_metric("logs", settings.storage_logs_dir)
    cache = _path_metric("cache", settings.storage_cache_dir)

    disk_target = db_path.parent if not is_memory else Path(".")
    disk_free, disk_total = _disk_usage(disk_target)
    disk_free_percent = None
    if disk_free is not None and disk_total and disk_total > 0:
        disk_free_percent = round(100.0 * disk_free / disk_total, 2)

    journal_mode = None
    table_row_counts: dict[str, int] = {}
    connection = None if is_memory else _open_readonly(db_path)
    if connection is not None:
        try:
            journal_mode = _read_journal_mode(connection)
            table_row_counts = _table_row_counts(connection)
        finally:
            connection.close()

    prior_size = _prior_database_size(prior_metrics)
    db_growth = None
    if database_size is not None and prior_size is not None:
        db_growth = max(0, database_size - prior_size)

    level, detail = evaluate_storage_level(
        settings,
        disk_free_bytes=disk_free,
        disk_free_percent=disk_free_percent,
        wal_size_bytes=wal_size,
        journal_size_bytes=journal_size,
        db_growth_bytes=db_growth,
    )

    path_labels = {
        "live_database": logical_path_label("live_database"),
        "logs": logical_path_label("logs") if logs.configured else "(unset)",
        "cache": logical_path_label("cache") if cache.configured else "(unset)",
    }

    return StorageMetrics(
        level=level,
        detail=detail,
        database_size_bytes=database_size,
        wal_size_bytes=wal_size,
        shm_size_bytes=shm_size,
        journal_size_bytes=journal_size,
        logs=logs,
        cache=cache,
        disk_free_bytes=disk_free,
        disk_total_bytes=disk_total,
        disk_free_percent=disk_free_percent,
        journal_mode=journal_mode,
        table_row_counts=table_row_counts,
        db_growth_bytes=db_growth,
        prior_database_size_bytes=prior_size,
        path_labels=path_labels,
    )
