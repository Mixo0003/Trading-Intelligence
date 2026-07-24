from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.database import opportunity_storage
from app.database.opportunity_storage import (
    MIGRATION_ID,
    OpportunitySettingsRevisionConflict,
    claim_delivery,
    consume_setting_edit,
    get_delivery,
    initialize_opportunity_schema,
    is_migration_applied,
    load_scanner_state,
    load_setting_edit,
    load_subscription,
    save_observation_metrics,
    save_scanner_state,
    save_setting_edit,
    save_signal,
    save_subscription,
    upsert_market_state,
    write_settings_audit,
)
from app.database.storage import get_connection, initialize_database, upsert_user
from app.opportunity_intel.models import (
    CanonicalObservation,
    DeliveryStatus,
    ExecutionMetrics,
    MovementDirection,
    ObservationMode,
    OpportunityBaseline,
    OpportunityDelivery,
    OpportunityReasonCode,
    OpportunityScannerState,
    OpportunitySignal,
    OpportunitySubscription,
    ScannerStatus,
    SignalFamily,
    SignalStatus,
)
from app.opportunity_intel.settings import OpportunityScannerSettings, balanced_settings
from tests.conftest import make_test_settings


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(database_path=str(tmp_path / "opportunity_foundation.db"))
    initialize_database(value)
    upsert_user(value, 1001, "owner", "Owner")
    return value


def _schema_names(settings):
    with get_connection(settings) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    return tables, indexes


def _seed_market_and_signal(settings, *, uid="opp-1", status=SignalStatus.ACTIVE):
    now = datetime.now(timezone.utc)
    baseline = OpportunityBaseline(
        asset_id="asset-1",
        mode=ObservationMode.NORMAL,
        baseline_version=1,
        consecutive_complete_cycles=2,
        last_complete_cycle_id="cycle-2",
        last_observed_at=now,
        ready=True,
    )
    upsert_market_state(
        settings,
        baseline=baseline,
        condition_id="condition-1",
        event_id="event-1",
        market_id="market-1",
        event_slug="market-one",
        question="Will the example happen?",
        outcome="Yes",
        category_ids=("politics",),
        metadata_at=now,
        end_at=now + timedelta(days=1),
    )
    signal = OpportunitySignal(
        opportunity_uid=uid,
        opportunity_key=f"key-{uid}",
        asset_id="asset-1",
        episode_sequence=1,
        material_change_sequence=0,
        family=SignalFamily.EXECUTABLE_MOMENTUM,
        direction=MovementDirection.UP,
        mode=ObservationMode.NORMAL,
        score=Decimal("77.5"),
        reason_codes=(OpportunityReasonCode.FAST_5M_MOVE,),
        signal_at=now,
        first_seen_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(minutes=10),
        source_observed_at=now,
        status=status,
    )
    save_signal(settings, signal)
    return signal


def test_empty_database_initialization_creates_marker_tables_and_indexes(tmp_path):
    value = make_test_settings(database_path=str(tmp_path / "empty.db"))
    initialize_database(value)
    tables, indexes = _schema_names(value)
    assert opportunity_storage._TABLES <= tables
    assert opportunity_storage._INDEXES <= indexes
    assert is_migration_applied(value) is True
    with get_connection(value) as connection:
        marker = connection.execute(
            """
            SELECT schema_version FROM opportunity_schema_migrations
            WHERE migration_id = ?
            """,
            (MIGRATION_ID,),
        ).fetchone()
    assert marker is not None
    assert marker["schema_version"] == 1


