"""V2.1.1 Phase 4A: online SQLite backup and integrity gate."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.constants import STORAGE_BACKUP_LOCK_JOB
from app.database.storage import get_connection, initialize_database
from app.health.sanitize import assert_no_private_path, sanitize_config_summary
from app.storage.backup import (
    _verify_backup_file,
    backup_status,
    create_online_backup,
    inventory_expired_backups,
)
from app.storage.backup_paths import (
    backup_path_is_safe,
    reject_reason_for_backup_dir,
    resolve_backup_dir,
)
from app.storage.lock import acquire_backup_lock, release_backup_lock
from app.storage.paths import project_root
from app.telegram.auth import require_admin
from app.telegram.health.buttons import (
    CB_HEALTH_BACKUP,
    CB_HEALTH_BACKUP_CONFIRM,
    HEALTH_CALLBACKS,
    health_inline_keyboard,
)
from app.telegram.health.formatting import format_backup_status
from tests.conftest import make_test_settings


@pytest.fixture
def isolated(tmp_path):
    db_dir = tmp_path / "db"
    backup_dir = tmp_path / "backups"
    db_dir.mkdir()
    backup_dir.mkdir()
    db_path = db_dir / "live.db"
    settings = make_test_settings(
        database_path=str(db_path),
        storage_backup_dir=str(backup_dir.resolve()),
        storage_backup_enabled=True,
        storage_backup_min_free_bytes=1,
        storage_backup_max_count=2,
        storage_backup_max_age_days=7,
        admin_ids=[1001],
    )
    initialize_database(settings)
    with get_connection(settings) as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, language, created_at, last_seen_at)
            VALUES (1, 'u', 'U', 'en', ?, ?)
            """,
            (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )
    return settings, db_path, backup_dir


