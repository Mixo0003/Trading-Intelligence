"""Additive Market Opportunity Intelligence storage (market_opportunity_v1)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.config.settings import Settings
from app.database.storage import get_connection
from app.market_opportunity.versions import (
    MARKET_OPPORTUNITY_SCHEMA_MARKER,
    MARKET_OPPORTUNITY_SCHEMA_VERSION,
)

MIGRATION_ID = MARKET_OPPORTUNITY_SCHEMA_MARKER
SCHEMA_VERSION = MARKET_OPPORTUNITY_SCHEMA_VERSION


class MarketOpportunityMigrationError(RuntimeError):
    pass


def is_migration_applied(settings: Settings) -> bool:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'moi_schema_migrations'
            """
        ).fetchone()
        if row is None:
            return False
        found = connection.execute(
            """
            SELECT 1 FROM moi_schema_migrations WHERE migration_id = ?
            """,
            (MIGRATION_ID,),
        ).fetchone()
        return found is not None


def initialize_market_opportunity_schema(settings: Settings) -> None:
    """Idempotent additive apply of market_opportunity_v1."""
    with get_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _apply_v1_on_connection(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _apply_v1_on_connection(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS moi_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    existing = connection.execute(
        "SELECT 1 FROM moi_schema_migrations WHERE migration_id = ?",
        (MIGRATION_ID,),
    ).fetchone()
    if existing is not None:
        return

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS moi_opportunities (
            opportunity_uid TEXT PRIMARY KEY,
            family TEXT NOT NULL,
            opportunity_type TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT', 'WAIT')),
            status TEXT NOT NULL,
            confidence TEXT NOT NULL CHECK (confidence IN ('LOW', 'MEDIUM', 'HIGH')),
            asset_class TEXT NOT NULL,
            symbol TEXT NOT NULL,
            source_id TEXT NOT NULL,
            venue TEXT NOT NULL,
            market_id TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            verified_price TEXT NOT NULL,
            entry_zone_low TEXT NOT NULL,
            entry_zone_high TEXT NOT NULL,
            targets_json TEXT NOT NULL,
            invalidation_price TEXT NOT NULL,
            time_horizon TEXT NOT NULL,
            reward_risk TEXT NOT NULL,
            counterarguments_json TEXT NOT NULL,
            paper_only INTEGER NOT NULL CHECK (paper_only = 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT
        );

        CREATE TABLE IF NOT EXISTS moi_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_uid TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            source_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            source_event_at TEXT,
            series_kind TEXT NOT NULL,
            is_catalyst INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            UNIQUE (opportunity_uid, evidence_id),
            FOREIGN KEY (opportunity_uid) REFERENCES moi_opportunities (opportunity_uid)
        );

        CREATE TABLE IF NOT EXISTS moi_outcomes (
            opportunity_uid TEXT PRIMARY KEY,
            entry_reached INTEGER,
            entry_missed INTEGER,
            targets_reached_json TEXT,
            invalidation_reached INTEGER,
            mfe TEXT,
            mae TEXT,
            return_1d TEXT,
            return_5d TEXT,
            return_20d TEXT,
            measured_at TEXT,
            FOREIGN KEY (opportunity_uid) REFERENCES moi_opportunities (opportunity_uid)
        );

        CREATE TABLE IF NOT EXISTS moi_runtime_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT NOT NULL,
            detail TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            enabled_detectors_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE INDEX IF NOT EXISTS idx_moi_opportunities_status_updated
            ON moi_opportunities (status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_moi_evidence_opportunity
            ON moi_evidence (opportunity_uid);
        """
    )
    applied_at = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO moi_schema_migrations (migration_id, schema_version, applied_at)
        VALUES (?, ?, ?)
        """,
        (MIGRATION_ID, SCHEMA_VERSION, applied_at),
    )


def upsert_runtime_status(
    settings: Settings,
    *,
    status: str,
    detail: str,
    checked_at: datetime,
    enabled_detectors: list[str] | None = None,
) -> None:
    import json

    payload = json.dumps(list(enabled_detectors or []))
    checked = checked_at.astimezone(timezone.utc).isoformat()
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO moi_runtime_status (
                id, status, detail, checked_at, enabled_detectors_json
            ) VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                detail = excluded.detail,
                checked_at = excluded.checked_at,
                enabled_detectors_json = excluded.enabled_detectors_json
            """,
            (status, detail, checked, payload),
        )


def list_published_opportunity_uids(settings: Settings) -> list[str]:
    """V4.1 shell helper — returns empty until later versions publish."""
    if not is_migration_applied(settings):
        return []
    with get_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT opportunity_uid FROM moi_opportunities
            WHERE status = 'published'
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [str(row[0]) for row in rows]