def test_existing_pre_v223_database_is_preserved(tmp_path):
    db_path = tmp_path / "existing.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                language TEXT NOT NULL DEFAULT 'en',
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE legacy_business_data (
                id INTEGER PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO legacy_business_data(id, value) VALUES (1, 'preserve-me');
            """
        )
        connection.commit()
    finally:
        connection.close()
    value = make_test_settings(database_path=str(db_path))
    initialize_opportunity_schema(value)
    with get_connection(value) as connection:
        row = connection.execute(
            "SELECT id, value FROM legacy_business_data"
        ).fetchone()
    assert tuple(row) == (1, "preserve-me")
    assert is_migration_applied(value)


def test_migration_second_invocation_is_idempotent(settings):
    before = _schema_names(settings)
    result = initialize_opportunity_schema(settings)
    after = _schema_names(settings)
    assert result.already_applied is True
    assert before == after
    with get_connection(settings) as connection:
        count = connection.execute(
            """
            SELECT COUNT(*) FROM opportunity_schema_migrations
            WHERE migration_id = ?
            """,
            (MIGRATION_ID,),
        ).fetchone()[0]
    assert count == 1


def test_migration_failure_rolls_back_all_new_objects(tmp_path, monkeypatch):
    value = make_test_settings(database_path=str(tmp_path / "rollback.db"))

    def fail(point):
        if point == "after_indexes":
            raise RuntimeError("injected migration failure")

    monkeypatch.setattr(opportunity_storage, "_MIGRATION_FAILPOINT_HOOK", fail)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        initialize_opportunity_schema(value)
    tables, indexes = _schema_names(value)
    assert not (opportunity_storage._TABLES & tables)
    assert not (opportunity_storage._INDEXES & indexes)


def test_database_constraints_and_foreign_keys_are_active(settings):
    missing_user = OpportunitySubscription(
        telegram_id=999999,
        enabled=False,
        settings=balanced_settings(),
    )
    with pytest.raises(sqlite3.IntegrityError):
        save_subscription(settings, missing_user, expected_revision=None)
    with get_connection(settings) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO opportunity_subscriptions (
                    telegram_id, enabled, revision, minimum_score,
                    minimum_liquidity_usd, minimum_24h_volume_usd,
                    maximum_relative_spread, maximum_absolute_spread,
                    maximum_slippage, paper_allocation_usd, all_categories,
                    category_ids_json, live_mode, cooldown_seconds, alert_mode,
                    timezone_name, created_at, updated_at
                ) VALUES (
                    999999, 1, 0, 65, '10000', '25000', '0.10', '0.05',
                    '0.02', '75', 1, '[]', 'BROKEN', 1800, 'INSTANT',
                    'UTC', 't', 't'
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO opportunity_scanner_state (
                    state_key, status, schema_version, baseline_version,
                    score_version, global_baseline_cycles, discovery_complete,
                    updated_at
                ) VALUES ('scanner', 'UNKNOWN', 1, 1, 1, 0, 0, 't')
                """
            )


def test_settings_save_read_and_revision_conflict(settings):
    initial = OpportunitySubscription(
        telegram_id=1001,
        enabled=False,
        settings=balanced_settings(),
    )
    save_subscription(settings, initial, expected_revision=None)
    assert load_subscription(settings, 1001) == initial

    updated_settings = replace(
        initial.settings,
        revision=1,
        minimum_score=70,
    )
    updated = OpportunitySubscription(
        telegram_id=1001,
        enabled=True,
        settings=updated_settings,
    )
    save_subscription(settings, updated, expected_revision=0)
    assert load_subscription(settings, 1001) == updated

    stale = OpportunitySubscription(
        telegram_id=1001,
        enabled=True,
        settings=replace(updated_settings, revision=1, minimum_score=75),
    )
    with pytest.raises(OpportunitySettingsRevisionConflict):
        save_subscription(settings, stale, expected_revision=0)
    assert load_subscription(settings, 1001) == updated


