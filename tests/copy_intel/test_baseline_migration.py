"""Live-state migration: legacy COMPLETE without baseline_version must not alert."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.copy_intel.event_identity import build_event_uid
from app.copy_intel.freshness import (
    COPY_BASELINE_VERSION,
    baseline_is_complete,
    ensure_baseline_current,
    filter_alertable_events,
    invalidate_baseline_for_rebuild,
    rebuild_all_copy_baselines,
    run_baseline,
)
from app.copy_intel.ingestion import ingest_wallet
from app.copy_intel.models import PositionSnapshot, TradeEvent
from app.copy_intel.watchlist import add_wallet, set_monitoring
from app.database import copy_storage
from app.database.storage import initialize_database
from app.telegram.auth import require_admin
from app.telegram.health.buttons import (
    CB_HEALTH_REBUILD_BASELINES,
    CB_HEALTH_REBUILD_CONFIRM,
    HEALTH_CALLBACKS,
    copy_health_keyboard,
)
from app.telegram.health.formatting import (
    format_rebuild_baselines_confirm,
    format_rebuild_baselines_result,
)
from tests.conftest import make_test_settings

W1 = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
W2 = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "baseline_mig.db"),
        copy_intel_enabled=True,
        copy_max_event_age_seconds=600,
        admin_ids=[1001],
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


def _trade(*, tx: str, condition_id: str, ts: int, side: str = "BUY", size: float = 10.0) -> dict:
    return {
        "transactionHash": tx,
        "conditionId": condition_id,
        "asset": f"asset-{condition_id}",
        "outcome": "Yes",
        "side": side,
        "size": size,
        "price": 0.5,
        "timestamp": ts,
        "title": "T",
        "slug": "t",
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


def _legacy_complete(settings, wallet: str, *, positions: list[dict], version: int | None = None) -> None:
    """Simulate a pre-upgrade COMPLETE subscription (missing/old baseline_version)."""
    add_wallet(settings, wallet)
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    copy_storage.upsert_wallet_monitor_state(
        settings,
        wallet,
        subscription_started_at=now,
        baseline_started_at=now,
        baseline_status="COMPLETE",
        baseline_error=None,
        newest_event_ts=int(time.time()) - 3600,
        newest_event_uid="legacy-uid",
        monitoring_enabled=True,
        baseline_version=version,
    )
    copy_storage.upsert_wallet_event_watermark(
        settings,
        wallet,
        newest_event_ts=int(time.time()) - 3600,
        newest_event_uid="legacy-uid",
    )
    for raw in positions:
        copy_storage.upsert_position_snapshot(
            settings,
            wallet,
            raw["conditionId"],
            asset=raw.get("asset"),
            outcome=raw.get("outcome"),
            size=float(raw.get("size") or 0),
            avg_price=float(raw.get("avgPrice") or 0),
            cur_price=float(raw.get("curPrice") or 0),
            title=raw.get("title"),
            slug=raw.get("slug"),
            event_slug=raw.get("eventSlug"),
        )
        # Old incompatible UID format (not live-compatible).
        copy_storage.mark_processed_copy_event(
            settings,
            event_uid=f"baseline:{wallet}:{raw['conditionId']}",
            wallet=wallet,
            event_ts=int(time.time()) - 7200,
            observed_at=int(time.time()) - 7200,
            is_baseline=True,
            alertable=False,
            reject_reason="baseline",
        )


def _open_event(wallet: str, condition_id: str, size: float = 100.0) -> TradeEvent:
    snap = PositionSnapshot(
        wallet=wallet,
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
    event = TradeEvent(
        wallet=wallet.lower(),
        event_type="open",
        condition_id=condition_id,
        asset=snap.asset,
        outcome=snap.outcome,
        side="BUY",
        price=snap.avg_price,
        size=size,
        tx_hash=None,
        ts=None,
        title=snap.title,
        slug=snap.slug,
        dedup_key="test",
        prev_size=0.0,
        new_size=size,
    )
    return replace(event, event_uid=build_event_uid(event))


class TestLegacyMigration:
    def test_legacy_complete_no_version_zero_alerts(self, settings):
        positions = [_pos(f"cond-{i}") for i in range(5)]
        _legacy_complete(settings, W1, positions=positions, version=None)
        assert not baseline_is_complete(settings, W1)
        events = [_open_event(W1, f"cond-{i}") for i in range(5)]
        assert filter_alertable_events(settings, W1, events) == []

    def test_legacy_old_uids_rebuild_zero_alerts(self, settings):
        positions = [_pos(f"cond-{i}") for i in range(8)]
        _legacy_complete(settings, W1, positions=positions, version=None)
        client = _client(positions)
        state = ensure_baseline_current(settings, W1)
        assert state.baseline_status == "MIGRATING"
        assert run_baseline(settings, W1, client=client) == "COMPLETE"
        assert baseline_is_complete(settings, W1)
        st = copy_storage.get_wallet_monitor_state(settings, W1)
        assert st is not None
        assert st.baseline_version == COPY_BASELINE_VERSION
        events = ingest_wallet(settings, W1, client=client)
        assert filter_alertable_events(settings, W1, events) == []

    def test_twenty_existing_positions_after_upgrade(self, settings):
        positions = [_pos(f"cond-{i}") for i in range(20)]
        _legacy_complete(settings, W1, positions=positions)
        client = _client(positions)
        ensure_baseline_current(settings, W1)
        assert run_baseline(settings, W1, client=client) == "COMPLETE"
        assert ingest_wallet(settings, W1, client=client) == []
        assert filter_alertable_events(settings, W1, []) == []
        # Synthetic rediscovery of opens must still be suppressed by seeds.
        opens = [_open_event(W1, f"cond-{i}") for i in range(20)]
        assert filter_alertable_events(settings, W1, opens) == []

    def test_trades_activity_closed_seeded(self, settings):
        now = int(time.time())
        positions = [_pos("cond-1")]
        trades = [_trade(tx="0xold1", condition_id="cond-1", ts=now - 100)]
        closed = [
            {
                "conditionId": "cond-closed",
                "size": 50,
                "avgPrice": 0.3,
                "curPrice": 0.0,
                "outcome": "Yes",
                "title": "Closed",
                "slug": "closed",
            }
        ]
        _legacy_complete(settings, W1, positions=positions)
        client = _client(positions, trades=trades, activity=trades, closed=closed)
        ensure_baseline_current(settings, W1)
        assert run_baseline(settings, W1, client=client) == "COMPLETE"
        # Replay same trade as a live parse — must not alert.
        from app.copy_intel.ingestion import parse_trade_event

        te = parse_trade_event(trades[0], W1)
        assert te is not None
        assert filter_alertable_events(settings, W1, [te], now=now) == []

    def test_first_post_rebuild_unchanged_polls_silent(self, settings):
        positions = [_pos("cond-1"), _pos("cond-2")]
        _legacy_complete(settings, W1, positions=positions)
        client = _client(positions)
        ensure_baseline_current(settings, W1)
        assert run_baseline(settings, W1, client=client) == "COMPLETE"
        for _ in range(3):
            events = ingest_wallet(settings, W1, client=client)
            assert filter_alertable_events(settings, W1, events) == []

    def test_genuine_new_trade_after_complete_once(self, settings):
        positions = [_pos("cond-1", 100)]
        _legacy_complete(settings, W1, positions=positions)
        client = _client(positions)
        ensure_baseline_current(settings, W1)
        assert run_baseline(settings, W1, client=client) == "COMPLETE"
        # New market open via position-diff remains alertable (not size-churn).
        client2 = _client([_pos("cond-1", 100), _pos("cond-new", 40)])
        events = ingest_wallet(settings, W1, client=client2)
        first = filter_alertable_events(settings, W1, events)
        second = filter_alertable_events(settings, W1, events)
        assert len(first) == 1
        assert first[0].condition_id == "cond-new"
        assert first[0].event_type == "open"
        assert second == []

    def test_unsigned_increase_on_baselined_market_silent(self, settings):
        positions = [_pos("cond-1", 100)]
        _legacy_complete(settings, W1, positions=positions)
        client = _client(positions)
        ensure_baseline_current(settings, W1)
        assert run_baseline(settings, W1, client=client) == "COMPLETE"
        client2 = _client([_pos("cond-1", 180)])
        events = ingest_wallet(settings, W1, client=client2)
        assert len(events) == 1
        assert events[0].event_type == "increase"
        assert filter_alertable_events(settings, W1, events) == []

    def test_restart_after_migrated_zero_replay(self, settings):
        positions = [_pos("cond-1")]
        _legacy_complete(settings, W1, positions=positions)
        client = _client(positions)
        ensure_baseline_current(settings, W1)
        assert run_baseline(settings, W1, client=client) == "COMPLETE"
        # Simulate process restart: ensure again, then poll.
        assert baseline_is_complete(settings, W1)
        ensure_baseline_current(settings, W1)
        assert baseline_is_complete(settings, W1)
        events = ingest_wallet(settings, W1, client=client)
        assert filter_alertable_events(settings, W1, events) == []

    def test_partial_failure_failed_zero_alerts(self, settings):
        positions = [_pos("cond-1")]
        _legacy_complete(settings, W1, positions=positions)
        ensure_baseline_current(settings, W1)
        bad = _client(positions, fail="positions")
        assert run_baseline(settings, W1, client=bad) == "FAILED"
        assert not baseline_is_complete(settings, W1)
        assert filter_alertable_events(settings, W1, [_open_event(W1, "cond-1")]) == []

    def test_concurrent_tick_during_migration_zero_alerts(self, settings):
        positions = [_pos(f"cond-{i}") for i in range(6)]
        _legacy_complete(settings, W1, positions=positions)
        client = _client(positions)
        opens = [_open_event(W1, f"cond-{i}") for i in range(6)]

        def _tick(_: int) -> int:
            ensure_baseline_current(settings, W1)
            if not baseline_is_complete(settings, W1):
                run_baseline(settings, W1, client=client)
            return len(filter_alertable_events(settings, W1, opens))

        with ThreadPoolExecutor(max_workers=8) as pool:
            totals = list(pool.map(_tick, range(8)))
        assert sum(totals) == 0
        assert baseline_is_complete(settings, W1)

    def test_pause_resume_rebuilds_current_version(self, settings):
        positions = [_pos("cond-1")]
        client = _client(positions)
        add_wallet(settings, W1)
        assert run_baseline(settings, W1, client=client) == "COMPLETE"
        st = copy_storage.get_wallet_monitor_state(settings, W1)
        assert st is not None and st.baseline_version == COPY_BASELINE_VERSION
        set_monitoring(settings, W1, enabled=False, actor="1")
        set_monitoring(settings, W1, enabled=True, actor="1")
        st2 = copy_storage.get_wallet_monitor_state(settings, W1)
        assert st2 is not None
        assert st2.baseline_status == "PENDING"
        assert st2.baseline_version is None
        assert run_baseline(settings, W1, client=client) == "COMPLETE"
        st3 = copy_storage.get_wallet_monitor_state(settings, W1)
        assert st3 is not None and st3.baseline_version == COPY_BASELINE_VERSION

    def test_multiple_subscriptions_migrate_independently(self, settings):
        p1 = [_pos("cond-a")]
        p2 = [_pos("cond-b"), _pos("cond-c")]
        _legacy_complete(settings, W1, positions=p1)
        _legacy_complete(settings, W2, positions=p2)
        c1 = _client(p1)
        c2 = _client(p2)
        ensure_baseline_current(settings, W1)
        assert run_baseline(settings, W1, client=c1) == "COMPLETE"
        assert not baseline_is_complete(settings, W2)
        assert filter_alertable_events(settings, W2, [_open_event(W2, "cond-b")]) == []
        ensure_baseline_current(settings, W2)
        assert run_baseline(settings, W2, client=c2) == "COMPLETE"
        assert baseline_is_complete(settings, W1)
        assert baseline_is_complete(settings, W2)


class TestAdminRebuild:
    def test_rebuild_callbacks_registered(self):
        assert CB_HEALTH_REBUILD_BASELINES in HEALTH_CALLBACKS
        assert CB_HEALTH_REBUILD_CONFIRM in HEALTH_CALLBACKS
        labels = [b.text for row in copy_health_keyboard().inline_keyboard for b in row]
        assert "🔄 Rebuild Copy Baselines" in labels

    def test_confirm_text_no_wallets(self, settings):
        add_wallet(settings, W1)
        text = format_rebuild_baselines_confirm(settings)
        assert "Active subscriptions: 1" in text
        assert W1 not in text
        assert "0xaaaa" not in text

    def test_rebuild_preserves_watchlist_analytics_paper(self, settings):
        positions = [_pos("cond-1")]
        _legacy_complete(settings, W1, positions=positions)
        copy_storage.save_trader_metrics(
            settings,
            W1,
            window="lifetime",
            metrics={"score": 1.0},
        )
        copy_storage.open_paper_position(
            settings,
            wallet=W1,
            condition_id="cond-1",
            market_slug="slug-cond-1",
            outcome="Yes",
            notional=25.0,
            entry_price=0.4,
        )
        from app.database.storage import get_connection

        with get_connection(settings) as conn:
            processed_before = conn.execute("SELECT COUNT(*) AS c FROM processed_copy_events").fetchone()["c"]
            watch_before = conn.execute("SELECT COUNT(*) AS c FROM copy_watchlist").fetchone()["c"]
            paper_before = conn.execute("SELECT COUNT(*) AS c FROM copy_paper_positions").fetchone()["c"]
            metrics_before = conn.execute("SELECT COUNT(*) AS c FROM copy_trader_metrics").fetchone()["c"]

        client = _client(positions)
        result = rebuild_all_copy_baselines(settings, client=client)
        assert result["succeeded"] == 1
        assert result["failed"] == 0
        out = format_rebuild_baselines_result(result)
        assert "Succeeded: 1" in out
        assert W1 not in out

        with get_connection(settings) as conn:
            watch_after = conn.execute("SELECT COUNT(*) AS c FROM copy_watchlist").fetchone()["c"]
            paper_after = conn.execute("SELECT COUNT(*) AS c FROM copy_paper_positions").fetchone()["c"]
            metrics_after = conn.execute("SELECT COUNT(*) AS c FROM copy_trader_metrics").fetchone()["c"]
            processed_after = conn.execute("SELECT COUNT(*) AS c FROM processed_copy_events").fetchone()["c"]
        assert watch_after == watch_before == 1
        assert paper_after == paper_before == 1
        assert metrics_after == metrics_before == 1
        assert processed_after >= processed_before
    @pytest.mark.asyncio
    async def test_rebuild_requires_admin(self, settings):
        update = MagicMock()
        update.effective_user = MagicMock(id=9999)
        update.effective_message = MagicMock()
        update.effective_message.reply_text = AsyncMock()
        ok = await require_admin(update, settings, action="cb:health:rebuild_confirm")
        assert ok is False

    def test_invalidate_then_filter_silent(self, settings):
        positions = [_pos("cond-1")]
        add_wallet(settings, W1)
        assert run_baseline(settings, W1, client=_client(positions)) == "COMPLETE"
        invalidate_baseline_for_rebuild(settings, W1, reason="test")
        assert not baseline_is_complete(settings, W1)
        assert filter_alertable_events(settings, W1, [_open_event(W1, "cond-1")]) == []
