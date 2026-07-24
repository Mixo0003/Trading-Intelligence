"""V2.2.3 Checkpoint 4 — scanner cycle, baseline, and signal episodes."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.copy_intel.api_result import ApiSuccess
from app.copy_intel.order_book import parse_validated_order_book
from app.database.opportunity_storage import (
    commit_scanner_cycle,
    initialize_opportunity_schema,
    load_active_signals_for_asset,
    load_market_state,
    load_scanner_state,
)
from app.database.storage import initialize_database
from app.opportunity_intel.baseline import advance_baseline
from app.opportunity_intel.book import BookAdapterFailure, BookAdapterSuccess
from app.opportunity_intel.discovery import (
    DiscoveryResult,
    OutcomeCandidate,
    ResultStatus,
    ValidatedMarket,
    ValidatedOutcome,
)
from app.opportunity_intel.episodes import (
    build_opportunity_key,
    decide_episode_actions,
    identity_fingerprint,
)
from app.opportunity_intel.history import (
    FreshnessStatus,
    HistoryDirection,
    HistoryPoint,
    HistoryReadResult,
    MovementResult,
)
from app.opportunity_intel.live_confirm import (
    confirm_strict_live_books,
    noop_delay,
)
from app.opportunity_intel.models import (
    MovementDirection,
    ObservationMode,
    OpportunityDiagnosticCode,
    OpportunityReasonCode,
    ScannerStatus,
)
from app.opportunity_intel.scanner import OpportunityScannerService
from app.opportunity_intel.versions import BASELINE_VERSION, OPPORTUNITY_SCORE_VERSION
from tests.conftest import make_test_settings

NOW = datetime(2026, 7, 24, 16, 0, tzinfo=timezone.utc)
ASSET = "asset-scan-1"


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(database_path=str(tmp_path / "scanner_cp4.db"))
    initialize_database(value)
    initialize_opportunity_schema(value)
    return value


def _book(*, timestamp: float = 1_000.0, bid="0.495", ask="0.505", ask_size="5000"):
    payload = {
        "asset_id": ASSET,
        "timestamp": timestamp,
        "hash": "hash-scan",
        "bids": [{"price": bid, "size": "200"}],
        "asks": [{"price": ask, "size": ask_size}],
    }
    result = parse_validated_order_book(
        payload, requested_asset=ASSET, received_at=timestamp + 5
    )
    assert isinstance(result, ApiSuccess)
    return result.data


def _movement(*, change_5m="4", latest="0.50") -> MovementResult:
    return MovementResult(
        asset_id=ASSET,
        status=ResultStatus.COMPLETE,
        freshness=FreshnessStatus.FRESH,
        diagnostic_code=None,
        latest_price=Decimal(latest),
        latest_at=NOW - timedelta(seconds=10),
        price_5m=Decimal(latest) - Decimal(change_5m) / Decimal("100"),
        price_15m=Decimal(latest) - Decimal("6") / Decimal("100"),
        price_1h=Decimal(latest) - Decimal("8") / Decimal("100"),
        change_5m_pp=Decimal(change_5m),
        change_15m_pp=Decimal("6"),
        change_1h_pp=Decimal("8"),
        absolute_change_5m_pp=abs(Decimal(change_5m)),
        absolute_change_15m_pp=Decimal("6"),
        absolute_change_1h_pp=Decimal("8"),
        direction=HistoryDirection.UP,
    )


def _candidate(*, mode=ObservationMode.NORMAL) -> OutcomeCandidate:
    return OutcomeCandidate(
        event_id="event-1",
        market_id="market-1",
        condition_id="condition-1",
        event_slug="slug-1",
        question="Will it happen?",
        category_ids=("politics",),
        mode=mode,
        liquidity_usd=Decimal("50000"),
        volume_24h_usd=Decimal("100000"),
        metadata_at=NOW - timedelta(minutes=2),
        end_at=NOW + timedelta(days=2),
        asset_id=ASSET,
        outcome="Yes",
        outcome_index=0,
        price_hint=Decimal("0.50"),
    )


def _discovery(candidate: OutcomeCandidate, *, now: datetime = NOW) -> DiscoveryResult:
    market = ValidatedMarket(
        event_id=candidate.event_id,
        market_id=candidate.market_id,
        condition_id=candidate.condition_id,
        event_slug=candidate.event_slug,
        question=candidate.question,
        category_ids=candidate.category_ids,
        mode=candidate.mode,
        liquidity_usd=candidate.liquidity_usd,
        volume_24h_usd=candidate.volume_24h_usd,
        metadata_at=now - timedelta(minutes=1),
        end_at=now + timedelta(days=2),
        outcomes=(
            ValidatedOutcome(
                asset_id=candidate.asset_id,
                name=candidate.outcome,
                price_hint=candidate.price_hint,
                index=candidate.outcome_index,
            ),
        ),
    )
    return DiscoveryResult(
        status=ResultStatus.COMPLETE,
        markets=(market,),
        diagnostics=(),
        pages_fetched=1,
        completed_at=now,
        from_cache=False,
    )


class FakeHistory:
    def __init__(self, movement: MovementResult):
        self.movement = movement
        self.calls = 0

    def fetch(self, asset_id, *, start_at, end_at):
        self.calls += 1
        points = (
            HistoryPoint(at=NOW - timedelta(hours=1), price=Decimal("0.42")),
            HistoryPoint(at=NOW - timedelta(seconds=10), price=Decimal("0.50")),
        )
        return HistoryReadResult(
            asset_id=asset_id,
            status=ResultStatus.COMPLETE,
            points=points,
        )


class FakeBook:
    def __init__(self, books):
        self.books = list(books)
        self.calls = 0

    def fetch(self, asset_id, *, mode, received_at=None):
        self.calls += 1
        book = self.books[(self.calls - 1) % len(self.books)]
        from app.opportunity_intel.book import adapt_validated_book

        return adapt_validated_book(book, requested_asset=asset_id, mode=mode)


def _patch_select(monkeypatch, movement):
    monkeypatch.setattr(
        "app.opportunity_intel.scanner.select_movement",
        lambda history, *, mode, now: movement,
    )


def test_baseline_two_cycles_then_crossing_signal(settings, monkeypatch):
    candidate = _candidate()
    movement = _movement()
    book = _book()
    history = FakeHistory(movement)
    books = FakeBook([book])
    _patch_select(monkeypatch, movement)
    service = OpportunityScannerService(
        settings,
        history_client=history,
        book_client=books,
        concurrency=1,
        delay=noop_delay,
    )
    discovery = _discovery(candidate)

    cycle1 = service.run_cycle(
        now=NOW, strict_live_demand=False, discovery_result=discovery
    )
    assert cycle1.storage_ok
    assert cycle1.signals_created == 0
    assert cycle1.baseline_advanced == 1
    state = load_market_state(settings, ASSET)
    assert state is not None
    assert state.baseline.consecutive_complete_cycles == 1
    assert state.baseline.ready is False

    t2 = NOW + timedelta(minutes=5)
    cycle2 = service.run_cycle(
        now=t2,
        strict_live_demand=False,
        discovery_result=_discovery(candidate, now=t2),
    )
    assert cycle2.signals_created == 0
    state = load_market_state(settings, ASSET)
    assert state.baseline.consecutive_complete_cycles == 2
    assert state.baseline.ready is True
    assert state.was_eligible_at_baseline is True

    # Still suppressed while was_eligible_at_baseline remains set.
    t3 = NOW + timedelta(minutes=10)
    cycle3 = service.run_cycle(
        now=t3,
        strict_live_demand=False,
        discovery_result=_discovery(candidate, now=t3),
    )
    assert cycle3.signals_created == 0
    assert load_active_signals_for_asset(settings, ASSET) == ()


def test_incomplete_discovery_writes_nothing(settings, monkeypatch):
    service = OpportunityScannerService(settings, concurrency=1)
    incomplete = DiscoveryResult(
        status=ResultStatus.RETRYABLE_FAILURE,
        markets=(),
        diagnostics=(),
        pages_fetched=0,
        completed_at=None,
    )
    before = load_scanner_state(settings)
    result = service.run_cycle(
        now=NOW, strict_live_demand=False, discovery_result=incomplete
    )
    assert result.signals_created == 0
    assert result.baseline_advanced == 0
    assert OpportunityDiagnosticCode.DISCOVERY_INCOMPLETE in result.diagnostic_codes
    assert load_scanner_state(settings) == before
    assert load_market_state(settings, ASSET) is None


def test_post_baseline_crossing_after_suppress_clear(settings, monkeypatch):
    candidate = _candidate()
    movement = _movement()
    book = _book()
    history = FakeHistory(movement)
    books = FakeBook([book])
    _patch_select(monkeypatch, movement)
    service = OpportunityScannerService(
        settings,
        history_client=history,
        book_client=books,
        concurrency=1,
        delay=noop_delay,
    )
    discovery = _discovery(candidate)
    service.run_cycle(now=NOW, strict_live_demand=False, discovery_result=discovery)
    t2 = NOW + timedelta(minutes=5)
    service.run_cycle(
        now=t2,
        strict_live_demand=False,
        discovery_result=_discovery(candidate, now=t2),
    )

    # Two complete below-floor observations clear suppress.
    weak = _movement(change_5m="0.5")
    _patch_select(monkeypatch, weak)
    for minutes in (15, 20):
        when = NOW + timedelta(minutes=minutes)
        service.run_cycle(
            now=when,
            strict_live_demand=False,
            discovery_result=_discovery(candidate, now=when),
        )
    state = load_market_state(settings, ASSET)
    assert state.was_eligible_at_baseline is False

    _patch_select(monkeypatch, movement)
    when = NOW + timedelta(minutes=25)
    created = service.run_cycle(
        now=when,
        strict_live_demand=False,
        discovery_result=_discovery(candidate, now=when),
    )
    assert created.signals_created == 1
    signals = load_active_signals_for_asset(settings, ASSET)
    assert len(signals) == 1
    assert signals[0].episode_sequence == 1
    assert signals[0].material_change_sequence == 0
    key = build_opportunity_key(
        condition_id="condition-1",
        asset_id=ASSET,
        direction=MovementDirection.UP,
        mode=ObservationMode.NORMAL,
    )
    assert signals[0].opportunity_key == key


def test_storage_rollback_preserves_prior(settings, monkeypatch):
    candidate = _candidate()
    movement = _movement()
    book = _book()
    history = FakeHistory(movement)
    books = FakeBook([book])
    _patch_select(monkeypatch, movement)
    service = OpportunityScannerService(
        settings,
        history_client=history,
        book_client=books,
        concurrency=1,
        delay=noop_delay,
    )
    discovery = _discovery(candidate)
    service.run_cycle(now=NOW, strict_live_demand=False, discovery_result=discovery)
    prior = load_market_state(settings, ASSET)
    assert prior.baseline.consecutive_complete_cycles == 1

    monkeypatch.setattr(
        "app.opportunity_intel.scanner.commit_scanner_cycle",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    t2 = NOW + timedelta(minutes=5)
    failed = service.run_cycle(
        now=t2,
        strict_live_demand=False,
        discovery_result=_discovery(candidate, now=t2),
    )
    assert failed.storage_ok is False
    assert OpportunityDiagnosticCode.STORAGE_FAILURE in failed.diagnostic_codes
    after = load_market_state(settings, ASSET)
    assert after.baseline.consecutive_complete_cycles == 1


def test_live_confirmation_uses_injected_delay(monkeypatch):
    delays: list[float] = []

    def capture(seconds: float) -> None:
        delays.append(seconds)

    book = _book(timestamp=1_000.0)
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return book

    result = confirm_strict_live_books(
        asset_id=ASSET,
        fetch_book=fetch,
        delay=capture,
        delay_seconds=3,
    )
    assert isinstance(result, type(result))
    assert delays == [3.0]
    assert calls["n"] == 2
    assert result.second is book


def test_live_inter_read_instability_rejected():
    first = _book(timestamp=1_000.0, bid="0.40", ask="0.42")
    second = _book(timestamp=1_005.0, bid="0.60", ask="0.62")
    books = [first, second]
    idx = {"i": 0}

    def fetch():
        book = books[idx["i"]]
        idx["i"] += 1
        return book

    result = confirm_strict_live_books(
        asset_id=ASSET,
        fetch_book=fetch,
        delay=noop_delay,
        delay_seconds=2,
    )
    assert isinstance(result, type(result))
    from app.opportunity_intel.live_confirm import LiveConfirmationFailure

    assert isinstance(result, LiveConfirmationFailure)
    assert result.diagnostic_code == OpportunityDiagnosticCode.LIVE_BOOK_UNSTABLE


def test_advance_baseline_preserves_on_failure():
    from app.database.opportunity_storage import MarketStateRecord
    from app.opportunity_intel.models import OpportunityBaseline

    prior_baseline = OpportunityBaseline(
        asset_id=ASSET,
        mode=ObservationMode.NORMAL,
        baseline_version=BASELINE_VERSION,
        consecutive_complete_cycles=1,
        last_complete_cycle_id="c1",
        last_observed_at=NOW,
        ready=False,
    )
    prior = MagicMock()
    prior.baseline = prior_baseline
    prior.mode = ObservationMode.NORMAL
    prior.identity_fingerprint = "fp"
    prior.score_version = OPPORTUNITY_SCORE_VERSION
    prior.safety_policy_version = 1
    decision = advance_baseline(
        asset_id=ASSET,
        mode=ObservationMode.NORMAL,
        prior=prior,
        cycle_id="c2",
        observed_at=NOW,
        observation_valid=False,
        identity_fingerprint="fp",
        now=NOW,
    )
    assert decision.advanced is False
    assert decision.baseline.consecutive_complete_cycles == 1


def test_identical_source_observation_does_not_duplicate_signal():
    movement = _movement()
    decision = decide_episode_actions(
        mode=ObservationMode.NORMAL,
        now=NOW,
        condition_id="condition-1",
        asset_id=ASSET,
        end_at=NOW + timedelta(days=1),
        baseline_ready=True,
        allow_new_episode=True,
        prior=None,
        eligible=True,
        complete_below_floor=False,
        score=Decimal("80"),
        reason_codes=(OpportunityReasonCode.FAST_5M_MOVE,),
        movement=movement,
        source_observed_at=NOW,
        active_signals=(),
        market_closed=False,
        next_episode_sequence=1,
    )
    assert decision.create_signal is not None
    again = decide_episode_actions(
        mode=ObservationMode.NORMAL,
        now=NOW + timedelta(minutes=20),
        condition_id="condition-1",
        asset_id=ASSET,
        end_at=NOW + timedelta(days=1),
        baseline_ready=True,
        allow_new_episode=True,
        prior=None,
        eligible=True,
        complete_below_floor=False,
        score=Decimal("80"),
        reason_codes=(OpportunityReasonCode.FAST_5M_MOVE,),
        movement=movement,
        source_observed_at=NOW,
        active_signals=(decision.create_signal,),
        market_closed=False,
        next_episode_sequence=1,
    )
    assert again.create_signal is None
    assert again.material_updated is False
