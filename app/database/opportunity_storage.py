"""Additive SQLite storage foundations for Market Opportunity Scanner."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable

from app.config.settings import Settings
from app.database.storage import get_connection
from app.opportunity_intel.models import (
    AlertMode,
    CanonicalObservation,
    DeliveryStatus,
    ExecutionMetrics,
    LiveMode,
    MovementDirection,
    ObservationMode,
    OpportunityBaseline,
    OpportunityDelivery,
    OpportunityDiagnosticCode,
    OpportunityScannerState,
    OpportunitySignal,
    OpportunitySubscription,
    OpportunityReasonCode,
    ScannerStatus,
    SignalFamily,
    SignalStatus,
    decimal_text,
)
from app.opportunity_intel.settings import (
    CategoryAllowlist,
    OpportunityScannerSettings,
    QuietHours,
)

MIGRATION_ID = "opportunity_intel_v1"
MIGRATION_ID_RESUMABLE_DISCOVERY = "opportunity_intel_v2_resumable_discovery"
MIGRATION_ID_PERSISTED_CATALOG = "opportunity_intel_v3_persisted_catalog"
SCHEMA_VERSION = 1
SCHEMA_VERSION_RESUMABLE_DISCOVERY = 2
SCHEMA_VERSION_PERSISTED_CATALOG = 3

DEFAULT_CATALOG_RETENTION_VERSIONS = 3

_MIGRATION_FAILPOINT_HOOK: Callable[[str], None] | None = None
_V2_MIGRATION_FAILPOINT_HOOK: Callable[[str], None] | None = None
_V3_MIGRATION_FAILPOINT_HOOK: Callable[[str], None] | None = None

_TABLES = frozenset(
    {
        "opportunity_schema_migrations",
        "opportunity_market_state",
        "opportunity_metric_buckets",
        "opportunity_execution_metrics",
        "opportunity_signals",
        "opportunity_subscriptions",
        "opportunity_setting_edits",
        "opportunity_settings_audit",
        "opportunity_deliveries",
        "opportunity_delivery_gates",
        "opportunity_scanner_state",
    }
)

_INDEXES = frozenset(
    {
        "idx_opportunity_market_state_mode_seen",
        "idx_opportunity_metric_buckets_asset_time",
        "idx_opportunity_metric_buckets_bucket_at",
        "idx_opportunity_execution_metrics_observed",
        "idx_opportunity_signals_active_score",
        "idx_opportunity_signals_asset_last_seen",
        "idx_opportunity_subscriptions_enabled",
        "idx_opportunity_setting_edits_user_status",
        "idx_opportunity_settings_audit_user_time",
        "idx_opportunity_deliveries_status_retry",
        "idx_opportunity_deliveries_signal_wave",
        "idx_opportunity_deliveries_recipient_status",
    }
)
_TRIGGERS = frozenset({"trg_opportunity_delivery_payload_immutable"})

_AUDITABLE_SETTING_KEYS = frozenset(
    {
        "minimum_score",
        "minimum_liquidity_usd",
        "minimum_24h_volume_usd",
        "maximum_relative_spread",
        "maximum_absolute_spread",
        "maximum_slippage",
        "paper_allocation_usd",
        "categories",
        "live_mode",
        "cooldown_seconds",
        "alert_mode",
        "quiet_hours",
        "timezone_name",
    }
)
_TOKEN_FINGERPRINT_RE = re.compile(r"^[a-f0-9]{16,128}$")


class OpportunityMigrationError(RuntimeError):
    """Raised when the additive opportunity schema cannot be proven complete."""


class OpportunitySettingsRevisionConflict(RuntimeError):
    """Raised when a settings write uses a stale revision."""


@dataclass(frozen=True)
class OpportunityMigrationResult:
    migration_id: str
    already_applied: bool
    table_count: int
    index_count: int


@dataclass(frozen=True)
class ResumableDiscoveryMigrationResult:
    migration_id: str
    already_applied: bool
    table_present: bool
    index_present: bool
    applied_at: str | None


@dataclass(frozen=True)
class PersistedCatalogMigrationResult:
    migration_id: str
    already_applied: bool
    table_present: bool
    index_present: bool
    applied_at: str | None


@dataclass(frozen=True)
class CatalogVersionRecord:
    catalog_version_id: str
    discovery_session_id: str | None
    completed_at: datetime
    published_at: datetime
    market_count: int
    tradeable_market_count: int
    pages_completed: int
    status: str
    diagnostics: tuple[str, ...]
    created_at: datetime


@contextmanager
def _opportunity_connection(
    settings: Settings,
) -> Iterator[sqlite3.Connection]:
    """Use FK enforcement for this repository without changing legacy storage."""
    with get_connection(settings) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _failpoint(point: str) -> None:
    hook = _MIGRATION_FAILPOINT_HOOK
    if hook is not None:
        hook(point)


def _v2_failpoint(point: str) -> None:
    hook = _V2_MIGRATION_FAILPOINT_HOOK
    if hook is not None:
        hook(point)


def initialize_opportunity_schema(settings: Settings) -> OpportunityMigrationResult:
    """Apply v1, v2, then v3 in separate commits on one connection.

    Separate commits keep a failed later migration from rolling back earlier
    successful markers. Using one connection also works for SQLite ``:memory:``
    databases, which are connection-scoped.
    """
    with _opportunity_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = _initialize_on_connection(connection)
        connection.commit()

        connection.execute("BEGIN IMMEDIATE")
        apply_v2 = _apply_resumable_discovery_on_connection(connection)
        connection.commit()
        _ = apply_v2

        connection.execute("BEGIN IMMEDIATE")
        apply_v3 = _apply_persisted_catalog_on_connection(connection)
        # Leave the successful v3 transaction for get_connection to commit.
        _ = apply_v3
    return result


def apply_resumable_discovery_migration(
    settings: Settings,
) -> ResumableDiscoveryMigrationResult:
    """Additive v2 migration in its own transaction; never rolls back v1."""
    with _opportunity_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _apply_resumable_discovery_on_connection(connection)


def apply_persisted_catalog_migration(
    settings: Settings,
) -> PersistedCatalogMigrationResult:
    """Additive v3 migration in its own transaction; never rolls back v1/v2."""
    with _opportunity_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _apply_persisted_catalog_on_connection(connection)


def _initialize_on_connection(
    connection: sqlite3.Connection,
) -> OpportunityMigrationResult:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
            applied_at TEXT NOT NULL
        )
        """
    )
    existing = connection.execute(
        """
        SELECT migration_id
        FROM opportunity_schema_migrations
        WHERE migration_id = ?
        """,
        (MIGRATION_ID,),
    ).fetchone()
    if existing is not None:
        _ensure_opportunity_columns(connection)
        _ensure_delivery_foundations(connection)
        _validate_schema(connection)
        return OpportunityMigrationResult(
            migration_id=MIGRATION_ID,
            already_applied=True,
            table_count=len(_TABLES),
            index_count=len(_INDEXES),
        )

    for statement in _TABLE_DDL:
        connection.execute(statement)
    _failpoint("after_tables")
    for statement in _INDEX_DDL:
        connection.execute(statement)
    _failpoint("after_indexes")
    for statement in _TRIGGER_DDL:
        connection.execute(statement)
    _failpoint("after_triggers")
    _ensure_opportunity_columns(connection)
    _ensure_delivery_foundations(connection)
    _validate_schema(connection)
    _failpoint("before_marker")
    connection.execute(
        """
        INSERT INTO opportunity_schema_migrations (
            migration_id, schema_version, applied_at
        ) VALUES (?, ?, ?)
        """,
        (MIGRATION_ID, SCHEMA_VERSION, _iso(_now())),
    )
    return OpportunityMigrationResult(
        migration_id=MIGRATION_ID,
        already_applied=False,
        table_count=len(_TABLES),
        index_count=len(_INDEXES),
    )


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    names = {row[1] for row in rows}
    if column not in names:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def _ensure_opportunity_columns(connection: sqlite3.Connection) -> None:
    """Additive non-destructive columns for Checkpoint 4 hysteresis state."""
    for column, col_type in (
        ("score_version", "INTEGER NOT NULL DEFAULT 1"),
        ("safety_policy_version", "INTEGER NOT NULL DEFAULT 1"),
        ("below_threshold_streak", "INTEGER NOT NULL DEFAULT 0"),
        ("was_eligible_at_baseline", "INTEGER NOT NULL DEFAULT 0"),
        ("last_direction", "TEXT"),
        ("pending_live_direction", "TEXT"),
        ("last_absolute_move_pp", "TEXT"),
        ("last_signal_at", "TEXT"),
        ("identity_fingerprint", "TEXT"),
        ("baseline_reset_code", "TEXT"),
    ):
        _ensure_column(connection, "opportunity_market_state", column, col_type)


