"""Additive SQLite storage foundations for Market Opportunity Scanner."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, time, timezone
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
    OpportunityBaseline,
    OpportunityDelivery,
    OpportunityDiagnosticCode,
    OpportunityScannerState,
    OpportunitySignal,
    OpportunitySubscription,
    ScannerStatus,
    decimal_text,
)
from app.opportunity_intel.settings import (
    CategoryAllowlist,
    OpportunityScannerSettings,
    QuietHours,
)

MIGRATION_ID = "opportunity_intel_v1"
SCHEMA_VERSION = 1

_MIGRATION_FAILPOINT_HOOK: Callable[[str], None] | None = None

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


def initialize_opportunity_schema(settings: Settings) -> OpportunityMigrationResult:
    """Create the V2.2.3 schema in one additive, rollback-safe transaction."""
    with _opportunity_connection(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _initialize_on_connection(connection)


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
) -> None:
    categories = tuple(sorted(set(category_ids)))
    if not categories:
        raise ValueError("market state requires categories")
    now = _iso(_now())
    with _opportunity_connection(settings) as connection:
        connection.execute(
            """
            INSERT INTO opportunity_market_state (
                asset_id, condition_id, event_id, market_id, event_slug,
                question, outcome, category_ids_json, mode, active,
                end_at, metadata_at, baseline_version, baseline_count,
                baseline_ready, last_complete_cycle_id, last_observed_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                now,
                now,
            ),
        )


def save_signal(settings: Settings, signal: OpportunitySignal) -> None:
    with _opportunity_connection(settings) as connection:
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
    with _opportunity_connection(settings) as connection:
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
                'pending', 'claimed', 'failed', 'sent',
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
