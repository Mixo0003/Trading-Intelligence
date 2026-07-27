"""Additive opportunity_intel_v2_resumable_discovery migration proofs."""

from __future__ import annotations

import sqlite3

import pytest

from app.database import opportunity_storage
from app.database.opportunity_storage import (
    MIGRATION_ID,
    MIGRATION_ID_RESUMABLE_DISCOVERY,
    OpportunityMigrationError,
    apply_resumable_discovery_migration,
    initialize_opportunity_schema,
    is_migration_applied,
    is_resumable_discovery_migration_applied,
)
from app.database.storage import get_connection, initialize_database
from app.storage.retention_policies import build_retention_policies
from tests.conftest import make_test_settings


def _marker_rows(settings):
    with get_connection(settings) as connection:
        return connection.execute(
            """
            SELECT migration_id, schema_version
            FROM opportunity_schema_migrations
            ORDER BY migration_id
            """
        ).fetchall()


def _has_discovery_table(settings) -> bool:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'opportunity_discovery_sessions'
            """
        ).fetchone()
    return row is not None


def _legacy_count(settings) -> int:
    with get_connection(settings) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM opportunity_signals").fetchone()[0]
        )


def test_fresh_db_applies_v1_then_v2(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "fresh.db"))
    initialize_database(settings)
    assert is_migration_applied(settings)
    assert is_resumable_discovery_migration_applied(settings)
    markers = _marker_rows(settings)
    assert [row[0] for row in markers] == [
        MIGRATION_ID,
        MIGRATION_ID_RESUMABLE_DISCOVERY,
    ]
    assert _has_discovery_table(settings)


def test_existing_v1_only_db_applies_v2(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "v1_only.db"))
    # Apply v1 only (bypass initialize_opportunity_schema's v2 call).
    with get_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        opportunity_storage._initialize_on_connection(connection)
    assert is_migration_applied(settings)
    assert not is_resumable_discovery_migration_applied(settings)
    assert not _has_discovery_table(settings)

    with get_connection(settings) as connection:
        connection.execute(
            "CREATE TABLE legacy_business_data (id INTEGER PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO legacy_business_data(id, value) VALUES (1, 'preserve-me')"
        )

    result = apply_resumable_discovery_migration(settings)
    assert result.already_applied is False
    assert result.migration_id == MIGRATION_ID_RESUMABLE_DISCOVERY
    assert is_resumable_discovery_migration_applied(settings)
    assert _has_discovery_table(settings)
    markers = _marker_rows(settings)
    assert sum(1 for row in markers if row[0] == MIGRATION_ID) == 1
    with get_connection(settings) as connection:
        assert (
            connection.execute(
                "SELECT value FROM legacy_business_data WHERE id = 1"
            ).fetchone()[0]
            == "preserve-me"
        )


def test_v2_idempotency(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "idem.db"))
    initialize_database(settings)
    first = apply_resumable_discovery_migration(settings)
    second = apply_resumable_discovery_migration(settings)
    assert first.already_applied is True
    assert second.already_applied is True
    markers = _marker_rows(settings)
    assert sum(1 for row in markers if row[0] == MIGRATION_ID_RESUMABLE_DISCOVERY) == 1
    assert sum(1 for row in markers if row[0] == MIGRATION_ID) == 1


def test_v2_rollback_does_not_damage_v1(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "rollback.db"))
    with get_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        opportunity_storage._initialize_on_connection(connection)
    assert is_migration_applied(settings)
    assert not _has_discovery_table(settings)

    def fail(point: str) -> None:
        if point == "before_marker":
            raise RuntimeError("injected v2 failure")

    opportunity_storage._V2_MIGRATION_FAILPOINT_HOOK = fail
    try:
        with pytest.raises(RuntimeError, match="injected v2 failure"):
            apply_resumable_discovery_migration(settings)
    finally:
        opportunity_storage._V2_MIGRATION_FAILPOINT_HOOK = None

    assert is_migration_applied(settings)
    assert not is_resumable_discovery_migration_applied(settings)
    assert not _has_discovery_table(settings)
    markers = _marker_rows(settings)
    assert [row[0] for row in markers] == [MIGRATION_ID]


def test_v2_requires_v1(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "no_v1.db"))
    with pytest.raises(OpportunityMigrationError, match="opportunity_intel_v1"):
        apply_resumable_discovery_migration(settings)


def test_retention_registers_discovery_sessions():
    names = {policy.dataset for policy in build_retention_policies()}
    assert "opportunity_discovery_sessions" in names


def test_repeated_initialize_reports_both_already_applied(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "both.db"))
    first = initialize_opportunity_schema(settings)
    second = initialize_opportunity_schema(settings)
    assert first.migration_id == MIGRATION_ID
    assert second.already_applied is True
    v2_a = apply_resumable_discovery_migration(settings)
    v2_b = apply_resumable_discovery_migration(settings)
    assert v2_a.already_applied is True
    assert v2_b.already_applied is True