def _ensure_delivery_foundations(connection: sqlite3.Connection) -> None:
    """Idempotent gates table + delivery lookup index for Checkpoint 5."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_delivery_gates (
            gate_key TEXT PRIMARY KEY CHECK(gate_key = 'gates'),
            admin_delivery_enabled INTEGER NOT NULL
                CHECK(admin_delivery_enabled IN (0, 1)),
            user_delivery_enabled INTEGER NOT NULL
                CHECK(user_delivery_enabled IN (0, 1)),
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO opportunity_delivery_gates (
            gate_key, admin_delivery_enabled, user_delivery_enabled, updated_at
        ) VALUES ('gates', 1, 0, ?)
        """,
        (_iso(_now()),),
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_opportunity_deliveries_recipient_status
        ON opportunity_deliveries(telegram_id, status, updated_at)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_runtime_status (
            status_key TEXT PRIMARY KEY CHECK(status_key = 'runtime'),
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


_DISCOVERY_SESSION_TABLE = "opportunity_discovery_sessions"
_DISCOVERY_SESSION_INDEX = "idx_opportunity_discovery_sessions_expires"
_DISCOVERY_SESSION_FORBIDDEN_COLUMNS = frozenset(
    {
        "raw_payload",
        "raw_payload_json",
        "gamma_payload",
        "gamma_payload_json",
        "page_payload",
        "page_payload_json",
        "events_json",
        "raw_events_json",
    }
)


def _apply_resumable_discovery_on_connection(
    connection: sqlite3.Connection,
) -> ResumableDiscoveryMigrationResult:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
            applied_at TEXT NOT NULL
        )
        """
    )
    v1 = connection.execute(
        """
        SELECT 1 FROM opportunity_schema_migrations
        WHERE migration_id = ?
        """,
        (MIGRATION_ID,),
    ).fetchone()
    if v1 is None:
        raise OpportunityMigrationError(
            "opportunity_intel_v1 must be applied before "
            f"{MIGRATION_ID_RESUMABLE_DISCOVERY}"
        )

    existing = connection.execute(
        """
        SELECT applied_at
        FROM opportunity_schema_migrations
        WHERE migration_id = ?
        """,
        (MIGRATION_ID_RESUMABLE_DISCOVERY,),
    ).fetchone()
    if existing is not None:
        _validate_resumable_discovery_schema(connection)
        return ResumableDiscoveryMigrationResult(
            migration_id=MIGRATION_ID_RESUMABLE_DISCOVERY,
            already_applied=True,
            table_present=True,
            index_present=True,
            applied_at=str(existing["applied_at"]),
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_discovery_sessions (
            slot_key TEXT PRIMARY KEY CHECK(slot_key = 'active'),
            session_id TEXT NOT NULL,
            session_version INTEGER NOT NULL CHECK(session_version >= 1),
            state TEXT NOT NULL CHECK(
                state IN (
                    'NOT_STARTED',
                    'IN_PROGRESS',
                    'COMPLETE',
                    'RETRYABLE_FAILURE',
                    'PERMANENT_FAILURE',
                    'EXPIRED'
                )
            ),
            started_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            next_cursor TEXT,
            seen_cursors_json TEXT NOT NULL,
            pages_completed INTEGER NOT NULL CHECK(pages_completed >= 0),
            attempts_used INTEGER NOT NULL CHECK(attempts_used >= 0),
            markets_json TEXT NOT NULL,
            seen_event_ids_json TEXT NOT NULL,
            seen_market_ids_json TEXT NOT NULL,
            seen_condition_ids_json TEXT NOT NULL,
            seen_asset_ids_json TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL
        )
        """
    )
    _v2_failpoint("after_sessions_table")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_opportunity_discovery_sessions_expires
        ON opportunity_discovery_sessions(expires_at)
        """
    )
    _v2_failpoint("after_indexes")
    _validate_resumable_discovery_schema(connection)
    _v2_failpoint("before_marker")
    applied_at = _iso(_now())
    connection.execute(
        """
        INSERT INTO opportunity_schema_migrations (
            migration_id, schema_version, applied_at
        ) VALUES (?, ?, ?)
        """,
        (
            MIGRATION_ID_RESUMABLE_DISCOVERY,
            SCHEMA_VERSION_RESUMABLE_DISCOVERY,
            applied_at,
        ),
    )
    return ResumableDiscoveryMigrationResult(
        migration_id=MIGRATION_ID_RESUMABLE_DISCOVERY,
        already_applied=False,
        table_present=True,
        index_present=True,
        applied_at=applied_at,
    )


def _validate_resumable_discovery_schema(connection: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    if _DISCOVERY_SESSION_TABLE not in tables:
        raise OpportunityMigrationError(
            f"missing table {_DISCOVERY_SESSION_TABLE}"
        )
    if _DISCOVERY_SESSION_INDEX not in indexes:
        raise OpportunityMigrationError(
            f"missing index {_DISCOVERY_SESSION_INDEX}"
        )
    columns = {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({_DISCOVERY_SESSION_TABLE})"
        ).fetchall()
    }
    forbidden = sorted(columns & _DISCOVERY_SESSION_FORBIDDEN_COLUMNS)
    if forbidden:
        raise OpportunityMigrationError(
            f"raw payload columns forbidden on discovery sessions: {forbidden}"
        )
    required = {
        "slot_key",
        "session_id",
        "session_version",
        "state",
        "started_at",
        "expires_at",
        "updated_at",
        "next_cursor",
        "seen_cursors_json",
        "pages_completed",
        "attempts_used",
        "markets_json",
        "seen_event_ids_json",
        "seen_market_ids_json",
        "seen_condition_ids_json",
        "seen_asset_ids_json",
        "diagnostics_json",
    }
    missing = sorted(required - columns)
    if missing:
        raise OpportunityMigrationError(
            f"incomplete discovery session columns: {missing}"
        )


def is_resumable_discovery_migration_applied(settings: Settings) -> bool:
    with _opportunity_connection(settings) as connection:
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'opportunity_schema_migrations'
            """
        ).fetchone()
        if table is None:
            return False
        try:
            row = connection.execute(
                """
                SELECT 1 FROM opportunity_schema_migrations
                WHERE migration_id = ?
                """,
                (MIGRATION_ID_RESUMABLE_DISCOVERY,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None


_CATALOG_VERSIONS_TABLE = "opportunity_catalog_versions"
_CATALOG_MARKETS_TABLE = "opportunity_catalog_markets"
_CATALOG_VERSIONS_ACTIVE_INDEX = "idx_opportunity_catalog_versions_active"
_CATALOG_MARKETS_VERSION_INDEX = "idx_opportunity_catalog_markets_version"
_CATALOG_MARKETS_END_INDEX = "idx_opportunity_catalog_markets_end_at"
_CATALOG_FORBIDDEN_COLUMNS = frozenset(
    {
        "raw_payload",
        "raw_payload_json",
        "gamma_payload",
        "gamma_payload_json",
        "page_payload",
        "page_payload_json",
        "events_json",
        "raw_events_json",
    }
)


def _v3_failpoint(point: str) -> None:
    hook = _V3_MIGRATION_FAILPOINT_HOOK
    if hook is not None:
        hook(point)


def _apply_persisted_catalog_on_connection(
    connection: sqlite3.Connection,
) -> PersistedCatalogMigrationResult:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
            applied_at TEXT NOT NULL
        )
        """
    )
    v1 = connection.execute(
        """
        SELECT 1 FROM opportunity_schema_migrations
        WHERE migration_id = ?
        """,
        (MIGRATION_ID,),
    ).fetchone()
    if v1 is None:
        raise OpportunityMigrationError(
            "opportunity_intel_v1 must be applied before "
            f"{MIGRATION_ID_PERSISTED_CATALOG}"
        )
    v2 = connection.execute(
        """
        SELECT 1 FROM opportunity_schema_migrations
        WHERE migration_id = ?
        """,
        (MIGRATION_ID_RESUMABLE_DISCOVERY,),
    ).fetchone()
    if v2 is None:
        raise OpportunityMigrationError(
            "opportunity_intel_v2_resumable_discovery must be applied before "
            f"{MIGRATION_ID_PERSISTED_CATALOG}"
        )

    existing = connection.execute(
        """
        SELECT applied_at
        FROM opportunity_schema_migrations
        WHERE migration_id = ?
        """,
        (MIGRATION_ID_PERSISTED_CATALOG,),
    ).fetchone()
    if existing is not None:
        _validate_persisted_catalog_schema(connection)
        return PersistedCatalogMigrationResult(
            migration_id=MIGRATION_ID_PERSISTED_CATALOG,
            already_applied=True,
            table_present=True,
            index_present=True,
            applied_at=str(existing["applied_at"]),
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_catalog_versions (
            catalog_version_id TEXT PRIMARY KEY,
            discovery_session_id TEXT,
            completed_at TEXT NOT NULL,
            published_at TEXT NOT NULL,
            market_count INTEGER NOT NULL CHECK(market_count >= 0),
            tradeable_market_count INTEGER NOT NULL
                CHECK(tradeable_market_count >= 0),
            pages_completed INTEGER NOT NULL CHECK(pages_completed >= 0),
            status TEXT NOT NULL CHECK(
                status IN ('ACTIVE', 'SUPERSEDED', 'EXPIRED')
            ),
            diagnostics_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    _v3_failpoint("after_versions_table")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_catalog_markets (
            catalog_version_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            market_id TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            event_slug TEXT NOT NULL,
            question TEXT NOT NULL,
            category_ids_json TEXT NOT NULL,
            mode TEXT NOT NULL,
            liquidity_usd TEXT NOT NULL,
            volume_24h_usd TEXT NOT NULL,
            metadata_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            outcomes_json TEXT NOT NULL,
            enable_order_book INTEGER NOT NULL
                CHECK(enable_order_book IN (0, 1)),
            accepting_orders INTEGER NOT NULL
                CHECK(accepting_orders IN (0, 1)),
            active INTEGER NOT NULL CHECK(active IN (0, 1)),
            closed INTEGER NOT NULL CHECK(closed IN (0, 1)),
            PRIMARY KEY (catalog_version_id, market_id),
            FOREIGN KEY (catalog_version_id)
                REFERENCES opportunity_catalog_versions(catalog_version_id)
        )
        """
    )
    _v3_failpoint("after_markets_table")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_opportunity_catalog_versions_active
        ON opportunity_catalog_versions(status, published_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_opportunity_catalog_markets_version
        ON opportunity_catalog_markets(catalog_version_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_opportunity_catalog_markets_end_at
        ON opportunity_catalog_markets(catalog_version_id, end_at)
        """
    )
    _v3_failpoint("after_indexes")
    _validate_persisted_catalog_schema(connection)
    _v3_failpoint("before_marker")
    applied_at = _iso(_now())
    connection.execute(
        """
        INSERT INTO opportunity_schema_migrations (
            migration_id, schema_version, applied_at
        ) VALUES (?, ?, ?)
        """,
        (
            MIGRATION_ID_PERSISTED_CATALOG,
            SCHEMA_VERSION_PERSISTED_CATALOG,
            applied_at,
        ),
    )
    return PersistedCatalogMigrationResult(
        migration_id=MIGRATION_ID_PERSISTED_CATALOG,
        already_applied=False,
        table_present=True,
        index_present=True,
        applied_at=applied_at,
    )


def _validate_persisted_catalog_schema(connection: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    for table in (_CATALOG_VERSIONS_TABLE, _CATALOG_MARKETS_TABLE):
        if table not in tables:
            raise OpportunityMigrationError(f"missing table {table}")
    for index in (
        _CATALOG_VERSIONS_ACTIVE_INDEX,
        _CATALOG_MARKETS_VERSION_INDEX,
        _CATALOG_MARKETS_END_INDEX,
    ):
        if index not in indexes:
            raise OpportunityMigrationError(f"missing index {index}")
    for table in (_CATALOG_VERSIONS_TABLE, _CATALOG_MARKETS_TABLE):
        columns = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        forbidden = sorted(columns & _CATALOG_FORBIDDEN_COLUMNS)
        if forbidden:
            raise OpportunityMigrationError(
                f"raw payload columns forbidden on {table}: {forbidden}"
            )


def is_persisted_catalog_migration_applied(settings: Settings) -> bool:
    with _opportunity_connection(settings) as connection:
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'opportunity_schema_migrations'
            """
        ).fetchone()
        if table is None:
            return False
        try:
            row = connection.execute(
                """
                SELECT 1 FROM opportunity_schema_migrations
                WHERE migration_id = ?
                """,
                (MIGRATION_ID_PERSISTED_CATALOG,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None


def publish_complete_catalog(
    settings: Settings,
    *,
    markets: Iterable[Any],
    completed_at: datetime,
    discovery_session_id: str | None,
    pages_completed: int,
    diagnostics: Iterable[str] = (),
    retention_versions: int = DEFAULT_CATALOG_RETENTION_VERSIONS,
    catalog_version_id: str | None = None,
) -> str:
    """Atomically publish a COMPLETE catalog. Prior ACTIVE remains until commit."""
    from app.opportunity_intel.discovery_session import market_to_normalized_dict

    if retention_versions < 1:
        raise ValueError("retention_versions must be >= 1")
    market_list = list(markets)
    version_id = catalog_version_id or f"cat-{uuid.uuid4().hex}"
    published_at = _now()
    diagnostics_tuple = tuple(str(item)[:120] for item in diagnostics)[:50]
    with _opportunity_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE opportunity_catalog_versions
            SET status = 'SUPERSEDED'
            WHERE status = 'ACTIVE'
            """
        )
        connection.execute(
            """
            INSERT INTO opportunity_catalog_versions (
                catalog_version_id, discovery_session_id, completed_at,
                published_at, market_count, tradeable_market_count,
                pages_completed, status, diagnostics_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
            """,
            (
                version_id,
                discovery_session_id,
                _iso(completed_at),
                _iso(published_at),
                len(market_list),
                len(market_list),
                int(pages_completed),
                json.dumps(list(diagnostics_tuple)),
                _iso(published_at),
            ),
        )
        for market in market_list:
            payload = market_to_normalized_dict(market)
            connection.execute(
                """
                INSERT INTO opportunity_catalog_markets (
                    catalog_version_id, event_id, market_id, condition_id,
                    event_slug, question, category_ids_json, mode,
                    liquidity_usd, volume_24h_usd, metadata_at, end_at,
                    outcomes_json, enable_order_book, accepting_orders,
                    active, closed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 1, 0)
                """,
                (
                    version_id,
                    payload["event_id"],
                    payload["market_id"],
                    payload["condition_id"],
                    payload["event_slug"],
                    payload["question"],
                    json.dumps(payload["category_ids"]),
                    payload["mode"],
                    payload["liquidity_usd"],
                    payload["volume_24h_usd"],
                    payload["metadata_at"],
                    payload["end_at"],
                    json.dumps(payload["outcomes"]),
                ),
            )
        _prune_catalog_versions_on_connection(
            connection, retention_versions=retention_versions
        )
    return version_id


def _prune_catalog_versions_on_connection(
    connection: sqlite3.Connection, *, retention_versions: int
) -> None:
    rows = connection.execute(
        """
        SELECT catalog_version_id, status, published_at
        FROM opportunity_catalog_versions
        ORDER BY published_at DESC, catalog_version_id DESC
        """
    ).fetchall()
    keep: list[str] = []
    for row in rows:
        if row["status"] == "ACTIVE" or len(keep) < retention_versions:
            keep.append(str(row["catalog_version_id"]))
            continue
        version_id = str(row["catalog_version_id"])
        connection.execute(
            "DELETE FROM opportunity_catalog_markets WHERE catalog_version_id = ?",
            (version_id,),
        )
        connection.execute(
            "DELETE FROM opportunity_catalog_versions WHERE catalog_version_id = ?",
            (version_id,),
        )


def load_active_catalog_version(
    settings: Settings,
) -> CatalogVersionRecord | None:
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT catalog_version_id, discovery_session_id, completed_at,
                   published_at, market_count, tradeable_market_count,
                   pages_completed, status, diagnostics_json, created_at
            FROM opportunity_catalog_versions
            WHERE status = 'ACTIVE'
            ORDER BY published_at DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return _catalog_version_from_row(row)


def load_active_catalog_markets(settings: Settings) -> tuple[Any, ...]:
    """Load ACTIVE catalog markets as ValidatedMarket tuples."""
    from app.opportunity_intel.discovery_session import market_from_normalized_dict

    version = load_active_catalog_version(settings)
    if version is None:
        return ()
    with _opportunity_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT event_id, market_id, condition_id, event_slug, question,
                   category_ids_json, mode, liquidity_usd, volume_24h_usd,
                   metadata_at, end_at, outcomes_json
            FROM opportunity_catalog_markets
            WHERE catalog_version_id = ?
            ORDER BY market_id
            """,
            (version.catalog_version_id,),
        ).fetchall()
    markets = []
    for row in rows:
        markets.append(
            market_from_normalized_dict(
                {
                    "event_id": row["event_id"],
                    "market_id": row["market_id"],
                    "condition_id": row["condition_id"],
                    "event_slug": row["event_slug"],
                    "question": row["question"],
                    "category_ids": json.loads(row["category_ids_json"]),
                    "mode": row["mode"],
                    "liquidity_usd": row["liquidity_usd"],
                    "volume_24h_usd": row["volume_24h_usd"],
                    "metadata_at": row["metadata_at"],
                    "end_at": row["end_at"],
                    "outcomes": json.loads(row["outcomes_json"]),
                }
            )
        )
    return tuple(markets)


def load_active_catalog(
    settings: Settings,
) -> tuple[CatalogVersionRecord, tuple[Any, ...]] | None:
    version = load_active_catalog_version(settings)
    if version is None:
        return None
    return version, load_active_catalog_markets(settings)


def _catalog_version_from_row(row: sqlite3.Row) -> CatalogVersionRecord:
    diagnostics_raw = json.loads(row["diagnostics_json"] or "[]")
    diagnostics = tuple(str(item) for item in diagnostics_raw)
    return CatalogVersionRecord(
        catalog_version_id=str(row["catalog_version_id"]),
        discovery_session_id=(
            None
            if row["discovery_session_id"] is None
            else str(row["discovery_session_id"])
        ),
        completed_at=datetime.fromisoformat(str(row["completed_at"])),
        published_at=datetime.fromisoformat(str(row["published_at"])),
        market_count=int(row["market_count"]),
        tradeable_market_count=int(row["tradeable_market_count"]),
        pages_completed=int(row["pages_completed"]),
        status=str(row["status"]),
        diagnostics=diagnostics,
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


def _validate_schema(connection: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    triggers = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    missing_tables = sorted(_TABLES - tables)
    missing_indexes = sorted(_INDEXES - indexes)
    missing_triggers = sorted(_TRIGGERS - triggers)
    if missing_tables or missing_indexes or missing_triggers:
        raise OpportunityMigrationError(
            f"incomplete opportunity schema: tables={missing_tables}, "
            f"indexes={missing_indexes}, triggers={missing_triggers}"
        )


def is_migration_applied(settings: Settings) -> bool:
    with _opportunity_connection(settings) as connection:
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'opportunity_schema_migrations'
            """
        ).fetchone()
        if table is None:
            return False
        row = connection.execute(
            """
            SELECT 1 FROM opportunity_schema_migrations
            WHERE migration_id = ?
            """,
            (MIGRATION_ID,),
        ).fetchone()
        return row is not None


def _settings_payload(value: OpportunityScannerSettings) -> dict[str, Any]:
    quiet = value.quiet_hours
    return {
        "revision": value.revision,
        "minimum_score": value.minimum_score,
        "minimum_liquidity_usd": decimal_text(value.minimum_liquidity_usd),
        "minimum_24h_volume_usd": decimal_text(value.minimum_24h_volume_usd),
        "maximum_relative_spread": decimal_text(value.maximum_relative_spread),
        "maximum_absolute_spread": decimal_text(value.maximum_absolute_spread),
        "maximum_slippage": decimal_text(value.maximum_slippage),
        "paper_allocation_usd": decimal_text(value.paper_allocation_usd),
        "categories": {
            "all_categories": value.categories.all_categories,
            "category_ids": sorted(value.categories.category_ids),
        },
        "live_mode": value.live_mode.value,
        "cooldown_seconds": value.cooldown_seconds,
        "alert_mode": value.alert_mode.value,
        "quiet_hours": (
            None
            if quiet is None
            else {
                "start": quiet.start.isoformat(timespec="minutes"),
                "end": quiet.end.isoformat(timespec="minutes"),
            }
        ),
        "timezone_name": value.timezone_name,
    }


def _settings_from_payload(payload: dict[str, Any]) -> OpportunityScannerSettings:
    categories_payload = payload["categories"]
    quiet_payload = payload.get("quiet_hours")
    quiet = (
        None
        if quiet_payload is None
        else QuietHours(
            start=time.fromisoformat(quiet_payload["start"]),
            end=time.fromisoformat(quiet_payload["end"]),
        )
    )
    return OpportunityScannerSettings(
        revision=int(payload["revision"]),
        minimum_score=int(payload["minimum_score"]),
        minimum_liquidity_usd=Decimal(payload["minimum_liquidity_usd"]),
        minimum_24h_volume_usd=Decimal(payload["minimum_24h_volume_usd"]),
        maximum_relative_spread=Decimal(payload["maximum_relative_spread"]),
        maximum_absolute_spread=Decimal(payload["maximum_absolute_spread"]),
        maximum_slippage=Decimal(payload["maximum_slippage"]),
        paper_allocation_usd=Decimal(payload["paper_allocation_usd"]),
        categories=CategoryAllowlist(
            all_categories=bool(categories_payload["all_categories"]),
            category_ids=frozenset(categories_payload["category_ids"]),
        ),
        live_mode=LiveMode(payload["live_mode"]),
        cooldown_seconds=int(payload["cooldown_seconds"]),
        alert_mode=AlertMode(payload["alert_mode"]),
        quiet_hours=quiet,
        timezone_name=str(payload["timezone_name"]),
    )


def save_subscription(
    settings: Settings,
    subscription: OpportunitySubscription,
    *,
    expected_revision: int | None,
) -> None:
    """Insert/update settings with optimistic revision protection."""
    scanner = subscription.settings
    now = _iso(_now())
    payload = _settings_payload(scanner)
    categories = payload["categories"]
    quiet = payload["quiet_hours"]
    with _opportunity_connection(settings) as connection:
        current = connection.execute(
            """
            SELECT revision FROM opportunity_subscriptions
            WHERE telegram_id = ?
            """,
            (subscription.telegram_id,),
        ).fetchone()
        if current is None:
            if expected_revision is not None or scanner.revision != 0:
                raise OpportunitySettingsRevisionConflict(
                    "new settings require revision 0 and no expected revision"
                )
            connection.execute(
                """
                INSERT INTO opportunity_subscriptions (
                    telegram_id, enabled, revision, minimum_score,
                    minimum_liquidity_usd, minimum_24h_volume_usd,
                    maximum_relative_spread, maximum_absolute_spread,
                    maximum_slippage, paper_allocation_usd,
                    all_categories, category_ids_json, live_mode,
                    cooldown_seconds, alert_mode, quiet_start, quiet_end,
                    timezone_name, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    subscription.telegram_id,
                    int(subscription.enabled),
                    scanner.revision,
                    scanner.minimum_score,
                    payload["minimum_liquidity_usd"],
                    payload["minimum_24h_volume_usd"],
                    payload["maximum_relative_spread"],
                    payload["maximum_absolute_spread"],
                    payload["maximum_slippage"],
                    payload["paper_allocation_usd"],
                    int(categories["all_categories"]),
                    json.dumps(categories["category_ids"], separators=(",", ":")),
                    scanner.live_mode.value,
                    scanner.cooldown_seconds,
                    scanner.alert_mode.value,
                    quiet["start"] if quiet else None,
                    quiet["end"] if quiet else None,
                    scanner.timezone_name,
                    now,
                    now,
                ),
            )
            return

        current_revision = int(current["revision"])
        if (
            expected_revision is None
            or current_revision != expected_revision
            or scanner.revision != current_revision + 1
        ):
            raise OpportunitySettingsRevisionConflict(
                "settings revision changed before confirmation"
            )
        cursor = connection.execute(
            """
            UPDATE opportunity_subscriptions SET
                enabled = ?, revision = ?, minimum_score = ?,
                minimum_liquidity_usd = ?, minimum_24h_volume_usd = ?,
                maximum_relative_spread = ?, maximum_absolute_spread = ?,
                maximum_slippage = ?, paper_allocation_usd = ?,
                all_categories = ?, category_ids_json = ?, live_mode = ?,
                cooldown_seconds = ?, alert_mode = ?, quiet_start = ?,
                quiet_end = ?, timezone_name = ?, updated_at = ?
            WHERE telegram_id = ? AND revision = ?
            """,
            (
                int(subscription.enabled),
                scanner.revision,
                scanner.minimum_score,
                payload["minimum_liquidity_usd"],
                payload["minimum_24h_volume_usd"],
                payload["maximum_relative_spread"],
                payload["maximum_absolute_spread"],
                payload["maximum_slippage"],
                payload["paper_allocation_usd"],
                int(categories["all_categories"]),
                json.dumps(categories["category_ids"], separators=(",", ":")),
                scanner.live_mode.value,
                scanner.cooldown_seconds,
                scanner.alert_mode.value,
                quiet["start"] if quiet else None,
                quiet["end"] if quiet else None,
                scanner.timezone_name,
                now,
                subscription.telegram_id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise OpportunitySettingsRevisionConflict(
                "settings revision changed before update"
            )


def load_subscription(
    settings: Settings, telegram_id: int
) -> OpportunitySubscription | None:
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT * FROM opportunity_subscriptions WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()
    if row is None:
        return None
    return _subscription_from_row(row)


def save_setting_edit(
    settings: Settings,
    *,
    token_fingerprint: str,
    telegram_id: int,
    starting_revision: int,
    proposed_settings: OpportunityScannerSettings,
    expires_at: datetime,
) -> None:
    if not _TOKEN_FINGERPRINT_RE.fullmatch(token_fingerprint):
        raise ValueError("token_fingerprint must be a bounded lowercase hex digest")
    if proposed_settings.revision != starting_revision + 1:
        raise ValueError("proposed settings revision must follow starting revision")
    now = _now()
    if expires_at.tzinfo is None or expires_at.astimezone(timezone.utc) <= now:
        raise ValueError("edit token expiry must be in the future")
    with _opportunity_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO opportunity_setting_edits (
                token_fingerprint, telegram_id, starting_revision,
                proposed_settings_json, status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                token_fingerprint,
                telegram_id,
                starting_revision,
                json.dumps(
                    _settings_payload(proposed_settings),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                _iso(now),
                _iso(expires_at),
            ),
        )


def load_setting_edit(
    settings: Settings, token_fingerprint: str
) -> dict[str, Any] | None:
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT * FROM opportunity_setting_edits
            WHERE token_fingerprint = ?
            """,
            (token_fingerprint,),
        ).fetchone()
    if row is None:
        return None
    return {
        "token_fingerprint": row["token_fingerprint"],
        "telegram_id": int(row["telegram_id"]),
        "starting_revision": int(row["starting_revision"]),
        "proposed_settings": _settings_from_payload(
            json.loads(row["proposed_settings_json"])
        ),
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "consumed_at": row["consumed_at"],
    }


def consume_setting_edit(
    settings: Settings,
    *,
    token_fingerprint: str,
    telegram_id: int,
    status: str,
) -> bool:
    if status not in {"confirmed", "cancelled"}:
        raise ValueError("edit status must be confirmed or cancelled")
    now = _iso(_now())
    with _opportunity_connection(settings) as connection:
        cursor = connection.execute(
            """
            UPDATE opportunity_setting_edits
            SET status = ?, consumed_at = ?
            WHERE token_fingerprint = ? AND telegram_id = ?
              AND status = 'pending' AND expires_at > ?
            """,
            (status, now, token_fingerprint, telegram_id, now),
        )
        return cursor.rowcount == 1


def write_settings_audit(
    settings: Settings,
    *,
    telegram_id: int,
    from_revision: int,
    to_revision: int,
    changed_keys: Iterable[str],
    old_settings: OpportunityScannerSettings,
    new_settings: OpportunityScannerSettings,
) -> None:
    keys = tuple(sorted(set(changed_keys)))
    if not keys or any(key not in _AUDITABLE_SETTING_KEYS for key in keys):
        raise ValueError("audit contains an unsupported settings field")
    if (
        old_settings.revision != from_revision
        or new_settings.revision != to_revision
        or to_revision != from_revision + 1
    ):
        raise ValueError("audit revisions are inconsistent")
    old_payload = _settings_payload(old_settings)
    new_payload = _settings_payload(new_settings)
    sanitized = {
        "changed_keys": list(keys),
        "old": {key: old_payload[key] for key in keys},
        "new": {key: new_payload[key] for key in keys},
    }
    with _opportunity_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO opportunity_settings_audit (
                telegram_id, from_revision, to_revision,
                changed_keys_json, changes_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                from_revision,
                to_revision,
                json.dumps(keys, separators=(",", ":")),
                json.dumps(sanitized, sort_keys=True, separators=(",", ":")),
                _iso(_now()),
            ),
        )


@dataclass(frozen=True)
class MarketStateRecord:
    asset_id: str
    condition_id: str
    event_id: str
    market_id: str
    event_slug: str
    question: str
    outcome: str
    category_ids: tuple[str, ...]
    mode: ObservationMode
    active: bool
    end_at: datetime
    metadata_at: datetime
    baseline: OpportunityBaseline
    last_midpoint: Decimal | None
    last_score: Decimal | None
    score_version: int
    safety_policy_version: int
    below_threshold_streak: int
    was_eligible_at_baseline: bool
    last_direction: MovementDirection | None
    pending_live_direction: MovementDirection | None
    last_absolute_move_pp: Decimal | None
    last_signal_at: datetime | None
    identity_fingerprint: str | None
    baseline_reset_code: OpportunityDiagnosticCode | None


@dataclass(frozen=True)
class CycleAssetWrite:
    baseline: OpportunityBaseline
    condition_id: str
    event_id: str
    market_id: str
    event_slug: str
    question: str
    outcome: str
    category_ids: tuple[str, ...]
    metadata_at: datetime
    end_at: datetime
    active: bool
    observation: CanonicalObservation | None
    bucket_at: datetime | None
    canonical_score: Decimal | None
    score_version: int
    execution_metrics: tuple[ExecutionMetrics, ...]
    last_midpoint: Decimal | None
    last_score: Decimal | None
    safety_policy_version: int
    below_threshold_streak: int
    was_eligible_at_baseline: bool
    last_direction: MovementDirection | None
    pending_live_direction: MovementDirection | None
    last_absolute_move_pp: Decimal | None
    last_signal_at: datetime | None
    identity_fingerprint: str
    baseline_reset_code: OpportunityDiagnosticCode | None
    signals_to_expire: tuple[str, ...]
    signals_to_insert: tuple[OpportunitySignal, ...]


def upsert_market_state(
    settings: Settings,
    *,
    baseline: OpportunityBaseline,
    condition_id: str,
    event_id: str,
    market_id: str,
    event_slug: str,
    question: str,
    outcome: str,
    category_ids: Iterable[str],
    metadata_at: datetime,
    end_at: datetime,
    active: bool = True,
    last_midpoint: Decimal | None = None,
    last_score: Decimal | None = None,
    score_version: int = 1,
    safety_policy_version: int = 1,
    below_threshold_streak: int = 0,
    was_eligible_at_baseline: bool = False,
    last_direction: MovementDirection | None = None,
    pending_live_direction: MovementDirection | None = None,
    last_absolute_move_pp: Decimal | None = None,
    last_signal_at: datetime | None = None,
    identity_fingerprint: str | None = None,
    baseline_reset_code: OpportunityDiagnosticCode | None = None,
) -> None:
    with _opportunity_connection(settings) as connection:
        _upsert_market_state_on_connection(
            connection,
            baseline=baseline,
            condition_id=condition_id,
            event_id=event_id,
            market_id=market_id,
            event_slug=event_slug,
            question=question,
            outcome=outcome,
            category_ids=category_ids,
            metadata_at=metadata_at,
            end_at=end_at,
            active=active,
            last_midpoint=last_midpoint,
            last_score=last_score,
            score_version=score_version,
            safety_policy_version=safety_policy_version,
            below_threshold_streak=below_threshold_streak,
            was_eligible_at_baseline=was_eligible_at_baseline,
            last_direction=last_direction,
            pending_live_direction=pending_live_direction,
            last_absolute_move_pp=last_absolute_move_pp,
            last_signal_at=last_signal_at,
            identity_fingerprint=identity_fingerprint,
            baseline_reset_code=baseline_reset_code,
        )


def _upsert_market_state_on_connection(
    connection: sqlite3.Connection,
    *,
    baseline: OpportunityBaseline,
    condition_id: str,
    event_id: str,
    market_id: str,
    event_slug: str,
    question: str,
    outcome: str,
    category_ids: Iterable[str],
    metadata_at: datetime,
    end_at: datetime,
    active: bool = True,
    last_midpoint: Decimal | None = None,
    last_score: Decimal | None = None,
    score_version: int = 1,
    safety_policy_version: int = 1,
    below_threshold_streak: int = 0,
    was_eligible_at_baseline: bool = False,
    last_direction: MovementDirection | None = None,
    pending_live_direction: MovementDirection | None = None,
    last_absolute_move_pp: Decimal | None = None,
    last_signal_at: datetime | None = None,
    identity_fingerprint: str | None = None,
    baseline_reset_code: OpportunityDiagnosticCode | None = None,
) -> None:
    categories = tuple(sorted(set(category_ids)))
    if not categories:
        raise ValueError("market state requires categories")
    if below_threshold_streak not in (0, 1, 2):
        raise ValueError("below_threshold_streak must be 0, 1, or 2")
    now = _iso(_now())
    connection.execute(
        """
        INSERT INTO opportunity_market_state (
            asset_id, condition_id, event_id, market_id, event_slug,
            question, outcome, category_ids_json, mode, active,
            end_at, metadata_at, baseline_version, baseline_count,
            baseline_ready, last_complete_cycle_id, last_observed_at,
            last_midpoint, last_score, score_version, safety_policy_version,
            below_threshold_streak, was_eligible_at_baseline, last_direction,
            pending_live_direction, last_absolute_move_pp, last_signal_at,
            identity_fingerprint, baseline_reset_code, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(asset_id) DO UPDATE SET
            condition_id = excluded.condition_id,
            event_id = excluded.event_id,
            market_id = excluded.market_id,
            event_slug = excluded.event_slug,
            question = excluded.question,
            outcome = excluded.outcome,
            category_ids_json = excluded.category_ids_json,
            mode = excluded.mode,
            active = excluded.active,
            end_at = excluded.end_at,
            metadata_at = excluded.metadata_at,
            baseline_version = excluded.baseline_version,
            baseline_count = excluded.baseline_count,
            baseline_ready = excluded.baseline_ready,
            last_complete_cycle_id = excluded.last_complete_cycle_id,
            last_observed_at = excluded.last_observed_at,
            last_midpoint = excluded.last_midpoint,
            last_score = excluded.last_score,
            score_version = excluded.score_version,
            safety_policy_version = excluded.safety_policy_version,
            below_threshold_streak = excluded.below_threshold_streak,
            was_eligible_at_baseline = excluded.was_eligible_at_baseline,
            last_direction = excluded.last_direction,
            pending_live_direction = excluded.pending_live_direction,
            last_absolute_move_pp = excluded.last_absolute_move_pp,
            last_signal_at = excluded.last_signal_at,
            identity_fingerprint = excluded.identity_fingerprint,
            baseline_reset_code = excluded.baseline_reset_code,
            updated_at = excluded.updated_at
        """,
        (
            baseline.asset_id,
            condition_id,
            event_id,
            market_id,
            event_slug,
            question,
            outcome,
            json.dumps(categories, separators=(",", ":")),
            baseline.mode.value,
            int(active),
            _iso(end_at),
            _iso(metadata_at),
            baseline.baseline_version,
            baseline.consecutive_complete_cycles,
            int(baseline.ready),
            baseline.last_complete_cycle_id,
            (
                _iso(baseline.last_observed_at)
                if baseline.last_observed_at is not None
                else None
            ),
            decimal_text(last_midpoint) if last_midpoint is not None else None,
            decimal_text(last_score) if last_score is not None else None,
            score_version,
            safety_policy_version,
            below_threshold_streak,
            int(was_eligible_at_baseline),
            last_direction.value if last_direction is not None else None,
            (
                pending_live_direction.value
                if pending_live_direction is not None
                else None
            ),
            (
                decimal_text(last_absolute_move_pp)
                if last_absolute_move_pp is not None
                else None
            ),
            _iso(last_signal_at) if last_signal_at is not None else None,
            identity_fingerprint,
            baseline_reset_code.value if baseline_reset_code is not None else None,
            now,
            now,
        ),
    )


def save_signal(settings: Settings, signal: OpportunitySignal) -> None:
    with _opportunity_connection(settings) as connection:
        _insert_signal_on_connection(connection, signal)


def _insert_signal_on_connection(
    connection: sqlite3.Connection, signal: OpportunitySignal
) -> None:
    connection.execute(
        """
        INSERT INTO opportunity_signals (
            opportunity_uid, opportunity_key, asset_id, episode_sequence,
            material_change_sequence, family, direction, mode, score,
            reason_codes_json, signal_at, first_seen_at, last_seen_at,
            expires_at, source_observed_at, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal.opportunity_uid,
            signal.opportunity_key,
            signal.asset_id,
            signal.episode_sequence,
            signal.material_change_sequence,
            signal.family.value,
            signal.direction.value,
            signal.mode.value,
            decimal_text(signal.score),
            json.dumps(
                [code.value for code in signal.reason_codes],
                separators=(",", ":"),
            ),
            _iso(signal.signal_at),
            _iso(signal.first_seen_at),
            _iso(signal.last_seen_at),
            _iso(signal.expires_at),
            _iso(signal.source_observed_at),
            signal.status.value,
            _iso(_now()),
            _iso(_now()),
        ),
    )


def _expire_signal_on_connection(
    connection: sqlite3.Connection, opportunity_uid: str, *, expired_at: datetime
) -> None:
    connection.execute(
        """
        UPDATE opportunity_signals
        SET status = 'expired', expires_at = ?, updated_at = ?
        WHERE opportunity_uid = ? AND status = 'active'
        """,
        (_iso(expired_at), _iso(_now()), opportunity_uid),
    )


def save_observation_metrics(
    settings: Settings,
    *,
    observation: CanonicalObservation,
    bucket_at: datetime,
    canonical_score: Decimal,
    score_version: int,
    execution_metrics: Iterable[ExecutionMetrics],
) -> None:
    """Persist only derived metrics from one canonical observation."""
    with _opportunity_connection(settings) as connection:
        _save_observation_metrics_on_connection(
            connection,
            observation=observation,
            bucket_at=bucket_at,
            canonical_score=canonical_score,
            score_version=score_version,
            execution_metrics=execution_metrics,
        )


def _save_observation_metrics_on_connection(
    connection: sqlite3.Connection,
    *,
    observation: CanonicalObservation,
    bucket_at: datetime,
    canonical_score: Decimal,
    score_version: int,
    execution_metrics: Iterable[ExecutionMetrics],
) -> None:
    if bucket_at.tzinfo is None or bucket_at.utcoffset() is None:
        raise ValueError("bucket_at must be timezone-aware")
    if (
        not isinstance(canonical_score, Decimal)
        or not canonical_score.is_finite()
        or not Decimal("0") <= canonical_score <= Decimal("100")
    ):
        raise ValueError("canonical_score must be a Decimal between 0 and 100")
    if score_version < 1:
        raise ValueError("score_version must be positive")
    metrics = tuple(execution_metrics)
    if not metrics:
        raise ValueError("at least one execution metric is required")
    if observation.canonical_execution not in metrics:
        raise ValueError("canonical execution metric must be persisted")
    for metric in metrics:
        if (
            metric.asset_id != observation.asset_id
            or metric.observed_at != observation.observed_at
        ):
            raise ValueError("execution metric does not match observation")

    created_at = _iso(_now())
    connection.execute(
        """
        INSERT INTO opportunity_metric_buckets (
            asset_id, bucket_at, observed_at, mode, best_bid, best_ask,
            midpoint, absolute_spread, relative_spread, liquidity_usd,
            volume_24h_usd, move_5m, move_15m, move_1h,
            visible_bid_depth_usd, visible_ask_depth_usd,
            book_at, history_at, canonical_score, score_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset_id, bucket_at) DO UPDATE SET
            observed_at = excluded.observed_at,
            mode = excluded.mode,
            best_bid = excluded.best_bid,
            best_ask = excluded.best_ask,
            midpoint = excluded.midpoint,
            absolute_spread = excluded.absolute_spread,
            relative_spread = excluded.relative_spread,
            liquidity_usd = excluded.liquidity_usd,
            volume_24h_usd = excluded.volume_24h_usd,
            move_5m = excluded.move_5m,
            move_15m = excluded.move_15m,
            move_1h = excluded.move_1h,
            visible_bid_depth_usd = excluded.visible_bid_depth_usd,
            visible_ask_depth_usd = excluded.visible_ask_depth_usd,
            book_at = excluded.book_at,
            history_at = excluded.history_at,
            canonical_score = excluded.canonical_score,
            score_version = excluded.score_version
        """,
        (
            observation.asset_id,
            _iso(bucket_at),
            _iso(observation.observed_at),
            observation.mode.value,
            decimal_text(observation.best_bid),
            decimal_text(observation.best_ask),
            decimal_text(observation.midpoint),
            decimal_text(observation.absolute_spread),
            decimal_text(observation.relative_spread),
            decimal_text(observation.liquidity_usd),
            decimal_text(observation.volume_24h_usd),
            decimal_text(observation.move_5m),
            decimal_text(observation.move_15m),
            decimal_text(observation.move_1h),
            decimal_text(observation.visible_bid_depth_usd),
            decimal_text(observation.visible_ask_depth_usd),
            _iso(observation.book_at),
            _iso(observation.history_at),
            decimal_text(canonical_score),
            score_version,
        ),
    )
    for metric in metrics:
        connection.execute(
            """
            INSERT INTO opportunity_execution_metrics (
                asset_id, observed_at, allocation_usd, fully_executable,
                execution_vwap, slippage, quantity, consumed_ask_usd,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id, observed_at, allocation_usd) DO UPDATE SET
                fully_executable = excluded.fully_executable,
                execution_vwap = excluded.execution_vwap,
                slippage = excluded.slippage,
                quantity = excluded.quantity,
                consumed_ask_usd = excluded.consumed_ask_usd
            """,
            (
                metric.asset_id,
                _iso(metric.observed_at),
                decimal_text(metric.allocation_usd),
                int(metric.fully_executable),
                (
                    decimal_text(metric.execution_vwap)
                    if metric.execution_vwap is not None
                    else None
                ),
                (
                    decimal_text(metric.slippage)
                    if metric.slippage is not None
                    else None
                ),
                decimal_text(metric.quantity),
                decimal_text(metric.consumed_ask_usd),
                created_at,
            ),
        )


def save_scanner_state(
    settings: Settings, state: OpportunityScannerState
) -> None:
    with _opportunity_connection(settings) as connection:
        _save_scanner_state_on_connection(connection, state)


def _save_scanner_state_on_connection(
    connection: sqlite3.Connection, state: OpportunityScannerState
) -> None:
    connection.execute(
        """
        INSERT INTO opportunity_scanner_state (
            state_key, status, schema_version, baseline_version,
            score_version, global_baseline_cycles, discovery_complete,
            last_complete_discovery_at, last_complete_cycle_at,
            diagnostic_code, request_count, failure_count, updated_at
        ) VALUES ('scanner', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
        ON CONFLICT(state_key) DO UPDATE SET
            status = excluded.status,
            schema_version = excluded.schema_version,
            baseline_version = excluded.baseline_version,
            score_version = excluded.score_version,
            global_baseline_cycles = excluded.global_baseline_cycles,
            discovery_complete = excluded.discovery_complete,
            last_complete_discovery_at = excluded.last_complete_discovery_at,
            last_complete_cycle_at = excluded.last_complete_cycle_at,
            diagnostic_code = excluded.diagnostic_code,
            updated_at = excluded.updated_at
        """,
        (
            state.status.value,
            state.schema_version,
            state.baseline_version,
            state.score_version,
            state.global_baseline_cycles,
            int(state.discovery_complete),
            (
                _iso(state.last_complete_discovery_at)
                if state.last_complete_discovery_at
                else None
            ),
            (
                _iso(state.last_complete_cycle_at)
                if state.last_complete_cycle_at
                else None
            ),
            state.diagnostic_code.value if state.diagnostic_code else None,
            _iso(state.updated_at),
        ),
    )


def load_scanner_state(
    settings: Settings,
) -> OpportunityScannerState | None:
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT * FROM opportunity_scanner_state WHERE state_key = 'scanner'
            """
        ).fetchone()
    if row is None:
        return None
    return OpportunityScannerState(
        status=ScannerStatus(row["status"]),
        schema_version=int(row["schema_version"]),
        baseline_version=int(row["baseline_version"]),
        score_version=int(row["score_version"]),
        global_baseline_cycles=int(row["global_baseline_cycles"]),
        discovery_complete=bool(row["discovery_complete"]),
        last_complete_discovery_at=(
            datetime.fromisoformat(row["last_complete_discovery_at"])
            if row["last_complete_discovery_at"]
            else None
        ),
        last_complete_cycle_at=(
            datetime.fromisoformat(row["last_complete_cycle_at"])
            if row["last_complete_cycle_at"]
            else None
        ),
        diagnostic_code=(
            OpportunityDiagnosticCode(row["diagnostic_code"])
            if row["diagnostic_code"]
            else None
        ),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def claim_delivery(settings: Settings, delivery: OpportunityDelivery) -> bool:
    if delivery.status != DeliveryStatus.CLAIMED:
        raise ValueError("new delivery claim must use claimed status")
    with _opportunity_connection(settings) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO opportunity_deliveries (
                telegram_id, opportunity_uid, status, payload_text,
                signal_at, wave_at, settings_revision, claim_id,
                claimed_at, claim_expires_at, attempt_count, next_attempt_at,
                last_error_code, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery.telegram_id,
                delivery.opportunity_uid,
                delivery.status.value,
                delivery.payload_text,
                _iso(delivery.signal_at),
                _iso(delivery.wave_at),
                delivery.settings_revision,
                delivery.claim_id,
                _iso(delivery.claimed_at) if delivery.claimed_at else None,
                (
                    _iso(delivery.claim_expires_at)
                    if delivery.claim_expires_at
                    else None
                ),
                delivery.attempt_count,
                (
                    _iso(delivery.next_attempt_at)
                    if delivery.next_attempt_at
                    else None
                ),
                (
                    delivery.last_error_code.value
                    if delivery.last_error_code is not None
                    else None
                ),
                _iso(_now()),
                _iso(_now()),
            ),
        )
        return cursor.rowcount == 1


def get_delivery(
    settings: Settings, telegram_id: int, opportunity_uid: str
) -> sqlite3.Row | None:
    with _opportunity_connection(settings) as connection:
        return connection.execute(
            """
            SELECT * FROM opportunity_deliveries
            WHERE telegram_id = ? AND opportunity_uid = ?
            """,
            (telegram_id, opportunity_uid),
        ).fetchone()


def load_delivery(
    settings: Settings, telegram_id: int, opportunity_uid: str
) -> OpportunityDelivery | None:
    row = get_delivery(settings, telegram_id, opportunity_uid)
    if row is None:
        return None
    return _parse_delivery_row(row)


def _parse_delivery_row(row: sqlite3.Row) -> OpportunityDelivery:
    return OpportunityDelivery(
        telegram_id=int(row["telegram_id"]),
        opportunity_uid=row["opportunity_uid"],
        status=DeliveryStatus(row["status"]),
        payload_text=row["payload_text"],
        signal_at=datetime.fromisoformat(row["signal_at"]),
        wave_at=datetime.fromisoformat(row["wave_at"]),
        settings_revision=int(row["settings_revision"]),
        claim_id=row["claim_id"],
        claimed_at=(
            datetime.fromisoformat(row["claimed_at"]) if row["claimed_at"] else None
        ),
        claim_expires_at=(
            datetime.fromisoformat(row["claim_expires_at"])
            if row["claim_expires_at"]
            else None
        ),
        attempt_count=int(row["attempt_count"] or 0),
        next_attempt_at=(
            datetime.fromisoformat(row["next_attempt_at"])
            if row["next_attempt_at"]
            else None
        ),
        last_error_code=(
            OpportunityDiagnosticCode(row["last_error_code"])
            if row["last_error_code"]
            else None
        ),
    )


def load_delivery_gates(settings: Settings) -> "OpportunityDeliveryGates":
    from app.opportunity_intel.delivery_gates import OpportunityDeliveryGates

    with _opportunity_connection(settings) as connection:
        _ensure_delivery_foundations(connection)
        row = connection.execute(
            """
            SELECT admin_delivery_enabled, user_delivery_enabled
            FROM opportunity_delivery_gates
            WHERE gate_key = 'gates'
            """
        ).fetchone()
    if row is None:
        return OpportunityDeliveryGates()
    return OpportunityDeliveryGates(
        admin_delivery_enabled=bool(row["admin_delivery_enabled"]),
        user_delivery_enabled=bool(row["user_delivery_enabled"]),
    )


def save_delivery_gates(
    settings: Settings,
    gates: "OpportunityDeliveryGates",
) -> None:
    with _opportunity_connection(settings) as connection:
        _ensure_delivery_foundations(connection)
        connection.execute(
            """
            INSERT INTO opportunity_delivery_gates (
                gate_key, admin_delivery_enabled, user_delivery_enabled, updated_at
            ) VALUES ('gates', ?, ?, ?)
            ON CONFLICT(gate_key) DO UPDATE SET
                admin_delivery_enabled = excluded.admin_delivery_enabled,
                user_delivery_enabled = excluded.user_delivery_enabled,
                updated_at = excluded.updated_at
            """,
            (
                int(gates.admin_delivery_enabled),
                int(gates.user_delivery_enabled),
                _iso(_now()),
            ),
        )


def list_subscriptions(settings: Settings) -> tuple[OpportunitySubscription, ...]:
    with _opportunity_connection(settings) as connection:
        rows = connection.execute(
            "SELECT * FROM opportunity_subscriptions ORDER BY telegram_id"
        ).fetchall()
    return tuple(_subscription_from_row(row) for row in rows)


def _subscription_from_row(row: sqlite3.Row) -> OpportunitySubscription:
    categories = json.loads(row["category_ids_json"])
    quiet = (
        None
        if row["quiet_start"] is None
        else {
            "start": row["quiet_start"],
            "end": row["quiet_end"],
        }
    )
    scanner = _settings_from_payload(
        {
            "revision": row["revision"],
            "minimum_score": row["minimum_score"],
            "minimum_liquidity_usd": row["minimum_liquidity_usd"],
            "minimum_24h_volume_usd": row["minimum_24h_volume_usd"],
            "maximum_relative_spread": row["maximum_relative_spread"],
            "maximum_absolute_spread": row["maximum_absolute_spread"],
            "maximum_slippage": row["maximum_slippage"],
            "paper_allocation_usd": row["paper_allocation_usd"],
            "categories": {
                "all_categories": bool(row["all_categories"]),
                "category_ids": categories,
            },
            "live_mode": row["live_mode"],
            "cooldown_seconds": row["cooldown_seconds"],
            "alert_mode": row["alert_mode"],
            "quiet_hours": quiet,
            "timezone_name": row["timezone_name"],
        }
    )
    return OpportunitySubscription(
        telegram_id=int(row["telegram_id"]),
        enabled=bool(row["enabled"]),
        settings=scanner,
    )


def load_signal(
    settings: Settings, opportunity_uid: str
) -> OpportunitySignal | None:
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT * FROM opportunity_signals
            WHERE opportunity_uid = ?
            """,
            (opportunity_uid,),
        ).fetchone()
    if row is None:
        return None
    return _parse_signal_row(row)


@dataclass(frozen=True)
class PersistedObservationBundle:
    asset_id: str
    observed_at: datetime
    mode: ObservationMode
    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    absolute_spread: Decimal
    relative_spread: Decimal
    liquidity_usd: Decimal
    volume_24h_usd: Decimal
    move_5m: Decimal
    move_15m: Decimal
    move_1h: Decimal
    visible_bid_depth_usd: Decimal
    visible_ask_depth_usd: Decimal
    book_at: datetime
    history_at: datetime
    canonical_score: Decimal
    score_version: int
    execution_metrics: tuple[ExecutionMetrics, ...]
    condition_id: str
    event_id: str
    market_id: str
    event_slug: str
    question: str
    outcome: str
    category_ids: tuple[str, ...]
    end_at: datetime


def load_persisted_observation_bundle(
    settings: Settings,
    *,
    asset_id: str,
    observed_at: datetime,
) -> PersistedObservationBundle | None:
    """Load metric bucket + execution rows + market identity for one observation."""
    observed_iso = _iso(observed_at)
    with _opportunity_connection(settings) as connection:
        bucket = connection.execute(
            """
            SELECT * FROM opportunity_metric_buckets
            WHERE asset_id = ? AND observed_at = ?
            ORDER BY bucket_at DESC
            LIMIT 1
            """,
            (asset_id, observed_iso),
        ).fetchone()
        if bucket is None:
            return None
        market = connection.execute(
            "SELECT * FROM opportunity_market_state WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        if market is None:
            return None
        metric_rows = connection.execute(
            """
            SELECT * FROM opportunity_execution_metrics
            WHERE asset_id = ? AND observed_at = ?
            ORDER BY CAST(allocation_usd AS REAL)
            """,
            (asset_id, observed_iso),
        ).fetchall()
    metrics = tuple(
        ExecutionMetrics(
            asset_id=row["asset_id"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            allocation_usd=Decimal(row["allocation_usd"]),
            fully_executable=bool(row["fully_executable"]),
            execution_vwap=(
                Decimal(row["execution_vwap"]) if row["execution_vwap"] else None
            ),
            slippage=Decimal(row["slippage"]) if row["slippage"] else None,
            quantity=Decimal(row["quantity"]),
            consumed_ask_usd=Decimal(row["consumed_ask_usd"]),
        )
        for row in metric_rows
    )
    return PersistedObservationBundle(
        asset_id=bucket["asset_id"],
        observed_at=datetime.fromisoformat(bucket["observed_at"]),
        mode=ObservationMode(bucket["mode"]),
        best_bid=Decimal(bucket["best_bid"]),
        best_ask=Decimal(bucket["best_ask"]),
        midpoint=Decimal(bucket["midpoint"]),
        absolute_spread=Decimal(bucket["absolute_spread"]),
        relative_spread=Decimal(bucket["relative_spread"]),
        liquidity_usd=Decimal(bucket["liquidity_usd"]),
        volume_24h_usd=Decimal(bucket["volume_24h_usd"]),
        move_5m=Decimal(bucket["move_5m"]),
        move_15m=Decimal(bucket["move_15m"]),
        move_1h=Decimal(bucket["move_1h"]),
        visible_bid_depth_usd=Decimal(bucket["visible_bid_depth_usd"]),
        visible_ask_depth_usd=Decimal(bucket["visible_ask_depth_usd"]),
        book_at=datetime.fromisoformat(bucket["book_at"]),
        history_at=datetime.fromisoformat(bucket["history_at"]),
        canonical_score=Decimal(bucket["canonical_score"]),
        score_version=int(bucket["score_version"]),
        execution_metrics=metrics,
        condition_id=market["condition_id"],
        event_id=market["event_id"],
        market_id=market["market_id"],
        event_slug=market["event_slug"],
        question=market["question"],
        outcome=market["outcome"],
        category_ids=tuple(json.loads(market["category_ids_json"])),
        end_at=datetime.fromisoformat(market["end_at"]),
    )


def last_sent_at_for_opportunity_key(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_key: str,
) -> datetime | None:
    """Return the latest SENT delivery time for this recipient/family key."""
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT d.updated_at
            FROM opportunity_deliveries d
            INNER JOIN opportunity_signals s
                ON s.opportunity_uid = d.opportunity_uid
            WHERE d.telegram_id = ?
              AND d.status = 'sent'
              AND s.opportunity_key = ?
            ORDER BY d.updated_at DESC
            LIMIT 1
            """,
            (telegram_id, opportunity_key),
        ).fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(row["updated_at"])


def insert_delivery_wave(
    settings: Settings,
    deliveries: Iterable[OpportunityDelivery],
) -> tuple[int, int]:
    """Insert wave rows idempotently. Returns (inserted_count, ignored_count)."""
    rows = tuple(deliveries)
    inserted = 0
    ignored = 0
    with _opportunity_connection(settings) as connection:
        for delivery in rows:
            if delivery.status not in {
                DeliveryStatus.PENDING,
                DeliveryStatus.SUPPRESSED,
            }:
                raise ValueError("wave insert accepts only pending or suppressed rows")
            if delivery.status == DeliveryStatus.PENDING and (
                delivery.claim_id is not None
                or delivery.claimed_at is not None
                or delivery.claim_expires_at is not None
            ):
                raise ValueError("pending wave rows must not carry claim state")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO opportunity_deliveries (
                    telegram_id, opportunity_uid, status, payload_text,
                    signal_at, wave_at, settings_revision, claim_id,
                    claimed_at, claim_expires_at, attempt_count, next_attempt_at,
                    last_error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    delivery.telegram_id,
                    delivery.opportunity_uid,
                    delivery.status.value,
                    delivery.payload_text,
                    _iso(delivery.signal_at),
                    _iso(delivery.wave_at),
                    delivery.settings_revision,
                    delivery.attempt_count,
                    (
                        _iso(delivery.next_attempt_at)
                        if delivery.next_attempt_at
                        else None
                    ),
                    (
                        delivery.last_error_code.value
                        if delivery.last_error_code is not None
                        else None
                    ),
                    _iso(_now()),
                    _iso(_now()),
                ),
            )
            if cursor.rowcount == 1:
                inserted += 1
            else:
                ignored += 1
    return inserted, ignored


def claim_delivery_lease(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_uid: str,
    claim_id: str,
    now: datetime,
    lease_seconds: int = 120,
    max_attempts: int = 3,
) -> OpportunityDelivery | None:
    """Atomically claim a pending/due/expired delivery for send preparation."""
    now_utc = now.astimezone(timezone.utc)
    now_iso = _iso(now_utc)
    expires_iso = _iso(now_utc + timedelta(seconds=lease_seconds))
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT * FROM opportunity_deliveries
            WHERE telegram_id = ? AND opportunity_uid = ?
            """,
            (telegram_id, opportunity_uid),
        ).fetchone()
        if row is None:
            return None
        status = DeliveryStatus(row["status"])
        attempts = int(row["attempt_count"] or 0)
        if status in {
            DeliveryStatus.SENT,
            DeliveryStatus.DEAD_LETTER,
            DeliveryStatus.SUPPRESSED,
        }:
            return None
        if status == DeliveryStatus.CLAIMED:
            expires = (
                datetime.fromisoformat(row["claim_expires_at"])
                if row["claim_expires_at"]
                else None
            )
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires is None or expires > now_utc:
                return None
            new_attempts = attempts
        elif status == DeliveryStatus.PENDING:
            if attempts >= max_attempts:
                return None
            new_attempts = attempts + 1
        elif status in {DeliveryStatus.RETRY_WAIT, DeliveryStatus.FAILED}:
            if attempts >= max_attempts:
                return None
            next_at = (
                datetime.fromisoformat(row["next_attempt_at"])
                if row["next_attempt_at"]
                else None
            )
            if next_at is not None:
                if next_at.tzinfo is None:
                    next_at = next_at.replace(tzinfo=timezone.utc)
                if next_at > now_utc:
                    return None
            new_attempts = attempts + 1
        else:
            return None

        cursor = connection.execute(
            """
            UPDATE opportunity_deliveries
            SET status = 'claimed',
                claim_id = ?,
                claimed_at = ?,
                claim_expires_at = ?,
                attempt_count = ?,
                next_attempt_at = NULL,
                updated_at = ?
            WHERE telegram_id = ? AND opportunity_uid = ?
              AND status = ?
              AND attempt_count = ?
              AND (
                    (? = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?)
                 OR (? = 'pending')
                 OR (
                        ? IN ('retry_wait', 'failed')
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                 )
              )
            """,
            (
                claim_id,
                now_iso,
                expires_iso,
                new_attempts,
                now_iso,
                telegram_id,
                opportunity_uid,
                status.value,
                attempts,
                status.value,
                now_iso,
                status.value,
                status.value,
                now_iso,
            ),
        )
        if cursor.rowcount != 1:
            return None
        updated = connection.execute(
            """
            SELECT * FROM opportunity_deliveries
            WHERE telegram_id = ? AND opportunity_uid = ?
            """,
            (telegram_id, opportunity_uid),
        ).fetchone()
    return _parse_delivery_row(updated)


def mark_delivery_terminal(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_uid: str,
    claim_id: str,
    status: DeliveryStatus,
    error_code: OpportunityDiagnosticCode | None = None,
    next_attempt_at: datetime | None = None,
) -> bool:
    """Transition a claimed row to a terminal or retry_wait state (idempotent)."""
    if status not in {
        DeliveryStatus.SENT,
        DeliveryStatus.SUPPRESSED,
        DeliveryStatus.DEAD_LETTER,
        DeliveryStatus.RETRY_WAIT,
    }:
        raise ValueError("unsupported terminal delivery status")
    now_iso = _iso(_now())
    with _opportunity_connection(settings) as connection:
        existing = connection.execute(
            """
            SELECT status, claim_id FROM opportunity_deliveries
            WHERE telegram_id = ? AND opportunity_uid = ?
            """,
            (telegram_id, opportunity_uid),
        ).fetchone()
        if existing is None:
            return False
        current = DeliveryStatus(existing["status"])
        if current == status:
            return True
        if current in {
            DeliveryStatus.SENT,
            DeliveryStatus.DEAD_LETTER,
            DeliveryStatus.SUPPRESSED,
        }:
            return False
        if current != DeliveryStatus.CLAIMED or existing["claim_id"] != claim_id:
            return False
        cursor = connection.execute(
            """
            UPDATE opportunity_deliveries
            SET status = ?,
                last_error_code = ?,
                next_attempt_at = ?,
                claim_id = CASE WHEN ? IN ('sent', 'suppressed', 'dead_letter')
                    THEN NULL ELSE claim_id END,
                claimed_at = CASE WHEN ? IN ('sent', 'suppressed', 'dead_letter')
                    THEN NULL ELSE claimed_at END,
                claim_expires_at = CASE WHEN ? IN ('sent', 'suppressed', 'dead_letter')
                    THEN NULL ELSE claim_expires_at END,
                updated_at = ?
            WHERE telegram_id = ? AND opportunity_uid = ?
              AND status = 'claimed'
              AND claim_id = ?
            """,
            (
                status.value,
                error_code.value if error_code is not None else None,
                _iso(next_attempt_at) if next_attempt_at is not None else None,
                status.value,
                status.value,
                status.value,
                now_iso,
                telegram_id,
                opportunity_uid,
                claim_id,
            ),
        )
        return cursor.rowcount == 1


def list_deliveries_for_opportunity(
    settings: Settings, opportunity_uid: str
) -> tuple[OpportunityDelivery, ...]:
    with _opportunity_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM opportunity_deliveries
            WHERE opportunity_uid = ?
            ORDER BY telegram_id
            """,
            (opportunity_uid,),
        ).fetchall()
    return tuple(_parse_delivery_row(row) for row in rows)


def list_due_delivery_retries(
    settings: Settings,
    *,
    now: datetime,
    limit: int = 50,
    max_attempts: int = 3,
) -> tuple[OpportunityDelivery, ...]:
    now_iso = _iso(now.astimezone(timezone.utc))
    with _opportunity_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM opportunity_deliveries
            WHERE status IN ('retry_wait', 'failed')
              AND attempt_count < ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (max_attempts, now_iso, limit),
        ).fetchall()
    return tuple(_parse_delivery_row(row) for row in rows)


def list_expired_claims(
    settings: Settings,
    *,
    now: datetime,
    limit: int = 50,
) -> tuple[OpportunityDelivery, ...]:
    now_iso = _iso(now.astimezone(timezone.utc))
    with _opportunity_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM opportunity_deliveries
            WHERE status = 'claimed'
              AND claim_expires_at IS NOT NULL
              AND claim_expires_at <= ?
            ORDER BY claim_expires_at ASC
            LIMIT ?
            """,
            (now_iso, limit),
        ).fetchall()
    return tuple(_parse_delivery_row(row) for row in rows)


def list_pending_deliveries(
    settings: Settings,
    *,
    limit: int = 50,
    max_attempts: int = 3,
) -> tuple[OpportunityDelivery, ...]:
    with _opportunity_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM opportunity_deliveries
            WHERE status = 'pending'
              AND attempt_count < ?
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (max_attempts, limit),
        ).fetchall()
    return tuple(_parse_delivery_row(row) for row in rows)


def list_delivery_worker_batch(
    settings: Settings,
    *,
    now: datetime,
    limit: int = 20,
    max_attempts: int = 3,
) -> tuple[OpportunityDelivery, ...]:
    """Bounded PENDING + due RETRY_WAIT + expired CLAIMED rows without duplicates."""
    if limit < 1:
        raise ValueError("limit must be positive")
    remaining = int(limit)
    selected: list[OpportunityDelivery] = []
    seen: set[tuple[int, str]] = set()

    def _extend(rows: tuple[OpportunityDelivery, ...]) -> None:
        nonlocal remaining
        for row in rows:
            if remaining <= 0:
                return
            key = (row.telegram_id, row.opportunity_uid)
            if key in seen:
                continue
            seen.add(key)
            selected.append(row)
            remaining -= 1

    _extend(list_pending_deliveries(settings, limit=remaining, max_attempts=max_attempts))
    if remaining > 0:
        _extend(
            list_due_delivery_retries(
                settings, now=now, limit=remaining, max_attempts=max_attempts
            )
        )
    if remaining > 0:
        _extend(list_expired_claims(settings, now=now, limit=remaining))
    return tuple(selected)


def count_deliveries_by_status(settings: Settings) -> dict[str, int]:
    with _opportunity_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM opportunity_deliveries
            GROUP BY status
            """
        ).fetchall()
    return {str(row["status"]): int(row["n"]) for row in rows}


def save_runtime_snapshot(settings: Settings, payload: dict[str, Any]) -> None:
    """Persist sanitized runtime counters for health/status (no secrets/paths)."""
    with _opportunity_connection(settings) as connection:
        _ensure_delivery_foundations(connection)
        connection.execute(
            """
            INSERT INTO opportunity_runtime_status (
                status_key, payload_json, updated_at
            ) VALUES ('runtime', ?, ?)
            ON CONFLICT(status_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                _iso(_now()),
            ),
        )


def load_runtime_snapshot(settings: Settings) -> dict[str, Any] | None:
    with _opportunity_connection(settings) as connection:
        _ensure_delivery_foundations(connection)
        row = connection.execute(
            """
            SELECT payload_json FROM opportunity_runtime_status
            WHERE status_key = 'runtime'
            """
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        return None
    return payload


def load_active_discovery_session(settings: Settings):
    """Load normalized in-progress discovery session, or None."""
    from app.opportunity_intel.discovery_session import (
        DiscoverySession,
        session_from_normalized_dict,
    )

    apply_resumable_discovery_migration(settings)
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT * FROM opportunity_discovery_sessions
            WHERE slot_key = 'active'
            """
        ).fetchone()
    if row is None:
        return None
    payload = {
        "session_id": row["session_id"],
        "version": int(row["session_version"]),
        "state": row["state"],
        "started_at": row["started_at"],
        "expires_at": row["expires_at"],
        "next_cursor": row["next_cursor"],
        "seen_cursors": json.loads(row["seen_cursors_json"]),
        "pages_completed": int(row["pages_completed"]),
        "attempts_used": int(row["attempts_used"]),
        "markets": json.loads(row["markets_json"]),
        "seen_event_ids": json.loads(row["seen_event_ids_json"]),
        "seen_market_ids": json.loads(row["seen_market_ids_json"]),
        "seen_condition_ids": json.loads(row["seen_condition_ids_json"]),
        "seen_asset_ids": json.loads(row["seen_asset_ids_json"]),
        "diagnostics": json.loads(row["diagnostics_json"]),
        "updated_at": row["updated_at"],
    }
    session = session_from_normalized_dict(payload)
    assert isinstance(session, DiscoverySession)
    return session


def save_discovery_session(settings: Settings, session) -> None:
    from app.opportunity_intel.discovery_session import session_to_normalized_dict

    apply_resumable_discovery_migration(settings)
    payload = session_to_normalized_dict(session)
    with _opportunity_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO opportunity_discovery_sessions (
                slot_key, session_id, session_version, state,
                started_at, expires_at, updated_at, next_cursor,
                seen_cursors_json, pages_completed, attempts_used,
                markets_json, seen_event_ids_json, seen_market_ids_json,
                seen_condition_ids_json, seen_asset_ids_json, diagnostics_json
            ) VALUES (
                'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(slot_key) DO UPDATE SET
                session_id = excluded.session_id,
                session_version = excluded.session_version,
                state = excluded.state,
                started_at = excluded.started_at,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at,
                next_cursor = excluded.next_cursor,
                seen_cursors_json = excluded.seen_cursors_json,
                pages_completed = excluded.pages_completed,
                attempts_used = excluded.attempts_used,
                markets_json = excluded.markets_json,
                seen_event_ids_json = excluded.seen_event_ids_json,
                seen_market_ids_json = excluded.seen_market_ids_json,
                seen_condition_ids_json = excluded.seen_condition_ids_json,
                seen_asset_ids_json = excluded.seen_asset_ids_json,
                diagnostics_json = excluded.diagnostics_json
            """,
            (
                payload["session_id"],
                int(payload["version"]),
                payload["state"],
                payload["started_at"],
                payload["expires_at"],
                payload["updated_at"],
                payload["next_cursor"],
                json.dumps(payload["seen_cursors"], separators=(",", ":")),
                int(payload["pages_completed"]),
                int(payload["attempts_used"]),
                json.dumps(payload["markets"], separators=(",", ":")),
                json.dumps(payload["seen_event_ids"], separators=(",", ":")),
                json.dumps(payload["seen_market_ids"], separators=(",", ":")),
                json.dumps(payload["seen_condition_ids"], separators=(",", ":")),
                json.dumps(payload["seen_asset_ids"], separators=(",", ":")),
                json.dumps(payload["diagnostics"], separators=(",", ":")),
            ),
        )


def clear_discovery_session(settings: Settings, session_id: str | None = None) -> None:
    apply_resumable_discovery_migration(settings)
    with _opportunity_connection(settings) as connection:
        if session_id is None:
            connection.execute("DELETE FROM opportunity_discovery_sessions")
            return
        connection.execute(
            """
            DELETE FROM opportunity_discovery_sessions
            WHERE slot_key = 'active' AND session_id = ?
            """,
            (session_id,),
        )


@dataclass(frozen=True)
class DiscoveryProgress:
    """Sanitized discovery-session progress. Never exposes cursors or ids."""

    state: str
    pages_completed: int
    attempts_used: int
    markets_accumulated: int
    resumable: bool
    started_at: datetime | None
    updated_at: datetime | None
    expires_at: datetime | None


def load_discovery_progress(settings: Settings) -> DiscoveryProgress | None:
    """Light progress read for status UX; skips diagnostics and cursors."""
    apply_resumable_discovery_migration(settings)
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT state, pages_completed, attempts_used, markets_json,
                   next_cursor, started_at, updated_at, expires_at
            FROM opportunity_discovery_sessions
            WHERE slot_key = 'active'
            """
        ).fetchone()
    if row is None:
        return None
    try:
        markets = json.loads(row["markets_json"])
    except (TypeError, ValueError):
        markets = []
    return DiscoveryProgress(
        state=str(row["state"]),
        pages_completed=int(row["pages_completed"]),
        attempts_used=int(row["attempts_used"]),
        markets_accumulated=len(markets) if isinstance(markets, list) else 0,
        resumable=row["next_cursor"] is not None,
        started_at=_optional_dt(row["started_at"]),
        updated_at=_optional_dt(row["updated_at"]),
        expires_at=_optional_dt(row["expires_at"]),
    )


def count_baseline_ready_assets(settings: Settings) -> int:
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS n FROM opportunity_market_state
            WHERE baseline_ready = 1
            """
        ).fetchone()
    return int(row["n"] if row is not None else 0)


def count_market_states(settings: Settings) -> int:
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM opportunity_market_state"
        ).fetchone()
    return int(row["n"] if row is not None else 0)


def _optional_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class SqliteDiscoverySessionStore:
    """Restart-safe discovery session adapter (normalized metadata only)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def load_active_session(self):
        return load_active_discovery_session(self._settings)

    def save_session(self, session) -> None:
        save_discovery_session(self._settings, session)

    def clear_session(self, session_id: str | None = None) -> None:
        clear_discovery_session(self._settings, session_id)


def _parse_market_row(row: sqlite3.Row) -> MarketStateRecord:
    categories = tuple(json.loads(row["category_ids_json"]))
    last_direction = (
        MovementDirection(row["last_direction"])
        if row["last_direction"]
        else None
    )
    pending = (
        MovementDirection(row["pending_live_direction"])
        if row["pending_live_direction"]
        else None
    )
    reset_code = (
        OpportunityDiagnosticCode(row["baseline_reset_code"])
        if row["baseline_reset_code"]
        else None
    )
    return MarketStateRecord(
        asset_id=row["asset_id"],
        condition_id=row["condition_id"],
        event_id=row["event_id"],
        market_id=row["market_id"],
        event_slug=row["event_slug"],
        question=row["question"],
        outcome=row["outcome"],
        category_ids=categories,
        mode=ObservationMode(row["mode"]),
        active=bool(row["active"]),
        end_at=datetime.fromisoformat(row["end_at"]),
        metadata_at=datetime.fromisoformat(row["metadata_at"]),
        baseline=OpportunityBaseline(
            asset_id=row["asset_id"],
            mode=ObservationMode(row["mode"]),
            baseline_version=int(row["baseline_version"]),
            consecutive_complete_cycles=int(row["baseline_count"]),
            last_complete_cycle_id=row["last_complete_cycle_id"],
            last_observed_at=(
                datetime.fromisoformat(row["last_observed_at"])
                if row["last_observed_at"]
                else None
            ),
            ready=bool(row["baseline_ready"]),
        ),
        last_midpoint=(
            Decimal(row["last_midpoint"]) if row["last_midpoint"] else None
        ),
        last_score=Decimal(row["last_score"]) if row["last_score"] else None,
        score_version=int(row["score_version"] or 1),
        safety_policy_version=int(row["safety_policy_version"] or 1),
        below_threshold_streak=int(row["below_threshold_streak"] or 0),
        was_eligible_at_baseline=bool(row["was_eligible_at_baseline"] or 0),
        last_direction=last_direction,
        pending_live_direction=pending,
        last_absolute_move_pp=(
            Decimal(row["last_absolute_move_pp"])
            if row["last_absolute_move_pp"]
            else None
        ),
        last_signal_at=(
            datetime.fromisoformat(row["last_signal_at"])
            if row["last_signal_at"]
            else None
        ),
        identity_fingerprint=row["identity_fingerprint"],
        baseline_reset_code=reset_code,
    )


def load_market_state(
    settings: Settings, asset_id: str
) -> MarketStateRecord | None:
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            "SELECT * FROM opportunity_market_state WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
    if row is None:
        return None
    return _parse_market_row(row)


def load_all_market_states(settings: Settings) -> tuple[MarketStateRecord, ...]:
    with _opportunity_connection(settings) as connection:
        rows = connection.execute(
            "SELECT * FROM opportunity_market_state ORDER BY asset_id"
        ).fetchall()
    return tuple(_parse_market_row(row) for row in rows)


def _parse_signal_row(row: sqlite3.Row) -> OpportunitySignal:
    codes = tuple(
        OpportunityReasonCode(code) for code in json.loads(row["reason_codes_json"])
    )
    return OpportunitySignal(
        opportunity_uid=row["opportunity_uid"],
        opportunity_key=row["opportunity_key"],
        asset_id=row["asset_id"],
        episode_sequence=int(row["episode_sequence"]),
        material_change_sequence=int(row["material_change_sequence"]),
        family=SignalFamily(row["family"]),
        direction=MovementDirection(row["direction"]),
        mode=ObservationMode(row["mode"]),
        score=Decimal(row["score"]),
        reason_codes=codes,
        signal_at=datetime.fromisoformat(row["signal_at"]),
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        source_observed_at=datetime.fromisoformat(row["source_observed_at"]),
        status=SignalStatus(row["status"]),
    )


def load_active_signals_for_asset(
    settings: Settings, asset_id: str
) -> tuple[OpportunitySignal, ...]:
    with _opportunity_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM opportunity_signals
            WHERE asset_id = ? AND status = 'active'
            ORDER BY episode_sequence DESC, material_change_sequence DESC
            """,
            (asset_id,),
        ).fetchall()
    return tuple(_parse_signal_row(row) for row in rows)


