"""Regression tests for exactly-once Copy Intelligence alerts."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.copy_intel.event_identity import build_event_uid
from app.copy_intel.freshness import (
    filter_alertable_events,
    run_baseline,
)
from app.copy_intel.ingestion import (
    diff_positions,
    ingest_wallet,
)
from app.copy_intel.models import PositionSnapshot, TradeEvent
from app.copy_intel.scheduler import copy_intel_poll_job
from app.copy_intel.watchlist import add_wallet, set_monitoring
from app.database import copy_storage
from app.database.storage import initialize_database
from tests.conftest import make_test_settings

WALLET = "0xee00ba338c59557141789b127927a55f5cc5cea1"
CID = "cond-dedup-1"


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "dedup.db"),
        copy_intel_enabled=True,
        copy_max_event_age_seconds=600,
        admin_ids=[101, 202],
    )
    initialize_database(s)
    return s


def _pos(size: float = 100.0, condition_id: str = CID) -> dict:
    return {
        "conditionId": condition_id,
        "asset": "token-1",
        "outcome": "Yes",
        "size": size,
        "avgPrice": 0.4,
        "curPrice": 0.5,
        "title": "Test market",
        "slug": "test-market",
        "eventSlug": "test-event",
    }


def _client(positions: list[dict], trades: list[dict] | None = None):
    client = MagicMock()
    client.fetch_positions.return_value = positions
    client.fetch_trades.return_value = trades or []
    return client


def _snap(size: float, condition_id: str = CID) -> PositionSnapshot:
    return PositionSnapshot(
        wallet=WALLET,
        condition_id=condition_id,
        asset="token-1",
        outcome="Yes",
        size=size,
        avg_price=0.4,
        cur_price=0.5,
        title="Test market",
        slug="test-market",
        event_slug="test-event",
    )


def _event_with_uid(**kwargs) -> TradeEvent:
    base = dict(
        wallet=WALLET,
        event_type="open",
        condition_id=CID,
        asset="token-1",
        outcome="Yes",
        side="BUY",
        price=0.5,
        size=10.0,
        tx_hash=None,
        ts=None,
        title="Test",
        slug="test",
        dedup_key="dk",
        prev_size=0.0,
        new_size=10.0,
    )
    base.update(kwargs)
    event = TradeEvent(**base)
    return replace(event, event_uid=build_event_uid(event), dedup_key=event.dedup_key or "dk")


class TestStableIdentity:
    def test_identical_tx_ignores_observed_at(self):
        a = _event_with_uid(tx_hash="0xABC", ts=100, observed_at=1000)
        b = _event_with_uid(tx_hash="0xABC", ts=100, observed_at=9999)
        assert build_event_uid(a) == build_event_uid(b)

    def test_poll_seq_ignored(self):
        e = _event_with_uid(ts=None)
        assert build_event_uid(e, poll_seq=1) == build_event_uid(e, poll_seq=50)


class TestPositionDiffDedup:
    def test_unchanged_position_no_event(self):
        prev = {CID: _snap(100)}
        cur = {CID: _snap(100)}
        assert diff_positions(WALLET, prev, cur) == []

    def test_genuine_increase_one_event(self):
        prev = {CID: _snap(100)}
        cur = {CID: _snap(150)}
        events = diff_positions(WALLET, prev, cur)
        assert len(events) == 1
        assert events[0].event_type == "increase"
        assert events[0].event_uid
        assert "100.000000->150.000000" in (events[0].event_uid or "")

    def test_same_increase_repeated_no_duplicates(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_client([_pos(100)]))
        now = int(time.time())
        client = _client(
            [_pos(150)],
            trades=[
                {
                    "transactionHash": "0xincdup",
                    "conditionId": CID,
                    "asset": "token-1",
                    "outcome": "Yes",
                    "side": "BUY",
                    "size": 50.0,
                    "price": 0.5,
                    "timestamp": now - 15,
                    "title": "T",
                    "slug": "t",
                    "type": "TRADE",
                }
            ],
        )
        first = ingest_wallet(settings, WALLET, client=client)
        assert len(first) == 1
        alert1 = filter_alertable_events(settings, WALLET, first, now=now)
        assert len(alert1) == 1
        # Same book returned repeatedly (API replay / same size).
        for _ in range(10):
            again = ingest_wallet(settings, WALLET, client=client)
            assert again == []
            assert filter_alertable_events(settings, WALLET, again, now=now) == []

    def test_unsigned_increase_rejected(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_client([_pos(100)]))
        client = _client([_pos(150)])
        events = ingest_wallet(settings, WALLET, client=client)
        assert len(events) == 1
        assert events[0].tx_hash is None
        assert events[0].ts is None
        assert filter_alertable_events(settings, WALLET, events) == []
        assert copy_storage.is_processed_copy_event(settings, events[0].event_uid or "")

class TestExactlyOnceAcrossPolls:
    def test_same_api_event_across_10_polls(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_client([]))
        now = int(time.time())
        event = _event_with_uid(
            tx_hash="0xdeadbeef",
            ts=now - 30,
            event_type="open",
            size=25.0,
        )
        total = 0
        for _ in range(10):
            total += len(filter_alertable_events(settings, WALLET, [event], now=now))
        assert total == 1

    def test_same_event_after_restart(self, settings, tmp_path):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_client([]))
        now = int(time.time())
        event = _event_with_uid(tx_hash="0xrestart", ts=now - 20)
        assert len(filter_alertable_events(settings, WALLET, [event], now=now)) == 1
        settings2 = make_test_settings(
            database_path=settings.database_path,
            copy_max_event_age_seconds=600,
        )
        assert filter_alertable_events(settings2, WALLET, [event], now=now) == []

    def test_two_events_same_timestamp(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_client([]))
        now = int(time.time())
        ts = now - 15
        a = _event_with_uid(tx_hash="0xaaa1", ts=ts, condition_id="c1")
        b = _event_with_uid(tx_hash="0xbbb2", ts=ts, condition_id="c2")
        alertable = filter_alertable_events(settings, WALLET, [a, b], now=now)
        assert len(alertable) == 2


class TestBaselineAndResume:
    def test_baseline_zero_alerts(self, settings):
        add_wallet(settings, WALLET)
        client = _client([_pos(100), _pos(50, "cond-2")])
        assert run_baseline(settings, WALLET, client=client) == "COMPLETE"
        events = ingest_wallet(settings, WALLET, client=client)
        assert filter_alertable_events(settings, WALLET, events) == []

    def test_pause_resume_zero_historical(self, settings):
        add_wallet(settings, WALLET)
        client = _client([_pos(100)])
        run_baseline(settings, WALLET, client=client)
        set_monitoring(settings, WALLET, enabled=False, actor="1")
        set_monitoring(settings, WALLET, enabled=True, actor="1")
        assert run_baseline(settings, WALLET, client=client) == "COMPLETE"
        events = ingest_wallet(settings, WALLET, client=client)
        assert filter_alertable_events(settings, WALLET, events) == []


class TestConcurrentAndDelivery:
    def test_overlapping_polls_one_alert(self, settings):
        add_wallet(settings, WALLET)
        run_baseline(settings, WALLET, client=_client([]))
        now = int(time.time())
        event = _event_with_uid(tx_hash="0xconcurrent", ts=now - 10)

        def _once() -> int:
            return len(filter_alertable_events(settings, WALLET, [event], now=now))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: _once(), range(8)))
        assert sum(results) == 1

    def test_job_lock_blocks_overlap(self, settings):
        assert copy_storage.try_acquire_job_lock(
            settings, job_name="copy_intel_poll", owner="a", ttl_seconds=120
        )
        assert not copy_storage.try_acquire_job_lock(
            settings, job_name="copy_intel_poll", owner="b", ttl_seconds=120
        )
        copy_storage.release_job_lock(settings, job_name="copy_intel_poll", owner="a")
        assert copy_storage.try_acquire_job_lock(
            settings, job_name="copy_intel_poll", owner="b", ttl_seconds=120
        )

    def test_per_user_delivery_isolation(self, settings):
        uid = "tx:0xiso:open:cond:yes"
        assert copy_storage.claim_copy_alert_delivery(
            settings, subscriber_id=101, wallet=WALLET, event_uid=uid, payload_text="A"
        )
        assert not copy_storage.claim_copy_alert_delivery(
            settings, subscriber_id=101, wallet=WALLET, event_uid=uid, payload_text="A"
        )
        assert copy_storage.claim_copy_alert_delivery(
            settings, subscriber_id=202, wallet=WALLET, event_uid=uid, payload_text="A"
        )

    def test_send_failure_retry_at_most_one_success(self, settings):
        uid = "tx:0xfail:open:cond:yes"
        assert copy_storage.claim_copy_alert_delivery(
            settings, subscriber_id=101, wallet=WALLET, event_uid=uid, payload_text="CARD"
        )
        copy_storage.mark_copy_alert_delivery(
            settings, subscriber_id=101, wallet=WALLET, event_uid=uid, status="failed"
        )
        # Reclaim for retry
        assert copy_storage.claim_copy_alert_delivery(
            settings, subscriber_id=101, wallet=WALLET, event_uid=uid, payload_text="CARD"
        )
        copy_storage.mark_copy_alert_delivery(
            settings, subscriber_id=101, wallet=WALLET, event_uid=uid, status="sent"
        )
        # No further claims after successful send
        assert not copy_storage.claim_copy_alert_delivery(
            settings, subscriber_id=101, wallet=WALLET, event_uid=uid, payload_text="CARD"
        )


@pytest.mark.asyncio
async def test_scheduler_skips_when_locked(settings):
    add_wallet(settings, WALLET)
    assert copy_storage.try_acquire_job_lock(
        settings, job_name="copy_intel_poll", owner="other", ttl_seconds=300
    )
    context = MagicMock()
    context.application.bot_data = {"settings": settings}
    context.bot = AsyncMock()
    await copy_intel_poll_job(context)
    context.bot.send_message.assert_not_awaited()
