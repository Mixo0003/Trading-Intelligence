"""V2.1.1 Phase 3B: verified-backup-gated bounded SQLite retention pruning."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config.constants import (
    STORAGE_RETENTION_PRUNE_DRY_RUN,
    STORAGE_RETENTION_PRUNE_ENABLED,
    STORAGE_RETENTION_PRUNE_LOCK_JOB,
)
from app.database.storage import get_connection, initialize_database
from app.health.sanitize import assert_no_private_path, sanitize_config_summary
from app.storage.backup import (
    compute_source_database_identity,
    create_online_backup,
    revalidate_verified_backup_for_pruning,
)
from app.storage.lock import acquire_prune_lock, release_prune_lock
from app.storage.retention_policies import build_retention_policies
from app.storage.retention_prune import (
    _CONFIRMATIONS,
    create_prune_confirmation,
    execute_confirmed_prune,
    run_retention_prune,
)
from tests.conftest import make_test_settings


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


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
        storage_retention_prune_enabled=False,
        storage_retention_prune_dry_run=True,
        storage_retention_prune_max_rows_per_run=10,
        storage_retention_prune_max_rows_per_policy=5,
        storage_retention_prune_max_backup_age_seconds=86400,
        storage_retention_prune_confirm_ttl_seconds=60,
        admin_ids=[1001],
    )
    initialize_database(settings)
    with get_connection(settings) as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, language, created_at, last_seen_at)
            VALUES (1, 'u', 'U', 'en', ?, ?)
            """,
            (_iso_days_ago(1), _iso_days_ago(1)),
        )
        for i in range(8):
            conn.execute(
                """
                INSERT INTO health_checks (component, status, detail, metrics_json, checked_at)
                VALUES ('sqlite', 'HEALTHY', 'ok', '{}', ?)
                """,
                (_iso_days_ago(30 - i),),
            )
        conn.execute(
            """
            INSERT INTO health_checks (component, status, detail, metrics_json, checked_at)
            VALUES ('sqlite', 'HEALTHY', 'recent', '{}', ?)
            """,
            (_iso_days_ago(1),),
        )
        conn.execute(
            """
            INSERT INTO job_executions (job_name, status, started_at, expected_interval_seconds)
            VALUES ('running_job', 'running', ?, 60)
            """,
            (_iso_days_ago(40),),
        )
        conn.execute(
            """
            INSERT INTO job_executions (job_name, status, started_at, finished_at, duration_ms)
            VALUES ('old_job', 'ok', ?, ?, 10)
            """,
            (_iso_days_ago(40), _iso_days_ago(40)),
        )
    return settings, db_path, backup_dir


def _enable_destructive(settings, **overrides):
    return replace(
        settings,
        storage_retention_prune_enabled=True,
        storage_retention_prune_dry_run=False,
        **overrides,
    )


def _verified_backup(settings):
    report = create_online_backup(settings)
    assert report.status == "VERIFIED"
    return report


class TestDefaultsAndDryRun:
    def test_pruning_disabled_by_default(self):
        assert STORAGE_RETENTION_PRUNE_ENABLED is False
        settings = make_test_settings()
        assert settings.storage_retention_prune_enabled is False

    def test_dry_run_enabled_by_default(self):
        assert STORAGE_RETENTION_PRUNE_DRY_RUN is True
        settings = make_test_settings()
        assert settings.storage_retention_prune_dry_run is True

    def test_dry_run_zero_deletes(self, isolated):
        settings, db_path, _backup = isolated
        before = db_path.stat().st_size
        with get_connection(settings) as conn:
            count_before = conn.execute("SELECT COUNT(*) FROM health_checks").fetchone()[0]
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=True,
            operator_id="1",
        )
        assert report.status == "DRY_RUN"
        assert sum(report.deleted_counts.values()) == 0
        with get_connection(settings) as conn:
            count_after = conn.execute("SELECT COUNT(*) FROM health_checks").fetchone()[0]
        assert count_after == count_before
        assert db_path.stat().st_size == before
        assert report.mutation_proof.get("delete_statements") == 0

    def test_dry_run_tracks_no_delete_sql(self, isolated):
        settings, _db, _b = isolated
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=True,
        )
        assert report.status == "DRY_RUN"
        assert report.mutation_proof.get("delete_statements") == 0
        assert not any(
            s.upper().startswith("DELETE") for s in report.sql_statements_executed
        )


