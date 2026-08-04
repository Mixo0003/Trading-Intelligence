"""SQLite storage for copy-trading intelligence (V2 Phase 1, read-only)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config.constants import (
    COPY_MAX_ALLOC_PER_MARKET,
    COPY_MAX_ALLOC_PER_WALLET,
    COPY_MAX_ALLOC_TOTAL,
)
from app.config.settings import Settings
from app.database.storage import get_connection


@dataclass(frozen=True)
class WatchlistEntry:
    wallet: str
    label: str | None
    enabled: bool
    added_by: int | None
    created_at: str
    updated_at: str


def initialize_copy_schema(settings: Settings) -> None:
    with get_connection(settings) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_watchlist (
                wallet TEXT PRIMARY KEY,
                label TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                added_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_position_snapshots (
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
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_trade_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key TEXT NOT NULL UNIQUE,
                wallet TEXT NOT NULL,
                event_type TEXT NOT NULL,
                condition_id TEXT,
                asset TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                tx_hash TEXT,
                ts INTEGER,
                title TEXT,
                slug TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_trader_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                window TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                computed_at TEXT NOT NULL,
                UNIQUE(wallet, window)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                market_slug TEXT,
                market_title TEXT,
                outcome TEXT,
                action TEXT NOT NULL,
                trader_score REAL,
                max_alloc REAL,
                reason TEXT,
                entry_price REAL,
                current_price REAL,
                price_drift REAL,
                slippage_est REAL,
                created_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_paper_positions (
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
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_json TEXT,
                created_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_poll_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_user_settings (
                telegram_id INTEGER PRIMARY KEY,
                profile TEXT NOT NULL DEFAULT 'Balanced',
                settings_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_discovery_scores (
                wallet TEXT PRIMARY KEY,
                label TEXT,
                decision TEXT NOT NULL,
                final_score REAL NOT NULL,
                performance_score REAL,
                risk_score REAL,
                data_confidence TEXT,
                risk_level TEXT,
                reason TEXT,
                metrics_json TEXT,
                computed_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS wallet_monitor_state (
                wallet TEXT PRIMARY KEY,
                subscription_started_at TEXT NOT NULL,
                baseline_started_at TEXT,
                baseline_status TEXT NOT NULL DEFAULT 'PENDING',
                baseline_error TEXT,
                newest_event_ts INTEGER,
                newest_event_uid TEXT,
                monitoring_enabled INTEGER NOT NULL DEFAULT 1,
                last_polled_at TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        _ensure_copy_column(connection, "wallet_monitor_state", "last_polled_at", "TEXT")
        _ensure_copy_column(connection, "wallet_monitor_state", "baseline_version", "INTEGER")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS wallet_event_watermarks (
                wallet TEXT PRIMARY KEY,
                newest_event_ts INTEGER,
                newest_event_uid TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS processed_copy_events (
                event_uid TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                event_ts INTEGER,
                observed_at INTEGER,
                is_baseline INTEGER NOT NULL DEFAULT 0,
                alertable INTEGER NOT NULL DEFAULT 0,
                reject_reason TEXT,
                created_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_processed_copy_events_wallet
            ON processed_copy_events(wallet)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS baseline_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                positions_count INTEGER,
                error TEXT
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_alert_deliveries (
                subscriber_id INTEGER NOT NULL,
                wallet TEXT NOT NULL,
                event_uid TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (subscriber_id, wallet, event_uid)
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_copy_alert_deliveries_status
            ON copy_alert_deliveries(status)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS copy_job_locks (
                job_name TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                locked_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)

        # Keep base DDL + V2.2.2 migration on one connection so ``:memory:``
        # databases (default pytest settings) retain tables across steps.
        _migrate_legacy_position_snapshots_to_v2_on_connection(connection)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_copy_column(
    connection: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    names = {row[1] for row in rows}
    if column not in names:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


# V2.2.2 Checkpoint 3 — parallel asset-keyed snapshots + migration.
MIGRATION_ID_POSITION_SNAPSHOTS_V2 = "v2.2.2_copy_position_snapshots_v2"

# Named failpoints for focused tests only. Production always no-ops.
# Points: during_legacy_row_migration | during_quarantine_insertion |
#         during_validation | before_marker_write
_MIGRATION_FAILPOINT_HOOK = None


def _migration_failpoint(point: str) -> None:
    hook = _MIGRATION_FAILPOINT_HOOK
    if hook is not None:
        hook(point)


class CopySchemaMigrationError(RuntimeError):
    """Raised when the additive snapshots v2 migration cannot complete safely."""


@dataclass(frozen=True)
class PositionSnapshotsV2MigrationResult:
    migration_id: str
    already_applied: bool
    legacy_total: int
    migrated: int
    quarantined: int
    applied_at: str | None


def _valid_snapshot_asset(asset: object) -> str | None:
    if asset is None:
        return None
    text = str(asset).strip()
    return text if text else None


def _ensure_v222_additive_schema(connection: sqlite3.Connection) -> None:
    """Create additive V2.2.2 tables/columns/indexes inside the caller's transaction."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS copy_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            details_json TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS copy_position_snapshots_v2 (
            wallet TEXT NOT NULL,
            asset TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT,
            size REAL NOT NULL,
            avg_price REAL,
            cur_price REAL,
            title TEXT,
            slug TEXT,
            event_slug TEXT,
            snapshot_at TEXT NOT NULL,
            PRIMARY KEY (wallet, asset)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS copy_position_snapshots_migration_quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT,
            asset TEXT,
            outcome TEXT,
            size REAL,
            avg_price REAL,
            cur_price REAL,
            title TEXT,
            slug TEXT,
            event_slug TEXT,
            snapshot_at TEXT,
            legacy_row_id INTEGER,
            reason TEXT NOT NULL,
            quarantined_at TEXT NOT NULL,
            diagnostic_json TEXT
        )
    """)
    _ensure_copy_column(
        connection, "wallet_monitor_state", "last_complete_positions_at", "TEXT"
    )
    _ensure_copy_column(
        connection, "wallet_monitor_state", "last_correlated_trade_ts", "INTEGER"
    )
    _ensure_copy_column(
        connection, "processed_copy_events", "diagnostic_json", "TEXT"
    )
    _ensure_copy_column(connection, "copy_paper_positions", "asset", "TEXT")
    _ensure_copy_column(
        connection, "copy_paper_positions", "source_event_uid", "TEXT"
    )
    _ensure_copy_column(
        connection, "copy_alert_deliveries", "attempt_count", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_copy_column(
        connection, "copy_alert_deliveries", "next_attempt_at", "TEXT"
    )
    _ensure_copy_column(
        connection, "copy_alert_deliveries", "last_error_code", "TEXT"
    )
    _ensure_copy_column(connection, "copy_alert_deliveries", "claimed_at", "TEXT")
    _ensure_copy_column(connection, "copy_alert_deliveries", "sent_at", "TEXT")
    _ensure_copy_column(connection, "copy_alert_deliveries", "decision", "TEXT")
    connection.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_copy_paper_positions_source_event_uid
        ON copy_paper_positions(source_event_uid)
        WHERE source_event_uid IS NOT NULL
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS copy_trade_correlation_quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            asset TEXT,
            condition_id TEXT,
            event_type TEXT,
            reason TEXT NOT NULL,
            prev_size REAL,
            new_size REAL,
            detected_at INTEGER,
            diagnostic_json TEXT,
            quarantined_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS copy_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            market_slug TEXT,
            market_title TEXT,
            outcome TEXT,
            action TEXT NOT NULL,
            trader_score REAL,
            max_alloc REAL,
            reason TEXT,
            entry_price REAL,
            current_price REAL,
            price_drift REAL,
            slippage_est REAL,
            created_at TEXT NOT NULL,
            event_uid TEXT,
            envelope_json TEXT,
            reason_codes_json TEXT
        )
    """)
    _ensure_copy_column(connection, "copy_recommendations", "event_uid", "TEXT")
    _ensure_copy_column(connection, "copy_recommendations", "envelope_json", "TEXT")
    _ensure_copy_column(connection, "copy_recommendations", "reason_codes_json", "TEXT")
    connection.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_copy_recommendations_event_uid
        ON copy_recommendations(event_uid)
        WHERE event_uid IS NOT NULL
    """)


def _already_applied_result(
    applied_at: str | None, details_json: str | None
) -> PositionSnapshotsV2MigrationResult:
    details: dict = {}
    if details_json:
        try:
            details = json.loads(details_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {}
    return PositionSnapshotsV2MigrationResult(
        migration_id=MIGRATION_ID_POSITION_SNAPSHOTS_V2,
        already_applied=True,
        legacy_total=int(details.get("legacy_total", 0)),
        migrated=int(details.get("migrated", 0)),
        quarantined=int(details.get("quarantined", 0)),
        applied_at=applied_at,
    )


def is_position_snapshots_v2_migration_applied(settings: Settings) -> bool:
    with get_connection(settings) as connection:
        try:
            row = connection.execute(
                """
                SELECT 1 FROM copy_schema_migrations
                WHERE migration_id = ?
                LIMIT 1
                """,
                (MIGRATION_ID_POSITION_SNAPSHOTS_V2,),
            ).fetchone()
        except sqlite3.OperationalError:
            # Additive schema rolled back with a failed single-transaction attempt.
            return False
    return row is not None


def get_position_snapshots_v2_migration_status(
    settings: Settings,
) -> sqlite3.Row | None:
    with get_connection(settings) as connection:
        try:
            return connection.execute(
                """
                SELECT migration_id, applied_at, details_json
                FROM copy_schema_migrations
                WHERE migration_id = ?
                """,
                (MIGRATION_ID_POSITION_SNAPSHOTS_V2,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None


def migrate_legacy_position_snapshots_to_v2(
    settings: Settings,
) -> PositionSnapshotsV2MigrationResult:
    """
    Single-transaction, idempotent migration of legacy condition-keyed snapshots
    into the parallel asset-keyed v2 table.

    One SQLite transaction covers:
      additive schema DDL → legacy read → migrate/quarantine → validation → marker.

    Commit only after every step succeeds. Any error rolls back the entire attempt
    (including additive schema created in this attempt). Legacy
    ``copy_position_snapshots`` is read-only — never renamed, deleted, truncated,
    or rewritten.
    """
    with get_connection(settings) as connection:
        return _migrate_legacy_position_snapshots_to_v2_on_connection(connection)


def _migrate_legacy_position_snapshots_to_v2_on_connection(
    connection: sqlite3.Connection,
) -> PositionSnapshotsV2MigrationResult:
    """Run additive migration on an open connection (shared with schema init)."""
    # Python sqlite3 default isolation_level '' autocommits DDL unless an
    # explicit transaction is already open. BEGIN first so additive schema,
    # data, validation, and marker share one commit/rollback unit when this
    # runs as a standalone migrate call.
    try:
        connection.execute("BEGIN")
    except sqlite3.OperationalError:
        # Already inside a transaction (initialize_copy_schema same-connection path).
        pass
    _ensure_v222_additive_schema(connection)

    existing = connection.execute(
        """
        SELECT applied_at, details_json FROM copy_schema_migrations
        WHERE migration_id = ?
        """,
        (MIGRATION_ID_POSITION_SNAPSHOTS_V2,),
    ).fetchone()
    if existing is not None:
        return _already_applied_result(
            existing["applied_at"], existing["details_json"]
        )

    connection.execute("PRAGMA foreign_keys=ON")

    # Incomplete prior attempts (e.g. pre-correction schema-first leftovers)
    # leave no marker; reset v2/quarantine targets only — never legacy.
    connection.execute("DELETE FROM copy_position_snapshots_v2")
    connection.execute("DELETE FROM copy_position_snapshots_migration_quarantine")

    legacy_rows = connection.execute(
        "SELECT * FROM copy_position_snapshots ORDER BY id ASC"
    ).fetchall()
    legacy_total = len(legacy_rows)
    migrated = 0
    quarantined = 0
    seen_wallet_asset: set[tuple[str, str]] = set()
    now = _now_iso()

    for row in legacy_rows:
        wallet = str(row["wallet"] or "").lower()
        asset = _valid_snapshot_asset(row["asset"])
        if asset is None:
            connection.execute(
                """
                INSERT INTO copy_position_snapshots_migration_quarantine (
                    wallet, condition_id, asset, outcome, size, avg_price, cur_price,
                    title, slug, event_slug, snapshot_at, legacy_row_id, reason,
                    quarantined_at, diagnostic_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet,
                    row["condition_id"],
                    row["asset"],
                    row["outcome"],
                    row["size"],
                    row["avg_price"],
                    row["cur_price"],
                    row["title"],
                    row["slug"],
                    row["event_slug"],
                    row["snapshot_at"],
                    row["id"],
                    "missing_or_invalid_asset",
                    now,
                    json.dumps(
                        {
                            "legacy_row_id": row["id"],
                            "wallet": wallet,
                            "condition_id": row["condition_id"],
                            "asset": row["asset"],
                            "reason": "missing_or_invalid_asset",
                        }
                    ),
                ),
            )
            quarantined += 1
            _migration_failpoint("during_quarantine_insertion")
            continue

        key = (wallet, asset)
        if key in seen_wallet_asset:
            raise CopySchemaMigrationError(
                f"duplicate (wallet, asset) during snapshots v2 migration: {wallet}/{asset}"
            )
        seen_wallet_asset.add(key)

        try:
            connection.execute(
                """
                INSERT INTO copy_position_snapshots_v2 (
                    wallet, asset, condition_id, outcome, size, avg_price, cur_price,
                    title, slug, event_slug, snapshot_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet,
                    asset,
                    row["condition_id"],
                    row["outcome"],
                    float(row["size"] or 0),
                    row["avg_price"],
                    row["cur_price"],
                    row["title"],
                    row["slug"],
                    row["event_slug"],
                    row["snapshot_at"] or now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise CopySchemaMigrationError(
                f"duplicate (wallet, asset) during snapshots v2 migration: {wallet}/{asset}"
            ) from exc
        migrated += 1
        _migration_failpoint("during_legacy_row_migration")

    _migration_failpoint("during_validation")

    if legacy_total != migrated + quarantined:
        raise CopySchemaMigrationError(
            f"migration count mismatch: legacy_total={legacy_total} "
            f"migrated={migrated} quarantined={quarantined}"
        )

    fk_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if fk_violations:
        raise CopySchemaMigrationError(
            f"foreign_key_check failed with {len(fk_violations)} violation(s)"
        )

    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    integrity_value = integrity[0] if integrity is not None else "failed"
    if str(integrity_value).lower() != "ok":
        raise CopySchemaMigrationError(
            f"integrity_check failed: {integrity_value}"
        )

    _migration_failpoint("before_marker_write")

    details = {
        "legacy_total": legacy_total,
        "migrated": migrated,
        "quarantined": quarantined,
    }
    connection.execute(
        """
        INSERT INTO copy_schema_migrations (migration_id, applied_at, details_json)
        VALUES (?, ?, ?)
        """,
        (MIGRATION_ID_POSITION_SNAPSHOTS_V2, now, json.dumps(details)),
    )

    return PositionSnapshotsV2MigrationResult(
        migration_id=MIGRATION_ID_POSITION_SNAPSHOTS_V2,
        already_applied=False,
        legacy_total=legacy_total,
        migrated=migrated,
        quarantined=quarantined,
        applied_at=now,
    )


def upsert_position_snapshot_v2(
    settings: Settings,
    wallet: str,
    *,
    asset: str,
    condition_id: str,
    outcome: str | None,
    size: float,
    avg_price: float | None,
    cur_price: float | None,
    title: str | None,
    slug: str | None,
    event_slug: str | None,
) -> None:
    cleaned_asset = _valid_snapshot_asset(asset)
    if cleaned_asset is None:
        raise ValueError("asset is required for v2 position snapshots")
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO copy_position_snapshots_v2 (
                wallet, asset, condition_id, outcome, size, avg_price, cur_price,
                title, slug, event_slug, snapshot_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet, asset) DO UPDATE SET
                condition_id = excluded.condition_id,
                outcome = excluded.outcome,
                size = excluded.size,
                avg_price = excluded.avg_price,
                cur_price = excluded.cur_price,
                title = excluded.title,
                slug = excluded.slug,
                event_slug = excluded.event_slug,
                snapshot_at = excluded.snapshot_at
            """,
            (
                wallet.lower(),
                cleaned_asset,
                condition_id,
                outcome,
                size,
                avg_price,
                cur_price,
                title,
                slug,
                event_slug,
                _now_iso(),
            ),
        )


