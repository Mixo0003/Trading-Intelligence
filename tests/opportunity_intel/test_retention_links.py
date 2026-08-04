from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.database.opportunity_storage import (
    claim_delivery,
    save_signal,
    upsert_market_state,
)
from app.database.storage import get_connection, initialize_database, upsert_user
from app.opportunity_intel.models import (
    DeliveryStatus,
    MovementDirection,
    ObservationMode,
    OpportunityBaseline,
    OpportunityDelivery,
    OpportunityReasonCode,
    OpportunitySignal,
    SignalFamily,
    SignalStatus,
)
from app.polymarket.links import (
    POLYMARKET_EVENT_BASE_URL,
    build_validated_market_link,
)
from app.storage.retention_audit import run_retention_audit
from app.storage.retention_policies import (
    RETENTION_OPPORTUNITY_DELIVERIES_DAYS,
    RETENTION_OPPORTUNITY_METRICS_DAYS,
    RETENTION_OPPORTUNITY_SIGNALS_DAYS,
    build_retention_policies,
    validate_policy_registry,
)
from app.telegram.copy_intel.formatting import (
    build_validated_market_link as copy_market_link,
)
from tests.conftest import make_test_settings


def test_validated_event_link_accepts_only_safe_single_segment_slugs():
    assert (
        build_validated_market_link("world-cup-winner")
        == f"{POLYMARKET_EVENT_BASE_URL}/world-cup-winner"
    )
    assert copy_market_link("world-cup-winner") == (
        "https://polymarket.com/event/world-cup-winner"
    )


@pytest.mark.parametrize(
    "slug",
    [
        None,
        "",
        " ",
        " leading",
        "trailing ",
        "two words",
        "a/b",
        "a\\b",
        "../event",
        "a..b",
        "a?x=1",
        "a#fragment",
        "https://polymarket.com/event/x",
        "http://evil.example/x",
        "//evil.example/x",
        "javascript:alert",
        "a%2fb",
        "a\nb",
        "a\x00b",
        "-leading",
        "trailing-",
        "x" * 201,
    ],
)
def test_validated_event_link_rejects_injection_and_malformed_slugs(slug):
    assert build_validated_market_link(slug) is None
    assert copy_market_link(slug) is None


def test_opportunity_retention_registry_and_protection_are_exact():
    assert validate_policy_registry() == []
    policies = {policy.dataset: policy for policy in build_retention_policies()}

    metrics = policies["opportunity_metric_history"]
    execution = policies["opportunity_execution_metric_history"]
    signals = policies["opportunity_signal_history"]
    deliveries = policies["opportunity_terminal_delivery_history"]

    assert metrics.retention_days == RETENTION_OPPORTUNITY_METRICS_DAYS == 30
    assert execution.retention_days == 30
    assert signals.retention_days == RETENTION_OPPORTUNITY_SIGNALS_DAYS == 180
    assert signals.terminal_predicate == "status = 'expired'"
    assert signals.protected_exclusion_predicate == "status = 'active'"
    assert deliveries.retention_days == RETENTION_OPPORTUNITY_DELIVERIES_DAYS == 60
    assert "claimed" in deliveries.protected_exclusion_predicate
    assert "failed" in deliveries.protected_exclusion_predicate

    for dataset in (
        "opportunity_market_state",
        "opportunity_subscriptions",
        "opportunity_scanner_state",
    ):
        assert policies[dataset].kind == "PROTECTED"
        assert policies[dataset].batch_size == 0


def test_copy_formatting_imports_shared_helper_without_wrapper_duplication():
    assert copy_market_link is build_validated_market_link


def test_retention_audit_excludes_active_signals_and_live_claims(tmp_path):
    settings = make_test_settings(database_path=str(tmp_path / "retention.db"))
    initialize_database(settings)
    upsert_user(settings, 1001, "owner", "Owner")
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

    signals = []
    for index, status in enumerate((SignalStatus.ACTIVE, SignalStatus.EXPIRED), 1):
        signal = OpportunitySignal(
            opportunity_uid=f"opp-{index}",
            opportunity_key=f"key-{index}",
            asset_id="asset-1",
            episode_sequence=1,
            material_change_sequence=0,
            family=SignalFamily.EXECUTABLE_MOMENTUM,
            direction=MovementDirection.UP,
            mode=ObservationMode.NORMAL,
            score=Decimal("77"),
            reason_codes=(OpportunityReasonCode.FAST_5M_MOVE,),
            signal_at=now,
            first_seen_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(minutes=10),
            source_observed_at=now,
            status=status,
        )
        save_signal(settings, signal)
        signals.append(signal)

    for index, signal in enumerate(signals, 1):
        delivery = OpportunityDelivery(
            telegram_id=1001,
            opportunity_uid=signal.opportunity_uid,
            status=DeliveryStatus.CLAIMED,
            payload_text=f"MARKET OPPORTUNITY\nCard {index}",
            signal_at=signal.signal_at,
            wave_at=signal.signal_at,
            settings_revision=0,
            claim_id=f"claim-{index}",
            claimed_at=now,
            claim_expires_at=now + timedelta(minutes=2),
        )
        assert claim_delivery(settings, delivery)

    old = (now - timedelta(days=200)).isoformat()
    with get_connection(settings) as connection:
        connection.execute(
            "UPDATE opportunity_signals SET last_seen_at = ?",
            (old,),
        )
        connection.execute(
            """
            UPDATE opportunity_deliveries
            SET status = 'sent', updated_at = ?
            WHERE opportunity_uid = 'opp-2'
            """,
            (old,),
        )
        connection.execute(
            """
            UPDATE opportunity_deliveries
            SET updated_at = ?
            WHERE opportunity_uid = 'opp-1'
            """,
            (old,),
        )

    report = run_retention_audit(settings)
    signal_policy = next(
        item for item in report.policies if item.dataset == "opportunity_signal_history"
    )
    delivery_policy = next(
        item
        for item in report.policies
        if item.dataset == "opportunity_terminal_delivery_history"
    )
    assert signal_policy.candidate_rows == 1
    assert signal_policy.protected_or_excluded_rows == 1
    assert delivery_policy.candidate_rows == 1
    assert delivery_policy.protected_or_excluded_rows == 1
