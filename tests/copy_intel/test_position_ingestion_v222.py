"""V2.2.2 Checkpoint 4 — asset-keyed complete-book position ingestion.

All databases here are temporary pytest paths only.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.copy_intel.api_result import ApiFailure
from app.copy_intel.freshness import (
    COPY_BASELINE_VERSION,
    baseline_is_complete,
    filter_alertable_events,
    run_baseline,
)
from app.copy_intel.ingestion import (
    diff_positions,
    ingest_wallet,
    load_snapshots_as_models,
    load_snapshots_v2_as_models,
)
from app.copy_intel.models import PositionSnapshot
from app.copy_intel.position_book import (
    PositionBookFailure,
    build_canonical_position_book,
    fetch_validated_position_book,
    needs_consistency_second_read,
)
from app.copy_intel.watchlist import add_wallet
from app.database import copy_storage
from app.database.copy_storage import commit_complete_position_book_v2
from app.database.storage import initialize_database
from tests.conftest import make_test_settings

WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CONDITION = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ASSET_YES = "token-yes-111"
ASSET_NO = "token-no-222"


def _assert_temp_db(settings) -> None:
    path = str(settings.database_path)
    assert (
        path == ":memory:"
        or "pytest" in path.lower()
        or "tmp" in path.lower()
        or "test" in path.lower()
    )
    assert not path.endswith("trading_intelligence.db")


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(database_path=str(tmp_path / "cp4_ingest.db"))
    _assert_temp_db(s)
    initialize_database(s)
    return s


def _raw(
    *,
    asset: str = ASSET_YES,
    condition_id: str = CONDITION,
    outcome: str = "Yes",
    size: float = 10.0,
) -> dict:
    return {
        "conditionId": condition_id,
        "asset": asset,
        "outcome": outcome,
        "size": size,
        "avgPrice": 0.4,
        "curPrice": 0.41,
        "title": "Sanitized",
        "slug": "sanitized",
        "eventSlug": "sanitized-event",
    }


def _client(positions, *, pages: list[list[dict]] | None = None, trades=None):
    client = MagicMock()
    if pages is not None:

        def _fetch(wallet, limit=100, offset=0):
            idx = 0 if limit <= 0 else offset // limit
            if idx < len(pages):
                return pages[idx]
            return []

        client.fetch_positions.side_effect = _fetch
    else:
        client.fetch_positions.return_value = positions
    client.fetch_trades.return_value = trades if trades is not None else []
    return client


def _seed_v2(settings, *, asset: str = ASSET_YES, size: float = 10.0) -> None:
    add_wallet(settings, WALLET)
    commit_complete_position_book_v2(
        settings,
        WALLET,
        {
            asset: {
                "condition_id": CONDITION,
                "outcome": "Yes",
                "size": size,
                "avg_price": 0.4,
                "cur_price": 0.41,
                "title": "Sanitized",
                "slug": "sanitized",
                "event_slug": "sanitized-event",
            }
        },
        stamp_complete=True,
    )


class TestNewIncreaseUnchanged:
    def _buy_trade(self, *, size: float, price: float = 0.4, ts: int | None = None, tx: str = "0xfill1"):
        import time as _time

        return {
            "transactionHash": tx,
            "conditionId": CONDITION,
            "asset": ASSET_YES,
            "outcome": "Yes",
            "side": "BUY",
            "size": size,
            "price": price,
            "timestamp": ts if ts is not None else int(_time.time()) - 5,
            "title": "Sanitized",
            "slug": "sanitized",
            "type": "TRADE",
        }

    def test_one_asset_new_position(self, settings):
        add_wallet(settings, WALLET)
        trade = self._buy_trade(size=12.0)
        events = ingest_wallet(
            settings,
            WALLET,
            client=_client([_raw(size=12.0)], trades=[trade]),
        )
        assert len(events) == 1
        assert events[0].event_type == "open"
        assert events[0].asset == ASSET_YES
        assert events[0].trader_vwap == pytest.approx(0.4)
        assert events[0].ts == trade["timestamp"]
        loaded = load_snapshots_v2_as_models(settings, WALLET)
        assert set(loaded) == {ASSET_YES}
        assert float(loaded[ASSET_YES].size) == 12.0

    def test_one_asset_increase(self, settings):
        _seed_v2(settings, size=10.0)
        trade = self._buy_trade(size=15.0, price=0.5, tx="0xinc")
        events = ingest_wallet(
            settings,
            WALLET,
            client=_client([_raw(size=25.0)], trades=[trade]),
        )
        assert len(events) == 1
        assert events[0].event_type == "increase"
        assert events[0].prev_size == 10.0
        assert events[0].new_size == 25.0
        assert events[0].trader_vwap == pytest.approx(0.5)

    def test_unchanged_position(self, settings):
        _seed_v2(settings, size=10.0)
        events = ingest_wallet(settings, WALLET, client=_client([_raw(size=10.0)]))
        assert events == []
        loaded = load_snapshots_v2_as_models(settings, WALLET)
        assert float(loaded[ASSET_YES].size) == 10.0


class TestTwoOutcomesAndFailClosed:
    def test_two_outcomes_under_one_condition_remain_separate(self, settings):
        add_wallet(settings, WALLET)
        import time as _time

        now = int(_time.time()) - 5
        rows = [
            _raw(asset=ASSET_YES, outcome="Yes", size=100.0),
            _raw(asset=ASSET_NO, outcome="No", size=55.0),
        ]
        trades = [
            {
                "transactionHash": "0xyes",
                "conditionId": CONDITION,
                "asset": ASSET_YES,
                "outcome": "Yes",
                "side": "BUY",
                "size": 100.0,
                "price": 0.4,
                "timestamp": now,
            },
            {
                "transactionHash": "0xno",
                "conditionId": CONDITION,
                "asset": ASSET_NO,
                "outcome": "No",
                "side": "BUY",
                "size": 55.0,
                "price": 0.6,
                "timestamp": now,
            },
        ]
        events = ingest_wallet(settings, WALLET, client=_client(rows, trades=trades))
        assert {e.asset for e in events} == {ASSET_YES, ASSET_NO}
        assert all(e.event_type == "open" for e in events)
        loaded = load_snapshots_v2_as_models(settings, WALLET)
        assert set(loaded) == {ASSET_YES, ASSET_NO}

    def test_duplicate_asset_fails_closed(self, settings):
        _seed_v2(settings, size=10.0)
        before = load_snapshots_v2_as_models(settings, WALLET)
        rows = [_raw(size=10.0), _raw(size=20.0)]
        book = build_canonical_position_book(rows, WALLET)
        assert isinstance(book, PositionBookFailure)
        assert book.code == "duplicate_asset"
        events = ingest_wallet(settings, WALLET, client=_client(rows))
        assert events == []
        assert load_snapshots_v2_as_models(settings, WALLET).keys() == before.keys()
        assert float(load_snapshots_v2_as_models(settings, WALLET)[ASSET_YES].size) == 10.0

    def test_missing_asset_fails_closed(self, settings):
        _seed_v2(settings, size=10.0)
        bad = _raw(size=11.0)
        bad["asset"] = ""
        events = ingest_wallet(settings, WALLET, client=_client([bad]))
        assert events == []
        assert float(load_snapshots_v2_as_models(settings, WALLET)[ASSET_YES].size) == 10.0

    def test_non_finite_or_negative_size_fails_closed(self, settings):
        _seed_v2(settings, size=10.0)
        for bad_size in (float("nan"), float("inf"), -1.0):
            bad = _raw(size=1.0)
            bad["size"] = bad_size
            events = ingest_wallet(settings, WALLET, client=_client([bad]))
            assert events == []
            assert float(load_snapshots_v2_as_models(settings, WALLET)[ASSET_YES].size) == 10.0


class TestApiFailureVsEmpty:
    def test_api_failure_retains_previous_snapshots(self, settings):
        _seed_v2(settings, size=10.0)
        client = MagicMock()
        client.fetch_positions.return_value = ApiFailure(
            code="timeout",
            status_code=None,
            retryable=True,
            request_count=1,
            message="request timed out",
        )
        assert ingest_wallet(settings, WALLET, client=client) == []
        assert float(load_snapshots_v2_as_models(settings, WALLET)[ASSET_YES].size) == 10.0
        state = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state is not None
        assert state.last_complete_positions_at is not None
        stamped = state.last_complete_positions_at
        assert ingest_wallet(settings, WALLET, client=client) == []
        state2 = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state2 is not None
        assert state2.last_complete_positions_at == stamped

    def test_successful_empty_distinct_from_failure(self, settings):
        add_wallet(settings, WALLET)
        events = ingest_wallet(settings, WALLET, client=_client([]))
        assert events == []
        assert load_snapshots_v2_as_models(settings, WALLET) == {}
        state = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state is not None
        assert state.last_complete_positions_at is not None

        _seed_v2(settings, size=7.0)
        fail_client = MagicMock()
        fail_client.fetch_positions.return_value = ApiFailure(
            code="http_error",
            status_code=503,
            retryable=True,
            request_count=1,
            message="http status 503",
        )
        assert ingest_wallet(settings, WALLET, client=fail_client) == []
        assert float(load_snapshots_v2_as_models(settings, WALLET)[ASSET_YES].size) == 7.0


class TestSecondReadConsistency:
    def test_empty_after_nonempty_triggers_second_read(self, settings):
        _seed_v2(settings, size=10.0)
        previous = load_snapshots_v2_as_models(settings, WALLET)
        assert needs_consistency_second_read(previous, {}, was_multipage=False) is True
        calls = {"n": 0}

        def _fetch(wallet, limit=100, offset=0):
            calls["n"] += 1
            return []

        client = MagicMock()
        client.fetch_positions.side_effect = _fetch
        assert ingest_wallet(settings, WALLET, client=client) == []
        assert calls["n"] >= 2
        assert load_snapshots_v2_as_models(settings, WALLET) == {}

    def test_asset_removal_triggers_second_read(self, settings):
        add_wallet(settings, WALLET)
        commit_complete_position_book_v2(
            settings,
            WALLET,
            {
                ASSET_YES: {
                    "condition_id": CONDITION,
                    "outcome": "Yes",
                    "size": 10.0,
                    "avg_price": 0.4,
                    "cur_price": 0.41,
                    "title": "t",
                    "slug": "s",
                    "event_slug": "e",
                },
                ASSET_NO: {
                    "condition_id": CONDITION,
                    "outcome": "No",
                    "size": 5.0,
                    "avg_price": 0.6,
                    "cur_price": 0.59,
                    "title": "t",
                    "slug": "s",
                    "event_slug": "e",
                },
            },
            stamp_complete=True,
        )
        previous = load_snapshots_v2_as_models(settings, WALLET)
        current = {ASSET_YES: previous[ASSET_YES]}
        assert needs_consistency_second_read(previous, current, was_multipage=False) is True

        calls = {"n": 0}

        def _fetch(wallet, limit=100, offset=0):
            calls["n"] += 1
            return [_raw(asset=ASSET_YES, size=10.0)]

        client = MagicMock()
        client.fetch_positions.side_effect = _fetch
        assert ingest_wallet(settings, WALLET, client=client) == []
        assert calls["n"] >= 2
        loaded = load_snapshots_v2_as_models(settings, WALLET)
        assert set(loaded) == {ASSET_YES}

    def test_multipage_book_triggers_second_read(self, settings):
        add_wallet(settings, WALLET)
        page1 = [_raw(asset=f"a{i}", size=1.0 + i) for i in range(3)]
        page2 = [_raw(asset=f"b{i}", condition_id=f"c{i}", size=2.0) for i in range(2)]
        client = _client([], pages=[page1, page2, []])
        book = fetch_validated_position_book(client, WALLET, page_size=3)
        assert book.ok
        assert book.was_multipage is True
        assert needs_consistency_second_read({}, book.positions, was_multipage=True) is True

    def test_differing_second_read_retains_old_snapshot(self, settings, caplog):
        _seed_v2(settings, size=10.0)
        responses = [
            [],
            [_raw(size=99.0)],
        ]
        idx = {"i": 0}

        def _fetch(wallet, limit=100, offset=0):
            i = idx["i"]
            idx["i"] += 1
            return responses[min(i, len(responses) - 1)]

        client = MagicMock()
        client.fetch_positions.side_effect = _fetch
        with caplog.at_level("WARNING"):
            events = ingest_wallet(settings, WALLET, client=client)
        assert events == []
        assert float(load_snapshots_v2_as_models(settings, WALLET)[ASSET_YES].size) == 10.0
        assert any("inconsistent_second_read" in r.message for r in caplog.records)

    def test_matching_second_read_commits_snapshots(self, settings):
        _seed_v2(settings, size=10.0)
        client = MagicMock()
        client.fetch_positions.return_value = []
        assert ingest_wallet(settings, WALLET, client=client) == []
        assert load_snapshots_v2_as_models(settings, WALLET) == {}


class TestAtomicPersistAndStamp:
    def test_atomic_snapshot_replacement_rollback_on_persistence_failure(
        self, settings, monkeypatch
    ):
        _seed_v2(settings, size=10.0)
        before = load_snapshots_v2_as_models(settings, WALLET)

        def boom(*args, **kwargs):
            raise RuntimeError("injected persistence failure")

        monkeypatch.setattr(
            "app.copy_intel.ingestion.copy_storage.commit_complete_position_book_v2",
            boom,
        )
        with pytest.raises(RuntimeError, match="injected persistence failure"):
            ingest_wallet(settings, WALLET, client=_client([_raw(size=50.0)]))
        loaded = load_snapshots_v2_as_models(settings, WALLET)
        assert float(loaded[ASSET_YES].size) == float(before[ASSET_YES].size)

    def test_last_complete_positions_at_updates_only_after_success(self, settings):
        add_wallet(settings, WALLET)
        state0 = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state0 is not None
        assert state0.last_complete_positions_at is None
        ingest_wallet(settings, WALLET, client=_client([_raw(size=3.0)]))
        state1 = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state1 is not None
        assert state1.last_complete_positions_at is not None
        stamped = state1.last_complete_positions_at

        fail = MagicMock()
        fail.fetch_positions.return_value = ApiFailure(
            code="http_error",
            status_code=500,
            retryable=True,
            request_count=1,
            message="http status 500",
        )
        ingest_wallet(settings, WALLET, client=fail)
        state2 = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state2 is not None
        assert state2.last_complete_positions_at == stamped


class TestBaselineV3AndLegacy:
    def test_v3_baseline_produces_zero_alerts(self, settings):
        add_wallet(settings, WALLET)
        rows = [
            _raw(asset=ASSET_YES, outcome="Yes", size=100.0),
            _raw(asset=ASSET_NO, outcome="No", size=55.0),
        ]
        assert run_baseline(settings, WALLET, client=_client(rows)) == "COMPLETE"
        assert baseline_is_complete(settings, WALLET)
        state = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state is not None
        assert state.baseline_version == COPY_BASELINE_VERSION
        events = ingest_wallet(settings, WALLET, client=_client(rows))
        assert events == []
        assert filter_alertable_events(settings, WALLET, events) == []
        assert set(load_snapshots_v2_as_models(settings, WALLET)) == {ASSET_YES, ASSET_NO}

    def test_failed_v3_baseline_remains_non_complete(self, settings):
        add_wallet(settings, WALLET)
        client = MagicMock()
        client.fetch_positions.return_value = ApiFailure(
            code="timeout",
            status_code=None,
            retryable=True,
            request_count=1,
            message="request timed out",
        )
        assert run_baseline(settings, WALLET, client=client) == "FAILED"
        assert baseline_is_complete(settings, WALLET) is False
        state = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state is not None
        assert state.baseline_status == "FAILED"

    def test_legacy_snapshot_table_remains_unchanged(self, settings):
        add_wallet(settings, WALLET)
        copy_storage.upsert_position_snapshot(
            settings,
            WALLET,
            CONDITION,
            asset=ASSET_YES,
            outcome="Yes",
            size=42.0,
            avg_price=0.4,
            cur_price=0.41,
            title="legacy",
            slug="legacy",
            event_slug="legacy",
        )
        before = load_snapshots_as_models(settings, WALLET)
        ingest_wallet(settings, WALLET, client=_client([_raw(size=9.0)]))
        after = load_snapshots_as_models(settings, WALLET)
        assert {k: float(v.size) for k, v in before.items()} == {
            k: float(v.size) for k, v in after.items()
        }
        assert float(load_snapshots_v2_as_models(settings, WALLET)[ASSET_YES].size) == 9.0


class TestReduceCloseSuppressed:
    def test_reduce_and_close_do_not_enter_alert_or_recommendation_paths(self, settings):
        add_wallet(settings, WALLET)
        prev = {
            ASSET_YES: PositionSnapshot(
                wallet=WALLET,
                condition_id=CONDITION,
                asset=ASSET_YES,
                outcome="Yes",
                size=100.0,
                avg_price=0.4,
                cur_price=0.41,
                title="t",
                slug="s",
                event_slug="e",
            )
        }
        reduce_events = diff_positions(
            WALLET,
            prev,
            {
                ASSET_YES: PositionSnapshot(
                    wallet=WALLET,
                    condition_id=CONDITION,
                    asset=ASSET_YES,
                    outcome="Yes",
                    size=40.0,
                    avg_price=0.4,
                    cur_price=0.41,
                    title="t",
                    slug="s",
                    event_slug="e",
                )
            },
        )
        close_events = diff_positions(WALLET, prev, {})
        assert reduce_events[0].event_type == "reduce"
        assert close_events[0].event_type == "close"

        _seed_v2(settings, size=100.0)
        returned = ingest_wallet(settings, WALLET, client=_client([_raw(size=40.0)]))
        assert all(e.event_type in {"open", "increase"} for e in returned)
        assert returned == []

        run_baseline(settings, WALLET, client=_client([_raw(size=100.0)]))
        assert filter_alertable_events(settings, WALLET, reduce_events) == []
        assert filter_alertable_events(settings, WALLET, close_events) == []
        # Recommendation engine maps reduce/close → SKIP (unsupported event type).
        from app.copy_intel.recommendation import build_recommendation

        for event in reduce_events + close_events:
            rec = build_recommendation(settings, event, client=object())
            assert rec.action == "SKIP"
            assert "unsupported_event_type" in rec.reason_codes