def load_position_snapshots_v2(
    settings: Settings, wallet: str
) -> dict[str, sqlite3.Row]:
    """Load v2 snapshots keyed by asset."""
    with get_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM copy_position_snapshots_v2 WHERE wallet = ?
            """,
            (wallet.lower(),),
        ).fetchall()
    return {row["asset"]: row for row in rows}


def replace_wallet_snapshots_v2(
    settings: Settings,
    wallet: str,
    positions_by_asset: dict[str, dict],
) -> None:
    """Replace the exact current v2 book for a wallet (does not touch legacy table)."""
    commit_complete_position_book_v2(
        settings,
        wallet,
        positions_by_asset,
        stamp_complete=False,
    )


def commit_complete_position_book_v2(
    settings: Settings,
    wallet: str,
    positions_by_asset: dict[str, dict],
    *,
    stamp_complete: bool = True,
) -> None:
    """
    Atomically replace the v2 book and optionally stamp last_complete_positions_at.

    Does not touch the legacy ``copy_position_snapshots`` table. Any error rolls
    back both snapshot writes and the completeness stamp.
    """
    now = _now_iso()
    wallet_l = wallet.lower()
    with get_connection(settings) as connection:
        connection.execute(
            "DELETE FROM copy_position_snapshots_v2 WHERE wallet = ?",
            (wallet_l,),
        )
        for asset, payload in positions_by_asset.items():
            cleaned_asset = _valid_snapshot_asset(asset)
            if cleaned_asset is None:
                raise ValueError("asset is required for v2 position snapshots")
            connection.execute(
                """
                INSERT INTO copy_position_snapshots_v2 (
                    wallet, asset, condition_id, outcome, size, avg_price, cur_price,
                    title, slug, event_slug, snapshot_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet_l,
                    cleaned_asset,
                    payload["condition_id"],
                    payload.get("outcome"),
                    float(payload.get("size") or 0),
                    payload.get("avg_price"),
                    payload.get("cur_price"),
                    payload.get("title"),
                    payload.get("slug"),
                    payload.get("event_slug"),
                    payload.get("snapshot_at") or now,
                ),
            )
        if stamp_complete:
            connection.execute(
                """
                UPDATE wallet_monitor_state
                SET last_complete_positions_at = ?, updated_at = ?
                WHERE wallet = ?
                """,
                (now, now, wallet_l),
            )


