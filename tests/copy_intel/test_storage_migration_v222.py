"""V2.2.2 Checkpoint 3 — additive storage migration and V3 baseline foundations.

All databases here are temporary pytest paths only. Never open production/runtime DBs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.copy_intel.freshness import COPY_BASELINE_VERSION
from app.database import copy_storage
from app.database.copy_storage import (
    MIGRATION_ID_POSITION_SNAPSHOTS_V2,
    CopySchemaMigrationError,
    get_position_snapshots_v2_migration_status,
    is_position_snapshots_v2_migration_applied,
    list_snapshot_migration_quarantine,
    load_position_snapshots_v2,
    migrate_legacy_position_snapshots_to_v2,
    open_paper_position,
    replace_wallet_snapshots_v2,
    upsert_position_snapshot_v2,
)
from app.database.storage import get_connection, initialize_database
from tests.conftest import make_test_settings

WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ASSET_YES = "token-yes-111"
ASSET_NO = "token-no-222"
CONDITION = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _assert_temp_db(settings) -> None:
    path = str(settings.database_path)
    assert path == ":memory:" or "pytest" in path.lower() or "tmp" in path.lower() or "test" in path.lower()
    assert "production" not in path.lower()
    # Guard against accidental real runtime DB filenames.
    assert not path.endswith("trading_intelligence.db")
    assert "/data/" not in path.replace("\\", "/").lower() or "tmp" in path.lower() or "pytest" in path.lower()


def _legacy_only_schema(db_path: Path) -> None:
    """Create a pre-V2.2.2 copy schema with legacy snapshots only."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            CREATE TABLE copy_position_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                asset TEXT,
                outcome TEXT,
                size REAL NOT NULL DEFAULT 0,
                avg_price REAL,
                cur_price REAL,
                title TEXT,
                slug TEXT,
                event_slug TEXT,
                snapshot_at TEXT NOT NULL,
                UNIQUE(wallet, condition_id)
            );
            CREATE TABLE wallet_monitor_state (
                wallet TEXT PRIMARY KEY,
                subscription_started_at TEXT NOT NULL,
                baseline_started_at TEXT,
                baseline_status TEXT NOT NULL DEFAULT 'PENDING',
                baseline_error TEXT,
                newest_event_ts INTEGER,
                newest_event_uid TEXT,
                monitoring_enabled INTEGER NOT NULL DEFAULT 1,
                last_polled_at TEXT,
                baseline_version INTEGER,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE copy_paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                market_slug TEXT,
                outcome TEXT,
                notional REAL NOT NULL,
                entry_price REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                opened_at TEXT NOT NULL,
                closed_at TEXT
            );
            CREATE TABLE processed_copy_events (
                event_uid TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                event_ts INTEGER,
                observed_at INTEGER,
                is_baseline INTEGER NOT NULL DEFAULT 0,
                alertable INTEGER NOT NULL DEFAULT 0,
                reject_reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE copy_alert_deliveries (
                subscriber_id INTEGER NOT NULL,
                wallet TEXT NOT NULL,
                event_uid TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (subscriber_id, wallet, event_uid)
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def _insert_legacy_snapshot(
    db_path: Path,
    *,
    wallet: str,
    condition_id: str,
    asset: str | None,
    size: float = 10.0,
    outcome: str = "Yes",
) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO copy_position_snapshots (
                wallet, condition_id, asset, outcome, size, avg_price, cur_price,
                title, slug, event_slug, snapshot_at
            )
            VALUES (?, ?, ?, ?, ?, 0.4, 0.5, 't', 's', 'e', ?)
            """,
            (wallet.lower(), condition_id, asset, outcome, size, "2026-07-22T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()


def _legacy_rows(db_path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute("SELECT * FROM copy_position_snapshots ORDER BY id").fetchall())
    finally:
        connection.close()


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(database_path=str(tmp_path / "cp3_storage.db"))
    _assert_temp_db(s)
    return s


class TestFreshDatabaseInitialization:
    def test_fresh_db_creates_v2_tables_and_migration_marker(self, settings):
        initialize_database(settings)
        _assert_temp_db(settings)
        with get_connection(settings) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "copy_position_snapshots" in tables
            assert "copy_position_snapshots_v2" in tables
            assert "copy_position_snapshots_migration_quarantine" in tables
            assert "copy_schema_migrations" in tables
            cols_monitor = {
                row[1] for row in conn.execute("PRAGMA table_info(wallet_monitor_state)")
            }
            assert "last_complete_positions_at" in cols_monitor
            cols_processed = {
                row[1] for row in conn.execute("PRAGMA table_info(processed_copy_events)")
            }
            assert "diagnostic_json" in cols_processed
            cols_paper = {
                row[1] for row in conn.execute("PRAGMA table_info(copy_paper_positions)")
            }
            assert "asset" in cols_paper
            assert "source_event_uid" in cols_paper
            cols_delivery = {
                row[1] for row in conn.execute("PRAGMA table_info(copy_alert_deliveries)")
            }
            assert {"attempt_count", "next_attempt_at", "last_error_code"} <= cols_delivery
            idx = conn.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type='index' AND name='idx_copy_paper_positions_source_event_uid'
                """
            ).fetchone()
            assert idx is not None
            assert "source_event_uid" in (idx[0] or "")
            assert "WHERE" in (idx[0] or "").upper()
        assert is_position_snapshots_v2_migration_applied(settings) is True
        status = get_position_snapshots_v2_migration_status(settings)
        assert status is not None
        assert status["migration_id"] == MIGRATION_ID_POSITION_SNAPSHOTS_V2


class TestExistingDatabaseMigration:
    def test_existing_legacy_db_migrates_additive_schema(self, tmp_path):
        db_path = tmp_path / "legacy_existing.db"
        _legacy_only_schema(db_path)
        _insert_legacy_snapshot(
            db_path,
            wallet=WALLET,
            condition_id=CONDITION,
            asset=ASSET_YES,
            size=12.5,
        )
        settings = make_test_settings(database_path=str(db_path))
        _assert_temp_db(settings)
        before = _legacy_rows(db_path)
        assert len(before) == 1

        initialize_database(settings)

        after = _legacy_rows(db_path)
        assert len(after) == 1
        assert dict(after[0]) == dict(before[0])
        assert is_position_snapshots_v2_migration_applied(settings) is True
        v2 = load_position_snapshots_v2(settings, WALLET)
        assert set(v2) == {ASSET_YES}
        assert float(v2[ASSET_YES]["size"]) == 12.5


class TestValidLegacyRowMigration:
    def test_valid_legacy_row_migrates_to_v2(self, tmp_path):
        db_path = tmp_path / "valid_legacy.db"
        _legacy_only_schema(db_path)
        _insert_legacy_snapshot(
            db_path,
            wallet=WALLET,
            condition_id="cond-1",
            asset=ASSET_YES,
            size=7.0,
            outcome="Yes",
        )
        settings = make_test_settings(database_path=str(db_path))
        copy_storage.initialize_copy_schema(settings)
        status = get_position_snapshots_v2_migration_status(settings)
        assert status is not None
        assert is_position_snapshots_v2_migration_applied(settings) is True
        migrated = migrate_legacy_position_snapshots_to_v2(settings)
        assert migrated.already_applied is True
        assert migrated.migrated == 1
        assert migrated.quarantined == 0
        v2 = load_position_snapshots_v2(settings, WALLET)
        assert ASSET_YES in v2
        assert v2[ASSET_YES]["condition_id"] == "cond-1"
        assert v2[ASSET_YES]["outcome"] == "Yes"
        assert float(v2[ASSET_YES]["size"]) == 7.0


class TestTwoAssetsOneCondition:
    def test_two_assets_under_one_condition_migrate_separately(self, tmp_path):
        db_path = tmp_path / "two_assets.db"
        settings = make_test_settings(database_path=str(db_path))
        initialize_database(settings)
        # Direct v2 helper coverage: same condition_id, two assets.
        upsert_position_snapshot_v2(
            settings,
            WALLET,
            asset=ASSET_YES,
            condition_id=CONDITION,
            outcome="Yes",
            size=10.0,
            avg_price=0.4,
            cur_price=0.41,
            title="m",
            slug="m",
            event_slug="m",
        )
        upsert_position_snapshot_v2(
            settings,
            WALLET,
            asset=ASSET_NO,
            condition_id=CONDITION,
            outcome="No",
            size=5.0,
            avg_price=0.6,
            cur_price=0.59,
            title="m",
            slug="m",
            event_slug="m",
        )
        loaded = load_position_snapshots_v2(settings, WALLET)
        assert set(loaded) == {ASSET_YES, ASSET_NO}
        assert loaded[ASSET_YES]["condition_id"] == CONDITION
        assert loaded[ASSET_NO]["condition_id"] == CONDITION
        assert float(loaded[ASSET_YES]["size"]) == 10.0
        assert float(loaded[ASSET_NO]["size"]) == 5.0


class TestMissingAssetQuarantine:
    def test_missing_asset_goes_to_quarantine(self, tmp_path):
        db_path = tmp_path / "missing_asset.db"
        _legacy_only_schema(db_path)
        _insert_legacy_snapshot(
            db_path,
            wallet=WALLET,
            condition_id=CONDITION,
            asset=None,
            size=3.0,
        )
        _insert_legacy_snapshot(
            db_path,
            wallet=WALLET,
            condition_id="cond-empty-asset",
            asset="   ",
            size=4.0,
        )
        settings = make_test_settings(database_path=str(db_path))
        initialize_database(settings)
        status = get_position_snapshots_v2_migration_status(settings)
        assert status is not None
        quarantine = list_snapshot_migration_quarantine(settings, wallet=WALLET)
        assert len(quarantine) == 2
        assert all(row["reason"] == "missing_or_invalid_asset" for row in quarantine)
        assert load_position_snapshots_v2(settings, WALLET) == {}
        # Legacy preserved.
        assert len(_legacy_rows(db_path)) == 2


def _table_names(settings) -> set[str]:
    with get_connection(settings) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }


def _assert_single_tx_migration_fully_rolled_back(settings) -> None:
    """After a failed single-transaction attempt, additive schema must not remain."""
    assert is_position_snapshots_v2_migration_applied(settings) is False
    assert get_position_snapshots_v2_migration_status(settings) is None
    tables = _table_names(settings)
    assert "copy_position_snapshots_v2" not in tables
    assert "copy_position_snapshots_migration_quarantine" not in tables
    assert "copy_schema_migrations" not in tables


class TestDuplicateWalletAssetRollback:
    def test_duplicate_wallet_asset_rolls_back_entire_migration(self, tmp_path):
        db_path = tmp_path / "dup_asset.db"
        _legacy_only_schema(db_path)
        # Two condition_ids, same asset — cannot both map into PRIMARY KEY (wallet, asset).
        _insert_legacy_snapshot(
            db_path,
            wallet=WALLET,
            condition_id="cond-a",
            asset=ASSET_YES,
            size=1.0,
        )
        _insert_legacy_snapshot(
            db_path,
            wallet=WALLET,
            condition_id="cond-b",
            asset=ASSET_YES,
            size=2.0,
        )
        settings = make_test_settings(database_path=str(db_path))
        # Single transaction: additive schema + data roll back together on failure.
        with pytest.raises(CopySchemaMigrationError, match="duplicate"):
            initialize_database(settings)

        _assert_single_tx_migration_fully_rolled_back(settings)
        # Legacy untouched.
        legacy = _legacy_rows(db_path)
        assert len(legacy) == 2
        assert {row["condition_id"] for row in legacy} == {"cond-a", "cond-b"}


