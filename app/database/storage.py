import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from app.config.settings import Settings


@contextmanager
def get_connection(settings: Settings) -> Iterator[sqlite3.Connection]:
    """Yield a transactional SQLite connection and always close it."""
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(settings: Settings) -> None:
    with get_connection(settings) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                language TEXT NOT NULL DEFAULT 'en',
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS user_filters (
                telegram_id INTEGER PRIMARY KEY,
                market_source TEXT NOT NULL DEFAULT 'polymarket',
                category TEXT NOT NULL DEFAULT 'weather',
                min_edge REAL NOT NULL DEFAULT 0.05,
                notifications_enabled INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS analysis_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                market_url TEXT NOT NULL,
                market_title TEXT,
                decision TEXT NOT NULL,
                confidence REAL,
                ev REAL,
                edge REAL,
                payload TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS report_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_window TEXT NOT NULL,
                market_url TEXT NOT NULL,
                market_title TEXT,
                recommendation TEXT,
                sent_at TEXT NOT NULL,
                UNIQUE(scan_window, market_url)
            )
        """)

        _ensure_column(connection, "analysis_records", "ev", "REAL")
        _ensure_column(connection, "analysis_records", "edge", "REAL")
        _ensure_column(connection, "analysis_records", "payload", "TEXT")

    from app.database.copy_storage import initialize_copy_schema
    from app.database.opportunity_storage import initialize_opportunity_schema
    from app.health.storage import initialize_health_schema

    initialize_copy_schema(settings)
    initialize_health_schema(settings)
    initialize_opportunity_schema(settings)


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    names = {row[1] for row in rows}
    if column not in names:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def upsert_user(
    settings: Settings,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    language: str = "en",
) -> None:
    with get_connection(settings) as connection:
        connection.execute("""
            INSERT INTO users (
                telegram_id,
                username,
                first_name,
                language,
                created_at,
                last_seen_at
            )
            VALUES (
                ?,
                ?,
                ?,
                ?,
                datetime('now'),
                datetime('now')
            )
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_seen_at = datetime('now')
        """, (telegram_id, username, first_name, language))

        connection.execute("""
            INSERT OR IGNORE INTO user_filters (
                telegram_id,
                market_source,
                category,
                min_edge,
                notifications_enabled
            )
            VALUES (?, 'polymarket', 'weather', 0.05, 0)
        """, (telegram_id,))


def save_analysis_record(
    settings: Settings,
    telegram_id: int,
    market_url: str,
    market_title: str | None,
    decision: str,
    confidence: float | None,
    ev: float | None = None,
    edge: float | None = None,
    payload: str | None = None,
) -> None:
    with get_connection(settings) as connection:
        connection.execute("""
            INSERT INTO analysis_records (
                telegram_id,
                market_url,
                market_title,
                decision,
                confidence,
                ev,
                edge,
                payload,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            telegram_id,
            market_url,
            market_title,
            decision,
            confidence,
            ev,
            edge,
            payload,
        ))


def get_recent_analyses(settings: Settings, limit: int = 10) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        rows = connection.execute("""
            SELECT market_title, market_url, decision, confidence, ev, edge, created_at
            FROM analysis_records
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return list(rows)


def was_report_sent(settings: Settings, scan_window: str, market_url: str) -> bool:
    with get_connection(settings) as connection:
        row = connection.execute("""
            SELECT 1 FROM report_history
            WHERE scan_window = ? AND market_url = ?
            LIMIT 1
        """, (scan_window, market_url)).fetchone()
    return row is not None


def mark_report_sent(
    settings: Settings,
    scan_window: str,
    market_url: str,
    market_title: str | None,
    recommendation: str | None,
) -> None:
    with get_connection(settings) as connection:
        connection.execute("""
            INSERT OR IGNORE INTO report_history (
                scan_window,
                market_url,
                market_title,
                recommendation,
                sent_at
            )
            VALUES (?, ?, ?, ?, datetime('now'))
        """, (scan_window, market_url, market_title, recommendation))