def record_snapshot_migration_quarantine(
    settings: Settings,
    *,
    wallet: str,
    reason: str,
    condition_id: str | None = None,
    asset: str | None = None,
    outcome: str | None = None,
    size: float | None = None,
    avg_price: float | None = None,
    cur_price: float | None = None,
    title: str | None = None,
    slug: str | None = None,
    event_slug: str | None = None,
    snapshot_at: str | None = None,
    legacy_row_id: int | None = None,
    diagnostic: dict | None = None,
) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO copy_position_snapshots_migration_quarantine (
                wallet, condition_id, asset, outcome, size, avg_price, cur_price,
                title, slug, event_slug, snapshot_at, legacy_row_id, reason,
                quarantined_at, diagnostic_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wallet.lower(),
                condition_id,
                asset,
                outcome,
                size,
                avg_price,
                cur_price,
                title,
                slug,
                event_slug,
                snapshot_at,
                legacy_row_id,
                reason,
                _now_iso(),
                json.dumps(diagnostic) if diagnostic is not None else None,
            ),
        )


def list_snapshot_migration_quarantine(
    settings: Settings, *, wallet: str | None = None
) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        if wallet is None:
            rows = connection.execute(
                """
                SELECT * FROM copy_position_snapshots_migration_quarantine
                ORDER BY id ASC
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM copy_position_snapshots_migration_quarantine
                WHERE wallet = ?
                ORDER BY id ASC
                """,
                (wallet.lower(),),
            ).fetchall()
    return list(rows)


def record_trade_correlation_quarantine(
    settings: Settings,
    *,
    wallet: str,
    reason: str,
    asset: str | None = None,
    condition_id: str | None = None,
    event_type: str | None = None,
    prev_size: float | None = None,
    new_size: float | None = None,
    detected_at: int | None = None,
    diagnostic: dict | None = None,
) -> None:
    """Persist a sanitized trade-correlation quarantine row (no secrets/paths)."""
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO copy_trade_correlation_quarantine (
                wallet, asset, condition_id, event_type, reason,
                prev_size, new_size, detected_at, diagnostic_json, quarantined_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wallet.lower(),
                asset,
                condition_id,
                event_type,
                reason,
                prev_size,
                new_size,
                detected_at,
                json.dumps(diagnostic) if diagnostic is not None else None,
                _now_iso(),
            ),
        )


def list_trade_correlation_quarantine(
    settings: Settings, *, wallet: str | None = None
) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        if wallet is None:
            rows = connection.execute(
                """
                SELECT * FROM copy_trade_correlation_quarantine
                ORDER BY id ASC
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM copy_trade_correlation_quarantine
                WHERE wallet = ?
                ORDER BY id ASC
                """,
                (wallet.lower(),),
            ).fetchall()
    return list(rows)


