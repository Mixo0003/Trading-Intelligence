"""V2.1.1 Phase 1: storage metrics and disk thresholds (read-only)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.database.storage import get_connection, initialize_database
from app.health.monitor import probe_infrastructure, run_health_check
from app.health.sanitize import (
    assert_no_private_path,
    logical_path_label,
    sanitize_config_summary,
    sanitize_path,
)
from app.health.storage_metrics import collect_storage_metrics, evaluate_storage_level
from app.telegram.auth import is_admin
from app.telegram.health.formatting import format_db_health
from app.telegram.inline_menus import main_inline_keyboard
from tests.conftest import make_test_settings


@pytest.fixture
def settings(tmp_path):
    logs = tmp_path / "logs"
    cache = tmp_path / "cache"
    logs.mkdir()
    cache.mkdir()
    s = make_test_settings(
        database_path=str(tmp_path / "metrics.db"),
        storage_logs_dir=str(logs),
        storage_cache_dir=str(cache),
        admin_ids=[1001],
    )
    initialize_database(s)
    return s


class TestPathSanitization:
    def test_logical_labels(self):
        assert logical_path_label("live_database") == "live_database"
        assert logical_path_label("logs") == "logs"
        assert logical_path_label("cache") == "cache"

    def test_absolute_path_sanitized(self):
        windows = r"C:\Users\someone\Trading-Intelligence\data\trading_intelligence.db"
        posix = "/home/someone/Trading-Intelligence/data/trading_intelligence.db"
        assert sanitize_path(windows) == "trading_intelligence.db"
        assert sanitize_path(posix) == "trading_intelligence.db"
        assert assert_no_private_path(sanitize_path(windows))
        assert assert_no_private_path(logical_path_label("live_database"))
        assert not assert_no_private_path(windows)
        assert not assert_no_private_path(posix)

    def test_metrics_and_config_use_logical_labels(self, settings):
        metrics = collect_storage_metrics(settings)
        payload = metrics.as_sanitized_dict()
        assert payload["path_labels"]["live_database"] == "live_database"
        assert payload["path_labels"]["logs"] == "logs"
        assert payload["path_labels"]["cache"] == "cache"
        blob = str(payload)
        assert "Users" not in blob
        assert ":\\" not in blob
        cfg = sanitize_config_summary(settings)
        assert cfg["database"] == "live_database"
        assert cfg["storage_logs"] == "logs"
        assert cfg["storage_cache"] == "cache"


class TestMissingSidecarsAndEmptyDirs:
    def test_missing_wal_journal_shm(self, settings):
        metrics = collect_storage_metrics(settings)
        assert metrics.database_size_bytes is not None
        assert metrics.database_size_bytes > 0
        assert metrics.wal_size_bytes is None
        assert metrics.shm_size_bytes is None
        assert metrics.journal_size_bytes is None

    def test_empty_logs_and_cache_directories(self, settings):
        metrics = collect_storage_metrics(settings)
        assert metrics.logs.configured is True
        assert metrics.logs.exists is True
        assert metrics.logs.size_bytes == 0
        assert metrics.cache.configured is True
        assert metrics.cache.exists is True
        assert metrics.cache.size_bytes == 0

    def test_unset_cache_directory(self, tmp_path):
        s = make_test_settings(
            database_path=str(tmp_path / "unset.db"),
            storage_logs_dir="",
            storage_cache_dir="",
        )
        initialize_database(s)
        metrics = collect_storage_metrics(s)
        assert metrics.logs.configured is False
        assert metrics.cache.configured is False
        assert metrics.path_labels["logs"] == "(unset)"
        assert metrics.path_labels["cache"] == "(unset)"


class TestThresholds:
    def test_free_space_healthy_degraded_critical(self, settings):
        level, _ = evaluate_storage_level(
            settings,
            disk_free_bytes=settings.storage_degraded_free_bytes + 1,
            disk_free_percent=50.0,
            wal_size_bytes=None,
            journal_size_bytes=None,
            db_growth_bytes=0,
        )
        assert level == "HEALTHY"

        level, detail = evaluate_storage_level(
            settings,
            disk_free_bytes=settings.storage_critical_free_bytes + 1,
            disk_free_percent=8.0,
            wal_size_bytes=None,
            journal_size_bytes=None,
            db_growth_bytes=0,
        )
        assert level == "DEGRADED"
        assert "degraded" in detail.lower()

        level, detail = evaluate_storage_level(
            settings,
            disk_free_bytes=settings.storage_critical_free_bytes - 1,
            disk_free_percent=1.0,
            wal_size_bytes=None,
            journal_size_bytes=None,
            db_growth_bytes=0,
        )
        assert level == "CRITICAL"
        assert "critical" in detail.lower()

    def test_wal_and_growth_thresholds(self, settings):
        level, _ = evaluate_storage_level(
            settings,
            disk_free_bytes=10 * 1024 * 1024 * 1024,
            disk_free_percent=50.0,
            wal_size_bytes=settings.storage_degraded_wal_bytes,
            journal_size_bytes=None,
            db_growth_bytes=None,
        )
        assert level == "DEGRADED"

        level, _ = evaluate_storage_level(
            settings,
            disk_free_bytes=10 * 1024 * 1024 * 1024,
            disk_free_percent=50.0,
            wal_size_bytes=None,
            journal_size_bytes=None,
            db_growth_bytes=settings.storage_critical_db_growth_bytes,
        )
        assert level == "CRITICAL"


class TestRowCountsAndReadOnly:
    def test_row_count_collection(self, settings):
        with get_connection(settings) as connection:
            connection.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, language, created_at, last_seen_at)
                VALUES (1, 'a', 'A', 'en', 't', 't')
                """
            )
        metrics = collect_storage_metrics(settings)
        assert metrics.table_row_counts.get("users") == 1
        assert "processed_copy_events" in metrics.table_row_counts
        assert metrics.journal_mode is not None

    def test_collect_does_not_mutate_sqlite(self, settings):
        def _snapshot(connection):
            return {
                "users": connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "processed_copy_events": connection.execute(
                    "SELECT COUNT(*) FROM processed_copy_events"
                ).fetchone()[0],
                "copy_watchlist": connection.execute(
                    "SELECT COUNT(*) FROM copy_watchlist"
                ).fetchone()[0],
                "analysis_records": connection.execute(
                    "SELECT COUNT(*) FROM analysis_records"
                ).fetchone()[0],
                "page_count": connection.execute("PRAGMA page_count").fetchone()[0],
            }

        with get_connection(settings) as connection:
            before = _snapshot(connection)

        collect_storage_metrics(settings)
        collect_storage_metrics(settings, prior_metrics={"database_size_bytes": 1})

        with get_connection(settings) as connection:
            after = _snapshot(connection)

        assert after == before