class TestMigrationRestartIdempotent:
    def test_migration_restart_is_idempotent(self, tmp_path):
        db_path = tmp_path / "idempotent.db"
        _legacy_only_schema(db_path)
        _insert_legacy_snapshot(
            db_path,
            wallet=WALLET,
            condition_id=CONDITION,
            asset=ASSET_YES,
            size=9.0,
        )
        settings = make_test_settings(database_path=str(db_path))
        initialize_database(settings)
        first = migrate_legacy_position_snapshots_to_v2(settings)
        second = migrate_legacy_position_snapshots_to_v2(settings)
        assert first.already_applied is True
        assert second.already_applied is True
        assert first.migrated == second.migrated == 1
        assert first.quarantined == second.quarantined == 0
        assert len(load_position_snapshots_v2(settings, WALLET)) == 1
        assert len(_legacy_rows(db_path)) == 1


class TestLegacyTablePreserved:
    def test_legacy_table_present_and_unchanged_after_migration(self, tmp_path):
        db_path = tmp_path / "legacy_preserved.db"
        _legacy_only_schema(db_path)
        _insert_legacy_snapshot(
            db_path,
            wallet=WALLET,
            condition_id=CONDITION,
            asset=ASSET_YES,
            size=11.0,
        )
        before = [dict(row) for row in _legacy_rows(db_path)]
        settings = make_test_settings(database_path=str(db_path))
        initialize_database(settings)
        after = [dict(row) for row in _legacy_rows(db_path)]
        assert before == after
        with get_connection(settings) as conn:
            assert (
                conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='copy_position_snapshots'
                    """
                ).fetchone()
                is not None
            )


class TestMigrationMarkerAfterSuccessOnly:
    def test_marker_written_only_after_success(self, tmp_path):
        db_path = tmp_path / "marker_success.db"
        _legacy_only_schema(db_path)
        _insert_legacy_snapshot(
            db_path,
            wallet=WALLET,
            condition_id=CONDITION,
            asset=ASSET_YES,
        )
        settings = make_test_settings(database_path=str(db_path))
        assert is_position_snapshots_v2_migration_applied(settings) is False
        result = migrate_legacy_position_snapshots_to_v2(settings)
        assert result.already_applied is False
        assert result.migrated == 1
        assert is_position_snapshots_v2_migration_applied(settings) is True


class TestSingleTransactionFailureAndRestart:
    """Checkpoint 3 correction — single-tx failure, rollback, and idempotent restart."""

    FAILPOINTS = (
        "during_legacy_row_migration",
        "during_quarantine_insertion",
        "during_validation",
        "before_marker_write",
    )

    def _legacy_for_failpoint(self, db_path: Path, failpoint: str) -> list[dict]:
        if failpoint == "during_quarantine_insertion":
            _insert_legacy_snapshot(
                db_path,
                wallet=WALLET,
                condition_id=CONDITION,
                asset=None,
                size=3.0,
            )
            _insert_legacy_snapshot(
                db_path,
                wallet=WALLET,
                condition_id="cond-valid",
                asset=ASSET_YES,
                size=4.0,
            )
        else:
            _insert_legacy_snapshot(
                db_path,
                wallet=WALLET,
                condition_id=CONDITION,
                asset=ASSET_YES,
                size=8.0,
            )
            if failpoint == "during_legacy_row_migration":
                _insert_legacy_snapshot(
                    db_path,
                    wallet=WALLET,
                    condition_id="cond-2",
                    asset=ASSET_NO,
                    size=2.0,
                )
        return [dict(row) for row in _legacy_rows(db_path)]

    def _install_failpoint(self, monkeypatch, point: str):
        def hook(seen: str) -> None:
            if seen == point:
                raise RuntimeError(f"injected failure at {point}")

        monkeypatch.setattr(copy_storage, "_MIGRATION_FAILPOINT_HOOK", hook)

    @pytest.mark.parametrize("failpoint", FAILPOINTS)
    def test_failure_rolls_back_schema_data_and_marker(
        self, tmp_path, monkeypatch, failpoint
    ):
        db_path = tmp_path / f"fail_{failpoint}.db"
        _legacy_only_schema(db_path)
        before = self._legacy_for_failpoint(db_path, failpoint)
        settings = make_test_settings(database_path=str(db_path))
        self._install_failpoint(monkeypatch, failpoint)

        with pytest.raises(RuntimeError, match=failpoint):
            initialize_database(settings)

        _assert_single_tx_migration_fully_rolled_back(settings)
        assert [dict(row) for row in _legacy_rows(db_path)] == before

    @pytest.mark.parametrize("failpoint", FAILPOINTS)
    def test_restart_after_failure_succeeds_idempotently(
        self, tmp_path, monkeypatch, failpoint
    ):
        db_path = tmp_path / f"restart_{failpoint}.db"
        _legacy_only_schema(db_path)
        before = self._legacy_for_failpoint(db_path, failpoint)
        settings = make_test_settings(database_path=str(db_path))
        self._install_failpoint(monkeypatch, failpoint)

        with pytest.raises(RuntimeError, match=failpoint):
            initialize_database(settings)
        _assert_single_tx_migration_fully_rolled_back(settings)

        monkeypatch.setattr(copy_storage, "_MIGRATION_FAILPOINT_HOOK", None)
        initialize_database(settings)

        assert is_position_snapshots_v2_migration_applied(settings) is True
        assert [dict(row) for row in _legacy_rows(db_path)] == before
        retry = migrate_legacy_position_snapshots_to_v2(settings)
        assert retry.already_applied is True
        if failpoint == "during_quarantine_insertion":
            assert retry.migrated == 1
            assert retry.quarantined == 1
            assert ASSET_YES in load_position_snapshots_v2(settings, WALLET)
            assert len(list_snapshot_migration_quarantine(settings, wallet=WALLET)) == 1
        elif failpoint == "during_legacy_row_migration":
            assert retry.migrated == 2
            assert retry.quarantined == 0
            assert set(load_position_snapshots_v2(settings, WALLET)) == {
                ASSET_YES,
                ASSET_NO,
            }
        else:
            assert retry.migrated == 1
            assert retry.quarantined == 0
            assert set(load_position_snapshots_v2(settings, WALLET)) == {ASSET_YES}

    def test_no_partial_migrated_or_quarantine_after_failure(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "no_partial.db"
        _legacy_only_schema(db_path)
        before = self._legacy_for_failpoint(db_path, "before_marker_write")
        settings = make_test_settings(database_path=str(db_path))
        self._install_failpoint(monkeypatch, "before_marker_write")
        with pytest.raises(RuntimeError, match="before_marker_write"):
            copy_storage.initialize_copy_schema(settings)
        _assert_single_tx_migration_fully_rolled_back(settings)
        assert [dict(row) for row in _legacy_rows(db_path)] == before

    def test_schema_first_leftover_without_marker_is_resumable(
        self, tmp_path, monkeypatch
    ):
        """Pre-correction leftover: additive tables exist, no marker, dirty v2 row."""
        db_path = tmp_path / "leftover_schema.db"
        _legacy_only_schema(db_path)
        _insert_legacy_snapshot(
            db_path,
            wallet=WALLET,
            condition_id=CONDITION,
            asset=ASSET_YES,
            size=5.0,
        )
        before = [dict(row) for row in _legacy_rows(db_path)]
        settings = make_test_settings(database_path=str(db_path))

        # Simulate committed additive schema without migration marker (old design).
        with get_connection(settings) as conn:
            copy_storage._ensure_v222_additive_schema(conn)
            conn.execute(
                """
                INSERT INTO copy_position_snapshots_v2 (
                    wallet, asset, condition_id, outcome, size, avg_price, cur_price,
                    title, slug, event_slug, snapshot_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?)
                """,
                (WALLET, "stale-asset", CONDITION, "Yes", 99.0, "stale"),
            )

        assert is_position_snapshots_v2_migration_applied(settings) is False
        self._install_failpoint(monkeypatch, "before_marker_write")
        with pytest.raises(RuntimeError, match="before_marker_write"):
            migrate_legacy_position_snapshots_to_v2(settings)

        # Leftover schema remains (was outside this attempt); no marker; legacy intact.
        assert is_position_snapshots_v2_migration_applied(settings) is False
        assert "copy_position_snapshots_v2" in _table_names(settings)
        # Failed attempt rolled back, restoring the prior stale row (pre-tx state).
        with get_connection(settings) as conn:
            stale = conn.execute(
                "SELECT asset, size FROM copy_position_snapshots_v2"
            ).fetchall()
        assert len(stale) == 1
        assert stale[0]["asset"] == "stale-asset"
        assert [dict(row) for row in _legacy_rows(db_path)] == before

        monkeypatch.setattr(copy_storage, "_MIGRATION_FAILPOINT_HOOK", None)
        result = migrate_legacy_position_snapshots_to_v2(settings)
        assert result.already_applied is False
        assert result.migrated == 1
        assert is_position_snapshots_v2_migration_applied(settings) is True
        v2 = load_position_snapshots_v2(settings, WALLET)
        assert set(v2) == {ASSET_YES}
        assert float(v2[ASSET_YES]["size"]) == 5.0
        assert [dict(row) for row in _legacy_rows(db_path)] == before


class TestIntegrityValidation:
    def test_foreign_key_and_integrity_checks_on_success(self, tmp_path, monkeypatch):
        db_path = tmp_path / "integrity_ok.db"
        _legacy_only_schema(db_path)
        _insert_legacy_snapshot(
            db_path,
            wallet=WALLET,
            condition_id=CONDITION,
            asset=ASSET_YES,
        )
        settings = make_test_settings(database_path=str(db_path))
        seen = {"fk": 0, "integrity": 0}

        from contextlib import contextmanager
        from app.database import storage as storage_mod

        @contextmanager
        def tracking_connection(settings_arg):
            with storage_mod.get_connection(settings_arg) as conn:
                original_execute = conn.execute

                def execute(sql, *a, **k):
                    text = str(sql).lower()
                    if "pragma foreign_key_check" in text:
                        seen["fk"] += 1
                    if "pragma integrity_check" in text:
                        seen["integrity"] += 1
                    return original_execute(sql, *a, **k)

                class _Proxy:
                    def __init__(self, inner):
                        self._inner = inner

                    def execute(self, sql, *a, **k):
                        return execute(sql, *a, **k)

                    def __getattr__(self, name):
                        return getattr(self._inner, name)

                yield _Proxy(conn)

        monkeypatch.setattr(copy_storage, "get_connection", tracking_connection)
        initialize_database(settings)
        assert seen["fk"] >= 1
        assert seen["integrity"] >= 1
        assert is_position_snapshots_v2_migration_applied(settings) is True

        with get_connection(settings) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


class TestPaperSourceEventUid:
    def test_source_event_uid_unique_for_non_null(self, settings):
        initialize_database(settings)
        assert (
            open_paper_position(
                settings,
                wallet=WALLET,
                condition_id=CONDITION,
                market_slug="m",
                outcome="Yes",
                notional=10.0,
                entry_price=0.4,
                asset=ASSET_YES,
                source_event_uid="uid-1",
            )
            is True
        )
        assert (
            open_paper_position(
                settings,
                wallet=WALLET,
                condition_id=CONDITION,
                market_slug="m",
                outcome="Yes",
                notional=11.0,
                entry_price=0.41,
                asset=ASSET_YES,
                source_event_uid="uid-1",
            )
            is False
        )
        with get_connection(settings) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM copy_paper_positions WHERE source_event_uid = ?",
                ("uid-1",),
            ).fetchone()["c"]
        assert count == 1

    def test_null_source_event_uid_backward_compatible(self, settings):
        initialize_database(settings)
        assert (
            open_paper_position(
                settings,
                wallet=WALLET,
                condition_id=CONDITION,
                market_slug="m",
                outcome="Yes",
                notional=10.0,
                entry_price=0.4,
                source_event_uid=None,
            )
            is True
        )
        assert (
            open_paper_position(
                settings,
                wallet=WALLET,
                condition_id="cond-2",
                market_slug="m2",
                outcome="No",
                notional=12.0,
                entry_price=0.5,
                source_event_uid=None,
            )
            is True
        )
        with get_connection(settings) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM copy_paper_positions WHERE source_event_uid IS NULL"
            ).fetchone()["c"]
        assert count == 2


class TestBaselineVersionAndHelpers:
    def test_baseline_version_is_three(self):
        assert COPY_BASELINE_VERSION == 3

    def test_replace_wallet_snapshots_v2_helper(self, settings):
        initialize_database(settings)
        replace_wallet_snapshots_v2(
            settings,
            WALLET,
            {
                ASSET_YES: {
                    "condition_id": CONDITION,
                    "outcome": "Yes",
                    "size": 1.5,
                    "avg_price": 0.2,
                    "cur_price": 0.21,
                    "title": "t",
                    "slug": "s",
                    "event_slug": "e",
                }
            },
        )
        loaded = load_position_snapshots_v2(settings, WALLET)
        assert set(loaded) == {ASSET_YES}
        assert float(loaded[ASSET_YES]["size"]) == 1.5


class TestNoProductionDbTouch:
    def test_all_settings_use_temp_paths(self, settings, tmp_path):
        _assert_temp_db(settings)
        other = make_test_settings(database_path=str(tmp_path / "another_temp.db"))
        _assert_temp_db(other)
        initialize_database(other)
        # Production ingest path still targets legacy table only.
        copy_storage.upsert_position_snapshot(
            other,
            WALLET,
            CONDITION,
            asset=ASSET_YES,
            outcome="Yes",
            size=1.0,
            avg_price=0.1,
            cur_price=0.1,
            title="t",
            slug="s",
            event_slug="e",
        )
        with get_connection(other) as conn:
            legacy = conn.execute(
                "SELECT COUNT(*) AS c FROM copy_position_snapshots"
            ).fetchone()["c"]
            # Fresh init migrated empty legacy first; then we inserted one legacy row.
            # Runtime pipeline must not auto-write v2.
            v2 = conn.execute(
                "SELECT COUNT(*) AS c FROM copy_position_snapshots_v2"
            ).fetchone()["c"]
        assert legacy == 1
        assert v2 == 0