def list_active_signals(
    settings: Settings, *, limit: int = 50
) -> tuple[OpportunitySignal, ...]:
    if limit < 1:
        raise ValueError("limit must be positive")
    with _opportunity_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM opportunity_signals
            WHERE status = 'active'
            ORDER BY CAST(score AS REAL) DESC, signal_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return tuple(_parse_signal_row(row) for row in rows)


def list_recent_signals(
    settings: Settings, *, limit: int = 10
) -> tuple[OpportunitySignal, ...]:
    if limit < 1:
        raise ValueError("limit must be positive")
    with _opportunity_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM opportunity_signals
            ORDER BY signal_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return tuple(_parse_signal_row(row) for row in rows)


def count_active_signals(settings: Settings) -> int:
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS n FROM opportunity_signals WHERE status = 'active'
            """
        ).fetchone()
    return int(row["n"] if row is not None else 0)


def load_active_signal_by_key(
    settings: Settings, opportunity_key: str
) -> OpportunitySignal | None:
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT * FROM opportunity_signals
            WHERE opportunity_key = ? AND status = 'active'
            ORDER BY episode_sequence DESC, material_change_sequence DESC
            LIMIT 1
            """,
            (opportunity_key,),
        ).fetchone()
    if row is None:
        return None
    return _parse_signal_row(row)


