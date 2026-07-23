"""Tests for copy-event freshness, baseline, and watermarks."""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from app.copy_intel.freshness import (
    begin_subscription,
    build_event_uid,
    filter_alertable_events,
    run_baseline,
)
from app.copy_intel.ingestion import ingest_wallet, persist_snapshots
from app.copy_intel.models import PositionSnapshot, TradeEvent
from app.copy_intel.watchlist import add_wallet, toggle_wallet
from app.database import copy_storage
from app.database.storage import initialize_database
from tests.conftest import make_test_settings

WALLET = "0xee00ba338c59557141789b127927a55f5cc5cea1"


def _event(
    *,
    event_type: str = "open",
    condition_id: str = "cond-1",
    size: float = 10.0,
    ts: int | None = None,
    tx_hash: str | None = None,
    event_uid: str | None = None,
    observed_at: int | None = None,
) -> TradeEvent:
    return TradeEvent(
        wallet=WALLET,
        event_type=event_type,  # type: ignore[arg-type]
        condition_id=condition_id,
        asset="token-1",
        outcome="Yes",
        side="BUY",
        price=0.5,
        size=size,
        tx_hash=tx_hash,
        ts=ts,
        title="Test market",
        slug="test-market",
        dedup_key=f"dk-{condition_id}-{event_type}-{size}-{ts}-{tx_hash}",
        observed_at=observed_at,
        event_uid=event_uid,
    )


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "fresh.db"),
        copy_intel_enabled=True,
        copy_max_event_age_seconds=600,
    )
    initialize_database(s)
    return s


def _mock_client(positions: list[dict], trades: list[dict] | None = None):
    client = MagicMock()
    client.fetch_positions.return_value = positions
    client.fetch_trades.return_value = trades if trades is not None else []
    return client


def _pos(condition_id: str = "cond-1", size: float = 100.0) -> dict:
    return {
        "conditionId": condition_id,
        "asset": f"token-{condition_id}",
        "outcome": "Yes",
        "size": size,
        "avgPrice": 0.4,
        "curPrice": 0.5,
        "title": "Test market",
        "slug": "test-market",
        "eventSlug": "test-event",
    }


class TestBaseline:
    def test_initial_baseline_sends_zero_alerts(self, settings):
        add_wallet(settings, WALLET)
        client = _mock_client([_pos(), _pos("cond-2", 50.0)])
        status = run_baseline(settings, WALLET, client=client)
        assert status == "COMPLETE"
        events = ingest_wallet(settings, WALLET, client=client)
        # Same book after baseline → no diffs
        assert events == []
        alertable = filter_alertable_events(settings, WALLET, events)
        assert alertable == []

    def test_old_events_stored_but_not_alerted(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([_pos()]))
        now = int(time.time())
        old = _event(ts=now - 10_000, event_uid="old-1", observed_at=now)
        # Persist via trade events table path is separate; freshness must reject.
        alertable = filter_alertable_events(settings, WALLET, [old], now=now)
        assert alertable == []
        assert copy_storage.is_processed_copy_event(settings, "old-1")

    def test_genuinely_new_event_alerted_once(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([]))
        now = int(time.time())
        fresh = _event(ts=now - 30, event_uid="new-1", observed_at=now)
        first = filter_alertable_events(settings, WALLET, [fresh], now=now)
        second = filter_alertable_events(settings, WALLET, [fresh], now=now)
        assert len(first) == 1
        assert second == []

    def test_no_alerts_until_baseline_completes(self, settings):
        begin_subscription(settings, WALLET)
        now = int(time.time())
        fresh = _event(ts=now - 10, event_uid="premature", observed_at=now)
        assert filter_alertable_events(settings, WALLET, [fresh], now=now) == []


class TestDedupAndOrdering:
    def test_identical_timestamps_distinct_ids(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([]))
        now = int(time.time())
        ts = now - 20
        a = _event(ts=ts, event_uid="id-a", condition_id="c1", observed_at=now)
        b = _event(ts=ts, event_uid="id-b", condition_id="c2", observed_at=now)
        alertable = filter_alertable_events(settings, WALLET, [a, b], now=now)
        assert {e.event_uid for e in alertable} == {"id-a", "id-b"}

    def test_duplicated_ids_rejected(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([]))
        now = int(time.time())
        e1 = _event(ts=now - 10, event_uid="dup", observed_at=now)
        e2 = _event(ts=now - 5, event_uid="dup", observed_at=now)
        alertable = filter_alertable_events(settings, WALLET, [e1, e2], now=now)
        assert len(alertable) == 1

    def test_out_of_order_older_than_watermark(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([]))
        now = int(time.time())
        newer = _event(ts=now - 10, event_uid="newer", observed_at=now)
        assert filter_alertable_events(settings, WALLET, [newer], now=now)
        older = _event(ts=now - 100, event_uid="older", observed_at=now)
        assert filter_alertable_events(settings, WALLET, [older], now=now) == []

    def test_overlapping_pagination_same_uid(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([]))
        now = int(time.time())
        page1 = _event(ts=now - 15, event_uid="page-item", observed_at=now)
        page2 = replace(page1)
        assert len(filter_alertable_events(settings, WALLET, [page1], now=now)) == 1
        assert filter_alertable_events(settings, WALLET, [page2], now=now) == []


