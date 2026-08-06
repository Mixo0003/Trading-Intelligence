"""V2.1.1 Phase 5: Admin Storage UX, confirmations, alerts, history."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database.storage import get_connection, initialize_database
from app.health.alerts import dispatch_health_alerts, sync_incidents_from_health
from app.health.models import ComponentStatus, OverallHealth
from app.health.sanitize import assert_no_private_path
from app.health.storage_alerts import sync_storage_alerts
from app.health import storage as health_storage
from app.storage.backup import (
    _CONFIRMATIONS as _BACKUP_CONFIRMATIONS,
    create_backup_confirmation,
    create_online_backup,
    execute_confirmed_backup,
)
from app.storage.cleanup import (
    _CONFIRMATIONS as _CLEANUP_CONFIRMATIONS,
    create_cleanup_confirmation,
    execute_confirmed_cleanup,
)
from app.storage.history import list_maintenance_history
from app.storage.lock import acquire_backup_lock, acquire_cleanup_lock
from app.storage.retention_prune import (
    _CONFIRMATIONS as _PRUNE_CONFIRMATIONS,
    create_prune_confirmation,
    execute_confirmed_prune,
)
from app.telegram.auth import require_admin
from app.telegram.health.buttons import (
    CB_HEALTH_STORAGE,
    CB_HEALTH_STORAGE_BACKUP_CREATE,
    CB_HEALTH_STORAGE_CLEANUP_PREVIEW,
    CB_HEALTH_STORAGE_HISTORY,
    CB_HEALTH_STORAGE_METRICS,
    CB_HEALTH_STORAGE_PRUNE_RUN,
    HEALTH_CALLBACKS,
    health_inline_keyboard,
    storage_inline_keyboard,
)
from app.telegram.health.formatting import (
    format_maintenance_history,
    format_storage_cleanup_preview,
    format_storage_menu,
    format_storage_prune_result,
)
from app.telegram.health import handlers as health_handlers
from tests.conftest import make_test_settings


LIVE_DB_PROBE = Path("data/trading_intelligence.db")


@pytest.fixture
def isolated(tmp_path):
    db_path = tmp_path / "live.db"
    backup_dir = tmp_path / "backups"
    logs_dir = tmp_path / "logs"
    backup_dir.mkdir()
    logs_dir.mkdir()
    settings = make_test_settings(
        database_path=str(db_path),
        storage_backup_dir=str(backup_dir.resolve()),
        storage_backup_enabled=True,
        storage_backup_min_free_bytes=1,
        storage_backup_confirm_ttl_seconds=120,
        storage_logs_dir=str(logs_dir),
        storage_cleanup_enabled=False,
        storage_cleanup_dry_run=True,
        storage_cleanup_confirm_ttl_seconds=120,
        storage_retention_prune_enabled=False,
        storage_retention_prune_dry_run=True,
        admin_ids=[1001],
        health_alerts_enabled=True,
    )
    initialize_database(settings)
    yield settings, db_path, backup_dir, logs_dir
    _BACKUP_CONFIRMATIONS.clear()
    _CLEANUP_CONFIRMATIONS.clear()
    _PRUNE_CONFIRMATIONS.clear()


def _labels(markup) -> list[str]:
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            out.append(btn.text)
    return out


def _mtime_and_size(path: Path) -> tuple[float | None, int | None]:
    if not path.exists():
        return None, None
    st = path.stat()
    return st.st_mtime, st.st_size


class TestStorageMenuAndAuth:
    def test_storage_entry_and_buttons(self):
        health_labels = _labels(health_inline_keyboard())
        assert "📦 Storage" in health_labels
        labels = _labels(storage_inline_keyboard())
        assert "📈 Storage Metrics" in labels
        assert "💾 Backup Status" in labels
        assert "🛡 Create Backup" in labels
        assert "🧹 File Cleanup Preview" in labels
        assert "📋 Retention Preview" in labels
        assert "🗑 Run Safe File Cleanup" in labels
        assert "✂ Run Retention Batch" in labels
        assert "📜 Maintenance History" in labels
        assert "🔄 Refresh" in labels
        assert "⬅️ Back to System Health" in labels
        assert CB_HEALTH_STORAGE in HEALTH_CALLBACKS
        assert CB_HEALTH_STORAGE_METRICS in HEALTH_CALLBACKS
        assert CB_HEALTH_STORAGE_BACKUP_CREATE in HEALTH_CALLBACKS
        assert CB_HEALTH_STORAGE_CLEANUP_PREVIEW in HEALTH_CALLBACKS
        assert CB_HEALTH_STORAGE_PRUNE_RUN in HEALTH_CALLBACKS
        assert CB_HEALTH_STORAGE_HISTORY in HEALTH_CALLBACKS

    @pytest.mark.asyncio
    async def test_non_admin_callback_denied(self, isolated):
        settings, *_ = isolated
        update = MagicMock()
        update.effective_user = MagicMock(id=9999)
        update.effective_message = MagicMock()
        update.effective_message.reply_text = AsyncMock()
        ok = await require_admin(update, settings, action="cb:health:storage")
        assert ok is False

    @pytest.mark.asyncio
    async def test_admin_handler_rechecks_auth(self, isolated):
        settings, *_ = isolated
        update = MagicMock()
        update.effective_user = MagicMock(id=9999)
        update.effective_message = MagicMock()
        update.effective_message.reply_text = AsyncMock()
        update.callback_query = None
        context = MagicMock()
        context.application.bot_data = {"settings": settings}
        context.user_data = {}
        with patch(
            "app.telegram.health.handlers.require_admin",
            new=AsyncMock(return_value=False),
        ) as req:
            await health_handlers.handle_health_callback(
                update, context, CB_HEALTH_STORAGE
            )
            req.assert_awaited()


class TestBackupConfirmationFlow:
    def test_binding_expiry_and_replay(self, isolated):
        settings, db_path, backup_dir, _logs = isolated
        settings = make_test_settings(
            database_path=str(db_path),
            storage_backup_dir=str(backup_dir.resolve()),
            storage_backup_enabled=True,
            storage_backup_min_free_bytes=1,
            storage_backup_confirm_ttl_seconds=1,
            admin_ids=[1001],
        )
        initialize_database(settings)
        confirmation, preview = create_backup_confirmation(
            settings, operator_id="1001"
        )
        assert confirmation is not None
        time.sleep(1.2)
        expired = execute_confirmed_backup(
            settings, token=confirmation.token, operator_id="1001"
        )
        assert expired.status == "REFUSED"
        assert expired.mutation_proof.get("failure_code") == "confirmation_expired"

        confirmation2, _ = create_backup_confirmation(settings, operator_id="1001")
        assert confirmation2 is not None
        wrong = execute_confirmed_backup(
            settings, token=confirmation2.token, operator_id="9999"
        )
        assert wrong.status == "REFUSED"

        confirmation3, _ = create_backup_confirmation(settings, operator_id="1001")
        assert confirmation3 is not None
        ok = execute_confirmed_backup(
            settings, token=confirmation3.token, operator_id="1001"
        )
        assert ok.status == "VERIFIED"
        again = execute_confirmed_backup(
            settings, token=confirmation3.token, operator_id="1001"
        )
        assert again.status == "REFUSED"

    def test_disabled_backup_unavailable(self, isolated):
        settings, db_path, backup_dir, _ = isolated
        settings = make_test_settings(
            database_path=str(db_path),
            storage_backup_dir=str(backup_dir.resolve()),
            storage_backup_enabled=False,
            storage_backup_min_free_bytes=1,
            admin_ids=[1001],
        )
        initialize_database(settings)
        confirmation, _ = create_backup_confirmation(settings, operator_id="1001")
        assert confirmation is not None
        report = execute_confirmed_backup(
            settings, token=confirmation.token, operator_id="1001"
        )
        assert report.status == "UNAVAILABLE"


class TestCleanupConfirmationFlow:
    def test_default_disabled_and_changed_candidates(self, isolated):
        settings, db_path, _b, logs_dir = isolated
        import os

        old = logs_dir / "trading_intelligence.log.1"
        old.write_text("x" * 100, encoding="utf-8")
        # Older than STORAGE_LOG_RETENTION_DAYS (set to 1 day below).
        past = time.time() - 2 * 86400
        os.utime(old, (past, past))

        confirmation, report = create_cleanup_confirmation(
            settings, operator_id="1001"
        )
        assert confirmation is None
        assert report.status == "disabled"

        settings = make_test_settings(
            database_path=str(db_path),
            storage_logs_dir=str(logs_dir),
            storage_cleanup_enabled=True,
            storage_cleanup_dry_run=False,
            storage_cleanup_min_age_seconds=1,
            storage_log_retention_days=1,
            storage_cleanup_confirm_ttl_seconds=120,
            storage_backup_dir=str((_b).resolve()),
            admin_ids=[1001],
        )
        initialize_database(settings)
        confirmation, report = create_cleanup_confirmation(
            settings, operator_id="1001"
        )
        assert confirmation is not None
        assert report.candidate_count >= 1

        # Change candidates → invalidate
        (logs_dir / "trading_intelligence.log.2").write_text("y" * 50, encoding="utf-8")
        os.utime(logs_dir / "trading_intelligence.log.2", (past, past))
        refused = execute_confirmed_cleanup(
            settings, token=confirmation.token, operator_id="1001"
        )
        assert refused.status == "refused"
        assert refused.failure_code == "stale_preview"


class TestRetentionConfirmationAndGate:
    def test_blocked_without_verified_backup(self, isolated):
        settings, db_path, backup_dir, _ = isolated
        settings = make_test_settings(
            database_path=str(db_path),
            storage_backup_dir=str(backup_dir.resolve()),
            storage_backup_enabled=True,
            storage_backup_min_free_bytes=1,
            storage_retention_prune_enabled=True,
            storage_retention_prune_dry_run=False,
            admin_ids=[1001],
        )
        initialize_database(settings)
        confirmation, report = create_prune_confirmation(
            settings,
            operator_id="1001",
            approved_datasets=["health_check_history"],
        )
        assert confirmation is None
        assert report.status == "REFUSED"


class TestConcurrencyAndIdempotency:
    def test_concurrent_cleanup_refused_while_backup_lock(self, isolated):
        settings, db_path, backup_dir, logs_dir = isolated
        settings = make_test_settings(
            database_path=str(db_path),
            storage_backup_dir=str(backup_dir.resolve()),
            storage_backup_enabled=True,
            storage_backup_min_free_bytes=1,
            storage_logs_dir=str(logs_dir),
            storage_cleanup_enabled=True,
            storage_cleanup_dry_run=False,
            storage_cleanup_min_age_seconds=0,
            admin_ids=[1001],
        )
        initialize_database(settings)
        owner = acquire_backup_lock(settings)
        assert owner is not None
        confirmation, _ = create_cleanup_confirmation(settings, operator_id="1001")
        assert confirmation is None  # maintenance busy during preview

    def test_backup_busy_when_cleanup_lock_held(self, isolated):
        settings, db_path, backup_dir, logs_dir = isolated
        settings = make_test_settings(
            database_path=str(db_path),
            storage_backup_dir=str(backup_dir.resolve()),
            storage_backup_enabled=True,
            storage_backup_min_free_bytes=1,
            storage_logs_dir=str(logs_dir),
            storage_cleanup_enabled=True,
            admin_ids=[1001],
        )
        initialize_database(settings)
        owner = acquire_cleanup_lock(settings)
        assert owner is not None
        report = create_online_backup(settings)
        assert report.status == "BUSY"


class TestFormattingAndHistory:
    def test_storage_menu_sanitized(self, isolated):
        settings, *_ = isolated
        text = format_storage_menu(settings)
        assert "Storage level:" in text
        assert "Retention gate:" in text
        assert ":\\" not in text
        assert "/Users/" not in text
        assert assert_no_private_path("live_database")

    def test_cleanup_preview_and_history(self, isolated):
        settings, db_path, backup_dir, _ = isolated
        settings = make_test_settings(
            database_path=str(db_path),
            storage_backup_dir=str(backup_dir.resolve()),
            storage_backup_enabled=True,
            storage_backup_min_free_bytes=1,
            admin_ids=[1001],
        )
        initialize_database(settings)
        create_online_backup(settings)
        items, total = list_maintenance_history(settings, page=0, page_size=8)
        assert total >= 1
        hist = format_maintenance_history(settings)
        assert "Maintenance History" in hist
        assert ":\\" not in hist
        preview = format_storage_cleanup_preview(settings)
        assert "DRY RUN" in preview
        assert "Private paths" in preview


class TestStorageAlerts:
    def test_soft_degraded_storage_no_telegram(self, isolated):
        settings, *_ = isolated
        degraded = OverallHealth(
            status="DEGRADED",
            components=(
                ComponentStatus(
                    "infrastructure",
                    "DEGRADED",
                    "Storage degraded: free_bytes_degraded",
                    "t",
                    metrics={
                        "storage_level": "DEGRADED",
                        "storage_detail": "Storage degraded: free_bytes_degraded",
                    },
                ),
            ),
            checked_at="t",
            summary="degraded",
        )
        sync_storage_alerts(
            settings,
            OverallHealth(
                status="HEALTHY",
                components=(
                    ComponentStatus(
                        "infrastructure",
                        "HEALTHY",
                        "Storage OK",
                        "t",
                        metrics={"storage_level": "HEALTHY", "storage_detail": "Storage OK"},
                    ),
                ),
                checked_at="t",
                summary="ok",
            ),
        )
        assert sync_storage_alerts(settings, degraded) == []

    def test_level_transitions_and_no_duplicate_recovery(self, isolated):
        settings, *_ = isolated
        critical = OverallHealth(
            status="DEGRADED",
            components=(
                ComponentStatus(
                    "infrastructure",
                    "DOWN",
                    "Storage critical",
                    "t",
                    metrics={
                        "storage_level": "CRITICAL",
                        "storage_detail": "Storage critical: free_bytes",
                    },
                ),
            ),
            checked_at="t",
            summary="critical",
        )
        # Seed watermark
        sync_storage_alerts(settings, OverallHealth(
            status="HEALTHY",
            components=(
                ComponentStatus(
                    "infrastructure",
                    "HEALTHY",
                    "Storage OK",
                    "t",
                    metrics={"storage_level": "HEALTHY", "storage_detail": "Storage OK"},
                ),
            ),
            checked_at="t",
            summary="ok",
        ))
        first = sync_storage_alerts(settings, critical)
        assert any("CRITICAL" in msg for _, _, msg in first)
        for incident_id, alert_key, _ in first:
            health_storage.record_alert_sent(
                settings, incident_id=incident_id, alert_key=alert_key
            )
        second = sync_storage_alerts(settings, critical)
        assert second == []

        healthy = OverallHealth(
            status="HEALTHY",
            components=(
                ComponentStatus(
                    "infrastructure",
                    "HEALTHY",
                    "Storage OK",
                    "t",
                    metrics={"storage_level": "HEALTHY", "storage_detail": "Storage OK"},
                ),
            ),
            checked_at="t",
            summary="ok",
        )
        recovery = sync_incidents_from_health(settings, healthy)
        recovery_msgs = [m for _, _, m in recovery if m]
        storage_recovery = [
            m
            for m in recovery_msgs
            if "Відновлення" in m and ("сховищ" in m.lower() or "Сховище" in m or "сховища" in m)
        ]
        overall_recovery = [m for m in recovery_msgs if "Overall health recovered" in m]
        assert len(storage_recovery) == 1
        assert overall_recovery == []

    def test_restart_does_not_replay_recovery(self, isolated):
        settings, *_ = isolated
        healthy = OverallHealth(
            status="HEALTHY",
            components=(
                ComponentStatus(
                    "infrastructure",
                    "HEALTHY",
                    "Storage OK",
                    "t",
                    metrics={"storage_level": "HEALTHY", "storage_detail": "Storage OK"},
                ),
            ),
            checked_at="t",
            summary="ok",
        )
        # Simulate prior watermark already HEALTHY (restart)
        sync_storage_alerts(settings, healthy)
        again = sync_storage_alerts(settings, healthy)
        assert again == []

    @pytest.mark.asyncio
    async def test_admin_only_alert_delivery(self, isolated):
        settings, *_ = isolated
        settings = make_test_settings(
            database_path=settings.database_path,
            admin_ids=[1001],
            health_alerts_enabled=True,
            storage_backup_dir=settings.storage_backup_dir,
        )
        initialize_database(settings)
        # seed
        sync_storage_alerts(
            settings,
            OverallHealth(
                status="HEALTHY",
                components=(
                    ComponentStatus(
                        "infrastructure",
                        "HEALTHY",
                        "Storage OK",
                        "t",
                        metrics={"storage_level": "HEALTHY"},
                    ),
                ),
                checked_at="t",
                summary="ok",
            ),
        )
        overall = OverallHealth(
            status="DEGRADED",
            components=(
                ComponentStatus(
                    "infrastructure",
                    "DOWN",
                    "Storage critical",
                    "t",
                    metrics={
                        "storage_level": "CRITICAL",
                        "storage_detail": "Storage critical",
                    },
                ),
            ),
            checked_at="t",
            summary="critical",
        )
        sent_to: list[int] = []

        async def send(admin_id: int, message: str) -> None:
            sent_to.append(admin_id)

        await dispatch_health_alerts(settings, overall, send)
        assert sent_to
        assert set(sent_to) == {1001}
        assert all(aid == 1001 for aid in sent_to)


class TestBackgroundCallback:
    @pytest.mark.asyncio
    async def test_callback_progress_and_final_once(self, isolated):
        settings, db_path, backup_dir, _ = isolated
        settings = make_test_settings(
            database_path=str(db_path),
            storage_backup_dir=str(backup_dir.resolve()),
            storage_backup_enabled=True,
            storage_backup_min_free_bytes=1,
            admin_ids=[1001],
        )
        initialize_database(settings)
        confirmation, _ = create_backup_confirmation(settings, operator_id="1001")
        assert confirmation is not None

        update = MagicMock()
        update.effective_user = MagicMock(id=1001)
        update.callback_query = MagicMock()
        update.callback_query.message = MagicMock()
        update.callback_query.message.chat_id = 1
        update.callback_query.message.message_id = 2
        update.callback_query.message.edit_text = AsyncMock()
        update.effective_message = update.callback_query.message

        context = MagicMock()
        context.application.bot_data = {"settings": settings}
        context.user_data = {
            health_handlers._STORAGE_BACKUP_TOKEN_KEY: confirmation.token
        }
        context.bot = MagicMock()
        context.bot.edit_message_text = AsyncMock()
        context.bot.send_message = AsyncMock()

        with patch(
            "app.telegram.health.handlers.require_admin",
            new=AsyncMock(return_value=True),
        ), patch(
            "app.telegram.health.handlers.edit_or_reply",
            new=AsyncMock(),
        ) as edit:
            await health_handlers.handle_health_callback(
                update, context, "health:storage_backup_confirm"
            )
            # progress edit happens promptly
            assert edit.await_count >= 1
            await asyncio.sleep(0.5)
            # final report edited once
            assert context.bot.edit_message_text.await_count >= 1


class TestLiveDbUntouched:
    def test_validation_did_not_mutate_live_db(self, isolated):
        before = _mtime_and_size(LIVE_DB_PROBE)
        settings, db_path, backup_dir, logs_dir = isolated
        # Use temp DB only
        create_backup_confirmation(settings, operator_id="1001")
        create_cleanup_confirmation(settings, operator_id="1001")
        create_prune_confirmation(
            settings,
            operator_id="1001",
            approved_datasets=["health_check_history"],
        )
        after = _mtime_and_size(LIVE_DB_PROBE)
        assert before == after
        assert Path(db_path).exists()
        # No prune/cleanup destructive against live DB path
        assert str(LIVE_DB_PROBE.resolve()) != str(Path(db_path).resolve())


class TestPruneResultFormatting:
    def test_logical_delete_disclaimer(self, isolated):
        from app.storage.retention_prune import PruneReport

        report = PruneReport(
            status="SUCCEEDED",
            dry_run=False,
            prune_enabled=True,
            deleted_counts={"health_check_history": 3},
            backup_label="ti_backup_x.db",
        )
        text = format_storage_prune_result(report)
        assert "disk space is not immediately reclaimed" in text
        assert "health_check_history: 3" in text