class TestHealthIntegration:
    def test_probe_infrastructure_includes_storage_metrics(self, settings):
        status = probe_infrastructure(settings)
        assert "storage_level" in status.metrics
        assert status.metrics["path_labels"]["live_database"] == "live_database"
        assert "table_row_counts" in status.metrics
        assert status.status in {"HEALTHY", "DEGRADED", "DOWN"}

    def test_probe_maps_critical_to_down(self, settings):
        with patch(
            "app.health.monitor.collect_storage_metrics",
        ) as mocked:
            from app.health.models import StorageMetrics, StoragePathMetric

            mocked.return_value = StorageMetrics(
                level="CRITICAL",
                detail="Storage critical: free_bytes_critical",
                database_size_bytes=100,
                wal_size_bytes=None,
                shm_size_bytes=None,
                journal_size_bytes=None,
                logs=StoragePathMetric("logs", True, True, 0),
                cache=StoragePathMetric("cache", False, False, 0),
                disk_free_bytes=1,
                disk_total_bytes=100,
                disk_free_percent=1.0,
                journal_mode="delete",
                table_row_counts={"users": 0},
                db_growth_bytes=None,
                prior_database_size_bytes=None,
                path_labels={"live_database": "live_database", "logs": "logs", "cache": "(unset)"},
            )
            status = probe_infrastructure(settings)
        assert status.status == "DOWN"
        assert "critical" in status.detail.lower()

    def test_format_db_health_sanitized(self, settings):
        overall = run_health_check(settings, bot_data={"health_polling_active": True})
        text = format_db_health(settings, overall)
        assert "Storage level:" in text
        assert "live_database" in text
        assert "Users\\" not in text
        assert "/home/" not in text

    def test_admin_health_unchanged_for_non_admin(self, settings):
        assert is_admin(settings, 1001)
        assert not is_admin(settings, 9999)
        labels = [
            btn.text
            for row in main_inline_keyboard(is_admin=False).inline_keyboard
            for btn in row
        ]
        assert "🩺 System Health" not in labels


class TestGrowthSample:
    def test_prior_sample_growth(self, settings):
        db_path = Path(settings.database_path)
        current = db_path.stat().st_size
        metrics = collect_storage_metrics(
            settings,
            prior_metrics={"database_size_bytes": max(0, current - 10)},
        )
        assert metrics.prior_database_size_bytes == max(0, current - 10)
        assert metrics.db_growth_bytes == 10