def add_watchlist_wallet(
    settings: Settings,
    wallet: str,
    *,
    label: str | None = None,
    added_by: int | None = None,
) -> None:
    now = _now_iso()
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO copy_watchlist (wallet, label, enabled, added_by, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                label = COALESCE(excluded.label, copy_watchlist.label),
                updated_at = excluded.updated_at
            """,
            (wallet.lower(), label, added_by, now, now),
        )


def remove_watchlist_wallet(settings: Settings, wallet: str) -> bool:
    with get_connection(settings) as connection:
        cursor = connection.execute(
            "DELETE FROM copy_watchlist WHERE wallet = ?",
            (wallet.lower(),),
        )
    return cursor.rowcount > 0


def toggle_watchlist_wallet(settings: Settings, wallet: str) -> bool | None:
    with get_connection(settings) as connection:
        row = connection.execute(
            "SELECT enabled FROM copy_watchlist WHERE wallet = ?",
            (wallet.lower(),),
        ).fetchone()
        if row is None:
            return None
        new_val = 0 if row["enabled"] else 1
        connection.execute(
            "UPDATE copy_watchlist SET enabled = ?, updated_at = ? WHERE wallet = ?",
            (new_val, _now_iso(), wallet.lower()),
        )
    return bool(new_val)


def set_watchlist_enabled(settings: Settings, wallet: str, *, enabled: bool) -> bool | None:
    with get_connection(settings) as connection:
        row = connection.execute(
            "SELECT enabled FROM copy_watchlist WHERE wallet = ?",
            (wallet.lower(),),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "UPDATE copy_watchlist SET enabled = ?, updated_at = ? WHERE wallet = ?",
            (1 if enabled else 0, _now_iso(), wallet.lower()),
        )
    return enabled


def list_watchlist(settings: Settings) -> list[WatchlistEntry]:
    with get_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT wallet, label, enabled, added_by, created_at, updated_at
            FROM copy_watchlist
            ORDER BY created_at ASC
            """
        ).fetchall()
    return [
        WatchlistEntry(
            wallet=row["wallet"],
            label=row["label"],
            enabled=bool(row["enabled"]),
            added_by=row["added_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


def list_enabled_wallets(settings: Settings) -> list[str]:
    with get_connection(settings) as connection:
        rows = connection.execute(
            "SELECT wallet FROM copy_watchlist WHERE enabled = 1 ORDER BY created_at ASC"
        ).fetchall()
    return [row["wallet"] for row in rows]


def upsert_position_snapshot(
    settings: Settings,
    wallet: str,
    condition_id: str,
    *,
    asset: str | None,
    outcome: str | None,
    size: float,
    avg_price: float | None,
    cur_price: float | None,
    title: str | None,
    slug: str | None,
    event_slug: str | None,
) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO copy_position_snapshots (
                wallet, condition_id, asset, outcome, size, avg_price, cur_price,
                title, slug, event_slug, snapshot_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet, condition_id) DO UPDATE SET
                asset = excluded.asset,
                outcome = excluded.outcome,
                size = excluded.size,
                avg_price = excluded.avg_price,
                cur_price = excluded.cur_price,
                title = excluded.title,
                slug = excluded.slug,
                event_slug = excluded.event_slug,
                snapshot_at = excluded.snapshot_at
            """,
            (
                wallet.lower(),
                condition_id,
                asset,
                outcome,
                size,
                avg_price,
                cur_price,
                title,
                slug,
                event_slug,
                _now_iso(),
            ),
        )


def load_position_snapshots(settings: Settings, wallet: str) -> dict[str, sqlite3.Row]:
    with get_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM copy_position_snapshots WHERE wallet = ?
            """,
            (wallet.lower(),),
        ).fetchall()
    return {row["condition_id"]: row for row in rows}


def insert_trade_event(
    settings: Settings,
    *,
    dedup_key: str,
    wallet: str,
    event_type: str,
    condition_id: str | None,
    asset: str | None,
    outcome: str | None,
    side: str | None,
    price: float | None,
    size: float | None,
    tx_hash: str | None,
    ts: int | None,
    title: str | None,
    slug: str | None,
    raw_json: dict | None,
) -> bool:
    with get_connection(settings) as connection:
        try:
            connection.execute(
                """
                INSERT INTO copy_trade_events (
                    dedup_key, wallet, event_type, condition_id, asset, outcome,
                    side, price, size, tx_hash, ts, title, slug, raw_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dedup_key,
                    wallet.lower(),
                    event_type,
                    condition_id,
                    asset,
                    outcome,
                    side,
                    price,
                    size,
                    tx_hash,
                    ts,
                    title,
                    slug,
                    json.dumps(raw_json) if raw_json else None,
                    _now_iso(),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def save_trader_metrics(
    settings: Settings,
    wallet: str,
    window: str,
    metrics: dict,
) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO copy_trader_metrics (wallet, window, metrics_json, computed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(wallet, window) DO UPDATE SET
                metrics_json = excluded.metrics_json,
                computed_at = excluded.computed_at
            """,
            (wallet.lower(), window, json.dumps(metrics), _now_iso()),
        )


def load_trader_metrics(settings: Settings, wallet: str, window: str = "lifetime") -> dict | None:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT metrics_json FROM copy_trader_metrics
            WHERE wallet = ? AND window = ?
            """,
            (wallet.lower(), window),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["metrics_json"])


def save_recommendation(settings: Settings, rec: dict) -> int:
    """Legacy insert helper. Prefer ``save_recommendation_envelope`` for V2.2.2."""
    with get_connection(settings) as connection:
        cursor = connection.execute(
            """
            INSERT INTO copy_recommendations (
                wallet, market_slug, market_title, outcome, action, trader_score,
                max_alloc, reason, entry_price, current_price, price_drift,
                slippage_est, created_at, event_uid, envelope_json, reason_codes_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec["wallet"].lower(),
                rec.get("market_slug"),
                rec.get("market_title"),
                rec.get("outcome"),
                rec["action"],
                rec.get("trader_score"),
                rec.get("max_alloc"),
                rec.get("reason"),
                rec.get("entry_price"),
                rec.get("current_price"),
                rec.get("price_drift"),
                rec.get("slippage_est"),
                _now_iso(),
                rec.get("event_uid"),
                rec.get("envelope_json"),
                rec.get("reason_codes_json"),
            ),
        )
        return int(cursor.lastrowid)


def save_recommendation_envelope(settings: Settings, rec: dict) -> bool:
    """Persist a recommendation envelope idempotently by non-null ``event_uid``.

    Returns True when a new row was inserted, False when an existing UID was kept.
    Rows without ``event_uid`` always insert (legacy / non-authoritative path).
    """
    event_uid = rec.get("event_uid")
    with get_connection(settings) as connection:
        if event_uid:
            existing = connection.execute(
                """
                SELECT id FROM copy_recommendations WHERE event_uid = ?
                """,
                (str(event_uid),),
            ).fetchone()
            if existing is not None:
                return False
        connection.execute(
            """
            INSERT INTO copy_recommendations (
                wallet, market_slug, market_title, outcome, action, trader_score,
                max_alloc, reason, entry_price, current_price, price_drift,
                slippage_est, created_at, event_uid, envelope_json, reason_codes_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec["wallet"].lower(),
                rec.get("market_slug"),
                rec.get("market_title"),
                rec.get("outcome"),
                rec["action"],
                rec.get("trader_score"),
                rec.get("max_alloc"),
                rec.get("reason"),
                rec.get("entry_price"),
                rec.get("current_price"),
                rec.get("price_drift"),
                rec.get("slippage_est"),
                _now_iso(),
                str(event_uid) if event_uid else None,
                rec.get("envelope_json"),
                rec.get("reason_codes_json"),
            ),
        )
        return True


def count_recommendations_for_event_uid(settings: Settings, event_uid: str) -> int:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS c FROM copy_recommendations WHERE event_uid = ?
            """,
            (str(event_uid),),
        ).fetchone()
    return int(row["c"] if row else 0)


def count_open_paper_positions(settings: Settings) -> int:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS c FROM copy_paper_positions WHERE status = 'open'
            """
        ).fetchone()
    return int(row["c"] if row else 0)


def get_recent_recommendations(settings: Settings, limit: int = 10) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM copy_recommendations
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return list(rows)


def open_paper_position(
    settings: Settings,
    *,
    wallet: str,
    condition_id: str,
    market_slug: str | None,
    outcome: str | None,
    notional: float,
    entry_price: float,
    asset: str | None = None,
    source_event_uid: str | None = None,
) -> bool:
    """
    Open a paper position.

    When ``source_event_uid`` is provided, persistence is idempotent: a second
    insert with the same non-null UID is ignored and returns False.
    Null ``source_event_uid`` remains backward compatible (multiple rows allowed).
    """
    with get_connection(settings) as connection:
        try:
            connection.execute(
                """
                INSERT INTO copy_paper_positions (
                    wallet, condition_id, market_slug, outcome, notional,
                    entry_price, status, opened_at, asset, source_event_uid
                )
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    wallet.lower(),
                    condition_id,
                    market_slug,
                    outcome,
                    notional,
                    entry_price,
                    _now_iso(),
                    asset,
                    source_event_uid,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            if source_event_uid is None:
                raise
            return False


@dataclass(frozen=True)
class PaperCommitStorageResult:
    ok: bool
    inserted: bool
    code: str
    message: str
    paper_id: int | None = None
    recommendation_id: int | None = None


# Test-only hook: callable(stage) may raise to force mid-transaction failures.
_PAPER_COMMIT_TEST_HOOK = None


def get_paper_position_by_source_event_uid(
    settings: Settings, source_event_uid: str
) -> sqlite3.Row | None:
    with get_connection(settings) as connection:
        return connection.execute(
            """
            SELECT * FROM copy_paper_positions
            WHERE source_event_uid = ?
            LIMIT 1
            """,
            (str(source_event_uid),),
        ).fetchone()


def commit_copy_recommendation_and_paper(
    settings: Settings,
    *,
    recommendation: dict,
    paper: dict,
    audit_payload: dict | None = None,
) -> PaperCommitStorageResult:
    """Atomically insert COPY recommendation envelope + paper position.

    Rechecks exposure inside the transaction for the exact proposed notional.
    Does not silently resize. Idempotent on ``source_event_uid`` when payload matches.
    Conflicting duplicate payload fails closed.
    """
    event_uid = str(paper["source_event_uid"])
    wallet = str(paper["wallet"]).lower()
    condition_id = str(paper["condition_id"])
    notional = float(paper["notional"])
    entry_price = float(paper["entry_price"])
    bankroll = float(settings.copy_paper_bankroll_usd)

    with get_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing_paper = connection.execute(
                """
                SELECT id, notional, entry_price, asset, status
                FROM copy_paper_positions
                WHERE source_event_uid = ?
                LIMIT 1
                """,
                (event_uid,),
            ).fetchone()
            if existing_paper is not None:
                if _paper_payload_matches(
                    existing_paper, notional=notional, entry_price=entry_price, asset=paper.get("asset")
                ):
                    connection.execute("COMMIT")
                    return PaperCommitStorageResult(
                        ok=True,
                        inserted=False,
                        code="copy_paper_idempotent",
                        message="paper already committed for event",
                        paper_id=int(existing_paper["id"]),
                    )
                connection.execute("ROLLBACK")
                return PaperCommitStorageResult(
                    ok=False,
                    inserted=False,
                    code="conflicting_duplicate_paper",
                    message="existing paper conflicts with proposed payload",
                    paper_id=int(existing_paper["id"]),
                )

            remaining = _remaining_exposure_capacity_conn(
                connection,
                bankroll=bankroll,
                wallet=wallet,
                condition_id=condition_id,
            )
            if remaining <= 0:
                connection.execute("ROLLBACK")
                return PaperCommitStorageResult(
                    ok=False,
                    inserted=False,
                    code="exposure_capacity_zero_at_commit",
                    message="no exposure capacity at commit",
                )
            if remaining + 1e-9 < notional:
                connection.execute("ROLLBACK")
                return PaperCommitStorageResult(
                    ok=False,
                    inserted=False,
                    code="exposure_changed_at_commit",
                    message="exposure capacity below proposed allocation",
                )

            _maybe_paper_commit_test_hook("before_recommendation")

            recommendation_id = _upsert_recommendation_envelope_conn(
                connection, recommendation
            )

            _maybe_paper_commit_test_hook("after_recommendation")

            try:
                cursor = connection.execute(
                    """
                    INSERT INTO copy_paper_positions (
                        wallet, condition_id, market_slug, outcome, notional,
                        entry_price, status, opened_at, asset, source_event_uid
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                    """,
                    (
                        wallet,
                        condition_id,
                        paper.get("market_slug"),
                        paper.get("outcome"),
                        notional,
                        entry_price,
                        _now_iso(),
                        paper.get("asset"),
                        event_uid,
                    ),
                )
            except sqlite3.IntegrityError:
                # Concurrent winner inserted first — re-read and classify.
                raced = connection.execute(
                    """
                    SELECT id, notional, entry_price, asset
                    FROM copy_paper_positions
                    WHERE source_event_uid = ?
                    LIMIT 1
                    """,
                    (event_uid,),
                ).fetchone()
                if raced is not None and _paper_payload_matches(
                    raced, notional=notional, entry_price=entry_price, asset=paper.get("asset")
                ):
                    connection.execute("COMMIT")
                    return PaperCommitStorageResult(
                        ok=True,
                        inserted=False,
                        code="copy_paper_idempotent",
                        message="concurrent paper already committed",
                        paper_id=int(raced["id"]),
                        recommendation_id=recommendation_id,
                    )
                connection.execute("ROLLBACK")
                return PaperCommitStorageResult(
                    ok=False,
                    inserted=False,
                    code="conflicting_duplicate_paper",
                    message="concurrent paper conflict",
                    paper_id=int(raced["id"]) if raced is not None else None,
                )

            _maybe_paper_commit_test_hook("after_paper")

            paper_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO copy_audit_log (actor, action, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "paper_commit",
                    "copy_paper_open",
                    json.dumps(audit_payload) if audit_payload else None,
                    _now_iso(),
                ),
            )
            connection.execute("COMMIT")
            return PaperCommitStorageResult(
                ok=True,
                inserted=True,
                code="copy_paper_committed",
                message="paper committed",
                paper_id=paper_id,
                recommendation_id=recommendation_id,
            )
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


