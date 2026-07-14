import json
import sqlite3
from pathlib import Path

from app.config.settings import Settings
from app.weather.market_forecasts import ForecastScan


def get_connection(settings: Settings) -> sqlite3.Connection:
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path, timeout=5.0)
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

        connection.execute("""
            CREATE TABLE IF NOT EXISTS weather_scan_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_timestamp TEXT NOT NULL,
                discovered_count INTEGER NOT NULL,
                analyzed_count INTEGER NOT NULL,
                forecast_result_count INTEGER NOT NULL,
                actionable_count INTEGER NOT NULL,
                skip_reason_counts_json TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 1
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS weather_scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                event_id TEXT,
                child_market_id TEXT,
                market_url TEXT,
                city TEXT NOT NULL,
                station TEXT,
                target_date TEXT,
                market_type TEXT,
                forecast_value REAL,
                expected_low REAL,
                expected_high REAL,
                selected_bucket TEXT,
                model_probability REAL,
                live_price REAL,
                status TEXT NOT NULL,
                validation_reason TEXT NOT NULL,
                forecast_models_json TEXT NOT NULL,
                source_divergence REAL,
                provider_spread REAL,
                result_json TEXT NOT NULL,
                FOREIGN KEY (snapshot_id) REFERENCES weather_scan_snapshots (id)
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


def save_completed_weather_scan(settings: Settings, scan: ForecastScan) -> int:
    """Atomically persist a completed scan and every analyzed result."""
    initialize_database(settings)
    payload = json.dumps(scan.to_dict(), separators=(",", ":"), ensure_ascii=False)
    with get_connection(settings) as connection:
        cursor = connection.execute(
            """
            INSERT INTO weather_scan_snapshots (
                scan_timestamp, discovered_count, analyzed_count,
                forecast_result_count, actionable_count,
                skip_reason_counts_json, snapshot_json, completed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                scan.scan_timestamp,
                scan.discovered_count,
                scan.analyzed_count,
                scan.forecast_result_count,
                scan.actionable_count,
                json.dumps(scan.skip_reason_counts, ensure_ascii=False),
                payload,
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        for result in scan.results:
            connection.execute(
                """
                INSERT INTO weather_scan_results (
                    snapshot_id, event_id, child_market_id, market_url, city,
                    station, target_date, market_type, forecast_value,
                    expected_low, expected_high, selected_bucket,
                    model_probability, live_price, status, validation_reason,
                    forecast_models_json, source_divergence, provider_spread,
                    result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    result.event_id,
                    result.child_market_id,
                    result.market_url,
                    result.city,
                    result.station,
                    result.target_date,
                    result.market_type,
                    result.forecast_value,
                    result.expected_low,
                    result.expected_high,
                    result.selected_bucket,
                    result.model_probability,
                    result.live_price,
                    result.status,
                    result.validation_reason,
                    json.dumps(result.forecast_models),
                    result.source_divergence,
                    result.provider_spread,
                    json.dumps(result.to_dict(), separators=(",", ":"), ensure_ascii=False),
                ),
            )
    return snapshot_id


def load_latest_completed_weather_scan(settings: Settings) -> ForecastScan | None:
    initialize_database(settings)
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT snapshot_json
            FROM weather_scan_snapshots
            WHERE completed = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return ForecastScan.from_dict(json.loads(row["snapshot_json"]))
