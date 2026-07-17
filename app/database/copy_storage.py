"""SQLite storage for copy-trading intelligence (V2 Phase 1, read-only)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_copy_column(
    connection: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    names = {row[1] for row in rows}
    if column not in names:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


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
    with get_connection(settings) as connection:
        cursor = connection.execute(
            """
            INSERT INTO copy_recommendations (
                wallet, market_slug, market_title, outcome, action, trader_score,
                max_alloc, reason, entry_price, current_price, price_drift,
                slippage_est, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        return int(cursor.lastrowid)


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
) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO copy_paper_positions (
                wallet, condition_id, market_slug, outcome, notional,
                entry_price, status, opened_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                wallet.lower(),
                condition_id,
                market_slug,
                outcome,
                notional,
                entry_price,
                _now_iso(),
            ),
        )


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


def save_copy_user_settings(settings: Settings, *, telegram_id: int, payload: dict) -> None:
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
    write_audit_log(
        settings,
        actor=str(telegram_id),
        action="copy_settings_save",
        payload={"profile": payload.get("profile")},
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
) -> bool:
    """Insert processed event marker. Returns False if already present."""
    with get_connection(settings) as connection:
        try:
            connection.execute(
                """
                INSERT INTO processed_copy_events (
                    event_uid, wallet, event_ts, observed_at, is_baseline,
                    alertable, reject_reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
) -> bool:
    """
    Atomically claim alert delivery for (subscriber, wallet, event_uid).
    Returns True only for the successful claimant (or a failed→retry reclaim).
    """
    now = _now_iso()
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT status FROM copy_alert_deliveries
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
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, 'claimed', ?, ?, ?)
                    """,
                    (
                        subscriber_id,
                        wallet.lower(),
                        event_uid,
                        payload_text,
                        now,
                        now,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False
        if row["status"] == "failed":
            cursor = connection.execute(
                """
                UPDATE copy_alert_deliveries
                SET status = 'claimed', payload_text = COALESCE(?, payload_text),
                    updated_at = ?
                WHERE subscriber_id = ? AND wallet = ? AND event_uid = ?
                  AND status = 'failed'
                """,
                (payload_text, now, subscriber_id, wallet.lower(), event_uid),
            )
            return cursor.rowcount > 0
        return False


def mark_copy_alert_delivery(
    settings: Settings,
    *,
    subscriber_id: int,
    wallet: str,
    event_uid: str,
    status: str,
    payload_text: str | None = None,
) -> None:
    now = _now_iso()
    with get_connection(settings) as connection:
        if payload_text is not None:
            connection.execute(
                """
                UPDATE copy_alert_deliveries
                SET status = ?, payload_text = ?, updated_at = ?
                WHERE subscriber_id = ? AND wallet = ? AND event_uid = ?
                """,
                (status, payload_text, now, subscriber_id, wallet.lower(), event_uid),
            )
        else:
            connection.execute(
                """
                UPDATE copy_alert_deliveries
                SET status = ?, updated_at = ?
                WHERE subscriber_id = ? AND wallet = ? AND event_uid = ?
                """,
                (status, now, subscriber_id, wallet.lower(), event_uid),
            )


def list_failed_copy_alert_deliveries(
    settings: Settings,
    *,
    limit: int = 50,
) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        return connection.execute(
            """
            SELECT * FROM copy_alert_deliveries
            WHERE status = 'failed' AND payload_text IS NOT NULL
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
