"""V2.1.1 Phase 2: explicit-path cleanup and log policy tests."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.database.storage import initialize_database
from app.health.monitor import probe_infrastructure
from app.health.sanitize import assert_no_private_path
from app.storage.cleanup import (
    inventory_cleanup_candidates,
    run_storage_cleanup,
)
from app.storage.lock import acquire_cleanup_lock, release_cleanup_lock
from app.storage.log_rotation import ensure_rotating_file_logging
from app.storage.paths import (
    is_protected_file,
    project_root,
    reject_reason_for_configured_root,
    resolve_approved_root,
)
from app.telegram.auth import is_admin
from app.telegram.health.formatting import format_db_health
from app.telegram.inline_menus import main_inline_keyboard
from app.health.monitor import run_health_check
from tests.conftest import make_test_settings


def _touch(path: Path, *, age_seconds: float, size: int = 32) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    past = time.time() - age_seconds
    os.utime(path, (past, past))


@pytest.fixture
def cleanup_env(tmp_path):
    logs = tmp_path / "logs"
    cache = tmp_path / "cache"
    proof = tmp_path / "proof"
    logs.mkdir()
    cache.mkdir()
    proof.mkdir()
    db_path = tmp_path / "metrics.db"
    s = make_test_settings(
        database_path=str(db_path),
        storage_logs_dir=str(logs),
        storage_cache_dir=str(cache),
        storage_proof_dir=str(proof),
        storage_active_log_name="trading_intelligence.log",
        storage_cleanup_enabled=False,
        storage_cleanup_dry_run=True,
        storage_log_retention_days=0,
        storage_cleanup_min_age_seconds=0,
        storage_cache_min_age_seconds=0,
        storage_proof_min_age_seconds=0,
        storage_cleanup_max_files_per_run=100,
        storage_cleanup_max_bytes_per_run=10_000_000,
        admin_ids=[1001],
    )
    initialize_database(s)
    return s, logs, cache, proof, db_path


class TestPathValidation:
    def test_missing_and_empty_dirs(self, cleanup_env):
        settings, logs, cache, proof, _db = cleanup_env
        missing = make_test_settings(
            database_path=settings.database_path,
            storage_logs_dir=str(logs / "missing"),
            storage_cache_dir="",
            storage_proof_dir="",
            storage_log_retention_days=0,
            storage_cleanup_min_age_seconds=0,
        )
        candidates, report = inventory_cleanup_candidates(missing)
        assert candidates == []
        assert report.candidate_count == 0

    def test_empty_configured_directories(self, cleanup_env):
        settings, _logs, _cache, _proof, _db = cleanup_env
        candidates, report = inventory_cleanup_candidates(settings)
        assert candidates == []
        assert report.reclaimable_bytes == 0

    def test_invalid_relative_and_traversal(self):
        assert reject_reason_for_configured_root("") == "empty"
        assert reject_reason_for_configured_root(".") == "ambiguous_relative"
        assert reject_reason_for_configured_root("..") == "ambiguous_relative"
        assert reject_reason_for_configured_root("logs/../..") == "path_traversal"
        assert resolve_approved_root("") is None
        assert resolve_approved_root("logs/../..") is None

    def test_filesystem_and_project_root_rejection(self):
        root = project_root()
        assert reject_reason_for_configured_root(str(root)) == "project_root"
        assert reject_reason_for_configured_root(str(root / "app")) == "source_or_git_tree"
        if os.name == "nt":
            assert reject_reason_for_configured_root("C:\\") == "filesystem_root"
        else:
            assert reject_reason_for_configured_root("/") == "filesystem_root"

    def test_symlink_escape_rejection(self, cleanup_env, tmp_path):
        settings, logs, _cache, _proof, _db = cleanup_env
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = logs / "escape.log.1"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation not permitted")
        _touch(logs / "trading_intelligence.log.1", age_seconds=10_000, size=20)
        # Escaping symlink must not become a candidate.
        candidates, report = inventory_cleanup_candidates(settings)
        names = {c.name for c in candidates}
        assert "escape.log.1" not in names
        assert report.skipped_invalid >= 1 or "trading_intelligence.log.1" in names


class TestProtectionAndBounds:
    def test_active_log_and_db_protection(self, cleanup_env):
        settings, logs, cache, proof, db_path = cleanup_env
        active = logs / "trading_intelligence.log"
        rotated = logs / "trading_intelligence.log.1"
        _touch(active, age_seconds=10_000, size=50)
        _touch(rotated, age_seconds=10_000, size=50)
        wal = Path(str(db_path) + "-wal")
        wal.write_bytes(b"wal")
        # Place a fake wal basename inside logs — still protected by basename.
        fake_wal = logs / db_path.name
        _touch(fake_wal, age_seconds=10_000, size=10)
        assert is_protected_file(
            active,
            database_path=str(db_path),
            active_log_path=active,
        )
        candidates, report = inventory_cleanup_candidates(settings)
        names = {c.name for c in candidates}
        assert "trading_intelligence.log" not in names
        assert db_path.name not in names
        assert "trading_intelligence.log.1" in names
        assert report.skipped_protected >= 1

    def test_minimum_age(self, cleanup_env):
        settings, logs, _cache, _proof, _db = cleanup_env
        settings = make_test_settings(
            **{
                **settings.__dict__,
                "storage_cleanup_min_age_seconds": 3600,
                "storage_log_retention_days": 0,
            }
        )
        _touch(logs / "trading_intelligence.log.1", age_seconds=10, size=20)
        candidates, report = inventory_cleanup_candidates(settings)
        assert candidates == []
        assert report.skipped_young >= 1

    def test_max_files_and_bytes(self, cleanup_env):
        settings, logs, cache, _proof, _db = cleanup_env
        for i in range(5):
            _touch(logs / f"trading_intelligence.log.{i+1}", age_seconds=10_000, size=100)
            _touch(cache / f"c{i}.bin", age_seconds=10_000, size=100)
        limited = make_test_settings(
            **{
                **settings.__dict__,
                "storage_cleanup_enabled": True,
                "storage_cleanup_dry_run": False,
                "storage_cleanup_max_files_per_run": 2,
                "storage_cleanup_max_bytes_per_run": 10_000_000,
                "storage_log_retention_days": 0,
                "storage_cleanup_min_age_seconds": 0,
                "storage_cache_min_age_seconds": 0,
            }
        )
        before = list(logs.glob("trading_intelligence.log.*"))
        report = run_storage_cleanup(limited)
        assert report.status == "completed"
        assert report.deleted_count <= 2
        assert report.skipped_limits >= 1
        assert len(list(logs.glob("trading_intelligence.log.*"))) >= len(before) - 2

    def test_dry_run_zero_deletion(self, cleanup_env):
        settings, logs, cache, proof, _db = cleanup_env
        _touch(logs / "trading_intelligence.log.1", age_seconds=10_000, size=40)
        _touch(cache / "old.bin", age_seconds=10_000, size=40)
        _touch(proof / "debug.txt", age_seconds=10_000, size=40)
        enabled_dry = make_test_settings(
            **{
                **settings.__dict__,
                "storage_cleanup_enabled": True,
                "storage_cleanup_dry_run": True,
                "storage_log_retention_days": 0,
                "storage_cleanup_min_age_seconds": 0,
                "storage_cache_min_age_seconds": 0,
                "storage_proof_min_age_seconds": 0,
            }
        )
        report = run_storage_cleanup(enabled_dry)
        assert report.candidate_count >= 3
        assert report.deleted_count == 0
        assert (logs / "trading_intelligence.log.1").exists()
        assert (cache / "old.bin").exists()
        assert (proof / "debug.txt").exists()

    def test_cleanup_disabled_by_default(self, cleanup_env):
        settings, logs, _cache, _proof, _db = cleanup_env
        _touch(logs / "trading_intelligence.log.1", age_seconds=10_000, size=40)
        report = run_storage_cleanup(settings)
        assert settings.storage_cleanup_enabled is False
        assert report.status == "disabled"
        assert report.deleted_count == 0
        assert (logs / "trading_intelligence.log.1").exists()

    def test_locked_file_and_partial_failure(self, cleanup_env):
        settings, logs, cache, _proof, _db = cleanup_env
        a = logs / "trading_intelligence.log.1"
        b = logs / "trading_intelligence.log.2"
        _touch(a, age_seconds=10_000, size=40)
        _touch(b, age_seconds=10_000, size=40)
        enabled = make_test_settings(
            **{
                **settings.__dict__,
                "storage_cleanup_enabled": True,
                "storage_cleanup_dry_run": False,
                "storage_log_retention_days": 0,
                "storage_cleanup_min_age_seconds": 0,
            }
        )
        original_unlink = Path.unlink

        def flaky_unlink(self, *args, **kwargs):
            if self.name.endswith(".1"):
                raise PermissionError("locked")
            return original_unlink(self, *args, **kwargs)

        with patch.object(Path, "unlink", flaky_unlink):
            report = run_storage_cleanup(enabled)
        assert report.status == "completed"
        assert report.skipped_locked >= 1
        assert report.deleted_count >= 1
        assert a.exists()
        assert not b.exists()


class TestMaintenanceLock:
    def test_lock_and_stale_recovery(self, cleanup_env):
        settings, _logs, _cache, _proof, _db = cleanup_env
        owner = acquire_cleanup_lock(settings, ttl_seconds=60)
        assert owner is not None
        assert acquire_cleanup_lock(settings, ttl_seconds=60) is None
        release_cleanup_lock(settings, owner)
        owner2 = acquire_cleanup_lock(settings, ttl_seconds=1)
        assert owner2 is not None
        time.sleep(1.1)
        # Stale lock can be stolen.
        owner3 = acquire_cleanup_lock(settings, ttl_seconds=60)
        assert owner3 is not None
        release_cleanup_lock(settings, owner3)


class TestSanitizedHealth:
    def test_sanitized_reporting_and_health(self, cleanup_env):
        settings, logs, cache, proof, _db = cleanup_env
        _touch(logs / "trading_intelligence.log.1", age_seconds=10_000, size=40)
        _touch(cache / "old.bin", age_seconds=10_000, size=40)
        status = probe_infrastructure(settings)
        metrics = status.metrics
        assert "cleanup_candidate_count" in metrics
        assert metrics["cleanup_roots"]["logs"] == "logs"
        blob = str(metrics)
        assert "Users\\" not in blob
        assert "/home/" not in blob
        for value in metrics["cleanup_roots"].values():
            assert assert_no_private_path(str(value))
        overall = run_health_check(settings, bot_data={"health_polling_active": True})
        text = format_db_health(settings, overall)
        assert "Cleanup candidates:" in text
        assert "live_database" in text
        assert not is_admin(settings, 9999)
        labels = [
            btn.text
            for row in main_inline_keyboard(is_admin=False).inline_keyboard
            for btn in row
        ]
        assert "🩺 System Health" not in labels

    def test_file_logging_opt_in(self, cleanup_env):
        settings, logs, _cache, _proof, _db = cleanup_env
        disabled = make_test_settings(**{**settings.__dict__, "storage_file_logging_enabled": False})
        assert ensure_rotating_file_logging(disabled) is False
        enabled = make_test_settings(**{**settings.__dict__, "storage_file_logging_enabled": True})
        assert ensure_rotating_file_logging(enabled) is True
        assert (logs / "trading_intelligence.log").parent == logs