class TestBackupGate:
    def test_missing_backup_refusal(self, isolated):
        settings, _db, backup_dir = isolated
        settings = _enable_destructive(settings)
        for path in backup_dir.glob("*"):
            path.unlink()
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "REFUSED"
        assert report.failure_code == "missing_backup"

    def test_unverified_backup_refusal(self, isolated):
        settings, db_path, backup_dir = isolated
        settings = _enable_destructive(settings)
        fake = backup_dir / "ti_backup_fake.db"
        src = sqlite3.connect(str(db_path))
        dest = sqlite3.connect(str(fake))
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        meta = {
            "status": "FAILED",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "integrity_result": "ok",
            "source_database_identity": compute_source_database_identity(str(db_path)),
            "label": fake.name,
        }
        (Path(str(fake) + ".meta.json")).write_text(json.dumps(meta), encoding="utf-8")
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "REFUSED"
        assert report.failure_code in {"unverified_backup", "missing_backup"}

    def test_failed_integrity_refusal(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        with patch(
            "app.storage.backup._verify_backup_file",
            return_value=("FAILED", "not ok", {"quick_check": "ok", "missing_required": []}),
        ):
            report = run_retention_prune(
                settings,
                approved_datasets=["health_check_history"],
                dry_run=False,
            )
        assert report.status == "REFUSED"
        assert report.failure_code == "failed_integrity"

    def test_expired_backup_refusal(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings, storage_retention_prune_max_backup_age_seconds=1)
        _verified_backup(settings)
        time.sleep(1.1)
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "REFUSED"
        assert report.failure_code == "expired_backup"

    def test_wrong_database_identity_refusal(self, isolated):
        settings, _db, backup_dir = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        for meta_path in backup_dir.glob("*.meta.json"):
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data["source_database_identity"] = "deadbeef" * 4
            meta_path.write_text(json.dumps(data), encoding="utf-8")
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "REFUSED"
        assert report.failure_code == "wrong_database_identity"

    def test_missing_backup_metadata_refusal(self, isolated):
        settings, _db, backup_dir = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        for meta_path in backup_dir.glob("*.meta.json"):
            meta_path.unlink()
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "REFUSED"
        assert report.failure_code == "missing_backup_metadata"

    def test_backup_revalidation_before_execution(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        calls = {"n": 0}
        real = revalidate_verified_backup_for_pruning

        def counting(settings_arg, **kwargs):
            calls["n"] += 1
            return real(settings_arg, **kwargs)

        with patch(
            "app.storage.retention_prune.revalidate_verified_backup_for_pruning",
            side_effect=counting,
        ):
            report = run_retention_prune(
                settings,
                approved_datasets=["health_check_history"],
                dry_run=False,
            )
        assert report.status == "SUCCEEDED"
        assert calls["n"] >= 2


class TestLocksAndPolicies:
    def test_lock_unavailable_refusal(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        owner = acquire_prune_lock(settings, ttl_seconds=60)
        assert owner is not None
        try:
            report = run_retention_prune(
                settings,
                approved_datasets=["health_check_history"],
                dry_run=False,
            )
            assert report.status == "REFUSED"
            assert report.failure_code == "lock_unavailable"
        finally:
            release_prune_lock(settings, owner)

    def test_stale_lock_bounded_recovery(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        owner = acquire_prune_lock(settings, ttl_seconds=1)
        assert owner is not None
        time.sleep(1.2)
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "SUCCEEDED"

    def test_approved_policy_allowlist(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "SUCCEEDED"
        assert "health_check_history" in report.deleted_counts

    def test_protected_policy_rejection(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        report = run_retention_prune(
            settings,
            approved_datasets=["processed_copy_events_dedup"],
            dry_run=False,
        )
        assert report.status == "REFUSED"
        assert report.failure_code == "protected_policy"

    def test_unknown_policy_rejection(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        report = run_retention_prune(
            settings,
            approved_datasets=["not_a_real_policy"],
            dry_run=False,
        )
        assert report.status == "REFUSED"
        assert report.failure_code == "unknown_policy"

    def test_missing_table_refusal(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        with get_connection(settings) as conn:
            conn.execute("DROP TABLE health_checks")
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "REFUSED"
        assert report.failure_code == "missing_table"


class TestDeletionSemantics:
    def test_terminal_state_and_recent_exclusion(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        with get_connection(settings) as conn:
            running_before = conn.execute(
                "SELECT COUNT(*) FROM job_executions WHERE status='running'"
            ).fetchone()[0]
            recent_before = conn.execute(
                "SELECT COUNT(*) FROM health_checks WHERE detail='recent'"
            ).fetchone()[0]
        report = run_retention_prune(
            settings,
            approved_datasets=["job_execution_history", "health_check_history"],
            dry_run=False,
        )
        assert report.status == "SUCCEEDED"
        with get_connection(settings) as conn:
            running_after = conn.execute(
                "SELECT COUNT(*) FROM job_executions WHERE status='running'"
            ).fetchone()[0]
            recent_after = conn.execute(
                "SELECT COUNT(*) FROM health_checks WHERE detail='recent'"
            ).fetchone()[0]
        assert running_after == running_before == 1
        assert recent_after == recent_before == 1

    def test_oldest_first_and_limits(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(
            settings,
            storage_retention_prune_max_rows_per_policy=3,
            storage_retention_prune_max_rows_per_run=3,
        )
        _verified_backup(settings)
        with get_connection(settings) as conn:
            before = conn.execute("SELECT COUNT(*) FROM health_checks").fetchone()[0]
            oldest = conn.execute(
                "SELECT checked_at FROM health_checks ORDER BY checked_at ASC LIMIT 3"
            ).fetchall()
            oldest_vals = [r[0] for r in oldest]
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "SUCCEEDED"
        assert report.deleted_counts["health_check_history"] == 3
        with get_connection(settings) as conn:
            after = conn.execute("SELECT COUNT(*) FROM health_checks").fetchone()[0]
            remaining_old = conn.execute(
                "SELECT COUNT(*) FROM health_checks WHERE checked_at = ?",
                (oldest_vals[0],),
            ).fetchone()[0]
        assert before - after == 3
        assert remaining_old == 0

    def test_per_run_limit_across_policies(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(
            settings,
            storage_retention_prune_max_rows_per_run=2,
            storage_retention_prune_max_rows_per_policy=10,
        )
        _verified_backup(settings)
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history", "job_execution_history"],
            dry_run=False,
        )
        assert report.status == "SUCCEEDED"
        assert sum(report.deleted_counts.values()) <= 2

    def test_protected_counts_unchanged(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        with get_connection(settings) as conn:
            users_before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "SUCCEEDED"
        assert report.mutation_proof.get("protected_counts_unchanged") is True
        with get_connection(settings) as conn:
            users_after = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        assert users_after == users_before

    def test_rollback_after_simulated_failure(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        with get_connection(settings) as conn:
            before = conn.execute("SELECT COUNT(*) FROM health_checks").fetchone()[0]
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
            _simulate_post_delete_failure=True,
        )
        assert report.status == "FAILED"
        assert report.rolled_back is True
        with get_connection(settings) as conn:
            after = conn.execute("SELECT COUNT(*) FROM health_checks").fetchone()[0]
        assert after == before

    def test_idempotent_repeat(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(
            settings,
            storage_retention_prune_max_rows_per_run=100,
            storage_retention_prune_max_rows_per_policy=100,
        )
        _verified_backup(settings)
        r1 = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert r1.status == "SUCCEEDED"
        r2 = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert r2.status == "SUCCEEDED"
        assert r2.deleted_counts.get("health_check_history", 0) == 0

    def test_concurrent_execution_safety(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(
            settings,
            storage_retention_prune_max_rows_per_run=100,
            storage_retention_prune_max_rows_per_policy=100,
        )
        _verified_backup(settings)
        results: list = []

        def worker():
            results.append(
                run_retention_prune(
                    settings,
                    approved_datasets=["health_check_history"],
                    dry_run=False,
                )
            )

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        statuses = {r.status for r in results}
        assert "SUCCEEDED" in statuses
        assert statuses <= {"SUCCEEDED", "REFUSED", "FAILED"}
        refused = [r for r in results if r.status == "REFUSED"]
        if refused:
            assert any(r.failure_code == "lock_unavailable" for r in refused)
        with get_connection(settings) as conn:
            recent = conn.execute(
                "SELECT COUNT(*) FROM health_checks WHERE detail='recent'"
            ).fetchone()[0]
        assert recent == 1

    def test_no_vacuum_no_journal_change(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        with get_connection(settings) as conn:
            mode_before = conn.execute("PRAGMA journal_mode").fetchone()[0]
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "SUCCEEDED"
        assert not any("VACUUM" in s.upper() for s in report.sql_statements_executed)
        with get_connection(settings) as conn:
            mode_after = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode_after).lower() == str(mode_before).lower()
        assert report.mutation_proof.get("no_vacuum") is True
        assert report.mutation_proof.get("journal_mode_unchanged") is True

    def test_parameterized_sql_uses_placeholders(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "SUCCEEDED"
        delete_sqls = [
            s for s in report.sql_statements_executed if s.upper().startswith("DELETE")
        ]
        assert delete_sqls
        for sql in delete_sqls:
            assert "?" in sql
            assert "DELETE FROM" in sql.upper()
            # Cutoff values must be bound params, not interpolated literals.
            assert "T" not in sql.split("WHERE", 1)[-1] or "?" in sql.split("WHERE", 1)[-1]


class TestAuditAndConfirmation:
    def test_sanitized_audit_record(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
            operator_id="1001",
        )
        assert report.status == "SUCCEEDED"
        payload = report.as_sanitized_dict()
        blob = json.dumps(payload)
        assert "Users" not in blob
        assert ":\\" not in blob
        assert assert_no_private_path(payload["prune_path_label"])
        cfg = sanitize_config_summary(settings)
        assert assert_no_private_path(cfg["database"])
        with get_connection(settings) as conn:
            row = conn.execute(
                "SELECT error FROM job_executions WHERE job_name = ? ORDER BY id DESC LIMIT 1",
                (STORAGE_RETENTION_PRUNE_LOCK_JOB,),
            ).fetchone()
        assert row is not None
        assert "wallet" not in (row[0] or "").lower()

    def test_confirmation_expiry_and_binding(self, isolated):
        settings, _db, _b = isolated
        settings = _enable_destructive(settings, storage_retention_prune_confirm_ttl_seconds=1)
        _verified_backup(settings)
        confirmation, preview = create_prune_confirmation(
            settings,
            operator_id="1001",
            approved_datasets=["health_check_history"],
        )
        assert confirmation is not None
        assert preview.status == "DRY_RUN"
        time.sleep(1.2)
        refused = execute_confirmed_prune(
            settings, token=confirmation.token, operator_id="1001"
        )
        assert refused.status == "REFUSED"
        assert refused.failure_code == "confirmation_expired"

        confirmation2, _ = create_prune_confirmation(
            settings,
            operator_id="1001",
            approved_datasets=["health_check_history"],
        )
        assert confirmation2 is not None
        wrong_op = execute_confirmed_prune(
            settings, token=confirmation2.token, operator_id="9999"
        )
        assert wrong_op.status == "REFUSED"
        assert wrong_op.failure_code == "confirmation_operator_mismatch"

        confirmation3, _ = create_prune_confirmation(
            settings,
            operator_id="1001",
            approved_datasets=["health_check_history"],
        )
        assert confirmation3 is not None
        ok = execute_confirmed_prune(
            settings, token=confirmation3.token, operator_id="1001"
        )
        assert ok.status == "SUCCEEDED"
        again = execute_confirmed_prune(
            settings, token=confirmation3.token, operator_id="1001"
        )
        assert again.status == "REFUSED"
        assert again.failure_code == "invalid_confirmation"
        _CONFIRMATIONS.clear()

    def test_destructive_refused_while_defaults(self, isolated):
        settings, _db, _b = isolated
        _verified_backup(settings)
        report = run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        assert report.status == "REFUSED"
        assert report.failure_code == "prune_disabled"


class TestRegistryAndRegressions:
    def test_approved_and_protected_datasets_present(self):
        policies = build_retention_policies()
        approved = {p.dataset for p in policies if p.kind == "APPROVED"}
        protected = {p.dataset for p in policies if p.kind == "PROTECTED"}
        assert "health_check_history" in approved
        assert "processed_copy_events_dedup" in protected
        assert "copy_watchlist" in protected
        assert "users" in protected

    def test_backup_untouched(self, isolated):
        settings, _db, backup_dir = isolated
        settings = _enable_destructive(settings)
        _verified_backup(settings)
        before = sorted(p.name for p in backup_dir.glob("*"))
        run_retention_prune(
            settings,
            approved_datasets=["health_check_history"],
            dry_run=False,
        )
        after = sorted(p.name for p in backup_dir.glob("*"))
        assert before == after