class TestBackupPathValidation:
    def test_missing_backup_configuration(self, tmp_path):
        db = tmp_path / "db" / "x.db"
        db.parent.mkdir()
        assert reject_reason_for_backup_dir("", database_path=str(db)) == "empty"
        assert resolve_backup_dir("", database_path=str(db)) is None

    def test_invalid_relative_path(self, tmp_path):
        db = tmp_path / "db" / "x.db"
        db.parent.mkdir()
        assert reject_reason_for_backup_dir("backups", database_path=str(db)) == "relative_not_allowed"
        assert reject_reason_for_backup_dir("./backups", database_path=str(db)) == "relative_not_allowed"

    def test_root_path_rejection(self, tmp_path):
        db = tmp_path / "db" / "x.db"
        db.parent.mkdir()
        root = Path(tmp_path.anchor).resolve()
        assert reject_reason_for_backup_dir(str(root), database_path=str(db)) == "filesystem_root"

    def test_project_source_path_rejection(self, tmp_path):
        db = tmp_path / "db" / "x.db"
        db.parent.mkdir()
        project = project_root()
        assert reject_reason_for_backup_dir(str(project), database_path=str(db)) == "project_root"
        assert reject_reason_for_backup_dir(str(project / "app"), database_path=str(db)) == "source_or_runtime_tree"
        assert reject_reason_for_backup_dir(str(project / "tests"), database_path=str(db)) == "source_or_runtime_tree"
        assert reject_reason_for_backup_dir(str(project / "data"), database_path=str(db)) == "source_or_runtime_tree"

    def test_traversal_rejection(self, tmp_path):
        db = tmp_path / "db" / "x.db"
        db.parent.mkdir()
        assert (
            reject_reason_for_backup_dir(
                str(tmp_path.resolve() / "x" / ".." / "y"), database_path=str(db)
            )
            == "path_traversal"
        )

    def test_symlink_escape_rejection(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        backup_root = tmp_path / "backups"
        backup_root.mkdir()
        link = backup_root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks not supported")
        db = tmp_path / "db" / "x.db"
        db.parent.mkdir()
        db.write_bytes(b"")
        target = link / "evil.db"
        assert backup_path_is_safe(target, backup_root=backup_root, database_path=str(db)) is False

    def test_live_db_overwrite_protection(self, tmp_path):
        db_dir = tmp_path / "db"
        db_dir.mkdir()
        db = db_dir / "live.db"
        db.write_bytes(b"x")
        assert reject_reason_for_backup_dir(str(db_dir.resolve()), database_path=str(db)) == (
            "live_database_directory"
        )
        assert reject_reason_for_backup_dir(str(db.resolve()), database_path=str(db)) == "live_database_path"
        backup_root = tmp_path / "backups"
        backup_root.mkdir()
        assert backup_path_is_safe(db, backup_root=backup_root, database_path=str(db)) is False
        assert (
            backup_path_is_safe(
                Path(str(db) + "-wal"), backup_root=backup_root, database_path=str(db)
            )
            is False
        )


class TestOnlineBackup:
    def test_unavailable_without_config(self, isolated):
        settings, _db, _b = isolated
        settings = make_test_settings(
            database_path=settings.database_path,
            storage_backup_dir="",
            storage_backup_min_free_bytes=1,
        )
        initialize_database(settings)
        report = create_online_backup(settings)
        assert report.status == "UNAVAILABLE"
        assert report.future_retention_gate == "blocked_until_verified_backup"

    def test_ordinary_file_copy_not_used(self, isolated):
        settings, db_path, backup_dir = isolated
        with (
            patch("shutil.copy2") as copy2,
            patch("shutil.copy") as copy,
            patch("shutil.copyfile") as copyfile,
        ):
            report = create_online_backup(settings)
            assert report.status == "VERIFIED"
            copy2.assert_not_called()
            copy.assert_not_called()
            copyfile.assert_not_called()
        assert report.mutation_proof.get("mechanism") == "sqlite3.Connection.backup"
        assert report.mutation_proof.get("no_shutil_copy") is True
        finals = list(backup_dir.glob("*.db"))
        assert len(finals) == 1
        assert finals[0].suffix == ".db"
        assert "Users" not in finals[0].name
        assert "token" not in finals[0].name.lower()

    def test_successful_online_backup_and_unique_names(self, isolated):
        settings, db_path, backup_dir = isolated
        r1 = create_online_backup(settings)
        r2 = create_online_backup(settings)
        assert r1.status == "VERIFIED"
        assert r2.status == "VERIFIED"
        assert r1.backup_label != r2.backup_label
        names = {p.name for p in backup_dir.glob("*.db")}
        assert len(names) == 2
        assert all(n.endswith(".db") for n in names)
        assert all(n.startswith("ti_backup_") for n in names)

    def test_atomic_temp_to_final_and_incomplete_cleanup(self, isolated):
        settings, db_path, backup_dir = isolated
        report = create_online_backup(settings)
        assert report.status == "VERIFIED"
        assert not list(backup_dir.glob("*.partial"))
        assert list(backup_dir.glob("*.db"))

        with patch(
            "app.storage.backup._verify_backup_file",
            return_value=("FAILED", "forced", {}),
        ):
            failed = create_online_backup(settings)
        assert failed.status == "FAILED"
        assert failed.future_retention_gate == "blocked_until_verified_backup"
        assert not list(backup_dir.glob("*.partial"))

        real_connect = sqlite3.connect

        def _boom_after_source(*args, **kwargs):
            # Allow lock/DB opens; fail only when opening the partial destination.
            path = str(args[0]) if args else ""
            if path.endswith(".partial"):
                raise RuntimeError("boom")
            return real_connect(*args, **kwargs)

        with patch("app.storage.backup.sqlite3.connect", side_effect=_boom_after_source):
            incomplete = create_online_backup(settings)
        assert incomplete.status == "INCOMPLETE"
        assert not list(backup_dir.glob("*.partial"))

    def test_integrity_check_success(self, isolated):
        settings, _db, backup_dir = isolated
        report = create_online_backup(settings)
        assert report.status == "VERIFIED"
        assert report.integrity_result == "ok"
        final = next(backup_dir.glob("*.db"))
        status, integrity, details = _verify_backup_file(final)
        assert status == "VERIFIED"
        assert integrity == "ok"
        assert not details["missing_required"]

    def test_integrity_check_failure(self, isolated):
        settings, _db, backup_dir = isolated
        create_online_backup(settings)
        final = next(backup_dir.glob("*.db"))
        data = final.read_bytes()
        final.write_bytes(b"\x00" * min(100, len(data)) + data[100:])
        status, integrity, _details = _verify_backup_file(final)
        assert status == "FAILED"
        assert integrity.lower() != "ok"

    def test_missing_critical_table_failure(self, tmp_path):
        empty = tmp_path / "empty.db"
        conn = sqlite3.connect(str(empty))
        conn.execute("CREATE TABLE only_foo (id INTEGER)")
        conn.commit()
        conn.close()
        status, integrity, details = _verify_backup_file(empty)
        assert status == "FAILED"
        assert "missing_tables" in integrity
        assert details["missing_required"]

    def test_safe_metadata_and_protected_counts(self, isolated):
        settings, db_path, backup_dir = isolated
        report = create_online_backup(settings)
        assert report.status == "VERIFIED"
        assert "users" in report.protected_counts
        assert report.protected_counts["users"] == 1
        assert report.source_table_count == report.backup_table_count
        meta = json.loads(
            (backup_dir / f"{report.backup_label}.meta.json").read_text(encoding="utf-8")
        )
        assert meta["status"] == "VERIFIED"
        assert meta["mechanism"] == "sqlite3.Connection.backup"

    def test_zero_live_db_mutations(self, isolated):
        settings, db_path, _backup = isolated
        before_size = db_path.stat().st_size
        with get_connection(settings) as conn:
            before_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        report = create_online_backup(settings)
        assert report.status == "VERIFIED"
        assert report.mutation_proof["source_size_unchanged"] is True
        with get_connection(settings) as conn:
            after_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            after_journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert after_users == before_users
        assert after_journal == journal
        assert db_path.stat().st_size == before_size


class TestBackupLock:
    def test_backup_lock_and_concurrent_rejection(self, isolated):
        settings, _db, _b = isolated
        owner = acquire_backup_lock(settings, ttl_seconds=60)
        assert owner is not None
        second = acquire_backup_lock(settings, ttl_seconds=60)
        assert second is None
        report = create_online_backup(settings)
        assert report.status == "BUSY"
        release_backup_lock(settings, owner)
        ok = create_online_backup(settings)
        assert ok.status == "VERIFIED"

    def test_stale_lock_recovery(self, isolated):
        settings, _db, _b = isolated
        past = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        with get_connection(settings) as conn:
            conn.execute(
                """
                INSERT INTO copy_job_locks (job_name, owner, locked_at, expires_at)
                VALUES (?, 'stale-owner', ?, ?)
                """,
                (STORAGE_BACKUP_LOCK_JOB, past, past),
            )
        owner = acquire_backup_lock(settings, ttl_seconds=30)
        assert owner is not None
        release_backup_lock(settings, owner)


class TestLowDiskAndRetentionPreview:
    def test_low_disk_refusal(self, isolated):
        settings, _db, _b = isolated
        with patch("app.storage.backup._disk_free", return_value=0):
            refused_settings = make_test_settings(
                database_path=settings.database_path,
                storage_backup_dir=settings.storage_backup_dir,
                storage_backup_min_free_bytes=10_000_000,
            )
            initialize_database(refused_settings)
            report = create_online_backup(refused_settings)
        assert report.status == "REFUSED"
        assert "low disk" in report.detail.lower()

    def test_dry_run_retention_protects_newest_and_only(self, isolated):
        settings, _db, backup_dir = isolated
        r1 = create_online_backup(settings)
        assert r1.status == "VERIFIED"
        labels, count = inventory_expired_backups(settings)
        assert count == 0
        assert labels == []

        r2 = create_online_backup(settings)
        r3 = create_online_backup(settings)
        assert r2.status == "VERIFIED" and r3.status == "VERIFIED"
        labels, count = inventory_expired_backups(settings)
        assert count >= 1
        files = list(backup_dir.glob("*.db"))
        assert len(files) == 3
        oldest = sorted(files, key=lambda p: p.stat().st_mtime)[0]
        meta_path = Path(str(oldest) + ".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["created_at"] = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        labels2, count2 = inventory_expired_backups(settings)
        assert count2 >= 1
        assert oldest.name in labels2
        assert oldest.exists()
        assert len(list(backup_dir.glob("*.db"))) == 3


class TestAdminAndSanitize:
    @pytest.mark.asyncio
    async def test_admin_only_status_and_non_admin_denial(self, isolated):
        settings, _db, _b = isolated
        create_online_backup(settings)
        text = format_backup_status(settings)
        assert "Online Backup" in text
        assert "C:\\" not in text
        assert "/Users/" not in text

        admin = MagicMock()
        admin.effective_user = MagicMock(id=1001)
        admin.callback_query = None
        admin.effective_message = MagicMock()
        assert await require_admin(admin, settings, action="cb:health_backup") is True

        guest = MagicMock()
        guest.effective_user = MagicMock(id=999)
        guest.callback_query = AsyncMock()
        guest.effective_message = MagicMock()
        guest.effective_message.reply_text = AsyncMock()
        assert await require_admin(guest, settings, action="cb:health_backup") is False

        assert CB_HEALTH_BACKUP in HEALTH_CALLBACKS
        assert CB_HEALTH_BACKUP_CONFIRM in HEALTH_CALLBACKS
        markup = health_inline_keyboard()
        flat = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert CB_HEALTH_BACKUP in flat

    def test_path_sanitization_in_config(self, isolated):
        settings, _db, _b = isolated
        cfg = sanitize_config_summary(settings)
        assert cfg["storage_backup"] == "backup"
        assert assert_no_private_path(cfg["storage_backup"])
        assert assert_no_private_path(cfg["database"])
        status = backup_status(settings)
        payload = status.as_sanitized_dict()
        for _key, value in payload.items():
            if isinstance(value, str):
                assert assert_no_private_path(value)


class TestNoShutilCopyPathInModule:
    def test_backup_module_does_not_copy_live_db(self):
        import inspect

        from app.storage import backup as mod

        src = inspect.getsource(mod.create_online_backup)
        assert "shutil.copy(" not in src
        assert "shutil.copy2(" not in src
        assert "shutil.copyfile(" not in src
        assert ".backup(" in src