def _maybe_paper_commit_test_hook(stage: str) -> None:
    hook = _PAPER_COMMIT_TEST_HOOK
    if hook is not None:
        hook(stage)


def _paper_payload_matches(
    row: sqlite3.Row,
    *,
    notional: float,
    entry_price: float,
    asset: str | None,
) -> bool:
    if abs(float(row["notional"]) - float(notional)) > 1e-9:
        return False
    if abs(float(row["entry_price"]) - float(entry_price)) > 1e-9:
        return False
    if asset is not None and row["asset"] is not None and str(row["asset"]) != str(asset):
        return False
    return True


def _remaining_exposure_capacity_conn(
    connection: sqlite3.Connection,
    *,
    bankroll: float,
    wallet: str,
    condition_id: str,
) -> float:
    totals = _paper_exposure_totals_conn(connection)
    total_cap = bankroll * COPY_MAX_ALLOC_TOTAL
    wallet_cap = bankroll * COPY_MAX_ALLOC_PER_WALLET
    market_cap = bankroll * COPY_MAX_ALLOC_PER_MARKET
    remaining_total = max(0.0, total_cap - totals["total"])
    remaining_wallet = max(0.0, wallet_cap - totals["by_wallet"].get(wallet.lower(), 0.0))
    remaining_market = max(
        0.0, market_cap - totals["by_market"].get(condition_id, 0.0)
    )
    return min(remaining_total, remaining_wallet, remaining_market)


def _paper_exposure_totals_conn(connection: sqlite3.Connection) -> dict[str, float | dict]:
    total = connection.execute(
        "SELECT COALESCE(SUM(notional), 0) AS s FROM copy_paper_positions WHERE status = 'open'"
    ).fetchone()["s"]
    by_wallet_rows = connection.execute(
        """
        SELECT wallet, COALESCE(SUM(notional), 0) AS s
        FROM copy_paper_positions WHERE status = 'open'
        GROUP BY wallet
        """
    ).fetchall()
    by_market_rows = connection.execute(
        """
        SELECT condition_id, COALESCE(SUM(notional), 0) AS s
        FROM copy_paper_positions WHERE status = 'open'
        GROUP BY condition_id
        """
    ).fetchall()
    return {
        "total": float(total),
        "by_wallet": {row["wallet"]: float(row["s"]) for row in by_wallet_rows},
        "by_market": {row["condition_id"]: float(row["s"]) for row in by_market_rows},
    }


def _upsert_recommendation_envelope_conn(
    connection: sqlite3.Connection, rec: dict
) -> int | None:
    """Insert recommendation idempotently by event_uid; never overwrite existing."""
    event_uid = rec.get("event_uid")
    if event_uid:
        existing = connection.execute(
            "SELECT id FROM copy_recommendations WHERE event_uid = ?",
            (str(event_uid),),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
    cursor = connection.execute(
        """
        INSERT INTO copy_recommendations (
            wallet, market_slug, market_title, outcome, action, trader_score,
            max_alloc, reason, entry_price, current_price, price_drift,
            slippage_est, created_at, event_uid, envelope_json, reason_codes_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec["wallet"].lower(),
            rec.get("market_slug"),
            rec.get("market_title"),
            rec.get("outcome"),
            rec["action"],
            rec.get("trader_score"),
            rec.get("max_alloc"),
            rec.get("reason"),
            rec.get("entry_price"),
            rec.get("current_price"),
            rec.get("price_drift"),
            rec.get("slippage_est"),
            _now_iso(),
            str(event_uid) if event_uid else None,
            rec.get("envelope_json"),
            rec.get("reason_codes_json"),
        ),
    )
    return int(cursor.lastrowid)


def list_open_paper_positions(settings: Settings) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM copy_paper_positions WHERE status = 'open'
            ORDER BY opened_at DESC
            """
        ).fetchall()
    return list(rows)


def paper_exposure_totals(settings: Settings) -> dict[str, float]:
    with get_connection(settings) as connection:
        total = connection.execute(
            "SELECT COALESCE(SUM(notional), 0) AS s FROM copy_paper_positions WHERE status = 'open'"
        ).fetchone()["s"]
        by_wallet_rows = connection.execute(
            """
            SELECT wallet, COALESCE(SUM(notional), 0) AS s
            FROM copy_paper_positions WHERE status = 'open'
            GROUP BY wallet
            """
        ).fetchall()
        by_market_rows = connection.execute(
            """
            SELECT condition_id, COALESCE(SUM(notional), 0) AS s
            FROM copy_paper_positions WHERE status = 'open'
            GROUP BY condition_id
            """
        ).fetchall()
    return {
        "total": float(total),
        "by_wallet": {row["wallet"]: float(row["s"]) for row in by_wallet_rows},
        "by_market": {row["condition_id"]: float(row["s"]) for row in by_market_rows},
    }


def write_audit_log(settings: Settings, actor: str, action: str, payload: dict | None = None) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO copy_audit_log (actor, action, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (actor, action, json.dumps(payload) if payload else None, _now_iso()),
        )


def set_poll_state(settings: Settings, key: str, value: str) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO copy_poll_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, _now_iso()),
        )


