"""SQLite persistence for the health monitor."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.config.settings import Settings
from app.database.storage import get_connection


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_health_schema(settings: Settings) -> None:
    with get_connection(settings) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS health_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                component TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                metrics_json TEXT,
                checked_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS component_states (
                component TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                consecutive_successes INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                metrics_json TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS health_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                component TEXT NOT NULL,
                incident_type TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                UNIQUE(component, incident_type, fingerprint)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS health_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id INTEGER NOT NULL,
                alert_key TEXT NOT NULL UNIQUE,
                sent_at TEXT NOT NULL,
                payload_json TEXT
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS job_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER,
                error TEXT,
                expected_interval_seconds INTEGER
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_job_executions_name_started
            ON job_executions(job_name, started_at DESC)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS provider_metrics (
                provider TEXT PRIMARY KEY,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_status_code INTEGER,
                last_error TEXT,
                last_success_at TEXT,
                last_failure_at TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS unauthorized_access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id_hash TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


def record_health_check(
    settings: Settings,
    *,
    component: str,
    status: str,
    detail: str,
    metrics: dict | None = None,
) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO health_checks (component, status, detail, metrics_json, checked_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (component, status, detail, json.dumps(metrics or {}), _now_iso()),
        )


def upsert_component_state(
    settings: Settings,
    *,
    component: str,
    status: str,
    consecutive_failures: int,
    consecutive_successes: int,
    detail: str,
    metrics: dict | None = None,
) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO component_states (
                component, status, consecutive_failures, consecutive_successes,
                detail, metrics_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(component) DO UPDATE SET
                status = excluded.status,
                consecutive_failures = excluded.consecutive_failures,
                consecutive_successes = excluded.consecutive_successes,
                detail = excluded.detail,
                metrics_json = excluded.metrics_json,
                updated_at = excluded.updated_at
            """,
            (
                component,
                status,
                consecutive_failures,
                consecutive_successes,
                detail,
                json.dumps(metrics or {}),
                _now_iso(),
            ),
        )


def get_component_state(settings: Settings, component: str) -> sqlite3.Row | None:
    with get_connection(settings) as connection:
        return connection.execute(
            "SELECT * FROM component_states WHERE component = ?",
            (component,),
        ).fetchone()


def list_component_states(settings: Settings) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        return list(
            connection.execute(
                "SELECT * FROM component_states ORDER BY component ASC"
            ).fetchall()
        )


def open_or_update_incident(
    settings: Settings,
    *,
    component: str,
    incident_type: str,
    fingerprint: str,
    severity: str,
    title: str,
    detail: str,
) -> tuple[int, str]:
    """
    Returns (incident_id, transition) where transition is
    'open' | 'updated' | 'unchanged'.
    """
    now = _now_iso()
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT id, state FROM health_incidents
            WHERE component = ? AND incident_type = ? AND fingerprint = ?
            """,
            (component, incident_type, fingerprint),
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO health_incidents (
                    component, incident_type, fingerprint, state, severity,
                    title, detail, opened_at, updated_at
                )
                VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)
                """,
                (component, incident_type, fingerprint, severity, title, detail, now, now),
            )
            return int(cursor.lastrowid), "open"
        if row["state"] == "resolved":
            connection.execute(
                """
                UPDATE health_incidents
                SET state = 'open', severity = ?, title = ?, detail = ?,
                    opened_at = ?, updated_at = ?, resolved_at = NULL
                WHERE id = ?
                """,
                (severity, title, detail, now, now, row["id"]),
            )
            return int(row["id"]), "open"
        if row["state"] in {"acknowledged", "suppressed"}:
            return int(row["id"]), "unchanged"
        connection.execute(
            """
            UPDATE health_incidents
            SET state = 'updated', severity = ?, title = ?, detail = ?, updated_at = ?
            WHERE id = ?
            """,
            (severity, title, detail, now, row["id"]),
        )
        return int(row["id"]), "updated"


def resolve_incident(
    settings: Settings,
    *,
    component: str,
    incident_type: str,
    fingerprint: str,
) -> int | None:
    now = _now_iso()
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT id, state FROM health_incidents
            WHERE component = ? AND incident_type = ? AND fingerprint = ?
            """,
            (component, incident_type, fingerprint),
        ).fetchone()
        if row is None or row["state"] == "resolved":
            return None
        connection.execute(
            """
            UPDATE health_incidents
            SET state = 'resolved', resolved_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, row["id"]),
        )
        return int(row["id"])


def acknowledge_incident(settings: Settings, incident_id: int) -> bool:
    with get_connection(settings) as connection:
        cursor = connection.execute(
            """
            UPDATE health_incidents
            SET state = 'acknowledged', updated_at = ?
            WHERE id = ? AND state IN ('open', 'updated')
            """,
            (_now_iso(), incident_id),
        )
    return cursor.rowcount > 0