def test_edit_token_persistence_and_single_use(settings):
    save_subscription(
        settings,
        OpportunitySubscription(
            telegram_id=1001,
            enabled=False,
            settings=balanced_settings(),
        ),
        expected_revision=None,
    )
    proposed = replace(balanced_settings(), revision=1, minimum_score=72)
    fingerprint = "a" * 32
    save_setting_edit(
        settings,
        token_fingerprint=fingerprint,
        telegram_id=1001,
        starting_revision=0,
        proposed_settings=proposed,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    loaded = load_setting_edit(settings, fingerprint)
    assert loaded is not None
    assert loaded["proposed_settings"] == proposed
    assert consume_setting_edit(
        settings,
        token_fingerprint=fingerprint,
        telegram_id=2002,
        status="confirmed",
    ) is False
    assert consume_setting_edit(
        settings,
        token_fingerprint=fingerprint,
        telegram_id=1001,
        status="confirmed",
    ) is True
    assert consume_setting_edit(
        settings,
        token_fingerprint=fingerprint,
        telegram_id=1001,
        status="confirmed",
    ) is False


def test_settings_audit_contains_only_allowlisted_sanitized_values(settings):
    old = balanced_settings()
    new = replace(old, revision=1, minimum_score=70)
    write_settings_audit(
        settings,
        telegram_id=1001,
        from_revision=0,
        to_revision=1,
        changed_keys=("minimum_score",),
        old_settings=old,
        new_settings=new,
    )
    with get_connection(settings) as connection:
        row = connection.execute(
            "SELECT changed_keys_json, changes_json FROM opportunity_settings_audit"
        ).fetchone()
    assert json.loads(row["changed_keys_json"]) == ["minimum_score"]
    payload = json.loads(row["changes_json"])
    assert payload["old"] == {"minimum_score": 65}
    assert payload["new"] == {"minimum_score": 70}
    serialized = row["changes_json"].lower()
    assert "token" not in serialized
    assert ".env" not in serialized
    assert "users\\" not in serialized


def test_delivery_claim_is_atomic_and_payload_is_immutable(settings):
    signal = _seed_market_and_signal(settings)
    now = datetime.now(timezone.utc)
    delivery = OpportunityDelivery(
        telegram_id=1001,
        opportunity_uid=signal.opportunity_uid,
        status=DeliveryStatus.CLAIMED,
        payload_text="MARKET OPPORTUNITY\nSanitized card",
        signal_at=signal.signal_at,
        wave_at=signal.signal_at,
        settings_revision=0,
        claim_id="claim-1",
        claimed_at=now,
        claim_expires_at=now + timedelta(minutes=2),
    )
    assert claim_delivery(settings, delivery) is True
    assert claim_delivery(settings, delivery) is False
    stored = get_delivery(settings, 1001, signal.opportunity_uid)
    assert stored is not None
    assert stored["payload_text"] == delivery.payload_text
    assert stored["status"] == "claimed"
    with get_connection(settings) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE opportunity_deliveries
                SET payload_text = ?
                WHERE telegram_id = ? AND opportunity_uid = ?
                """,
                ("different but sanitized card", 1001, signal.opportunity_uid),
            )


def test_observation_metrics_and_scanner_state_repository_primitives(settings):
    signal = _seed_market_and_signal(settings)
    observed_at = signal.source_observed_at
    canonical_execution = ExecutionMetrics(
        asset_id="asset-1",
        observed_at=observed_at,
        allocation_usd=Decimal("100"),
        fully_executable=True,
        execution_vwap=Decimal("0.56"),
        slippage=Decimal("0.01"),
        quantity=Decimal("178.571428"),
        consumed_ask_usd=Decimal("100"),
    )
    observation = CanonicalObservation(
        asset_id="asset-1",
        condition_id="condition-1",
        event_id="event-1",
        market_id="market-1",
        event_slug="market-one",
        question="Will the example happen?",
        outcome="Yes",
        category_ids=("politics",),
        mode=ObservationMode.NORMAL,
        observed_at=observed_at,
        metadata_at=observed_at,
        history_at=observed_at,
        book_at=observed_at,
        best_bid=Decimal("0.54"),
        best_ask=Decimal("0.56"),
        midpoint=Decimal("0.55"),
        absolute_spread=Decimal("0.02"),
        relative_spread=Decimal("0.03636"),
        liquidity_usd=Decimal("100000"),
        volume_24h_usd=Decimal("250000"),
        move_5m=Decimal("0.025"),
        move_15m=Decimal("0.045"),
        move_1h=Decimal("0.08"),
        visible_bid_depth_usd=Decimal("900"),
        visible_ask_depth_usd=Decimal("1000"),
        canonical_execution=canonical_execution,
    )
    save_observation_metrics(
        settings,
        observation=observation,
        bucket_at=observed_at,
        canonical_score=Decimal("76.7"),
        score_version=1,
        execution_metrics=(canonical_execution,),
    )
    scanner_state = OpportunityScannerState(
        status=ScannerStatus.BASELINING,
        schema_version=1,
        baseline_version=1,
        score_version=1,
        global_baseline_cycles=1,
        discovery_complete=True,
        last_complete_discovery_at=observed_at,
        last_complete_cycle_at=observed_at,
        updated_at=observed_at,
    )
    save_scanner_state(settings, scanner_state)
    assert load_scanner_state(settings) == scanner_state
    with get_connection(settings) as connection:
        metric = connection.execute(
            """
            SELECT canonical_score FROM opportunity_metric_buckets
            WHERE asset_id = 'asset-1'
            """
        ).fetchone()
        execution = connection.execute(
            """
            SELECT allocation_usd, execution_vwap
            FROM opportunity_execution_metrics
            WHERE asset_id = 'asset-1'
            """
        ).fetchone()
    assert metric["canonical_score"] == "76.7"
    assert tuple(execution) == ("100", "0.56")


def test_no_raw_api_or_telegram_payload_columns(settings):
    with get_connection(settings) as connection:
        columns = {
            (table, row[1])
            for table in opportunity_storage._TABLES
            if table != "opportunity_schema_migrations"
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
    names = {column for _, column in columns}
    assert "raw_json" not in names
    assert "api_payload" not in names
    assert "telegram_payload" not in names
    assert "payload_text" in names