class TestLifecycle:
    def test_baseline_retry_after_failure(self, settings):
        add_wallet(settings, WALLET)
        bad = MagicMock()
        bad.fetch_positions.side_effect = RuntimeError("api down")
        assert run_baseline(settings, WALLET, client=bad) == "FAILED"
        state = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state is not None and state.baseline_status == "FAILED"
        assert run_baseline(settings, WALLET, client=_mock_client([_pos()])) == "COMPLETE"

    def test_disable_reenable_creates_new_baseline(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([_pos()]))
        first_sub = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert first_sub is not None
        assert toggle_wallet(settings, WALLET) is False
        assert toggle_wallet(settings, WALLET) is True
        state = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state is not None
        assert state.baseline_status == "PENDING"
        assert state.subscription_started_at != first_sub.subscription_started_at
        # Existing position at re-enable must not alert
        assert run_baseline(settings, WALLET, client=_mock_client([_pos()])) == "COMPLETE"
        events = ingest_wallet(settings, WALLET, client=_mock_client([_pos()]))
        assert filter_alertable_events(settings, WALLET, events) == []

    def test_restart_recovery_keeps_processed_uids(self, settings, tmp_path):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([]))
        now = int(time.time())
        e = _event(ts=now - 10, event_uid="persist-1", observed_at=now)
        assert filter_alertable_events(settings, WALLET, [e], now=now)
        # Re-open DB path (same file) simulates restart
        settings2 = make_test_settings(
            database_path=settings.database_path,
            copy_max_event_age_seconds=600,
        )
        assert copy_storage.is_processed_copy_event(settings2, "persist-1")
        assert filter_alertable_events(settings2, WALLET, [e], now=now) == []


class TestAgeAndResolved:
    def test_stale_trade_rejection(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([]))
        now = int(time.time())
        stale = _event(ts=now - 601, event_uid="stale", observed_at=now)
        assert filter_alertable_events(settings, WALLET, [stale], now=now) == []

    def test_event_age_threshold(self, settings):
        settings = make_test_settings(
            database_path=settings.database_path,
            copy_max_event_age_seconds=60,
        )
        # schema already init
        begin_subscription(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([]))
        now = int(time.time())
        ok = _event(ts=now - 30, event_uid="age-ok", observed_at=now)
        bad = _event(ts=now - 90, event_uid="age-bad", observed_at=now)
        assert len(filter_alertable_events(settings, WALLET, [ok], now=now)) == 1
        assert filter_alertable_events(settings, WALLET, [bad], now=now) == []

    def test_resolved_market_rejection(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([]))
        now = int(time.time())
        e = _event(ts=now - 10, event_uid="resolved-1", condition_id="cond-x", observed_at=now)
        assert (
            filter_alertable_events(
                settings,
                WALLET,
                [e],
                now=now,
                market_resolved={"cond-x": True},
            )
            == []
        )

    def test_indexing_delay_within_age_accepted(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([]))
        now = int(time.time())
        delayed = _event(ts=now - 120, event_uid="delayed", observed_at=now)
        assert len(filter_alertable_events(settings, WALLET, [delayed], now=now)) == 1


class TestEventUid:
    def test_tx_hash_preferred(self):
        e = _event(tx_hash="0xABC", ts=1)
        assert build_event_uid(e).startswith("tx:0xabc:")

    def test_position_diff_composite_ignores_poll_seq(self):
        e = _event(ts=None)
        uid_a = build_event_uid(e, poll_seq=3)
        uid_b = build_event_uid(e, poll_seq=99)
        assert uid_a.startswith("pd:")
        assert uid_a == uid_b
        assert "|3" not in uid_a
        assert "|99" not in uid_b


class TestIngestAfterBaseline:
    def test_new_position_after_baseline_quarantined_without_trades(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_mock_client([_pos("cond-1")]))
        events = ingest_wallet(
            settings,
            WALLET,
            client=_mock_client([_pos("cond-1"), _pos("cond-new", 25.0)]),
        )
        # Checkpoint 5: no matching trades → no accepted event.
        assert events == []
        alertable = filter_alertable_events(settings, WALLET, events)
        assert alertable == []
