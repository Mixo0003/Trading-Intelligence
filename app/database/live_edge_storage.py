"""Additive Live Edge SQLite storage. Isolated from opportunity_signals."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, TypeVar

from app.config.settings import Settings
from app.database.storage import get_connection
from app.opportunity_intel.live_edge.models import (
    LiveEdgeDecision,
    LiveEdgeSignalType,
    WaitTrigger,
)
from app.opportunity_intel.live_edge.versions import (
    LIVE_EDGE_MIGRATION_ID,
    LIVE_EDGE_SCHEMA_VERSION,
)

_TABLES = frozenset(
    {
        "live_edge_signals",
        "live_edge_wait",
        "live_edge_deliveries",
        "live_edge_history",
        "live_edge_missed",
    }
)
_BUSY_TIMEOUT_MS = 20_000
_WRITE_RETRIES = 4
_T = TypeVar("_T")


@contextmanager
def _conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    with get_connection(settings) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        yield connection


def _is_locked_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in message or "busy" in message
    )


def retry_locked_write(action: Callable[[], _T]) -> _T:
    """Retry short Live Edge writes when SQLite reports busy/locked."""
    last: BaseException | None = None
    for attempt in range(_WRITE_RETRIES):
        try:
            return action()
        except sqlite3.OperationalError as exc:
            if not _is_locked_error(exc) or attempt + 1 >= _WRITE_RETRIES:
                raise
            last = exc
            time.sleep(0.2 * (2 ** attempt))
    assert last is not None
    raise last


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def initialize_live_edge_schema(settings: Settings) -> bool:
    """Idempotent additive migration. Does not alter existing scanner tables."""
    with _conn(settings) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS live_edge_schema_migrations (
                migration_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                applied_at TEXT NOT NULL
            )
            """
        )
        existing = connection.execute(
            "SELECT 1 FROM live_edge_schema_migrations WHERE migration_id = ?",
            (LIVE_EDGE_MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            _validate(connection)
            return False
        for statement in _DDL:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO live_edge_schema_migrations (
                migration_id, schema_version, applied_at
            ) VALUES (?, ?, ?)
            """,
            (LIVE_EDGE_MIGRATION_ID, LIVE_EDGE_SCHEMA_VERSION, _iso(_now())),
        )
        _validate(connection)
        return True


def _validate(connection: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = sorted(_TABLES - tables)
    if missing:
        raise RuntimeError(f"incomplete live_edge schema: {missing}")


@dataclass(frozen=True)
class StoredLiveEdgeSignal:
    signal_uid: str
    telegram_id: int
    market_id: str
    asset_id: str
    question: str
    category: str
    signal_type: str
    decision: str
    side: str
    conservative_p_low: str | None
    conservative_p_high: str | None
    executable_price: str | None
    edge_pp: str | None
    ev_usd: str | None
    roi: str | None
    reasons_json: str
    risks_json: str
    wait_trigger_json: str | None
    provenance_json: str
    card_json: str
    first_detected_at: str
    first_wait_at: str | None
    first_buy_at: str | None
    best_executable_price: str | None
    best_edge_pp: str | None
    best_ev_usd: str | None
    final_outcome: str | None
    missed: int
    missed_reason: str | None
    evaluated_at: str
    quote_age_seconds: str | None
    live_data_age_seconds: str | None


_UPSERT_SQL = """
            INSERT INTO live_edge_signals (
                signal_uid, telegram_id, market_id, asset_id, question, category, signal_type,
                decision, side, conservative_p_low, conservative_p_high,
                executable_price, edge_pp, ev_usd, roi, reasons_json, risks_json,
                wait_trigger_json, provenance_json, card_json, first_detected_at,
                first_wait_at, first_buy_at, best_executable_price, best_edge_pp,
                best_ev_usd, final_outcome, missed, missed_reason, evaluated_at,
                quote_age_seconds, live_data_age_seconds, created_at, updated_at
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            ON CONFLICT(signal_uid) DO UPDATE SET
                decision = excluded.decision,
                executable_price = excluded.executable_price,
                edge_pp = excluded.edge_pp,
                ev_usd = excluded.ev_usd,
                roi = excluded.roi,
                wait_trigger_json = excluded.wait_trigger_json,
                card_json = excluded.card_json,
                first_wait_at = COALESCE(live_edge_signals.first_wait_at, excluded.first_wait_at),
                first_buy_at = COALESCE(live_edge_signals.first_buy_at, excluded.first_buy_at),
                best_executable_price = excluded.best_executable_price,
                best_edge_pp = excluded.best_edge_pp,
                best_ev_usd = excluded.best_ev_usd,
                evaluated_at = excluded.evaluated_at,
                updated_at = excluded.updated_at
            """


def _upsert_params(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["signal_uid"],
        int(row["telegram_id"]),
        row["market_id"],
        row["asset_id"],
        row["question"],
        row["category"],
        row["signal_type"],
        row["decision"],
        row["side"],
        row.get("conservative_p_low"),
        row.get("conservative_p_high"),
        row.get("executable_price"),
        row.get("edge_pp"),
        row.get("ev_usd"),
        row.get("roi"),
        row["reasons_json"],
        row["risks_json"],
        row.get("wait_trigger_json"),
        row["provenance_json"],
        row["card_json"],
        row["first_detected_at"],
        row.get("first_wait_at"),
        row.get("first_buy_at"),
        row.get("best_executable_price"),
        row.get("best_edge_pp"),
        row.get("best_ev_usd"),
        row.get("final_outcome"),
        int(row.get("missed") or 0),
        row.get("missed_reason"),
        row["evaluated_at"],
        row.get("quote_age_seconds"),
        row.get("live_data_age_seconds"),
        row["first_detected_at"],
        row["evaluated_at"],
    )


def upsert_signal(settings: Settings, row: dict[str, Any]) -> None:
    upsert_signals(settings, (row,))


def upsert_signals(settings: Settings, rows: Sequence[dict[str, Any]]) -> None:
    """Persist many signals in one short transaction. Idempotent on signal_uid."""
    if not rows:
        return
    initialize_live_edge_schema(settings)
    payload = tuple(_upsert_params(row) for row in rows)

    def _write() -> None:
        with _conn(settings) as connection:
            connection.executemany(_UPSERT_SQL, payload)

    retry_locked_write(_write)


def list_active_signals(
    settings: Settings, *, telegram_id: int | None = None, limit: int = 20
) -> tuple[StoredLiveEdgeSignal, ...]:
    initialize_live_edge_schema(settings)
    with _conn(settings) as connection:
        if telegram_id is None:
            rows = connection.execute(
                """
                SELECT * FROM live_edge_signals
                WHERE decision IN ('BUY_NOW', 'WAIT', 'TOO_LATE', 'WATCH')
                ORDER BY CASE decision
                    WHEN 'BUY_NOW' THEN 0
                    WHEN 'WAIT' THEN 1
                    WHEN 'TOO_LATE' THEN 2
                    WHEN 'WATCH' THEN 3
                    ELSE 4
                END,
                evaluated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM live_edge_signals
                WHERE telegram_id = ?
                  AND decision IN ('BUY_NOW', 'WAIT', 'TOO_LATE', 'WATCH')
                ORDER BY CASE decision
                    WHEN 'BUY_NOW' THEN 0
                    WHEN 'WAIT' THEN 1
                    WHEN 'TOO_LATE' THEN 2
                    WHEN 'WATCH' THEN 3
                    ELSE 4
                END,
                evaluated_at DESC
                LIMIT ?
                """,
                (telegram_id, limit),
            ).fetchall()
    return tuple(_from_row(row) for row in rows)


def list_history(
    settings: Settings, *, telegram_id: int | None = None, limit: int = 20
) -> tuple[StoredLiveEdgeSignal, ...]:
    initialize_live_edge_schema(settings)
    with _conn(settings) as connection:
        if telegram_id is None:
            rows = connection.execute(
                """
                SELECT * FROM live_edge_signals
                ORDER BY first_detected_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM live_edge_signals
                WHERE telegram_id = ?
                ORDER BY first_detected_at DESC
                LIMIT ?
                """,
                (telegram_id, limit),
            ).fetchall()
    return tuple(_from_row(row) for row in rows)


def load_signal(settings: Settings, signal_uid: str) -> StoredLiveEdgeSignal | None:
    initialize_live_edge_schema(settings)
    with _conn(settings) as connection:
        row = connection.execute(
            "SELECT * FROM live_edge_signals WHERE signal_uid = ?",
            (signal_uid,),
        ).fetchone()
    return None if row is None else _from_row(row)


def record_delivery(
    settings: Settings,
    *,
    signal_uid: str,
    telegram_id: int,
    kind: str,
    delivered_at: datetime,
) -> bool:
    """Return True if this is the first delivery of this kind to this user."""
    initialize_live_edge_schema(settings)
    with _conn(settings) as connection:
        try:
            connection.execute(
                """
                INSERT INTO live_edge_deliveries (
                    signal_uid, telegram_id, kind, delivered_at
                ) VALUES (?, ?, ?, ?)
                """,
                (signal_uid, telegram_id, kind, _iso(delivered_at)),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def already_delivered(
    settings: Settings, *, signal_uid: str, telegram_id: int, kind: str
) -> bool:
    initialize_live_edge_schema(settings)
    with _conn(settings) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM live_edge_deliveries
            WHERE signal_uid = ? AND telegram_id = ? AND kind = ?
            """,
            (signal_uid, telegram_id, kind),
        ).fetchone()
    return row is not None


WATCH_DELIVERY_KIND_PREFIX = "watch:"
WATCH_WINDOW_SECONDS = 30 * 60
WATCH_MAX_PER_WINDOW = 3


def count_watch_deliveries_since(
    settings: Settings, *, telegram_id: int, since: datetime
) -> int:
    initialize_live_edge_schema(settings)
    with _conn(settings) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) FROM live_edge_deliveries
            WHERE telegram_id = ?
              AND kind LIKE ?
              AND delivered_at >= ?
            """,
            (telegram_id, f"{WATCH_DELIVERY_KIND_PREFIX}%", _iso(since)),
        ).fetchone()
    return int(row[0]) if row else 0


def watch_already_sent(
    settings: Settings, *, signal_uid: str, telegram_id: int, fingerprint: str
) -> bool:
    return already_delivered(
        settings,
        signal_uid=signal_uid,
        telegram_id=telegram_id,
        kind=f"{WATCH_DELIVERY_KIND_PREFIX}{fingerprint}",
    )


def upsert_wait(
    settings: Settings,
    *,
    wait_uid: str,
    signal_uid: str,
    market_id: str,
    asset_id: str,
    telegram_id: int,
    trigger: WaitTrigger,
    created_at: datetime,
) -> None:
    initialize_live_edge_schema(settings)
    payload = json.dumps(
        {
            "description_uk": trigger.description_uk,
            "max_yes_ask": str(trigger.max_yes_ask) if trigger.max_yes_ask else None,
            "min_edge_pp": str(trigger.min_edge_pp) if trigger.min_edge_pp else None,
            "required_map_win": trigger.required_map_win,
            "require_executable_bankroll": trigger.require_executable_bankroll,
        },
        separators=(",", ":"),
    )
    with _conn(settings) as connection:
        connection.execute(
            """
            INSERT INTO live_edge_wait (
                wait_uid, signal_uid, market_id, asset_id, telegram_id,
                trigger_json, created_at, last_checked_at, fired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(wait_uid) DO UPDATE SET
                trigger_json = excluded.trigger_json
            """,
            (
                wait_uid,
                signal_uid,
                market_id,
                asset_id,
                telegram_id,
                payload,
                _iso(created_at),
            ),
        )


def list_open_waits(settings: Settings) -> tuple[sqlite3.Row, ...]:
    initialize_live_edge_schema(settings)
    with _conn(settings) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM live_edge_wait WHERE fired_at IS NULL"
        ).fetchall()
    return tuple(rows)


def mark_wait_fired(settings: Settings, wait_uid: str, *, fired_at: datetime) -> None:
    initialize_live_edge_schema(settings)
    with _conn(settings) as connection:
        connection.execute(
            "UPDATE live_edge_wait SET fired_at = ? WHERE wait_uid = ?",
            (_iso(fired_at), wait_uid),
        )


def record_missed(
    settings: Settings,
    *,
    market_id: str,
    question: str,
    reason: str,
    window_json: str,
    created_at: datetime,
) -> None:
    initialize_live_edge_schema(settings)
    with _conn(settings) as connection:
        connection.execute(
            """
            INSERT INTO live_edge_missed (
                market_id, question, reason, window_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (market_id, question, reason, window_json, _iso(created_at)),
        )


def parse_wait_trigger(payload: str) -> WaitTrigger:
    data = json.loads(payload)
    return WaitTrigger(
        description_uk=data["description_uk"],
        max_yes_ask=(
            Decimal(data["max_yes_ask"]) if data.get("max_yes_ask") else None
        ),
        min_edge_pp=(
            Decimal(data["min_edge_pp"]) if data.get("min_edge_pp") else None
        ),
        required_map_win=data.get("required_map_win"),
        require_executable_bankroll=bool(data.get("require_executable_bankroll")),
    )


def _from_row(row: sqlite3.Row) -> StoredLiveEdgeSignal:
    return StoredLiveEdgeSignal(
        signal_uid=row["signal_uid"],
        telegram_id=int(row["telegram_id"]),
        market_id=row["market_id"],
        asset_id=row["asset_id"],
        question=row["question"],
        category=row["category"],
        signal_type=row["signal_type"],
        decision=row["decision"],
        side=row["side"],
        conservative_p_low=row["conservative_p_low"],
        conservative_p_high=row["conservative_p_high"],
        executable_price=row["executable_price"],
        edge_pp=row["edge_pp"],
        ev_usd=row["ev_usd"],
        roi=row["roi"],
        reasons_json=row["reasons_json"],
        risks_json=row["risks_json"],
        wait_trigger_json=row["wait_trigger_json"],
        provenance_json=row["provenance_json"],
        card_json=row["card_json"],
        first_detected_at=row["first_detected_at"],
        first_wait_at=row["first_wait_at"],
        first_buy_at=row["first_buy_at"],
        best_executable_price=row["best_executable_price"],
        best_edge_pp=row["best_edge_pp"],
        best_ev_usd=row["best_ev_usd"],
        final_outcome=row["final_outcome"],
        missed=int(row["missed"]),
        missed_reason=row["missed_reason"],
        evaluated_at=row["evaluated_at"],
        quote_age_seconds=row["quote_age_seconds"],
        live_data_age_seconds=row["live_data_age_seconds"],
    )


_DDL = (
    """
    CREATE TABLE IF NOT EXISTS live_edge_signals (
        signal_uid TEXT PRIMARY KEY,
        telegram_id INTEGER NOT NULL,
        market_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        question TEXT NOT NULL,
        category TEXT NOT NULL,
        signal_type TEXT NOT NULL,
        decision TEXT NOT NULL,
        side TEXT NOT NULL,
        conservative_p_low TEXT,
        conservative_p_high TEXT,
        executable_price TEXT,
        edge_pp TEXT,
        ev_usd TEXT,
        roi TEXT,
        reasons_json TEXT NOT NULL,
        risks_json TEXT NOT NULL,
        wait_trigger_json TEXT,
        provenance_json TEXT NOT NULL,
        card_json TEXT NOT NULL,
        first_detected_at TEXT NOT NULL,
        first_wait_at TEXT,
        first_buy_at TEXT,
        best_executable_price TEXT,
        best_edge_pp TEXT,
        best_ev_usd TEXT,
        final_outcome TEXT,
        missed INTEGER NOT NULL DEFAULT 0,
        missed_reason TEXT,
        evaluated_at TEXT NOT NULL,
        quote_age_seconds TEXT,
        live_data_age_seconds TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_edge_wait (
        wait_uid TEXT PRIMARY KEY,
        signal_uid TEXT NOT NULL,
        market_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        telegram_id INTEGER NOT NULL,
        trigger_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_checked_at TEXT,
        fired_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_edge_deliveries (
        signal_uid TEXT NOT NULL,
        telegram_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        delivered_at TEXT NOT NULL,
        PRIMARY KEY (signal_uid, telegram_id, kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_edge_history (
        history_id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_uid TEXT NOT NULL,
        telegram_id INTEGER,
        event TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_edge_missed (
        missed_id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT NOT NULL,
        question TEXT NOT NULL,
        reason TEXT NOT NULL,
        window_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_live_edge_signals_decision ON live_edge_signals(decision, evaluated_at)",
    "CREATE INDEX IF NOT EXISTS idx_live_edge_signals_user ON live_edge_signals(telegram_id, evaluated_at)",
    "CREATE INDEX IF NOT EXISTS idx_live_edge_wait_open ON live_edge_wait(fired_at, telegram_id)",
    "CREATE INDEX IF NOT EXISTS idx_live_edge_deliveries_user ON live_edge_deliveries(telegram_id, kind)",
)
