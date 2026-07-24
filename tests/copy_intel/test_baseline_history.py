"""Regression: baseline must suppress all pre-subscription historical alerts."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from app.copy_intel.event_identity import build_event_uid
from app.copy_intel.freshness import (
    filter_alertable_events,
    run_baseline,
)
from app.copy_intel.ingestion import diff_positions, ingest_wallet
from app.copy_intel.models import PositionSnapshot, TradeEvent
from app.copy_intel.watchlist import add_wallet, set_monitoring
from app.database import copy_storage
from app.database.storage import initialize_database
from tests.conftest import make_test_settings

WALLET = "0xee00ba338c59557141789b127927a55f5cc5cea1"


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "baseline_hist.db"),
        copy_intel_enabled=True,
        copy_max_event_age_seconds=600,
        admin_ids=[1],
    )
    initialize_database(s)
    return s


def _pos(condition_id: str, size: float = 100.0) -> dict:
    return {
        "conditionId": condition_id,
        "asset": f"asset-{condition_id}",
        "outcome": "Yes",
        "size": size,
        "avgPrice": 0.4,
        "curPrice": 0.5,
        "title": f"Market {condition_id}",
        "slug": f"slug-{condition_id}",
        "eventSlug": f"event-{condition_id}",
    }


def _trade(
    *,
    tx: str,
    condition_id: str = "cond-old",
    ts: int,
    side: str = "BUY",
    size: float = 10.0,
) -> dict:
    return {
        "transactionHash": tx,
        "conditionId": condition_id,
        "asset": "asset-1",
        "outcome": "Yes",
        "side": side,
        "size": size,
        "price": 0.5,
        "timestamp": ts,
        "title": "Old",
        "slug": "old",
        "type": "TRADE",
    }


def _client(
    positions: list[dict],
    *,
    trades: list[dict] | None = None,
    activity: list[dict] | None = None,
    closed: list[dict] | None = None,
    fail: str | None = None,
):
    client = MagicMock()
    if fail == "positions":
        client.fetch_positions.side_effect = RuntimeError("positions down")
    else:
        client.fetch_positions.return_value = positions
    if fail == "trades":
        client.fetch_trades.side_effect = RuntimeError("trades down")
    else:
        client.fetch_trades.return_value = trades or []
    client.fetch_activity.return_value = activity if activity is not None else (trades or [])
    client.fetch_closed_positions.return_value = closed or []
    return client


def _snap(condition_id: str, size: float) -> PositionSnapshot:
    return PositionSnapshot(
        wallet=WALLET,
        condition_id=condition_id,
        asset=f"asset-{condition_id}",
        outcome="Yes",
        size=size,
        avg_price=0.4,
        cur_price=0.5,
        title=f"Market {condition_id}",
        slug=f"slug-{condition_id}",
        event_slug=f"event-{condition_id}",
    )


class TestHistoricalBaseline:
    def test_subscribe_20_existing_positions_zero_alerts(self, settings):
        positions = [_pos(f"cond-{i}", 50.0 + i) for i in range(20)]
        add_wallet(settings, WALLET)
        client = _client(positions)
        assert run_baseline(settings, WALLET, client=client) == "COMPLETE"
        events = ingest_wallet(settings, WALLET, client=client)
        assert events == []
        assert filter_alertable_events(settings, WALLET, events) == []

    def test_same_positions_across_10_polls_zero_alerts(self, settings):
        positions = [_pos(f"cond-{i}") for i in range(20)]
        add_wallet(settings, WALLET)
        client = _client(positions)
        assert run_baseline(settings, WALLET, client=client) == "COMPLETE"
        for _ in range(10):
            events = ingest_wallet(settings, WALLET, client=client)
            assert events == []
            assert filter_alertable_events(settings, WALLET, events) == []

    def test_restart_after_baseline_zero_historical(self, settings):
        positions = [_pos("cond-1"), _pos("cond-2")]
        add_wallet(settings, WALLET)
        client = _client(positions)
        assert run_baseline(settings, WALLET, client=client) == "COMPLETE"
        settings2 = make_test_settings(
            database_path=settings.database_path,
            copy_max_event_age_seconds=600,
        )
        events = ingest_wallet(settings2, WALLET, client=client)
        assert filter_alertable_events(settings2, WALLET, events) == []

    def test_old_trades_and_activity_zero_alerts(self, settings):
        now = int(time.time())
        old_ts = now - 3600
        trades = [
            _trade(tx="0xold1", condition_id="cond-1", ts=old_ts),
            _trade(tx="0xold2", condition_id="cond-2", ts=old_ts - 10),
        ]
        positions = [_pos("cond-1", 100), _pos("cond-2", 200)]
        add_wallet(settings, WALLET)
        client = _client(positions, trades=trades, activity=trades)
        assert run_baseline(settings, WALLET, client=client) == "COMPLETE"
        # Even if a synthetic event reappears with that tx, it must be suppressed.
        event = TradeEvent(
            wallet=WALLET,
            event_type="open",
            condition_id="cond-1",
            asset="asset-cond-1",
            outcome="Yes",
            side="BUY",
            price=0.5,
            size=10.0,
            tx_hash="0xold1",
            ts=old_ts,
            title="Old",
            slug="old",
            dedup_key="dk",
        )
        from dataclasses import replace

        event = replace(event, event_uid=build_event_uid(event))
        assert filter_alertable_events(settings, WALLET, [event], now=now) == []

    def test_old_closed_positions_zero_alerts(self, settings):
        closed = [
            {
                "conditionId": "cond-closed",
                "asset": "a",
                "outcome": "Yes",
                "size": 25.0,
                "avgPrice": 0.4,
                "title": "Closed",
                "slug": "closed",
            }
        ]
        add_wallet(settings, WALLET)
        client = _client([], closed=closed)
        assert run_baseline(settings, WALLET, client=client) == "COMPLETE"
        prev = {"cond-closed": _snap("cond-closed", 25.0)}
        events = diff_positions(WALLET, prev, {})
        assert events
        assert filter_alertable_events(settings, WALLET, events) == []

    def test_partial_baseline_failure_no_alerts(self, settings):
        add_wallet(settings, WALLET)
        client = _client([_pos("cond-1")], fail="trades")
        assert run_baseline(settings, WALLET, client=client) == "FAILED"
        state = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state is not None
        assert state.baseline_status == "FAILED"
        now = int(time.time())
        event = TradeEvent(
            wallet=WALLET,
            event_type="open",
            condition_id="cond-1",
            asset="a",
            outcome="Yes",
            side="BUY",
            price=0.5,
            size=10.0,
            tx_hash="0xnew",
            ts=now - 10,
            title="X",
            slug="x",
            dedup_key="dk",
            prev_size=0.0,
            new_size=10.0,
        )
        from dataclasses import replace

        event = replace(event, event_uid=build_event_uid(event))
        assert filter_alertable_events(settings, WALLET, [event], now=now) == []

    def test_first_genuine_new_trade_one_alert(self, settings):
        add_wallet(settings, WALLET)
        client = _client([_pos("cond-1", 100)])
        assert run_baseline(settings, WALLET, client=client) == "COMPLETE"
        now = int(time.time())
        event = TradeEvent(
            wallet=WALLET,
            event_type="open",
            condition_id="cond-new",
            asset="a",
            outcome="Yes",
            side="BUY",
            price=0.5,
            size=12.0,
            tx_hash="0xbrandnew",
            ts=now - 5,
            title="New",
            slug="new",
            dedup_key="dk",
            prev_size=0.0,
            new_size=12.0,
        )
        from dataclasses import replace

        event = replace(event, event_uid=build_event_uid(event))
        first = filter_alertable_events(settings, WALLET, [event], now=now)
        second = filter_alertable_events(settings, WALLET, [event], now=now)
        assert len(first) == 1
        assert second == []

    def test_genuine_increase_after_complete(self, settings):
        add_wallet(settings, WALLET)
        client = _client([_pos("cond-1", 100)])
        assert run_baseline(settings, WALLET, client=client) == "COMPLETE"
        # Unsigned position-diff without matching trades is quarantined (no accepted event).
        client_unsigned = _client([_pos("cond-1", 150)])
        unsigned = ingest_wallet(settings, WALLET, client=client_unsigned)
        assert unsigned == []

        # Reset book then apply another unsigned increase (still quarantined).
        client_reset = _client([_pos("cond-1", 150)])
        ingest_wallet(settings, WALLET, client=client_reset)
        now = int(time.time())
        client_next = _client([_pos("cond-1", 200)])
        events = ingest_wallet(settings, WALLET, client=client_next)
        assert events == []
        # Native-tx increase remains alertable via the freshness gate when provided directly.
        from dataclasses import replace

        signed = TradeEvent(
            wallet=WALLET,
            event_type="increase",
            condition_id="cond-1",
            asset="asset-cond-1",
            outcome="Yes",
            side="BUY",
            price=0.5,
            size=50.0,
            tx_hash="0xinc1",
            ts=now - 10,
            title="t",
            slug="s",
            dedup_key="dk",
            prev_size=150.0,
            new_size=200.0,
        )
        signed = replace(signed, event_uid=build_event_uid(signed))
        alertable = filter_alertable_events(settings, WALLET, [signed], now=now)
        assert len(alertable) == 1

    def test_unchanged_preexisting_position_silent(self, settings):
        add_wallet(settings, WALLET)
        client = _client([_pos("cond-1", 100)])
        assert run_baseline(settings, WALLET, client=client) == "COMPLETE"
        events = ingest_wallet(settings, WALLET, client=client)
        assert events == []
        assert filter_alertable_events(settings, WALLET, events) == []

    def test_pause_resume_no_replay(self, settings):
        add_wallet(settings, WALLET)
        client = _client([_pos("cond-1", 100)], trades=[_trade(tx="0xpause", ts=int(time.time()) - 30)])
        assert run_baseline(settings, WALLET, client=client) == "COMPLETE"
        set_monitoring(settings, WALLET, enabled=False, actor="1")
        # Activity during pause represented by larger book + old-ish trade.
        paused_client = _client(
            [_pos("cond-1", 100), _pos("cond-2", 50)],
            trades=[
                _trade(tx="0xpause", ts=int(time.time()) - 30),
                _trade(tx="0xduring", condition_id="cond-2", ts=int(time.time()) - 20),
            ],
        )
        set_monitoring(settings, WALLET, enabled=True, actor="1")
        state = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state is not None and state.baseline_status == "PENDING"
        assert run_baseline(settings, WALLET, client=paused_client) == "COMPLETE"
        events = ingest_wallet(settings, WALLET, client=paused_client)
        assert filter_alertable_events(settings, WALLET, events) == []

    def test_two_subscriptions_isolated(self, settings):
        w2 = "0x1111111111111111111111111111111111111111"
        add_wallet(settings, WALLET)
        add_wallet(settings, w2)
        c1 = _client([_pos("cond-a")])
        c2 = _client([_pos("cond-b")])
        assert run_baseline(settings, WALLET, client=c1) == "COMPLETE"
        assert run_baseline(settings, w2, client=c2) == "COMPLETE"
        assert ingest_wallet(settings, WALLET, client=c1) == []
        assert ingest_wallet(settings, w2, client=c2) == []

    def test_overlapping_baseline_live_no_historical_leak(self, settings):
        positions = [_pos(f"cond-{i}") for i in range(5)]
        add_wallet(settings, WALLET)
        client = _client(positions)

        def _work(_: int) -> int:
            # Concurrent baseline + ingest attempts must not leak alerts.
            run_baseline(settings, WALLET, client=client)
            events = ingest_wallet(settings, WALLET, client=client)
            return len(filter_alertable_events(settings, WALLET, events))

        with ThreadPoolExecutor(max_workers=6) as pool:
            totals = list(pool.map(_work, range(6)))
        assert sum(totals) == 0
        state = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state is not None and state.baseline_status == "COMPLETE"
