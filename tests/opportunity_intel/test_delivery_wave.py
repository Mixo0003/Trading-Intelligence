"""Checkpoint 5: per-user filtering, same-wave delivery, claims, retries."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.database.opportunity_storage import (
    claim_delivery_lease,
    get_delivery,
    initialize_opportunity_schema,
    insert_delivery_wave,
    last_sent_at_for_opportunity_key,
    list_deliveries_for_opportunity,
    load_delivery,
    load_delivery_gates,
    mark_delivery_terminal,
    save_delivery_gates,
    save_observation_metrics,
    save_signal,
    save_subscription,
    upsert_market_state,
)
from app.database.storage import get_connection, initialize_database, upsert_user
from app.opportunity_intel.delivery import (
    MAX_SEND_ATTEMPTS,
    AuthorizedSendReady,
    AuthorizedSendSuppressed,
    classify_and_mark_send_failure,
    mark_send_failure,
    mark_send_success,
    prepare_authorized_send,
    recheck_delivery_authorization,
    select_delivery_wave,
)
from app.opportunity_intel.delivery_gates import OpportunityDeliveryGates
from app.opportunity_intel.filters import effective_cooldown_seconds, qualify_recipient
from app.opportunity_intel.models import (
    AlertMode,
    CanonicalObservation,
    DeliveryStatus,
    ExecutionMetrics,
    LiveMode,
    MovementDirection,
    ObservationMode,
    OpportunityBaseline,
    OpportunityDelivery,
    OpportunityDiagnosticCode,
    OpportunityReasonCode,
    OpportunitySignal,
    OpportunitySubscription,
    SignalFamily,
    SignalStatus,
)
from app.opportunity_intel.payload import (
    DeliveryCardPayload,
    build_delivery_card_payload,
)
from app.opportunity_intel.quiet_hours import is_within_quiet_hours
from app.opportunity_intel.settings import (
    CategoryAllowlist,
    OpportunityScannerSettings,
    QuietHours,
    STRICT_LIVE_COOLDOWN_FLOOR_SECONDS,
    balanced_settings,
)
from app.opportunity_intel.telegram_errors import classify_telegram_send_outcome
from tests.conftest import make_test_settings


ADMIN_ID = 1
USER_ID = 2002


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "opportunity_delivery.db"),
        admin_ids=[ADMIN_ID],
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    upsert_user(value, ADMIN_ID, "admin", "Admin")
    upsert_user(value, USER_ID, "user", "User")
    return value


def _now() -> datetime:
    return datetime(2026, 7, 24, 15, 0, tzinfo=timezone.utc)


def _execution(
    observed_at: datetime,
    allocation: str,
    *,
    fully: bool = True,
    slippage: str = "0.01",
) -> ExecutionMetrics:
    return ExecutionMetrics(
        asset_id="asset-1",
        observed_at=observed_at,
        allocation_usd=Decimal(allocation),
        fully_executable=fully,
        execution_vwap=Decimal("0.56") if fully else None,
        slippage=Decimal(slippage) if fully else None,
        quantity=Decimal("10"),
        consumed_ask_usd=Decimal(allocation) if fully else Decimal("0"),
    )


def _seed_signal_and_metrics(
    settings,
    *,
    uid: str = "opp_wave_1",
    score: str = "80",
    mode: ObservationMode = ObservationMode.NORMAL,
    allocations: tuple[str, ...] = ("25", "50", "100", "250"),
    categories: tuple[str, ...] = ("politics",),
    liquidity: str = "100000",
    volume: str = "250000",
    relative_spread: str = "0.03",
    absolute_spread: str = "0.02",
):
    now = _now()
    baseline = OpportunityBaseline(
        asset_id="asset-1",
        mode=mode,
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
        category_ids=categories,
        metadata_at=now,
        end_at=now + timedelta(days=1),
    )
    metrics = tuple(_execution(now, alloc) for alloc in allocations)
    observation = CanonicalObservation(
        asset_id="asset-1",
        condition_id="condition-1",
        event_id="event-1",
        market_id="market-1",
        event_slug="market-one",
        question="Will the example happen?",
        outcome="Yes",
        category_ids=categories,
        mode=mode,
        observed_at=now,
        metadata_at=now,
        history_at=now,
        book_at=now,
        best_bid=Decimal("0.54"),
        best_ask=Decimal("0.56"),
        midpoint=Decimal("0.55"),
        absolute_spread=Decimal(absolute_spread),
        relative_spread=Decimal(relative_spread),
        liquidity_usd=Decimal(liquidity),
        volume_24h_usd=Decimal(volume),
        move_5m=Decimal("0.03"),
        move_15m=Decimal("0.04"),
        move_1h=Decimal("0.05"),
        visible_bid_depth_usd=Decimal("900"),
        visible_ask_depth_usd=Decimal("1000"),
        canonical_execution=next(m for m in metrics if m.allocation_usd == Decimal("100")),
    )
    save_observation_metrics(
        settings,
        observation=observation,
        bucket_at=now,
        canonical_score=Decimal(score),
        score_version=1,
        execution_metrics=metrics,
    )
    signal = OpportunitySignal(
        opportunity_uid=uid,
        opportunity_key=(
            f"v1:condition-1:asset-1:EXECUTABLE_MOMENTUM:UP:{mode.value}"
        ),
        asset_id="asset-1",
        episode_sequence=1,
        material_change_sequence=0,
        family=SignalFamily.EXECUTABLE_MOMENTUM,
        direction=MovementDirection.UP,
        mode=mode,
        score=Decimal(score),
        reason_codes=(OpportunityReasonCode.FAST_5M_MOVE,),
        signal_at=now,
        first_seen_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(minutes=15),
        source_observed_at=now,
        status=SignalStatus.ACTIVE,
    )
    save_signal(settings, signal)
    return signal


def _instant_sub(
    telegram_id: int,
    *,
    revision: int = 0,
    allocation: str = "100",
    enabled: bool = True,
    alert_mode: AlertMode = AlertMode.INSTANT,
    live_mode: LiveMode = LiveMode.OFF,
    categories: CategoryAllowlist | None = None,
    quiet_hours: QuietHours | None = None,
    timezone_name: str = "UTC",
    minimum_score: int = 65,
    cooldown_seconds: int = 1800,
    maximum_slippage: str = "0.02",
) -> OpportunitySubscription:
    base = balanced_settings()
    scanner = OpportunityScannerSettings(
        revision=revision,
        minimum_score=minimum_score,
        minimum_liquidity_usd=base.minimum_liquidity_usd,
        minimum_24h_volume_usd=base.minimum_24h_volume_usd,
        maximum_relative_spread=base.maximum_relative_spread,
        maximum_absolute_spread=base.maximum_absolute_spread,
        maximum_slippage=Decimal(maximum_slippage),
        paper_allocation_usd=Decimal(allocation),
        categories=categories or CategoryAllowlist(),
        live_mode=live_mode,
        cooldown_seconds=cooldown_seconds,
        alert_mode=alert_mode,
        quiet_hours=quiet_hours,
        timezone_name=timezone_name,
    )
    return OpportunitySubscription(
        telegram_id=telegram_id,
        enabled=enabled,
        settings=scanner,
    )


def test_same_wave_for_admin_and_user_with_allocation_payloads(settings):
    signal = _seed_signal_and_metrics(settings)
    save_subscription(settings, _instant_sub(ADMIN_ID, allocation="100"), expected_revision=None)
    save_subscription(settings, _instant_sub(USER_ID, allocation="250"), expected_revision=None)
    save_delivery_gates(
        settings,
        OpportunityDeliveryGates(admin_delivery_enabled=True, user_delivery_enabled=True),
    )
    wave_at = _now() + timedelta(seconds=5)
    result = select_delivery_wave(
        settings, opportunity_uid=signal.opportunity_uid, wave_at=wave_at, now=_now()
    )
    assert result.inserted_count == 2
    assert result.signal_at == signal.signal_at
    assert result.wave_at == wave_at
    assert result.canonical_score == signal.score
    rows = list_deliveries_for_opportunity(settings, signal.opportunity_uid)
    assert {row.telegram_id for row in rows} == {ADMIN_ID, USER_ID}
    assert {row.wave_at for row in rows} == {wave_at}
    assert {row.signal_at for row in rows} == {signal.signal_at}
    assert {row.opportunity_uid for row in rows} == {signal.opportunity_uid}
    admin_text = next(r for r in rows if r.telegram_id == ADMIN_ID).payload_text
    user_text = next(r for r in rows if r.telegram_id == USER_ID).payload_text
    assert admin_text.startswith("MARKET OPPORTUNITY")
    assert user_text.startswith("MARKET OPPORTUNITY")
    assert "Proposed allocation: $100.00" in admin_text
    assert "Proposed allocation: $250.00" in user_text
    assert f"Score: {signal.score}" in admin_text or "Score:" in admin_text
    assert "{" not in admin_text and "}" not in admin_text
    assert "opportunity_uid" not in admin_text
    assert "canonical_score" not in admin_text


def test_wave_selection_is_idempotent(settings):
    signal = _seed_signal_and_metrics(settings)
    save_subscription(settings, _instant_sub(ADMIN_ID), expected_revision=None)
    first = select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())
    second = select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())
    assert first.inserted_count == 1
    assert second.inserted_count == 0
    assert second.ignored_count == 1


def test_user_gate_default_off_and_no_replay_when_enabled(settings):
    signal = _seed_signal_and_metrics(settings)
    save_subscription(settings, _instant_sub(ADMIN_ID), expected_revision=None)
    save_subscription(settings, _instant_sub(USER_ID), expected_revision=None)
    gates = load_delivery_gates(settings)
    assert gates.user_delivery_enabled is False
    assert gates.admin_delivery_enabled is True
    first = select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())
    rows = list_deliveries_for_opportunity(settings, signal.opportunity_uid)
    assert any(r.telegram_id == ADMIN_ID and r.status == DeliveryStatus.PENDING for r in rows)
    user_row = next(r for r in rows if r.telegram_id == USER_ID)
    assert user_row.status == DeliveryStatus.SUPPRESSED
    assert user_row.last_error_code == OpportunityDiagnosticCode.USER_GATE_DISABLED
    save_delivery_gates(
        settings,
        OpportunityDeliveryGates(admin_delivery_enabled=True, user_delivery_enabled=True),
    )
    second = select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())
    assert second.inserted_count == 0
    still = load_delivery(settings, USER_ID, signal.opportunity_uid)
    assert still is not None
    assert still.status == DeliveryStatus.SUPPRESSED


def test_missing_allocation_metric_suppresses_without_canonical_substitute(settings):
    signal = _seed_signal_and_metrics(settings, allocations=("100",))
    save_subscription(settings, _instant_sub(ADMIN_ID, allocation="500"), expected_revision=None)
    result = select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())
    assert result.pending == ()
    row = load_delivery(settings, ADMIN_ID, signal.opportunity_uid)
    assert row is not None
    assert row.status == DeliveryStatus.SUPPRESSED
    assert row.last_error_code == OpportunityDiagnosticCode.ALLOCATION_METRIC_MISSING
    assert "500" not in row.payload_text or '"allocation_usd"' not in row.payload_text


def test_filters_exclude_on_demand_disabled_and_thresholds(settings):
    signal = _seed_signal_and_metrics(
        settings, score="60", liquidity="8000", volume="12000", relative_spread="0.12"
    )
    save_subscription(
        settings,
        _instant_sub(ADMIN_ID, alert_mode=AlertMode.ON_DEMAND_ONLY),
        expected_revision=None,
    )
    upsert_user(settings, 3003, "u3", "U3")
    save_subscription(
        settings,
        _instant_sub(3003, enabled=False),
        expected_revision=None,
    )
    upsert_user(settings, 3004, "u4", "U4")
    save_subscription(
        settings,
        _instant_sub(3004, minimum_score=70),
        expected_revision=None,
    )
    save_delivery_gates(
        settings,
        OpportunityDeliveryGates(admin_delivery_enabled=True, user_delivery_enabled=True),
    )
    result = select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())
    assert result.pending == ()
    assert load_delivery(settings, ADMIN_ID, signal.opportunity_uid) is None
    assert load_delivery(settings, 3003, signal.opportunity_uid) is None


def test_category_live_slippage_filters(settings):
    live = _seed_signal_and_metrics(
        settings, uid="opp_live", mode=ObservationMode.LIVE_STRICT
    )
    save_subscription(settings, _instant_sub(ADMIN_ID, live_mode=LiveMode.OFF), expected_revision=None)
    result = select_delivery_wave(settings, opportunity_uid=live.opportunity_uid, now=_now())
    assert result.pending == ()

    normal = _seed_signal_and_metrics(
        settings,
        uid="opp_cat",
        categories=("sports",),
        allocations=("100",),
    )
    # Overwrite admin sub with category allowlist + tight slippage after prior revision 0 insert.
    save_subscription(
        settings,
        _instant_sub(
            ADMIN_ID,
            revision=1,
            live_mode=LiveMode.OFF,
            categories=CategoryAllowlist(all_categories=False, category_ids=frozenset({"politics"})),
            maximum_slippage="0.005",
        ),
        expected_revision=0,
    )
    # Re-seed metrics with high slippage on $100.
    now = _now()
    high_slip = _execution(now, "100", slippage="0.04")
    observation = CanonicalObservation(
        asset_id="asset-1",
        condition_id="condition-1",
        event_id="event-1",
        market_id="market-1",
        event_slug="market-one",
        question="Will the example happen?",
        outcome="Yes",
        category_ids=("sports",),
        mode=ObservationMode.NORMAL,
        observed_at=now,
        metadata_at=now,
        history_at=now,
        book_at=now,
        best_bid=Decimal("0.54"),
        best_ask=Decimal("0.56"),
        midpoint=Decimal("0.55"),
        absolute_spread=Decimal("0.02"),
        relative_spread=Decimal("0.03"),
        liquidity_usd=Decimal("100000"),
        volume_24h_usd=Decimal("250000"),
        move_5m=Decimal("0.03"),
        move_15m=Decimal("0.04"),
        move_1h=Decimal("0.05"),
        visible_bid_depth_usd=Decimal("900"),
        visible_ask_depth_usd=Decimal("1000"),
        canonical_execution=high_slip,
    )
    save_observation_metrics(
        settings,
        observation=observation,
        bucket_at=now,
        canonical_score=Decimal("80"),
        score_version=1,
        execution_metrics=(high_slip,),
    )
    result2 = select_delivery_wave(settings, opportunity_uid=normal.opportunity_uid, now=_now())
    assert result2.pending == ()


def test_quiet_hours_normal_overnight_timezone(settings):
    quiet = QuietHours(start=time(22, 0), end=time(6, 0))
    inside = datetime(2026, 7, 24, 3, 30, tzinfo=ZoneInfo("America/Chicago"))
    outside = datetime(2026, 7, 24, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
    assert is_within_quiet_hours(quiet, "America/Chicago", at=inside) is True
    assert is_within_quiet_hours(quiet, "America/Chicago", at=outside) is False
    day = QuietHours(start=time(9, 0), end=time(17, 0))
    assert is_within_quiet_hours(day, "UTC", at=_now()) is True
    assert is_within_quiet_hours(day, "UTC", at=_now().replace(hour=18)) is False


def test_cooldown_uses_sent_only_and_strict_floor(settings):
    signal = _seed_signal_and_metrics(settings, mode=ObservationMode.LIVE_STRICT)
    save_subscription(
        settings,
        _instant_sub(
            ADMIN_ID,
            live_mode=LiveMode.STRICT,
            cooldown_seconds=900,
        ),
        expected_revision=None,
    )
    assert (
        effective_cooldown_seconds(
            _instant_sub(ADMIN_ID, cooldown_seconds=900).settings,
            signal_mode=ObservationMode.LIVE_STRICT,
        )
        == STRICT_LIVE_COOLDOWN_FLOOR_SECONDS
    )
    wave = select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())
    assert wave.pending
    ready = prepare_authorized_send(
        settings, telegram_id=ADMIN_ID, opportunity_uid=signal.opportunity_uid, now=_now()
    )
    assert isinstance(ready, AuthorizedSendReady)
    assert mark_send_success(
        settings,
        telegram_id=ADMIN_ID,
        opportunity_uid=signal.opportunity_uid,
        claim_id=ready.claim_id,
    )
    sent_at = last_sent_at_for_opportunity_key(
        settings,
        telegram_id=ADMIN_ID,
        opportunity_key=signal.opportunity_key,
    )
    assert sent_at is not None

    # Failed/suppressed do not create cooldown anchors for a sibling uid.
    other = _seed_signal_and_metrics(settings, uid="opp_wave_2")
    save_subscription(
        settings,
        replace(
            _instant_sub(USER_ID),
            settings=replace(_instant_sub(USER_ID).settings, revision=0),
        ),
        expected_revision=None,
    )
    save_delivery_gates(
        settings,
        OpportunityDeliveryGates(admin_delivery_enabled=True, user_delivery_enabled=True),
    )
    # User never SENT — suppress path should not set cooldown via last_sent.
    assert (
        last_sent_at_for_opportunity_key(
            settings, telegram_id=USER_ID, opportunity_key=other.opportunity_key
        )
        is None
    )


def test_atomic_claim_concurrent_loser_and_expiry_recovery(settings):
    signal = _seed_signal_and_metrics(settings)
    save_subscription(settings, _instant_sub(ADMIN_ID), expected_revision=None)
    select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())

    def _claim(claim_id: str):
        return claim_delivery_lease(
            settings,
            telegram_id=ADMIN_ID,
            opportunity_uid=signal.opportunity_uid,
            claim_id=claim_id,
            now=_now(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_claim, "claim-a")
        second = pool.submit(_claim, "claim-b")
        results = [first.result(), second.result()]
    winners = [row for row in results if row is not None]
    assert len(winners) == 1
    claimed = winners[0]
    assert claimed.status == DeliveryStatus.CLAIMED
    # Active claim blocks.
    assert (
        claim_delivery_lease(
            settings,
            telegram_id=ADMIN_ID,
            opportunity_uid=signal.opportunity_uid,
            claim_id="claim-c",
            now=_now(),
        )
        is None
    )
    recovered = claim_delivery_lease(
        settings,
        telegram_id=ADMIN_ID,
        opportunity_uid=signal.opportunity_uid,
        claim_id="claim-d",
        now=_now() + timedelta(minutes=5),
        lease_seconds=60,
    )
    assert recovered is not None
    assert recovered.claim_id == "claim-d"
    assert recovered.attempt_count == claimed.attempt_count


def test_retry_scheduling_max_attempts_and_dead_letter(settings):
    signal = _seed_signal_and_metrics(settings)
    save_subscription(settings, _instant_sub(ADMIN_ID), expected_revision=None)
    select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())
    now = _now()
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        ready = prepare_authorized_send(
            settings,
            telegram_id=ADMIN_ID,
            opportunity_uid=signal.opportunity_uid,
            now=now + timedelta(minutes=30 * attempt),
            claim_id=f"claim-{attempt}",
        )
        assert isinstance(ready, AuthorizedSendReady)
        status = classify_and_mark_send_failure(
            settings,
            telegram_id=ADMIN_ID,
            opportunity_uid=signal.opportunity_uid,
            claim_id=ready.claim_id,
            exc=TimeoutError("timed out"),
            now=now + timedelta(minutes=30 * attempt),
        )
        if attempt < MAX_SEND_ATTEMPTS:
            assert status == DeliveryStatus.RETRY_WAIT
        else:
            assert status == DeliveryStatus.DEAD_LETTER
    final = load_delivery(settings, ADMIN_ID, signal.opportunity_uid)
    assert final is not None
    assert final.status == DeliveryStatus.DEAD_LETTER
    assert final.last_error_code == OpportunityDiagnosticCode.RETRY_EXHAUSTED
    # Terminal idempotency
    assert (
        mark_delivery_terminal(
            settings,
            telegram_id=ADMIN_ID,
            opportunity_uid=signal.opportunity_uid,
            claim_id="ignored",
            status=DeliveryStatus.SENT,
        )
        is False
    )


def test_permanent_telegram_error_dead_letters(settings):
    signal = _seed_signal_and_metrics(settings)
    save_subscription(settings, _instant_sub(ADMIN_ID), expected_revision=None)
    select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())
    ready = prepare_authorized_send(
        settings, telegram_id=ADMIN_ID, opportunity_uid=signal.opportunity_uid, now=_now()
    )
    assert isinstance(ready, AuthorizedSendReady)

    class Forbidden(Exception):
        pass

    status = mark_send_failure(
        settings,
        telegram_id=ADMIN_ID,
        opportunity_uid=signal.opportunity_uid,
        claim_id=ready.claim_id,
        classification=classify_telegram_send_outcome(Forbidden("bot was blocked by the user")),
    )
    assert status == DeliveryStatus.DEAD_LETTER
    row = load_delivery(settings, ADMIN_ID, signal.opportunity_uid)
    assert row is not None
    assert row.last_error_code == OpportunityDiagnosticCode.TELEGRAM_BLOCKED


def test_authorization_recheck_suppresses_without_reroute(settings):
    signal = _seed_signal_and_metrics(settings)
    save_subscription(settings, _instant_sub(ADMIN_ID), expected_revision=None)
    select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())

    # Disable subscription after selection.
    save_subscription(
        settings,
        _instant_sub(ADMIN_ID, revision=1, enabled=False),
        expected_revision=0,
    )
    result = prepare_authorized_send(
        settings, telegram_id=ADMIN_ID, opportunity_uid=signal.opportunity_uid, now=_now()
    )
    assert isinstance(result, AuthorizedSendSuppressed)
    assert result.reason == OpportunityDiagnosticCode.SUBSCRIPTION_DISABLED
    row = load_delivery(settings, ADMIN_ID, signal.opportunity_uid)
    assert row is not None
    assert row.status == DeliveryStatus.SUPPRESSED
    assert row.telegram_id == ADMIN_ID


def test_authorization_revision_gate_quiet_category_live(settings):
    signal = _seed_signal_and_metrics(settings)
    save_subscription(settings, _instant_sub(ADMIN_ID), expected_revision=None)
    select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())

    save_subscription(
        settings,
        _instant_sub(ADMIN_ID, revision=1, minimum_score=70),
        expected_revision=0,
    )
    ok, reason = recheck_delivery_authorization(
        settings,
        telegram_id=ADMIN_ID,
        opportunity_uid=signal.opportunity_uid,
        settings_revision=0,
        now=_now(),
    )
    assert ok is False
    assert reason == OpportunityDiagnosticCode.SETTINGS_REVISION_CHANGED

    save_delivery_gates(
        settings,
        OpportunityDeliveryGates(admin_delivery_enabled=False, user_delivery_enabled=False),
    )
    ok, reason = recheck_delivery_authorization(
        settings,
        telegram_id=ADMIN_ID,
        opportunity_uid=signal.opportunity_uid,
        settings_revision=1,
        now=_now(),
    )
    assert ok is False
    assert reason == OpportunityDiagnosticCode.ADMIN_GATE_DISABLED

    save_delivery_gates(
        settings,
        OpportunityDeliveryGates(admin_delivery_enabled=True, user_delivery_enabled=False),
    )
    save_subscription(
        settings,
        _instant_sub(
            ADMIN_ID,
            revision=2,
            quiet_hours=QuietHours(start=time(0, 0), end=time(23, 59)),
        ),
        expected_revision=1,
    )
    ok, reason = recheck_delivery_authorization(
        settings,
        telegram_id=ADMIN_ID,
        opportunity_uid=signal.opportunity_uid,
        settings_revision=2,
        now=_now(),
    )
    assert ok is False
    assert reason == OpportunityDiagnosticCode.QUIET_HOURS_ACTIVE


def test_payload_deterministic_immutable_and_sanitized(settings):
    signal = _seed_signal_and_metrics(settings)
    payload = build_delivery_card_payload(
        opportunity_uid=signal.opportunity_uid,
        signal_at=signal.signal_at,
        wave_at=signal.signal_at,
        signal_family=signal.family,
        question="Will the example happen?",
        outcome="Yes",
        event_slug="market-one",
        mode=signal.mode,
        canonical_score=signal.score,
        reason_codes=signal.reason_codes,
        direction=signal.direction,
        move_5m=Decimal("0.03"),
        move_15m=Decimal("0.04"),
        move_1h=Decimal("0.05"),
        best_bid=Decimal("0.54"),
        best_ask=Decimal("0.56"),
        midpoint=Decimal("0.55"),
        absolute_spread=Decimal("0.02"),
        relative_spread=Decimal("0.03"),
        allocation_usd=Decimal("100"),
        execution_vwap=Decimal("0.56"),
        slippage=Decimal("0.01"),
        executable_ask_depth_usd=Decimal("1000"),
        volume_24h_usd=Decimal("250000"),
        liquidity_usd=Decimal("100000"),
        end_at=signal.signal_at + timedelta(days=1),
        source_observed_at=signal.source_observed_at,
    )
    text_a = payload.to_payload_text()
    text_b = payload.to_payload_text()
    assert text_a == text_b
    parsed = json.loads(text_a)
    assert set(parsed) == {
        "opportunity_uid",
        "signal_at",
        "wave_at",
        "signal_family",
        "question",
        "outcome",
        "event_slug",
        "market_link",
        "mode",
        "canonical_score",
        "reason_codes",
        "direction",
        "move_5m",
        "move_15m",
        "move_1h",
        "best_bid",
        "best_ask",
        "midpoint",
        "absolute_spread",
        "relative_spread",
        "allocation_usd",
        "execution_vwap",
        "slippage",
        "executable_ask_depth_usd",
        "volume_24h_usd",
        "liquidity_usd",
        "end_at",
        "source_observed_at",
    }
    assert "token" not in text_a.lower()
    assert ".env" not in text_a
    assert "users\\" not in text_a
    assert parsed["market_link"] == "https://polymarket.com/event/market-one"

    save_subscription(settings, _instant_sub(ADMIN_ID), expected_revision=None)
    select_delivery_wave(settings, opportunity_uid=signal.opportunity_uid, now=_now())
    with get_connection(settings) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE opportunity_deliveries
                SET payload_text = ?
                WHERE telegram_id = ? AND opportunity_uid = ?
                """,
                (text_a.replace("80", "81"), ADMIN_ID, signal.opportunity_uid),
            )


def test_classifier_retryable_versus_permanent():
    class RetryAfter(Exception):
        retry_after = 12

    class NetworkError(Exception):
        pass

    class Forbidden(Exception):
        pass

    retryable = classify_telegram_send_outcome(RetryAfter())
    assert retryable.retryable is True
    assert retryable.code == OpportunityDiagnosticCode.TELEGRAM_RATE_LIMITED
    assert retryable.retry_after_seconds == 12
    assert classify_telegram_send_outcome(NetworkError()).retryable is True
    permanent = classify_telegram_send_outcome(Forbidden("forbidden"))
    assert permanent.retryable is False
