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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