def list_recent_incidents(settings: Settings, *, limit: int = 20) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        return list(
            connection.execute(
                """
                SELECT * FROM health_incidents
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )


def was_alert_sent(settings: Settings, alert_key: str) -> bool:
    with get_connection(settings) as connection:
        row = connection.execute(
            "SELECT 1 FROM health_alerts WHERE alert_key = ? LIMIT 1",
            (alert_key,),
        ).fetchone()
    return row is not None


def record_alert_sent(
    settings: Settings,
    *,
    incident_id: int,
    alert_key: str,
    payload: dict | None = None,
) -> bool:
    with get_connection(settings) as connection:
        try:
            connection.execute(
                """
                INSERT INTO health_alerts (incident_id, alert_key, sent_at, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (incident_id, alert_key, _now_iso(), json.dumps(payload or {})),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def insert_job_execution(
    settings: Settings,
    *,
    job_name: str,
    status: str,
    started_at: str,
    expected_interval_seconds: int | None = None,
) -> int:
    with get_connection(settings) as connection:
        cursor = connection.execute(
            """
            INSERT INTO job_executions (
                job_name, status, started_at, expected_interval_seconds
            )
            VALUES (?, ?, ?, ?)
            """,
            (job_name, status, started_at, expected_interval_seconds),
        )
        return int(cursor.lastrowid)


def finish_latest_job_execution(
    settings: Settings,
    *,
    job_name: str,
    status: str,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT id FROM job_executions
            WHERE job_name = ? AND finished_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (job_name,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO job_executions (
                    job_name, status, started_at, finished_at, duration_ms, error
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_name,
                    status,
                    _now_iso(),
                    _now_iso(),
                    duration_ms,
                    error,
                ),
            )
            return
        connection.execute(
            """
            UPDATE job_executions
            SET status = ?, finished_at = ?, duration_ms = ?, error = ?
            WHERE id = ?
            """,
            (status, _now_iso(), duration_ms, error, row["id"]),
        )


def latest_job_execution(settings: Settings, job_name: str) -> sqlite3.Row | None:
    with get_connection(settings) as connection:
        return connection.execute(
            """
            SELECT * FROM job_executions
            WHERE job_name = ?
            ORDER BY id DESC LIMIT 1
            """,
            (job_name,),
        ).fetchone()


def list_recent_job_executions(
    settings: Settings, *, limit: int = 30
) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        return list(
            connection.execute(
                """
                SELECT * FROM job_executions
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )


def upsert_provider_metric(
    settings: Settings,
    provider: str,
    *,
    success: bool,
    status_code: int | None = None,
    error: str | None = None,
) -> None:
    now = _now_iso()
    with get_connection(settings) as connection:
        row = connection.execute(
            "SELECT * FROM provider_metrics WHERE provider = ?",
            (provider,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO provider_metrics (
                    provider, success_count, failure_count, last_status_code,
                    last_error, last_success_at, last_failure_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    1 if success else 0,
                    0 if success else 1,
                    status_code,
                    error,
                    now if success else None,
                    None if success else now,
                    now,
                ),
            )
            return
        if success:
            connection.execute(
                """
                UPDATE provider_metrics
                SET success_count = success_count + 1,
                    last_status_code = ?,
                    last_error = NULL,
                    last_success_at = ?,
                    updated_at = ?
                WHERE provider = ?
                """,
                (status_code, now, now, provider),
            )
        else:
            connection.execute(
                """
                UPDATE provider_metrics
                SET failure_count = failure_count + 1,
                    last_status_code = ?,
                    last_error = ?,
                    last_failure_at = ?,
                    updated_at = ?
                WHERE provider = ?
                """,
                (status_code, error, now, now, provider),
            )


def list_provider_metrics(settings: Settings) -> list[sqlite3.Row]:
    with get_connection(settings) as connection:
        return list(
            connection.execute(
                "SELECT * FROM provider_metrics ORDER BY provider ASC"
            ).fetchall()
        )


def log_unauthorized_access(settings: Settings, user_id_hash: str, action: str) -> None:
    with get_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO unauthorized_access_log (user_id_hash, action, created_at)
            VALUES (?, ?, ?)
            """,
            (user_id_hash, action, _now_iso()),
        )


def prune_health_history(settings: Settings, *, keep_days: int = 14) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    with get_connection(settings) as connection:
        connection.execute(
            "DELETE FROM health_checks WHERE checked_at < ?",
            (cutoff_iso,),
        )
        connection.execute(
            "DELETE FROM job_executions WHERE started_at < ?",
            (cutoff_iso,),
        )
        connection.execute(
            "DELETE FROM health_alerts WHERE sent_at < ?",
            (cutoff_iso,),
        )