def next_episode_sequence(settings: Settings, opportunity_key: str) -> int:
    with _opportunity_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(episode_sequence), 0) AS max_episode
            FROM opportunity_signals
            WHERE opportunity_key = ?
            """,
            (opportunity_key,),
        ).fetchone()
    return int(row["max_episode"]) + 1


def commit_scanner_cycle(
    settings: Settings,
    *,
    asset_writes: Iterable[CycleAssetWrite],
    scanner_state: OpportunityScannerState,
    now: datetime,
) -> None:
    """Atomically persist one scanner cycle write set.

    Callers must complete all HTTP work before invoking this function.
    """
    writes = tuple(asset_writes)
    with _opportunity_connection(settings) as connection:
        for write in writes:
            _upsert_market_state_on_connection(
                connection,
                baseline=write.baseline,
                condition_id=write.condition_id,
                event_id=write.event_id,
                market_id=write.market_id,
                event_slug=write.event_slug,
                question=write.question,
                outcome=write.outcome,
                category_ids=write.category_ids,
                metadata_at=write.metadata_at,
                end_at=write.end_at,
                active=write.active,
                last_midpoint=write.last_midpoint,
                last_score=write.last_score,
                score_version=write.score_version,
                safety_policy_version=write.safety_policy_version,
                below_threshold_streak=write.below_threshold_streak,
                was_eligible_at_baseline=write.was_eligible_at_baseline,
                last_direction=write.last_direction,
                pending_live_direction=write.pending_live_direction,
                last_absolute_move_pp=write.last_absolute_move_pp,
                last_signal_at=write.last_signal_at,
                identity_fingerprint=write.identity_fingerprint,
                baseline_reset_code=write.baseline_reset_code,
            )
            if (
                write.observation is not None
                and write.bucket_at is not None
                and write.canonical_score is not None
                and write.execution_metrics
            ):
                _save_observation_metrics_on_connection(
                    connection,
                    observation=write.observation,
                    bucket_at=write.bucket_at,
                    canonical_score=write.canonical_score,
                    score_version=write.score_version,
                    execution_metrics=write.execution_metrics,
                )
            for uid in write.signals_to_expire:
                _expire_signal_on_connection(connection, uid, expired_at=now)
            for signal in write.signals_to_insert:
                _insert_signal_on_connection(connection, signal)
        _save_scanner_state_on_connection(connection, scanner_state)


_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS opportunity_market_state (
        asset_id TEXT PRIMARY KEY,
        condition_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        market_id TEXT NOT NULL,
        event_slug TEXT NOT NULL,
        question TEXT NOT NULL,
        outcome TEXT NOT NULL,
        category_ids_json TEXT NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('NORMAL', 'LIVE_STRICT')),
        active INTEGER NOT NULL CHECK(active IN (0, 1)),
        end_at TEXT NOT NULL,
        metadata_at TEXT NOT NULL,
        baseline_version INTEGER NOT NULL CHECK(baseline_version >= 1),
        baseline_count INTEGER NOT NULL CHECK(baseline_count BETWEEN 0 AND 2),
        baseline_ready INTEGER NOT NULL CHECK(baseline_ready IN (0, 1)),
        last_complete_cycle_id TEXT,
        last_observed_at TEXT,
        last_midpoint TEXT,
        last_score TEXT,
        score_version INTEGER NOT NULL DEFAULT 1 CHECK(score_version >= 1),
        safety_policy_version INTEGER NOT NULL DEFAULT 1
            CHECK(safety_policy_version >= 1),
        below_threshold_streak INTEGER NOT NULL DEFAULT 0
            CHECK(below_threshold_streak BETWEEN 0 AND 2),
        was_eligible_at_baseline INTEGER NOT NULL DEFAULT 0
            CHECK(was_eligible_at_baseline IN (0, 1)),
        last_direction TEXT CHECK(
            last_direction IS NULL OR last_direction IN ('UP', 'DOWN')
        ),
        pending_live_direction TEXT CHECK(
            pending_live_direction IS NULL
            OR pending_live_direction IN ('UP', 'DOWN')
        ),
        last_absolute_move_pp TEXT,
        last_signal_at TEXT,
        identity_fingerprint TEXT,
        baseline_reset_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(
            (baseline_count = 2 AND baseline_ready = 1)
            OR (baseline_count < 2 AND baseline_ready = 0)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_metric_buckets (
        asset_id TEXT NOT NULL,
        bucket_at TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('NORMAL', 'LIVE_STRICT')),
        best_bid TEXT NOT NULL,
        best_ask TEXT NOT NULL,
        midpoint TEXT NOT NULL,
        absolute_spread TEXT NOT NULL,
        relative_spread TEXT NOT NULL,
        liquidity_usd TEXT NOT NULL,
        volume_24h_usd TEXT NOT NULL,
        move_5m TEXT NOT NULL,
        move_15m TEXT NOT NULL,
        move_1h TEXT NOT NULL,
        visible_bid_depth_usd TEXT NOT NULL,
        visible_ask_depth_usd TEXT NOT NULL,
        book_at TEXT NOT NULL,
        history_at TEXT NOT NULL,
        canonical_score TEXT NOT NULL,
        score_version INTEGER NOT NULL CHECK(score_version >= 1),
        PRIMARY KEY(asset_id, bucket_at),
        FOREIGN KEY(asset_id) REFERENCES opportunity_market_state(asset_id)
            ON DELETE RESTRICT,
        CHECK(CAST(best_bid AS REAL) < CAST(best_ask AS REAL)),
        CHECK(CAST(midpoint AS REAL) BETWEEN 0 AND 1),
        CHECK(CAST(canonical_score AS REAL) BETWEEN 0 AND 100)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_execution_metrics (
        asset_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        allocation_usd TEXT NOT NULL,
        fully_executable INTEGER NOT NULL CHECK(fully_executable IN (0, 1)),
        execution_vwap TEXT,
        slippage TEXT,
        quantity TEXT NOT NULL,
        consumed_ask_usd TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(asset_id, observed_at, allocation_usd),
        FOREIGN KEY(asset_id) REFERENCES opportunity_market_state(asset_id)
            ON DELETE RESTRICT,
        CHECK(allocation_usd IN ('25', '50', '100', '250', '500', '1000')),
        CHECK(
            fully_executable = 0
            OR (execution_vwap IS NOT NULL AND slippage IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_signals (
        opportunity_uid TEXT PRIMARY KEY,
        opportunity_key TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        episode_sequence INTEGER NOT NULL CHECK(episode_sequence >= 1),
        material_change_sequence INTEGER NOT NULL
            CHECK(material_change_sequence >= 0),
        family TEXT NOT NULL CHECK(family = 'EXECUTABLE_MOMENTUM'),
        direction TEXT NOT NULL CHECK(direction IN ('UP', 'DOWN')),
        mode TEXT NOT NULL CHECK(mode IN ('NORMAL', 'LIVE_STRICT')),
        score TEXT NOT NULL,
        reason_codes_json TEXT NOT NULL,
        signal_at TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        source_observed_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active', 'expired')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(opportunity_key, episode_sequence, material_change_sequence),
        FOREIGN KEY(asset_id) REFERENCES opportunity_market_state(asset_id)
            ON DELETE RESTRICT,
        CHECK(CAST(score AS REAL) BETWEEN 0 AND 100)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_subscriptions (
        telegram_id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
        minimum_score INTEGER NOT NULL CHECK(minimum_score BETWEEN 50 AND 95),
        minimum_liquidity_usd TEXT NOT NULL,
        minimum_24h_volume_usd TEXT NOT NULL,
        maximum_relative_spread TEXT NOT NULL,
        maximum_absolute_spread TEXT NOT NULL,
        maximum_slippage TEXT NOT NULL,
        paper_allocation_usd TEXT NOT NULL CHECK(
            paper_allocation_usd IN ('25', '50', '100', '250', '500', '1000')
        ),
        all_categories INTEGER NOT NULL CHECK(all_categories IN (0, 1)),
        category_ids_json TEXT NOT NULL,
        live_mode TEXT NOT NULL CHECK(live_mode IN ('OFF', 'STRICT')),
        cooldown_seconds INTEGER NOT NULL
            CHECK(cooldown_seconds BETWEEN 900 AND 86400),
        alert_mode TEXT NOT NULL
            CHECK(alert_mode IN ('ON_DEMAND_ONLY', 'INSTANT')),
        quiet_start TEXT,
        quiet_end TEXT,
        timezone_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(
            (quiet_start IS NULL AND quiet_end IS NULL)
            OR (quiet_start IS NOT NULL AND quiet_end IS NOT NULL)
        ),
        CHECK(
            (all_categories = 1 AND category_ids_json = '[]')
            OR (all_categories = 0 AND category_ids_json <> '[]')
        ),
        CHECK(CAST(minimum_liquidity_usd AS REAL) BETWEEN 5000 AND 1000000),
        CHECK(CAST(minimum_24h_volume_usd AS REAL) BETWEEN 10000 AND 10000000),
        CHECK(CAST(maximum_relative_spread AS REAL) BETWEEN 0.01 AND 0.15),
        CHECK(CAST(maximum_absolute_spread AS REAL) BETWEEN 0.01 AND 0.08),
        CHECK(CAST(maximum_slippage AS REAL) BETWEEN 0.005 AND 0.05),
        FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_setting_edits (
        token_fingerprint TEXT PRIMARY KEY,
        telegram_id INTEGER NOT NULL,
        starting_revision INTEGER NOT NULL CHECK(starting_revision >= 0),
        proposed_settings_json TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK(status IN ('pending', 'confirmed', 'cancelled', 'expired')),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_settings_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        from_revision INTEGER NOT NULL CHECK(from_revision >= 0),
        to_revision INTEGER NOT NULL CHECK(to_revision > from_revision),
        changed_keys_json TEXT NOT NULL,
        changes_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_deliveries (
        telegram_id INTEGER NOT NULL,
        opportunity_uid TEXT NOT NULL,
        status TEXT NOT NULL CHECK(
            status IN (
                'pending', 'claimed', 'retry_wait', 'failed', 'sent',
                'dead_letter', 'suppressed'
            )
        ),
        payload_text TEXT NOT NULL CHECK(
            length(payload_text) BETWEEN 1 AND 4096
            AND instr(payload_text, char(0)) = 0
        ),
        signal_at TEXT NOT NULL,
        wave_at TEXT NOT NULL,
        settings_revision INTEGER NOT NULL CHECK(settings_revision >= 0),
        claim_id TEXT,
        claimed_at TEXT,
        claim_expires_at TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        next_attempt_at TEXT,
        last_error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(telegram_id, opportunity_uid),
        FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(opportunity_uid)
            REFERENCES opportunity_signals(opportunity_uid)
            ON DELETE RESTRICT,
        CHECK(
            status <> 'claimed'
            OR (
                claim_id IS NOT NULL
                AND claimed_at IS NOT NULL
                AND claim_expires_at IS NOT NULL
            )
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_delivery_gates (
        gate_key TEXT PRIMARY KEY CHECK(gate_key = 'gates'),
        admin_delivery_enabled INTEGER NOT NULL
            CHECK(admin_delivery_enabled IN (0, 1)),
        user_delivery_enabled INTEGER NOT NULL
            CHECK(user_delivery_enabled IN (0, 1)),
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_scanner_state (
        state_key TEXT PRIMARY KEY CHECK(state_key = 'scanner'),
        status TEXT NOT NULL CHECK(
            status IN ('DISABLED', 'BASELINING', 'HEALTHY', 'DEGRADED', 'DOWN')
        ),
        schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
        baseline_version INTEGER NOT NULL CHECK(baseline_version >= 1),
        score_version INTEGER NOT NULL CHECK(score_version >= 1),
        global_baseline_cycles INTEGER NOT NULL
            CHECK(global_baseline_cycles BETWEEN 0 AND 2),
        discovery_complete INTEGER NOT NULL CHECK(discovery_complete IN (0, 1)),
        last_complete_discovery_at TEXT,
        last_complete_cycle_at TEXT,
        diagnostic_code TEXT,
        request_count INTEGER NOT NULL DEFAULT 0 CHECK(request_count >= 0),
        failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
        updated_at TEXT NOT NULL
    )
    """,
)