def get_poll_state(settings: Settings, key: str) -> str | None:
    with get_connection(settings) as connection:
        row = connection.execute(
            "SELECT value FROM copy_poll_state WHERE key = ?",
            (key,),
        ).fetchone()
    return row["value"] if row else None


def save_copy_user_settings(
    settings: Settings,
    *,
    telegram_id: int,
    payload: dict,
    audit_action: str = "copy_settings_save",
    audit_extra: dict | None = None,
) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO copy_user_settings (telegram_id, profile, settings_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                profile = excluded.profile,
                settings_json = excluded.settings_json,
                updated_at = excluded.updated_at
            """,
            (
                telegram_id,
                payload.get("profile", "Balanced"),
                json.dumps(payload),
                _now_iso(),
            ),
        )
    audit_payload = {"profile": payload.get("profile")}
    if audit_extra:
        # Scalars / key lists only — never tokens, paths, or secrets.
        for key in ("changed_keys", "old", "new", "settings_fingerprint"):
            if key in audit_extra:
                audit_payload[key] = audit_extra[key]
    write_audit_log(
        settings,
        actor=str(telegram_id),
        action=audit_action,
        payload=audit_payload,
    )


def load_copy_user_settings(settings: Settings, telegram_id: int) -> dict | None:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT settings_json FROM copy_user_settings WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["settings_json"])


def save_discovery_score(settings: Settings, score) -> None:
    metrics = score.metrics
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO copy_discovery_scores (
                wallet, label, decision, final_score, performance_score, risk_score,
                data_confidence, risk_level, reason, metrics_json, computed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                label = excluded.label,
                decision = excluded.decision,
                final_score = excluded.final_score,
                performance_score = excluded.performance_score,
                risk_score = excluded.risk_score,
                data_confidence = excluded.data_confidence,
                risk_level = excluded.risk_level,
                reason = excluded.reason,
                metrics_json = excluded.metrics_json,
                computed_at = excluded.computed_at
            """,
            (
                score.wallet.lower(),
                score.label,
                score.decision,
                score.final_score,
                score.performance_score,
                score.risk_score,
                score.data_confidence,
                score.risk_level,
                score.reason,
                json.dumps(
                    {
                        "realized_pnl": metrics.realized_pnl,
                        "roi": metrics.roi,
                        "resolved_count": metrics.resolved_count,
                        "active_days": metrics.active_days,
                        "win_rate": metrics.win_rate,
                        "max_drawdown": metrics.max_drawdown,
                        "main_categories": list(metrics.main_categories),
                        "category_concentration": metrics.category_concentration,
                        "incomplete_metrics": list(metrics.incomplete_metrics),
                        "profile_url": metrics.profile_url,
                    }
                ),
                _now_iso(),
            ),
        )


def list_discovery_scores(
    settings: Settings,
    *,
    decision: str | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        if decision:
            rows = connection.execute(
                """
                SELECT * FROM copy_discovery_scores
                WHERE decision = ?
                ORDER BY final_score DESC
                LIMIT ?
                """,
                (decision, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM copy_discovery_scores
                ORDER BY final_score DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return list(rows)


def get_discovery_score(settings: Settings, wallet: str) -> sqlite3.Row | None:
    with get_connection(settings) as connection:
        return connection.execute(
            "SELECT * FROM copy_discovery_scores WHERE wallet = ?",
            (wallet.lower(),),
        ).fetchone()


@dataclass(frozen=True)
class WalletMonitorState:
    wallet: str
    subscription_started_at: str
    baseline_started_at: str | None
    baseline_status: str
    baseline_error: str | None
    newest_event_ts: int | None
    newest_event_uid: str | None
    monitoring_enabled: bool
    updated_at: str
    last_polled_at: str | None = None
    baseline_version: int | None = None
    last_complete_positions_at: str | None = None
    last_correlated_trade_ts: int | None = None


def get_watchlist_entry(settings: Settings, wallet: str) -> WatchlistEntry | None:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT wallet, label, enabled, added_by, created_at, updated_at
            FROM copy_watchlist WHERE wallet = ?
            """,
            (wallet.lower(),),
        ).fetchone()
    if row is None:
        return None
    return WatchlistEntry(
        wallet=row["wallet"],
        label=row["label"],
        enabled=bool(row["enabled"]),
        added_by=row["added_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def upsert_wallet_monitor_state(
    settings: Settings,
    wallet: str,
    *,
    subscription_started_at: str,
    baseline_started_at: str | None = None,
    baseline_status: str = "PENDING",
    baseline_error: str | None = None,
    newest_event_ts: int | None = None,
    newest_event_uid: str | None = None,
    monitoring_enabled: bool = True,
    baseline_version: int | None = None,
) -> None:
    now = _now_iso()
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO wallet_monitor_state (
                wallet, subscription_started_at, baseline_started_at, baseline_status,
                baseline_error, newest_event_ts, newest_event_uid, monitoring_enabled,
                baseline_version, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                subscription_started_at = excluded.subscription_started_at,
                baseline_started_at = excluded.baseline_started_at,
                baseline_status = excluded.baseline_status,
                baseline_error = excluded.baseline_error,
                newest_event_ts = excluded.newest_event_ts,
                newest_event_uid = excluded.newest_event_uid,
                monitoring_enabled = excluded.monitoring_enabled,
                baseline_version = excluded.baseline_version,
                updated_at = excluded.updated_at
            """,
            (
                wallet.lower(),
                subscription_started_at,
                baseline_started_at,
                baseline_status,
                baseline_error,
                newest_event_ts,
                newest_event_uid,
                1 if monitoring_enabled else 0,
                baseline_version,
                now,
            ),
        )


def update_wallet_monitor_fields(
    settings: Settings,
    wallet: str,
    **fields: object,
) -> None:
    if not fields:
        return
    allowed = {
        "subscription_started_at",
        "baseline_started_at",
        "baseline_status",
        "baseline_error",
        "newest_event_ts",
        "newest_event_uid",
        "monitoring_enabled",
        "last_polled_at",
        "baseline_version",
        "last_complete_positions_at",
        "last_correlated_trade_ts",
    }
    cols = []
    values: list[object] = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"Unsupported monitor field: {key}")
        cols.append(f"{key} = ?")
        if key == "monitoring_enabled":
            values.append(1 if value else 0)
        else:
            values.append(value)
    cols.append("updated_at = ?")
    values.append(_now_iso())
    values.append(wallet.lower())
    with get_connection(settings) as connection:
        connection.execute(
            f"UPDATE wallet_monitor_state SET {', '.join(cols)} WHERE wallet = ?",
            values,
        )


def get_wallet_monitor_state(settings: Settings, wallet: str) -> WalletMonitorState | None:
    with get_connection(settings) as connection:
        row = connection.execute(
            "SELECT * FROM wallet_monitor_state WHERE wallet = ?",
            (wallet.lower(),),
        ).fetchone()
    if row is None:
        return None
    keys = row.keys()
    return WalletMonitorState(
        wallet=row["wallet"],
        subscription_started_at=row["subscription_started_at"],
        baseline_started_at=row["baseline_started_at"],
        baseline_status=row["baseline_status"],
        baseline_error=row["baseline_error"],
        newest_event_ts=row["newest_event_ts"],
        newest_event_uid=row["newest_event_uid"],
        monitoring_enabled=bool(row["monitoring_enabled"]),
        updated_at=row["updated_at"],
        last_polled_at=row["last_polled_at"] if "last_polled_at" in keys else None,
        baseline_version=row["baseline_version"] if "baseline_version" in keys else None,
        last_complete_positions_at=(
            row["last_complete_positions_at"]
            if "last_complete_positions_at" in keys
            else None
        ),
        last_correlated_trade_ts=(
            int(row["last_correlated_trade_ts"])
            if "last_correlated_trade_ts" in keys
            and row["last_correlated_trade_ts"] is not None
            else None
        ),
    )


def upsert_wallet_event_watermark(
    settings: Settings,
    wallet: str,
    *,
    newest_event_ts: int | None,
    newest_event_uid: str | None,
) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO wallet_event_watermarks (
                wallet, newest_event_ts, newest_event_uid, updated_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                newest_event_ts = excluded.newest_event_ts,
                newest_event_uid = excluded.newest_event_uid,
                updated_at = excluded.updated_at
            """,
            (wallet.lower(), newest_event_ts, newest_event_uid, _now_iso()),
        )


def get_wallet_event_watermark(settings: Settings, wallet: str) -> sqlite3.Row | None:
    with get_connection(settings) as connection:
        return connection.execute(
            "SELECT * FROM wallet_event_watermarks WHERE wallet = ?",
            (wallet.lower(),),
        ).fetchone()


def mark_processed_copy_event(
    settings: Settings,
    *,
    event_uid: str,
    wallet: str,
    event_ts: int | None,
    observed_at: int | None,
    is_baseline: bool = False,
    alertable: bool = False,
    reject_reason: str | None = None,
    diagnostic_json: dict | str | None = None,
) -> bool:
    """Insert processed event marker. Returns False if already present."""
    if isinstance(diagnostic_json, dict):
        diagnostic_payload: str | None = json.dumps(diagnostic_json)
    else:
        diagnostic_payload = diagnostic_json
    with get_connection(settings) as connection:
        try:
            connection.execute(
                """
                INSERT INTO processed_copy_events (
                    event_uid, wallet, event_ts, observed_at, is_baseline,
                    alertable, reject_reason, created_at, diagnostic_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_uid,
                    wallet.lower(),
                    event_ts,
                    observed_at,
                    1 if is_baseline else 0,
                    1 if alertable else 0,
                    reject_reason,
                    _now_iso(),
                    diagnostic_payload,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def is_processed_copy_event(settings: Settings, event_uid: str) -> bool:
    with get_connection(settings) as connection:
        row = connection.execute(
            "SELECT 1 FROM processed_copy_events WHERE event_uid = ? LIMIT 1",
            (event_uid,),
        ).fetchone()
    return row is not None


def clear_position_snapshots(settings: Settings, wallet: str) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            "DELETE FROM copy_position_snapshots WHERE wallet = ?",
            (wallet.lower(),),
        )


def start_baseline_run(settings: Settings, wallet: str) -> int:
    with get_connection(settings) as connection:
        cursor = connection.execute(
            """
            INSERT INTO baseline_runs (wallet, started_at, status)
            VALUES (?, ?, 'RUNNING')
            """,
            (wallet.lower(), _now_iso()),
        )
        return int(cursor.lastrowid)


def finish_baseline_run(
    settings: Settings,
    run_id: int,
    *,
    status: str,
    positions_count: int | None = None,
    error: str | None = None,
) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            UPDATE baseline_runs
            SET finished_at = ?, status = ?, positions_count = ?, error = ?
            WHERE id = ?
            """,
            (_now_iso(), status, positions_count, error, run_id),
        )


def count_baseline_failures(settings: Settings, wallet: str, *, limit: int = 10) -> int:
    with get_connection(settings) as connection:
        rows = connection.execute(
            """
            SELECT status FROM baseline_runs
            WHERE wallet = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (wallet.lower(), limit),
        ).fetchall()
    return sum(1 for row in rows if row["status"] == "FAILED")


def get_latest_alertable_event_at(settings: Settings, wallet: str) -> str | None:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT created_at FROM processed_copy_events
            WHERE wallet = ? AND alertable = 1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (wallet.lower(),),
        ).fetchone()
    return row["created_at"] if row else None


