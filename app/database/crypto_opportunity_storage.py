"""Additive storage for V4.3 crypto opportunity profiles and delivery."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from app.config.settings import Settings
from app.crypto.opportunity.settings import (
    CryptoOpportunitySettings,
    CryptoOpportunitySubscription,
    balanced_crypto_settings,
)
from app.crypto.venue_registry import V43_EXCHANGE_IDS
from app.database.storage import get_connection

MIGRATION_ID = "crypto_opportunity_v1"
SCHEMA_VERSION = 1


def _apply_schema(connection: sqlite3.Connection) -> None:
    """Additive idempotent schema on an open connection. Never DROP."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_opportunity_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    # Always ensure tables exist (safe for :memory: reconnects).
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS crypto_opportunity_subscriptions (
            telegram_id INTEGER PRIMARY KEY,
            revision INTEGER NOT NULL,
            settings_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS crypto_opportunity_last_sent (
            telegram_id INTEGER NOT NULL,
            opportunity_key TEXT NOT NULL,
            opportunity_uid TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (telegram_id, opportunity_key)
        );

        CREATE TABLE IF NOT EXISTS crypto_opportunity_deliveries (
            telegram_id INTEGER NOT NULL,
            opportunity_uid TEXT NOT NULL,
            opportunity_key TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_text TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            claim_id TEXT,
            claim_expires_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (telegram_id, opportunity_uid)
        );

        CREATE INDEX IF NOT EXISTS idx_crypto_opp_deliveries_status
            ON crypto_opportunity_deliveries (status, next_attempt_at);
        """
    )
    existing = connection.execute(
        """
        SELECT 1 FROM crypto_opportunity_schema_migrations
        WHERE migration_id = ?
        """,
        (MIGRATION_ID,),
    ).fetchone()
    if existing is None:
        applied_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO crypto_opportunity_schema_migrations
                (migration_id, schema_version, applied_at)
            VALUES (?, ?, ?)
            """,
            (MIGRATION_ID, SCHEMA_VERSION, applied_at),
        )


def is_crypto_opportunity_migration_applied(settings: Settings) -> bool:
    with get_connection(settings) as connection:
        _apply_schema(connection)
        row = connection.execute(
            """
            SELECT 1 FROM crypto_opportunity_schema_migrations
            WHERE migration_id = ?
            """,
            (MIGRATION_ID,),
        ).fetchone()
    return row is not None


def initialize_crypto_opportunity_schema(settings: Settings) -> None:
    with get_connection(settings) as connection:
        _apply_schema(connection)


def _settings_to_json(value: CryptoOpportunitySettings) -> str:
    return json.dumps(
        {
            "revision": value.revision,
            "selected_exchanges": list(value.selected_exchanges),
            "selected_product_types": list(value.selected_product_types),
            "paper_allocation_usd": str(value.paper_allocation_usd),
            "minimum_net_profit_usd": str(value.minimum_net_profit_usd),
            "minimum_roi": str(value.minimum_roi),
            "maximum_slippage": str(value.maximum_slippage),
            "minimum_executable_depth_usd": str(value.minimum_executable_depth_usd),
            "maximum_signal_age_seconds": value.maximum_signal_age_seconds,
            "risk_preference": value.risk_preference,
            "evidence_requirements": value.evidence_requirements,
            "delivery_enabled": value.delivery_enabled,
            "cooldown_seconds": value.cooldown_seconds,
        }
    )


