"""Additive opportunity_intel_v3_persisted_catalog migration proofs."""

from __future__ import annotations

import sqlite3

import pytest

from app.database import opportunity_storage
from app.database.opportunity_storage import (
    MIGRATION_ID,
    MIGRATION_ID_PERSISTED_CATALOG,
    MIGRATION_ID_RESUMABLE_DISCOVERY,
    OpportunityMigrationError,
    apply_persisted_catalog_migration,
    apply_resumable_discovery_migration,
    initialize_opportunity_schema,
    is_migration_applied,
    is_persisted_catalog_migration_applied,
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


def _has_catalog_tables(settings) -> bool:
    with get_connection(settings) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    return {
        "opportunity_catalog_versions",
        "opportunity_catalog_markets",
    }.issubset(names)


def test_fresh_db_applies_v1_v2_v3(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "fresh.db"))
    initialize_database(settings)
    assert is_migration_applied(settings)
    assert is_resumable_discovery_migration_applied(settings)
    assert is_persisted_catalog_migration_applied(settings)
    markers = _marker_rows(settings)
    assert [row[0] for row in markers] == [
        MIGRATION_ID,
        MIGRATION_ID_RESUMABLE_DISCOVERY,
        MIGRATION_ID_PERSISTED_CATALOG,
    ]
    assert _has_catalog_tables(settings)


def test_production_shape_v1_v2_applies_v3(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "v1v2.db"))
    with get_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        opportunity_storage._initialize_on_connection(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        opportunity_storage._apply_resumable_discovery_on_connection(connection)
    assert is_migration_applied(settings)
    assert is_resumable_discovery_migration_applied(settings)
    assert not is_persisted_catalog_migration_applied(settings)

    with get_connection(settings) as connection:
        connection.execute(
            "CREATE TABLE legacy_business_data (id INTEGER PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO legacy_business_data(id, value) VALUES (1, 'preserve-me')"
        )
        before = connection.execute("PRAGMA journal_mode").fetchone()[0]

    result = apply_persisted_catalog_migration(settings)
    assert result.already_applied is False
    assert is_persisted_catalog_migration_applied(settings)
    assert _has_catalog_tables(settings)
    markers = _marker_rows(settings)
    assert sum(1 for row in markers if row[0] == MIGRATION_ID) == 1
    assert sum(1 for row in markers if row[0] == MIGRATION_ID_RESUMABLE_DISCOVERY) == 1
    assert sum(1 for row in markers if row[0] == MIGRATION_ID_PERSISTED_CATALOG) == 1
    with get_connection(settings) as connection:
        assert (
            connection.execute(
                "SELECT value FROM legacy_business_data WHERE id = 1"
            ).fetchone()[0]
            == "preserve-me"
        )
        after = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert after == before


def test_v3_idempotency(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "idem.db"))
    initialize_database(settings)
    first = apply_persisted_catalog_migration(settings)
    second = apply_persisted_catalog_migration(settings)
    assert first.already_applied is True
    assert second.already_applied is True
    markers = _marker_rows(settings)
    assert sum(1 for row in markers if row[0] == MIGRATION_ID_PERSISTED_CATALOG) == 1


def test_v3_rollback_does_not_damage_v1_v2(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "rollback.db"))
    with get_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        opportunity_storage._initialize_on_connection(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        opportunity_storage._apply_resumable_discovery_on_connection(connection)

    def fail(point: str) -> None:
        if point == "before_marker":
            raise RuntimeError("injected v3 failure")

    opportunity_storage._V3_MIGRATION_FAILPOINT_HOOK = fail
    try:
        with pytest.raises(RuntimeError, match="injected v3 failure"):
            apply_persisted_catalog_migration(settings)
    finally:
        opportunity_storage._V3_MIGRATION_FAILPOINT_HOOK = None

    assert is_migration_applied(settings)
    assert is_resumable_discovery_migration_applied(settings)
    assert not is_persisted_catalog_migration_applied(settings)
    assert not _has_catalog_tables(settings)


def test_v3_requires_v2(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "no_v2.db"))
    with get_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        opportunity_storage._initialize_on_connection(connection)
    with pytest.raises(OpportunityMigrationError, match="v2_resumable"):
        apply_persisted_catalog_migration(settings)


def test_no_raw_payload_columns(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "raw.db"))
    initialize_database(settings)
    with get_connection(settings) as connection:
        for table in ("opportunity_catalog_versions", "opportunity_catalog_markets"):
            cols = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert not (
                cols
                & {
                    "raw_payload",
                    "raw_payload_json",
                    "gamma_payload",
                    "events_json",
                }
            )


def test_retention_registers_catalog_tables():
    names = {policy.dataset for policy in build_retention_policies()}
    assert "opportunity_catalog_versions" in names
    assert "opportunity_catalog_markets" in names


def test_repeated_initialize_keeps_singular_markers(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "both.db"))
    initialize_opportunity_schema(settings)
    initialize_opportunity_schema(settings)
    markers = _marker_rows(settings)
    assert sum(1 for row in markers if row[0] == MIGRATION_ID) == 1
    assert sum(1 for row in markers if row[0] == MIGRATION_ID_RESUMABLE_DISCOVERY) == 1
    assert sum(1 for row in markers if row[0] == MIGRATION_ID_PERSISTED_CATALOG) == 1