def touch_wallet_last_polled(settings: Settings, wallet: str) -> None:
    now = _now_iso()
    state = get_wallet_monitor_state(settings, wallet)
    if state is None:
        return
    update_wallet_monitor_fields(settings, wallet, last_polled_at=now)


def prune_processed_copy_events(settings: Settings, *, keep_days: int = 30) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    with get_connection(settings) as connection:
        cursor = connection.execute(
            "DELETE FROM processed_copy_events WHERE created_at < ?",
            (cutoff_iso,),
        )
    return int(cursor.rowcount)


def try_acquire_job_lock(
    settings: Settings,
    *,
    job_name: str,
    owner: str,
    ttl_seconds: int,
) -> bool:
    """Atomically acquire a job lock. Stale locks (past expires_at) are stolen."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    expires = datetime.fromtimestamp(now.timestamp() + max(1, ttl_seconds), tz=timezone.utc).isoformat()
    with get_connection(settings) as connection:
        row = connection.execute(
            "SELECT owner, expires_at FROM copy_job_locks WHERE job_name = ?",
            (job_name,),
        ).fetchone()
        if row is not None:
            try:
                exp = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
            except ValueError:
                exp = now
            if exp > now:
                return False
            connection.execute(
                """
                UPDATE copy_job_locks
                SET owner = ?, locked_at = ?, expires_at = ?
                WHERE job_name = ?
                """,
                (owner, now_iso, expires, job_name),
            )
            return True
        try:
            connection.execute(
                """
                INSERT INTO copy_job_locks (job_name, owner, locked_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (job_name, owner, now_iso, expires),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def renew_job_lock(
    settings: Settings,
    *,
    job_name: str,
    owner: str,
    ttl_seconds: int,
) -> bool:
    """Extend lock expiry only when the caller still owns the lock."""
    now = datetime.now(timezone.utc)
    expires = datetime.fromtimestamp(
        now.timestamp() + max(1, ttl_seconds), tz=timezone.utc
    ).isoformat()
    with get_connection(settings) as connection:
        cursor = connection.execute(
            """
            UPDATE copy_job_locks
            SET expires_at = ?
            WHERE job_name = ? AND owner = ?
            """,
            (expires, job_name, owner),
        )
        return cursor.rowcount == 1


def release_job_lock(settings: Settings, *, job_name: str, owner: str) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            "DELETE FROM copy_job_locks WHERE job_name = ? AND owner = ?",
            (job_name, owner),
        )


