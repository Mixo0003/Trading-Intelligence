"""V2.1.1 Phase 3A: retention audit dry-run (read-only)."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.storage import get_connection, initialize_database
from app.storage.retention_audit import run_retention_audit
from app.storage.retention_policies import (
    RetentionPolicy,
    build_retention_policies,
    validate_policy_registry,
)
from app.telegram.auth import require_admin
from app.telegram.health.buttons import CB_HEALTH_RETENTION, HEALTH_CALLBACKS, health_inline_keyboard
from app.telegram.health.formatting import format_retention_preview
from app.telegram.inline_menus import main_inline_keyboard
from tests.conftest import make_test_settings


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(database_path=str(tmp_path / "retention.db"), admin_ids=[1001])
    initialize_database(s)
    return s


class TestPolicyRegistry:
    def test_registry_valid(self):
        errors = validate_policy_registry()
        assert errors == []
        policies = build_retention_policies()
        assert any(p.dataset == "processed_copy_events_dedup" and p.kind == "PROTECTED" for p in policies)
        assert any(p.dataset == "health_check_history" and p.kind == "APPROVED" for p in policies)

    def test_protected_never_approved_candidates(self, settings):
        with get_connection(settings) as connection:
            connection.execute(
                """
                INSERT INTO processed_copy_events (
                    event_uid, wallet, event_ts, observed_at, is_baseline, alertable, created_at
                ) VALUES ('e1', '0xabc', ?, ?, 0, 0, ?)
                """,
                (_iso_days_ago(120), _iso_days_ago(120), _iso_days_ago(120)),
            )
        report = run_retention_audit(settings)
        item = next(p for p in report.policies if p.dataset == "processed_copy_events_dedup")
        assert item.status == "PROTECTED"
        assert item.candidate_rows == 0


class TestSchemaAndPredicates:
    def test_missing_table_unsupported(self, settings):
        policy = RetentionPolicy(
            dataset="missing_table",
            table="does_not_exist_xyz",
            kind="APPROVED",
            timestamp_column="created_at",
            retention_days=14,
            batch_size=100,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="test",
        )
        # Inject via temporary monkeypatch of build list through evaluate path:
        from app.storage import retention_audit as mod

        original = mod.build_retention_policies
        try:
            mod.build_retention_policies = lambda: (policy,)
            report = run_retention_audit(settings)
        finally:
            mod.build_retention_policies = original
        assert report.policies[0].status == "UNSUPPORTED"
        assert "missing" in report.policies[0].reason

    def test_missing_timestamp_column(self, settings):
        policy = RetentionPolicy(
            dataset="bad_ts",
            table="users",
            kind="APPROVED",
            timestamp_column="not_a_real_column",
            retention_days=14,
            batch_size=100,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="test",
        )
        from app.storage import retention_audit as mod

        original = mod.build_retention_policies
        try:
            mod.build_retention_policies = lambda: (policy,)
            report = run_retention_audit(settings)
        finally:
            mod.build_retention_policies = original
        assert report.policies[0].status == "UNSUPPORTED"
        assert "timestamp column missing" in report.policies[0].reason

    def test_terminal_old_and_recent_excluded(self, settings):
        with get_connection(settings) as connection:
            connection.execute(
                """
                INSERT INTO health_checks (component, status, detail, metrics_json, checked_at)
                VALUES ('sqlite', 'HEALTHY', 'old', '{}', ?)
                """,
                (_iso_days_ago(30),),
            )
            connection.execute(
                """
                INSERT INTO health_checks (component, status, detail, metrics_json, checked_at)
                VALUES ('sqlite', 'HEALTHY', 'new', '{}', ?)
                """,
                (_iso_days_ago(1),),
            )
            connection.execute(
                """
                INSERT INTO job_executions (
                    job_name, status, started_at, finished_at, duration_ms, error, expected_interval_seconds
                ) VALUES ('copy_intel_poll', 'running', ?, NULL, NULL, NULL, 120)
                """,
                (_iso_days_ago(30),),
            )
            connection.execute(
                """
                INSERT INTO job_executions (
                    job_name, status, started_at, finished_at, duration_ms, error, expected_interval_seconds
                ) VALUES ('copy_intel_poll', 'ok', ?, ?, 10, NULL, 120)
                """,
                (_iso_days_ago(30), _iso_days_ago(30)),
            )
            connection.execute(
                """
                INSERT INTO copy_alert_deliveries (
                    subscriber_id, wallet, event_uid, status, payload_text, created_at, updated_at
                ) VALUES (1, '0xabc', 'u1', 'sent', NULL, ?, ?)
                """,
                (_iso_days_ago(90), _iso_days_ago(90)),
            )
            connection.execute(
                """
                INSERT INTO copy_alert_deliveries (
                    subscriber_id, wallet, event_uid, status, payload_text, created_at, updated_at
                ) VALUES (1, '0xabc', 'u2', 'claimed', NULL, ?, ?)
                """,
                (_iso_days_ago(90), _iso_days_ago(90)),
            )
        report = run_retention_audit(settings)
        health = next(p for p in report.policies if p.dataset == "health_check_history")
        assert health.status == "APPROVED"
        assert health.candidate_rows == 1
        jobs = next(p for p in report.policies if p.dataset == "job_execution_history")
        assert jobs.candidate_rows == 1
        deliveries = next(p for p in report.policies if p.dataset == "terminal_alert_deliveries")
        assert deliveries.candidate_rows == 1

    def test_expired_lock_logic(self, settings):
        past = _iso_days_ago(1)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with get_connection(settings) as connection:
            connection.execute(
                """
                INSERT INTO copy_job_locks (job_name, owner, locked_at, expires_at)
                VALUES ('old_lock', 'o1', ?, ?)
                """,
                (past, past),
            )
            connection.execute(
                """
                INSERT INTO copy_job_locks (job_name, owner, locked_at, expires_at)
                VALUES ('live_lock', 'o2', ?, ?)
                """,
                (past, future),
            )
        report = run_retention_audit(settings)
        locks = next(p for p in report.policies if p.dataset == "expired_job_locks")
        assert locks.status == "APPROVED"
        assert locks.candidate_rows == 1
        assert locks.protected_or_excluded_rows == 1


class TestDryRunMutation:
    def test_dry_run_zero_mutations(self, settings):
        with get_connection(settings) as connection:
            connection.execute(
                """
                INSERT INTO analysis_records (
                    telegram_id, market_url, market_title, decision, confidence, ev, edge, payload, created_at
                ) VALUES (1, 'u', 't', 'SKIP', 0.1, 0, 0, '{}', ?)
                """,
                (_iso_days_ago(120),),
            )
            before = {
                "analysis_records": connection.execute(
                    "SELECT COUNT(*) FROM analysis_records"
                ).fetchone()[0],
                "processed_copy_events": connection.execute(
                    "SELECT COUNT(*) FROM processed_copy_events"
                ).fetchone()[0],
                "users": connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            }
            page_count = connection.execute("PRAGMA page_count").fetchone()[0]

        report1 = run_retention_audit(settings)
        report2 = run_retention_audit(settings)
        assert report1.dry_run is True
        assert report1.mutation_proof.get("row_counts_unchanged") is True
        assert report2.mutation_proof.get("row_counts_unchanged") is True

        with get_connection(settings) as connection:
            after = {
                "analysis_records": connection.execute(
                    "SELECT COUNT(*) FROM analysis_records"
                ).fetchone()[0],
                "processed_copy_events": connection.execute(
                    "SELECT COUNT(*) FROM processed_copy_events"
                ).fetchone()[0],
                "users": connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            }
            after_pages = connection.execute("PRAGMA page_count").fetchone()[0]
        assert after == before
        assert after_pages == page_count

    def test_no_write_sql_in_audit_module(self):
        source = Path("app/storage/retention_audit.py").read_text(encoding="utf-8")
        for banned in ("DELETE FROM", "UPDATE ", "INSERT INTO", "DROP TABLE", "VACUUM", "wal_checkpoint"):
            assert banned not in source


class TestPreviewAndAdmin:
    def test_sanitized_preview(self, settings):
        text = format_retention_preview(settings)
        assert "DRY RUN — no database rows were deleted." in text
        assert "row counts only" in text.lower() or "Scope is row counts" in text
        assert "0x" not in text
        assert "Users\\" not in text
        assert "/home/" not in text
        assert "CREATE TABLE" not in text
        assert "SELECT " not in text

    def test_pagination_compact(self, settings):
        page0 = format_retention_preview(settings, page=0, page_size=5)
        page1 = format_retention_preview(settings, page=1, page_size=5)
        assert "Page 1/" in page0
        assert "Page 2/" in page1

    @pytest.mark.asyncio
    async def test_admin_only_access(self, settings):
        assert CB_HEALTH_RETENTION in HEALTH_CALLBACKS
        labels = [
            btn.text
            for row in health_inline_keyboard().inline_keyboard
            for btn in row
        ]
        assert "📋 Retention Dry-Run" in labels
        guest_labels = [
            btn.text
            for row in main_inline_keyboard(is_admin=False).inline_keyboard
            for btn in row
        ]
        assert "🩺 System Health" not in guest_labels

        update = MagicMock()
        update.effective_user = MagicMock(id=9999)
        update.effective_message = MagicMock()
        update.effective_message.reply_text = AsyncMock()
        ok = await require_admin(update, settings, action="cb:health:retention")
        assert ok is False

    def test_malformed_policy_does_not_abort(self, settings):
        from app.storage import retention_audit as mod

        bad = RetentionPolicy(
            dataset="broken",
            table="health_checks",
            kind="APPROVED",
            timestamp_column="checked_at",
            retention_days=14,
            batch_size=100,
            terminal_predicate="NOT A VALID SQL (((",
            protected_exclusion_predicate="",
            reason="bad",
        )
        good = next(p for p in build_retention_policies() if p.dataset == "health_check_history")
        original = mod.build_retention_policies
        try:
            mod.build_retention_policies = lambda: (bad, good)
            report = run_retention_audit(settings)
        finally:
            mod.build_retention_policies = original
        assert len(report.policies) == 2
        assert any(p.dataset == "health_check_history" for p in report.policies)