_INDEX_DDL = (
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_market_state_mode_seen
    ON opportunity_market_state(mode, active, last_observed_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_metric_buckets_asset_time
    ON opportunity_metric_buckets(asset_id, bucket_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_metric_buckets_bucket_at
    ON opportunity_metric_buckets(bucket_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_execution_metrics_observed
    ON opportunity_execution_metrics(observed_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_signals_active_score
    ON opportunity_signals(status, CAST(score AS REAL) DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_signals_asset_last_seen
    ON opportunity_signals(asset_id, last_seen_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_subscriptions_enabled
    ON opportunity_subscriptions(enabled)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_setting_edits_user_status
    ON opportunity_setting_edits(telegram_id, status, expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_settings_audit_user_time
    ON opportunity_settings_audit(telegram_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_deliveries_status_retry
    ON opportunity_deliveries(status, next_attempt_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_deliveries_signal_wave
    ON opportunity_deliveries(opportunity_uid, wave_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_deliveries_recipient_status
    ON opportunity_deliveries(telegram_id, status, updated_at)
    """,
)

_TRIGGER_DDL = (
    """
    CREATE TRIGGER IF NOT EXISTS trg_opportunity_delivery_payload_immutable
    BEFORE UPDATE OF payload_text ON opportunity_deliveries
    FOR EACH ROW
    WHEN NEW.payload_text <> OLD.payload_text
    BEGIN
        SELECT RAISE(ABORT, 'opportunity delivery payload is immutable');
    END
    """,
)