def _settings_from_json(raw: str) -> CryptoOpportunitySettings:
    payload = json.loads(raw)
    exchanges = tuple(payload.get("selected_exchanges") or list(V43_EXCHANGE_IDS))
    return CryptoOpportunitySettings(
        revision=int(payload.get("revision") or 0),
        selected_exchanges=exchanges,
        selected_product_types=tuple(
            payload.get("selected_product_types") or ("spot",)
        ),
        paper_allocation_usd=Decimal(str(payload["paper_allocation_usd"])),
        minimum_net_profit_usd=Decimal(str(payload.get("minimum_net_profit_usd", "0"))),
        minimum_roi=Decimal(str(payload.get("minimum_roi", "0"))),
        maximum_slippage=Decimal(str(payload["maximum_slippage"])),
        minimum_executable_depth_usd=Decimal(
            str(payload["minimum_executable_depth_usd"])
        ),
        maximum_signal_age_seconds=int(payload["maximum_signal_age_seconds"]),
        risk_preference=str(payload.get("risk_preference") or "balanced"),
        evidence_requirements=str(
            payload.get("evidence_requirements") or "public_book_and_fees"
        ),
        delivery_enabled=bool(payload.get("delivery_enabled", False)),
        cooldown_seconds=int(payload["cooldown_seconds"]),
    )


def ensure_profile(
    settings: Settings,
    telegram_id: int,
    defaults: CryptoOpportunitySettings | None = None,
) -> CryptoOpportunitySettings:
    existing = get_subscription(settings, telegram_id)
    if existing is not None:
        return existing.settings
    value = defaults or balanced_crypto_settings()
    save_subscription(
        settings,
        CryptoOpportunitySubscription(telegram_id=telegram_id, settings=value),
    )
    return value


def get_or_default_settings(
    settings: Settings, telegram_id: int
) -> CryptoOpportunitySettings:
    existing = get_subscription(settings, telegram_id)
    if existing is None:
        return balanced_crypto_settings()
    return existing.settings


def get_subscription(
    settings: Settings, telegram_id: int
) -> CryptoOpportunitySubscription | None:
    with get_connection(settings) as connection:
        _apply_schema(connection)
        row = connection.execute(
            """
            SELECT telegram_id, settings_json
            FROM crypto_opportunity_subscriptions
            WHERE telegram_id = ?
            """,
            (int(telegram_id),),
        ).fetchone()
    if row is None:
        return None
    return CryptoOpportunitySubscription(
        telegram_id=int(row[0]),
        settings=_settings_from_json(str(row[1])),
    )


def save_subscription(
    settings: Settings, subscription: CryptoOpportunitySubscription
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(settings) as connection:
        _apply_schema(connection)
        connection.execute(
            """
            INSERT INTO crypto_opportunity_subscriptions (
                telegram_id, revision, settings_json, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                revision = excluded.revision,
                settings_json = excluded.settings_json,
                updated_at = excluded.updated_at
            """,
            (
                int(subscription.telegram_id),
                int(subscription.settings.revision),
                _settings_to_json(subscription.settings),
                now,
            ),
        )


def last_sent_at_for_key(
    settings: Settings, *, telegram_id: int, opportunity_key: str
) -> datetime | None:
    with get_connection(settings) as connection:
        _apply_schema(connection)
        row = connection.execute(
            """
            SELECT sent_at FROM crypto_opportunity_last_sent
            WHERE telegram_id = ? AND opportunity_key = ?
            """,
            (int(telegram_id), opportunity_key),
        ).fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))


