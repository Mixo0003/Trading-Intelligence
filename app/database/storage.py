import sqlite3
from pathlib import Path

from app.config.settings import Settings


def get_connection(settings: Settings) -> sqlite3.Connection:
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


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
                created_at TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
            )
        """)


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
