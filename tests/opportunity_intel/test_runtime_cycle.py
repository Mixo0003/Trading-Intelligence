"""Checkpoint 7 — runtime cycle selection gates and non-blocking behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.copy_intel.api_result import ApiSuccess
from app.copy_intel.order_book import parse_validated_order_book
from app.database.opportunity_storage import (
    initialize_opportunity_schema,
    list_deliveries_for_opportunity,
    load_delivery_gates,
    save_subscription,
)
from app.database.storage import initialize_database, upsert_user
from app.opportunity_intel.discovery import (
    DiscoveryResult,
    OutcomeCandidate,
    ResultStatus,
    ValidatedMarket,
    ValidatedOutcome,
)
from app.opportunity_intel.history import (
    FreshnessStatus,
    HistoryDirection,
    HistoryPoint,
    HistoryReadResult,
    MovementResult,
)
from app.opportunity_intel.models import (
    AlertMode,
    ObservationMode,
    OpportunityDiagnosticCode,
    OpportunitySubscription,
)
from app.opportunity_intel.runtime import run_opportunity_scan_cycle
from app.opportunity_intel.scanner import OpportunityScannerService
from app.opportunity_intel.settings import balanced_settings
from tests.conftest import make_test_settings

NOW = datetime(2026, 7, 24, 17, 0, tzinfo=timezone.utc)
ASSET = "asset-rt-1"


@pytest.fixture
def settings(tmp_path):
    value = make_test_settings(
        database_path=str(tmp_path / "opp_runtime.db"),
        opportunity_scanner_enabled=True,
        opportunity_auto_alerts_enabled=False,
        opportunity_user_delivery_enabled=False,
    )
    initialize_database(value)
    initialize_opportunity_schema(value)
    upsert_user(value, 1, "admin", "Admin", language="en")
    return value


def _book(*, timestamp: float = 1_000.0):
    payload = {
        "asset_id": ASSET,
        "timestamp": timestamp,
        "hash": "hash-rt",
        "bids": [{"price": "0.495", "size": "200"}],
        "asks": [{"price": "0.505", "size": "5000"}],
    }
    result = parse_validated_order_book(
        payload, requested_asset=ASSET, received_at=timestamp + 5
    )
    assert isinstance(result, ApiSuccess)
    return result.data


def _movement() -> MovementResult:
    return MovementResult(
        asset_id=ASSET,
        status=ResultStatus.COMPLETE,
        freshness=FreshnessStatus.FRESH,
        diagnostic_code=None,
        latest_price=Decimal("0.50"),
        latest_at=NOW - timedelta(seconds=10),
        price_5m=Decimal("0.46"),
        price_15m=Decimal("0.44"),
        price_1h=Decimal("0.42"),
        change_5m_pp=Decimal("4"),
        change_15m_pp=Decimal("6"),
        change_1h_pp=Decimal("8"),
        absolute_change_5m_pp=Decimal("4"),
        absolute_change_15m_pp=Decimal("6"),
        absolute_change_1h_pp=Decimal("8"),
        direction=HistoryDirection.UP,
    )


def _candidate() -> OutcomeCandidate:
    return OutcomeCandidate(
        event_id="event-rt",
        market_id="market-rt",
        condition_id="condition-rt",
        event_slug="slug-rt",
        question="Runtime market?",
        category_ids=("politics",),
        mode=ObservationMode.NORMAL,
        liquidity_usd=Decimal("50000"),
        volume_24h_usd=Decimal("100000"),
        metadata_at=NOW - timedelta(minutes=2),
        end_at=NOW + timedelta(days=2),
        asset_id=ASSET,
        outcome="Yes",
        outcome_index=0,
        price_hint=Decimal("0.50"),
    )


def _discovery(candidate: OutcomeCandidate) -> DiscoveryResult:
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
        metadata_at=NOW - timedelta(minutes=1),
        end_at=NOW + timedelta(days=2),
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
        completed_at=NOW,
        from_cache=False,
    )


class FakeHistory:
    def __init__(self, movement: MovementResult):
        self.movement = movement

    def fetch(self, asset_id, *, start_at, end_at):
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


def _service(settings, monkeypatch, movement, books):
    monkeypatch.setattr(
        "app.opportunity_intel.scanner.select_movement",
        lambda history, *, mode, now: movement,
    )
    return OpportunityScannerService(
        settings,
        history_client=FakeHistory(movement),
        book_client=FakeBook(books),
        concurrency=2,
    )


def test_incomplete_discovery_no_selection(settings, monkeypatch):
    service = _service(settings, monkeypatch, _movement(), [_book()])
    incomplete = DiscoveryResult(
        status=ResultStatus.RETRYABLE_FAILURE,
        markets=(),
        diagnostics=(),
        pages_fetched=0,
        completed_at=NOW,
        from_cache=False,
    )
    result = run_opportunity_scan_cycle(
        settings,
        now=NOW,
        service=service,
        discovery_result=incomplete,
        skip_deadline=True,
    )
    assert result.selection_ran is False
    assert result.pending_inserted == 0
    assert result.cycle is not None
    assert result.cycle.discovery_status != ResultStatus.COMPLETE


def test_baseline_cycle_creates_zero_delivery(settings, monkeypatch):
    candidate = _candidate()
    service = _service(settings, monkeypatch, _movement(), [_book()])
    result = run_opportunity_scan_cycle(
        settings,
        now=NOW,
        service=service,
        discovery_result=_discovery(candidate),
        skip_deadline=True,
    )
    assert result.cycle is not None
    assert result.cycle.storage_ok is True
    assert result.cycle.signals_created == 0
    assert result.selection_ran is False
    assert result.pending_inserted == 0


def test_storage_failure_no_selection(settings, monkeypatch):
    candidate = _candidate()
    service = _service(settings, monkeypatch, _movement(), [_book()])
    monkeypatch.setattr(
        "app.opportunity_intel.scanner.commit_scanner_cycle",
        MagicMock(side_effect=RuntimeError("boom")),
    )
    result = run_opportunity_scan_cycle(
        settings,
        now=NOW,
        service=service,
        discovery_result=_discovery(candidate),
        skip_deadline=True,
    )
    assert result.cycle is not None
    assert result.cycle.storage_ok is False
    assert result.selection_ran is False


def test_successful_cycle_triggers_selection_when_signals(settings, monkeypatch):
    candidate = _candidate()
    movement = _movement()
    service = _service(settings, monkeypatch, movement, [_book()])
    # Seed two baseline cycles via prior run_cycle calls.
    for offset in (0, 1):
        run_opportunity_scan_cycle(
            settings,
            now=NOW + timedelta(minutes=offset),
            service=service,
            discovery_result=_discovery(candidate),
            skip_deadline=True,
        )
    save_subscription(
        settings,
        OpportunitySubscription(
            telegram_id=1,
            enabled=True,
            settings=balanced_settings(revision=0),
        ),
        expected_revision=None,
    )
    # Force eligibility/signal by using strong movement on ready baseline.
    result = run_opportunity_scan_cycle(
        settings,
        now=NOW + timedelta(minutes=5),
        service=service,
        discovery_result=_discovery(candidate),
        skip_deadline=True,
    )
    assert result.cycle is not None
    assert result.cycle.storage_ok is True
    if result.cycle.delivery_signal_uids:
        assert result.selection_ran is True
        for uid in result.cycle.delivery_signal_uids:
            rows = list_deliveries_for_opportunity(settings, uid)
            assert rows


def test_user_gate_synced_from_config(settings):
    enabled = make_test_settings(
        database_path=settings.database_path,
        opportunity_scanner_enabled=True,
        opportunity_user_delivery_enabled=True,
    )
    run_opportunity_scan_cycle(
        enabled,
        now=NOW,
        service=OpportunityScannerService(enabled, concurrency=1),
        discovery_result=DiscoveryResult(
            status=ResultStatus.RETRYABLE_FAILURE,
            markets=(),
            diagnostics=(),
            pages_fetched=0,
            completed_at=NOW,
            from_cache=False,
        ),
        skip_deadline=True,
    )
    gates = load_delivery_gates(enabled)
    assert gates.user_delivery_enabled is True


def test_one_failing_asset_does_not_stop_cycle(settings, monkeypatch):
    c1 = _candidate()
    c2 = OutcomeCandidate(
        event_id="event-rt-2",
        market_id="market-rt-2",
        condition_id="condition-rt-2",
        event_slug="slug-rt-2",
        question="Second?",
        category_ids=("politics",),
        mode=ObservationMode.NORMAL,
        liquidity_usd=Decimal("50000"),
        volume_24h_usd=Decimal("100000"),
        metadata_at=NOW - timedelta(minutes=2),
        end_at=NOW + timedelta(days=2),
        asset_id="asset-rt-2",
        outcome="Yes",
        outcome_index=0,
        price_hint=Decimal("0.50"),
    )
    market1 = ValidatedMarket(
        event_id=c1.event_id,
        market_id=c1.market_id,
        condition_id=c1.condition_id,
        event_slug=c1.event_slug,
        question=c1.question,
        category_ids=c1.category_ids,
        mode=c1.mode,
        liquidity_usd=c1.liquidity_usd,
        volume_24h_usd=c1.volume_24h_usd,
        metadata_at=NOW - timedelta(minutes=1),
        end_at=NOW + timedelta(days=2),
        outcomes=(
            ValidatedOutcome(
                asset_id=c1.asset_id,
                name=c1.outcome,
                price_hint=c1.price_hint,
                index=c1.outcome_index,
            ),
        ),
    )
    market2 = ValidatedMarket(
        event_id=c2.event_id,
        market_id=c2.market_id,
        condition_id=c2.condition_id,
        event_slug=c2.event_slug,
        question=c2.question,
        category_ids=c2.category_ids,
        mode=c2.mode,
        liquidity_usd=c2.liquidity_usd,
        volume_24h_usd=c2.volume_24h_usd,
        metadata_at=NOW - timedelta(minutes=1),
        end_at=NOW + timedelta(days=2),
        outcomes=(
            ValidatedOutcome(
                asset_id=c2.asset_id,
                name=c2.outcome,
                price_hint=c2.price_hint,
                index=c2.outcome_index,
            ),
        ),
    )
    discovery = DiscoveryResult(
        status=ResultStatus.COMPLETE,
        markets=(market1, market2),
        diagnostics=(),
        pages_fetched=1,
        completed_at=NOW,
        from_cache=False,
    )

    class MixedBook(FakeBook):
        def fetch(self, asset_id, *, mode, received_at=None):
            if asset_id == "asset-rt-2":
                from app.opportunity_intel.book import BookAdapterFailure

                return BookAdapterFailure(
                    status=ResultStatus.RETRYABLE_FAILURE,
                    diagnostic_code=OpportunityDiagnosticCode.BOOK_UNAVAILABLE,
                    request_count=1,
                )
            return super().fetch(asset_id, mode=mode, received_at=received_at)

    monkeypatch.setattr(
        "app.opportunity_intel.scanner.select_movement",
        lambda history, *, mode, now: _movement(),
    )
    service = OpportunityScannerService(
        settings,
        history_client=FakeHistory(_movement()),
        book_client=MixedBook([_book()]),
        concurrency=2,
    )
    result = run_opportunity_scan_cycle(
        settings,
        now=NOW,
        service=service,
        discovery_result=discovery,
        skip_deadline=True,
    )
    assert result.cycle is not None
    assert result.cycle.storage_ok is True
    assert result.cycle.books_failed >= 1
    assert result.cycle.books_valid >= 1