def record_sent(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_key: str,
    opportunity_uid: str,
    sent_at: datetime | None = None,
) -> None:
    when = (sent_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    with get_connection(settings) as connection:
        _apply_schema(connection)
        connection.execute(
            """
            INSERT INTO crypto_opportunity_last_sent (
                telegram_id, opportunity_key, opportunity_uid, sent_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id, opportunity_key) DO UPDATE SET
                opportunity_uid = excluded.opportunity_uid,
                sent_at = excluded.sent_at
            """,
            (int(telegram_id), opportunity_key, opportunity_uid, when),
        )


def insert_delivery(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_uid: str,
    opportunity_key: str,
    payload_text: str,
    status: str = "PENDING",
) -> bool:
    """Insert delivery row. Returns False if duplicate uid for user."""
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(settings) as connection:
        _apply_schema(connection)
        try:
            connection.execute(
                """
                INSERT INTO crypto_opportunity_deliveries (
                    telegram_id, opportunity_uid, opportunity_key, status,
                    payload_text, attempts, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    int(telegram_id),
                    opportunity_uid,
                    opportunity_key,
                    status,
                    payload_text,
                    now,
                    now,
                    now,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def list_pending_deliveries(settings: Settings, *, limit: int = 20) -> list[dict]:
    with get_connection(settings) as connection:
        _apply_schema(connection)
        rows = connection.execute(
            """
            SELECT telegram_id, opportunity_uid, opportunity_key, status,
                   payload_text, attempts, next_attempt_at, claim_id
            FROM crypto_opportunity_deliveries
            WHERE status IN ('PENDING', 'RETRY_WAIT')
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [
        {
            "telegram_id": int(row[0]),
            "opportunity_uid": str(row[1]),
            "opportunity_key": str(row[2]),
            "status": str(row[3]),
            "payload_text": str(row[4]),
            "attempts": int(row[5]),
            "next_attempt_at": row[6],
            "claim_id": row[7],
        }
        for row in rows
    ]


def mark_delivery_status(
    settings: Settings,
    *,
    telegram_id: int,
    opportunity_uid: str,
    status: str,
    attempts: int | None = None,
    last_error: str | None = None,
    next_attempt_at: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection(settings) as connection:
        _apply_schema(connection)
        connection.execute(
            """
            UPDATE crypto_opportunity_deliveries
            SET status = ?,
                attempts = COALESCE(?, attempts),
                last_error = ?,
                next_attempt_at = ?,
                updated_at = ?,
                claim_id = NULL,
                claim_expires_at = NULL
            WHERE telegram_id = ? AND opportunity_uid = ?
            """,
            (
                status,
                attempts,
                last_error,
                next_attempt_at,
                now,
                int(telegram_id),
                opportunity_uid,
            ),
        )


def list_delivery_enabled_telegram_ids(settings: Settings) -> list[int]:
    with get_connection(settings) as connection:
        _apply_schema(connection)
        rows = connection.execute(
            "SELECT telegram_id, settings_json FROM crypto_opportunity_subscriptions"
        ).fetchall()
    out: list[int] = []
    for row in rows:
        try:
            cfg = _settings_from_json(str(row[1]))
        except Exception:
            continue
        if cfg.delivery_enabled:
            out.append(int(row[0]))
    return out


def list_deliveries_for_user(
    settings: Settings,
    *,
    telegram_id: int,
    limit: int = 20,
) -> list[dict]:
    """Per-user delivery rows only (no cross-user leakage)."""
    with get_connection(settings) as connection:
        _apply_schema(connection)
        rows = connection.execute(
            """
            SELECT opportunity_uid, opportunity_key, status, attempts,
                   created_at, updated_at
            FROM crypto_opportunity_deliveries
            WHERE telegram_id = ?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (int(telegram_id), int(limit)),
        ).fetchall()
    return [
        {
            "opportunity_uid": str(row[0]),
            "opportunity_key": str(row[1]),
            "status": str(row[2]),
            "attempts": int(row[3]),
            "created_at": str(row[4]),
            "updated_at": str(row[5]),
        }
        for row in rows
    ]


def list_last_sent_for_user(
    settings: Settings,
    *,
    telegram_id: int,
    limit: int = 20,
) -> list[dict]:
    """Per-user last-sent cooldown rows only."""
    with get_connection(settings) as connection:
        _apply_schema(connection)
        rows = connection.execute(
            """
            SELECT opportunity_key, opportunity_uid, sent_at
            FROM crypto_opportunity_last_sent
            WHERE telegram_id = ?
            ORDER BY sent_at DESC
            LIMIT ?
            """,
            (int(telegram_id), int(limit)),
        ).fetchall()
    return [
        {
            "opportunity_key": str(row[0]),
            "opportunity_uid": str(row[1]),
            "sent_at": str(row[2]),
        }
        for row in rows
    ]