def claim_copy_alert_delivery(
    settings: Settings,
    *,
    subscriber_id: int,
    wallet: str,
    event_uid: str,
    payload_text: str | None = None,
    decision: str | None = None,
    now: float | None = None,
    allow_retry: bool = True,
    max_attempts: int = 3,
) -> bool:
    """
    Atomically claim alert delivery for (subscriber, wallet, event_uid).

    Returns True only for the successful claimant (new insert or due retry reclaim).
    Never reclaims ``sent``, ``dead_letter``, ``suppressed``, or active ``claimed``.
    """
    from app.copy_intel.delivery import (
        STATUS_CLAIMED,
        STATUS_DEAD_LETTER,
        STATUS_FAILED_LEGACY,
        STATUS_RETRY_WAIT,
        STATUS_SENT,
        STATUS_SUPPRESSED,
        to_iso,
        utc_now,
    )

    now_dt = utc_now(now=now)
    now_s = to_iso(now_dt)
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT status, attempt_count, next_attempt_at FROM copy_alert_deliveries
            WHERE subscriber_id = ? AND wallet = ? AND event_uid = ?
            """,
            (subscriber_id, wallet.lower(), event_uid),
        ).fetchone()
        if row is None:
            try:
                connection.execute(
                    """
                    INSERT INTO copy_alert_deliveries (
                        subscriber_id, wallet, event_uid, status, payload_text,
                        created_at, updated_at, attempt_count, claimed_at, decision
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        subscriber_id,
                        wallet.lower(),
                        event_uid,
                        STATUS_CLAIMED,
                        payload_text,
                        now_s,
                        now_s,
                        now_s,
                        decision,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

        status = str(row["status"] or "")
        if status in {STATUS_SENT, STATUS_DEAD_LETTER, STATUS_SUPPRESSED, STATUS_CLAIMED}:
            return False
        if status not in {STATUS_RETRY_WAIT, STATUS_FAILED_LEGACY}:
            return False
        if not allow_retry:
            return False
        keys = row.keys()
        prior_attempts = int(row["attempt_count"] or 0) if "attempt_count" in keys else 0
        if prior_attempts >= max_attempts:
            return False
        next_at = row["next_attempt_at"] if "next_attempt_at" in keys else None
        if next_at:
            try:
                due = datetime.fromisoformat(str(next_at))
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                if due > now_dt:
                    return False
            except ValueError:
                pass
        cursor = connection.execute(
            """
            UPDATE copy_alert_deliveries
            SET status = ?, payload_text = COALESCE(?, payload_text),
                updated_at = ?, attempt_count = ?, claimed_at = ?,
                decision = COALESCE(?, decision),
                next_attempt_at = NULL
            WHERE subscriber_id = ? AND wallet = ? AND event_uid = ?
              AND status IN (?, ?)
            """,
            (
                STATUS_CLAIMED,
                payload_text,
                now_s,
                prior_attempts + 1,
                now_s,
                decision,
                subscriber_id,
                wallet.lower(),
                event_uid,
                STATUS_RETRY_WAIT,
                STATUS_FAILED_LEGACY,
            ),
        )
        return cursor.rowcount > 0


def mark_copy_alert_delivery(
    settings: Settings,
    *,
    subscriber_id: int,
    wallet: str,
    event_uid: str,
    status: str,
    payload_text: str | None = None,
    last_error_code: str | None = None,
    next_attempt_at: str | None = None,
    now: float | None = None,
) -> None:
    from app.copy_intel.delivery import STATUS_SENT, to_iso, utc_now

    now_s = to_iso(utc_now(now=now))
    sent_at = now_s if status == STATUS_SENT else None
    with get_connection(settings) as connection:
        if payload_text is not None:
            connection.execute(
                """
                UPDATE copy_alert_deliveries
                SET status = ?, payload_text = ?, updated_at = ?,
                    last_error_code = COALESCE(?, last_error_code),
                    next_attempt_at = ?,
                    sent_at = COALESCE(?, sent_at)
                WHERE subscriber_id = ? AND wallet = ? AND event_uid = ?
                """,
                (
                    status,
                    payload_text,
                    now_s,
                    last_error_code,
                    next_attempt_at,
                    sent_at,
                    subscriber_id,
                    wallet.lower(),
                    event_uid,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE copy_alert_deliveries
                SET status = ?, updated_at = ?,
                    last_error_code = COALESCE(?, last_error_code),
                    next_attempt_at = ?,
                    sent_at = COALESCE(?, sent_at)
                WHERE subscriber_id = ? AND wallet = ? AND event_uid = ?
                """,
                (
                    status,
                    now_s,
                    last_error_code,
                    next_attempt_at,
                    sent_at,
                    subscriber_id,
                    wallet.lower(),
                    event_uid,
                ),
            )


def update_copy_alert_delivery_retry(
    settings: Settings,
    *,
    subscriber_id: int,
    wallet: str,
    event_uid: str,
    attempt_count: int | None = None,
    next_attempt_at: str | None = None,
    last_error_code: str | None = None,
) -> None:
    """Update retry scheduling fields on an existing delivery row."""
    assignments = ["updated_at = ?"]
    values: list[object] = [_now_iso()]
    if attempt_count is not None:
        assignments.append("attempt_count = ?")
        values.append(int(attempt_count))
    if next_attempt_at is not None:
        assignments.append("next_attempt_at = ?")
        values.append(next_attempt_at)
    if last_error_code is not None:
        assignments.append("last_error_code = ?")
        values.append(last_error_code)
    values.extend([subscriber_id, wallet.lower(), event_uid])
    with get_connection(settings) as connection:
        connection.execute(
            f"""
            UPDATE copy_alert_deliveries
            SET {', '.join(assignments)}
            WHERE subscriber_id = ? AND wallet = ? AND event_uid = ?
            """,
            values,
        )


def get_copy_alert_delivery(
    settings: Settings,
    *,
    subscriber_id: int,
    wallet: str,
    event_uid: str,
) -> sqlite3.Row | None:
    with get_connection(settings) as connection:
        return connection.execute(
            """
            SELECT * FROM copy_alert_deliveries
            WHERE subscriber_id = ? AND wallet = ? AND event_uid = ?
            """,
            (subscriber_id, wallet.lower(), event_uid),
        ).fetchone()


def list_failed_copy_alert_deliveries(
    settings: Settings,
    *,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Legacy helper: failed/retry_wait rows with payload (no due-time filter)."""
    with get_connection(settings) as connection:
        return connection.execute(
            """
            SELECT * FROM copy_alert_deliveries
            WHERE status IN ('failed', 'retry_wait') AND payload_text IS NOT NULL
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def list_due_copy_alert_retries(
    settings: Settings,
    *,
    now: float | None = None,
    limit: int = 50,
    max_attempts: int = 3,
) -> list[sqlite3.Row]:
    """Rows due for bounded retry (retry_wait/failed, attempt_count < max, next_attempt due)."""
    from app.copy_intel.delivery import to_iso, utc_now

    now_s = to_iso(utc_now(now=now))
    with get_connection(settings) as connection:
        return connection.execute(
            """
            SELECT * FROM copy_alert_deliveries
            WHERE status IN ('failed', 'retry_wait')
              AND payload_text IS NOT NULL
              AND COALESCE(attempt_count, 0) < ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (max_attempts, now_s, limit),
        ).fetchall()


def get_recent_recommendations_for_owner(
    settings: Settings,
    owner_id: int,
    *,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """User-facing: recommendations only for wallets owned by ``owner_id``."""
    with get_connection(settings) as connection:
        return connection.execute(
            """
            SELECT r.* FROM copy_recommendations r
            INNER JOIN copy_watchlist w ON lower(w.wallet) = lower(r.wallet)
            WHERE w.added_by = ?
            ORDER BY r.id DESC
            LIMIT ?
            """,
            (int(owner_id), limit),
        ).fetchall()


def paper_exposure_totals_for_owner(settings: Settings, owner_id: int) -> dict[str, float]:
    """User-facing: open paper exposure limited to wallets owned by ``owner_id``."""
    with get_connection(settings) as connection:
        total = connection.execute(
            """
            SELECT COALESCE(SUM(p.notional), 0) AS s
            FROM copy_paper_positions p
            INNER JOIN copy_watchlist w ON lower(w.wallet) = lower(p.wallet)
            WHERE p.status = 'open' AND w.added_by = ?
            """,
            (int(owner_id),),
        ).fetchone()["s"]
        by_wallet_rows = connection.execute(
            """
            SELECT p.wallet, COALESCE(SUM(p.notional), 0) AS s
            FROM copy_paper_positions p
            INNER JOIN copy_watchlist w ON lower(w.wallet) = lower(p.wallet)
            WHERE p.status = 'open' AND w.added_by = ?
            GROUP BY p.wallet
            """,
            (int(owner_id),),
        ).fetchall()
        by_market_rows = connection.execute(
            """
            SELECT p.condition_id, COALESCE(SUM(p.notional), 0) AS s
            FROM copy_paper_positions p
            INNER JOIN copy_watchlist w ON lower(w.wallet) = lower(p.wallet)
            WHERE p.status = 'open' AND w.added_by = ?
            GROUP BY p.condition_id
            """,
            (int(owner_id),),
        ).fetchall()
    return {
        "total": float(total),
        "by_wallet": {row["wallet"]: float(row["s"]) for row in by_wallet_rows},
        "by_market": {row["condition_id"]: float(row["s"]) for row in by_market_rows},
    }


def list_open_paper_positions_for_owner(
    settings: Settings, owner_id: int
) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        return connection.execute(
            """
            SELECT p.* FROM copy_paper_positions p
            INNER JOIN copy_watchlist w ON lower(w.wallet) = lower(p.wallet)
            WHERE p.status = 'open' AND w.added_by = ?
            ORDER BY p.opened_at DESC
            """,
            (int(owner_id),),
        ).fetchall()


def list_watchlist_for_owner(settings: Settings, owner_id: int) -> list[WatchlistEntry]:
    """Explicit owner-scoped watchlist (user-facing)."""
    return [
        e for e in list_watchlist(settings) if e.added_by == int(owner_id)
    ]
