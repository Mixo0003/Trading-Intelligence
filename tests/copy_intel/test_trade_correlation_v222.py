"""V2.2.2 Checkpoint 5 — authoritative trade correlation and Trader VWAP.

Temporary pytest DBs and sanitized fixtures only.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from app.copy_intel.api_result import ApiFailure, ApiSuccess
from app.copy_intel.data_client import CopyDataClient
from app.copy_intel.ingestion import ingest_wallet, load_snapshots_v2_as_models
from app.copy_intel.models import TradeEvent
from app.copy_intel.trade_correlation import (
    MAX_CORRELATION_CANDIDATE_FILLS,
    CorrelationQuarantine,
    TradeFill,
    build_correlated_canonical_payload,
    build_correlated_event_uid,
    compute_trader_vwap,
    correlate_transition,
    fill_fingerprint,
    parse_candidate_fill,
)
from app.copy_intel.watchlist import add_wallet
from app.database import copy_storage
from app.database.copy_storage import (
    commit_complete_position_book_v2,
    list_trade_correlation_quarantine,
)
from app.database.storage import initialize_database
from tests.conftest import make_test_settings

WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CONDITION = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ASSET_YES = "token-yes-111"
ASSET_NO = "token-no-222"


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(database_path=str(tmp_path / "cp5_corr.db"))
    initialize_database(s)
    return s


def _now() -> int:
    return int(time.time())


def _raw(*, asset=ASSET_YES, size=10.0, outcome="Yes", condition_id=CONDITION):
    return {
        "conditionId": condition_id,
        "asset": asset,
        "outcome": outcome,
        "size": size,
        "avgPrice": 0.4,
        "curPrice": 0.41,
        "title": "Sanitized",
        "slug": "sanitized",
        "eventSlug": "e",
    }


def _trade(
    *,
    tx: str,
    size: float,
    price: float = 0.4,
    ts: int | None = None,
    asset: str = ASSET_YES,
    condition_id: str = CONDITION,
    side: str = "BUY",
    **extra,
):
    row = {
        "transactionHash": tx,
        "conditionId": condition_id,
        "asset": asset,
        "outcome": "Yes",
        "side": side,
        "size": size,
        "price": price,
        "timestamp": ts if ts is not None else _now() - 10,
        "title": "Sanitized",
        "slug": "sanitized",
        "type": "TRADE",
    }
    row.update(extra)
    return row


def _client(positions, trades=None):
    client = MagicMock()
    client.fetch_positions.return_value = positions
    client.fetch_trades.return_value = trades if trades is not None else []
    return client


def _seed(settings, *, size=10.0, asset=ASSET_YES):
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
                "title": "t",
                "slug": "s",
                "event_slug": "e",
            }
        },
        stamp_complete=True,
    )


def _transition(*, event_type="open", prev=0.0, new=10.0, asset=ASSET_YES):
    return TradeEvent(
        wallet=WALLET,
        event_type=event_type,
        condition_id=CONDITION,
        asset=asset,
        outcome="Yes",
        side="BUY",
        price=None,
        size=round(new - prev, 6),
        tx_hash=None,
        ts=None,
        title="t",
        slug="s",
        dedup_key="dk",
        prev_size=prev,
        new_size=new,
    )


class TestAcceptPaths:
    def test_single_buy_new_position(self, settings):
        add_wallet(settings, WALLET)
        ts = _now() - 5
        events = ingest_wallet(
            settings,
            WALLET,
            client=_client([_raw(size=10.0)], trades=[_trade(tx="0xa", size=10.0, ts=ts)]),
        )
        assert len(events) == 1
        assert events[0].event_type == "open"
        assert events[0].trader_vwap == pytest.approx(0.4)
        assert events[0].first_fill_ts == ts
        assert events[0].last_fill_ts == ts
        assert events[0].matched_fill_count == 1

    def test_single_buy_increase(self, settings):
        _seed(settings, size=10.0)
        ts = _now() - 5
        events = ingest_wallet(
            settings,
            WALLET,
            client=_client(
                [_raw(size=25.0)],
                trades=[_trade(tx="0xb", size=15.0, price=0.55, ts=ts)],
            ),
        )
        assert len(events) == 1
        assert events[0].event_type == "increase"
        assert events[0].trader_vwap == pytest.approx(0.55)

    def test_taker_only_false_on_fetch_trades(self, settings, monkeypatch):
        calls: list[dict] = []

        def fake_data_get(self, path, wallet, *, limit, extra_params=None, **kwargs):
            calls.append(dict(extra_params or {}))
            return ApiSuccess(data=[], request_count=1)

        monkeypatch.setattr(CopyDataClient, "_data_get", fake_data_get)
        CopyDataClient(settings).fetch_trades(WALLET, limit=50, start_ts=1, end_ts=2)
        assert any(c.get("takerOnly") == "false" for c in calls)

    def test_multiple_buys_one_transaction(self, settings):
        add_wallet(settings, WALLET)
        ts = _now() - 5
        trades = [
            _trade(tx="0xsame", size=4.0, price=0.3, ts=ts),
            _trade(tx="0xsame", size=6.0, price=0.5, ts=ts),
        ]
        events = ingest_wallet(
            settings, WALLET, client=_client([_raw(size=10.0)], trades=trades)
        )
        assert len(events) == 1
        assert events[0].matched_fill_count == 2
        assert events[0].trader_vwap == pytest.approx((0.3 * 4 + 0.5 * 6) / 10)

    def test_multiple_buys_across_transactions(self, settings):
        add_wallet(settings, WALLET)
        t1, t2 = _now() - 20, _now() - 5
        trades = [
            _trade(tx="0x1", size=3.0, price=0.2, ts=t1),
            _trade(tx="0x2", size=7.0, price=0.8, ts=t2),
        ]
        events = ingest_wallet(
            settings, WALLET, client=_client([_raw(size=10.0)], trades=trades)
        )
        assert len(events) == 1
        assert events[0].first_fill_ts == t1
        assert events[0].last_fill_ts == t2
        assert events[0].trader_vwap == pytest.approx((0.2 * 3 + 0.8 * 7) / 10)

    def test_weighted_trader_vwap(self):
        fills = (
            TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 2.0, 0.25, 1),
            TradeFill("0xb", ASSET_YES, CONDITION, "BUY", 6.0, 0.75, 2),
        )
        assert compute_trader_vwap(fills) == pytest.approx((0.25 * 2 + 0.75 * 6) / 8)

    def test_first_and_last_fill_ts(self):
        fills = (
            TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 1.0, 0.4, 100),
            TradeFill("0xb", ASSET_YES, CONDITION, "BUY", 1.0, 0.4, 50),
            TradeFill("0xc", ASSET_YES, CONDITION, "BUY", 1.0, 0.4, 75),
        )
        outcome = correlate_transition(
            _transition(new=3.0),
            fills,
            lower_ts=0,
            upper_ts=1000,
            trade_watermark_ts=None,
            detected_at=2000,
        )
        assert not isinstance(outcome, CorrelationQuarantine)
        assert outcome.event.first_fill_ts == 50
        assert outcome.event.last_fill_ts == 100
        assert outcome.event.ts == 100
        assert outcome.event.detected_at == 2000
        assert outcome.event.detected_at != outcome.event.ts


class TestFilteringQuarantine:
    def test_same_tx_other_asset_excluded(self):
        fills = [
            TradeFill("0xsame", ASSET_YES, CONDITION, "BUY", 10.0, 0.4, 10),
            TradeFill("0xsame", ASSET_NO, CONDITION, "BUY", 10.0, 0.6, 10),
        ]
        outcome = correlate_transition(
            _transition(new=10.0, asset=ASSET_YES),
            fills,
            lower_ts=0,
            upper_ts=100,
            trade_watermark_ts=None,
            detected_at=1,
        )
        assert not isinstance(outcome, CorrelationQuarantine)
        assert outcome.event.matched_fill_count == 1
        assert outcome.fills[0].asset == ASSET_YES

    def test_same_condition_two_assets(self, settings):
        add_wallet(settings, WALLET)
        ts = _now() - 5
        events = ingest_wallet(
            settings,
            WALLET,
            client=_client(
                [
                    _raw(asset=ASSET_YES, size=5.0, outcome="Yes"),
                    _raw(asset=ASSET_NO, size=7.0, outcome="No"),
                ],
                trades=[
                    _trade(tx="0xy", size=5.0, asset=ASSET_YES, ts=ts),
                    _trade(tx="0xn", size=7.0, asset=ASSET_NO, price=0.6, ts=ts),
                ],
            ),
        )
        assert {e.asset for e in events} == {ASSET_YES, ASSET_NO}

    def test_wrong_asset_rejected(self):
        fills = [TradeFill("0xa", ASSET_NO, CONDITION, "BUY", 10.0, 0.4, 10)]
        outcome = correlate_transition(
            _transition(new=10.0, asset=ASSET_YES),
            fills,
            lower_ts=0,
            upper_ts=100,
            trade_watermark_ts=None,
            detected_at=1,
        )
        assert isinstance(outcome, CorrelationQuarantine)
        assert outcome.code == "trade_size_mismatch"

    def test_wrong_condition_rejected(self):
        fills = [TradeFill("0xa", ASSET_YES, "other-cond", "BUY", 10.0, 0.4, 10)]
        outcome = correlate_transition(
            _transition(new=10.0),
            fills,
            lower_ts=0,
            upper_ts=100,
            trade_watermark_ts=None,
            detected_at=1,
        )
        assert isinstance(outcome, CorrelationQuarantine)

    def test_sell_only_and_interleaved_sell(self):
        sell_only = [TradeFill("0xa", ASSET_YES, CONDITION, "SELL", 10.0, 0.4, 10)]
        out1 = correlate_transition(
            _transition(new=10.0),
            sell_only,
            lower_ts=0,
            upper_ts=100,
            trade_watermark_ts=None,
            detected_at=1,
        )
        assert isinstance(out1, CorrelationQuarantine)
        assert out1.code == "interleaved_sell"

        mixed = [
            TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 10.0, 0.4, 10),
            TradeFill("0xb", ASSET_YES, CONDITION, "SELL", 1.0, 0.4, 11),
        ]
        out2 = correlate_transition(
            _transition(new=10.0),
            mixed,
            lower_ts=0,
            upper_ts=100,
            trade_watermark_ts=None,
            detected_at=1,
        )
        assert isinstance(out2, CorrelationQuarantine)
        assert out2.code == "interleaved_sell"

    def test_missing_fields_rejected(self):
        assert (
            parse_candidate_fill(
                {
                    "asset": ASSET_YES,
                    "conditionId": CONDITION,
                    "side": "BUY",
                    "size": 1,
                    "price": 0.4,
                    "timestamp": 1,
                },
                wallet=WALLET,
            )
            is None
        )
        bad_ts = _trade(tx="0xa", size=1.0)
        bad_ts["timestamp"] = None
        assert parse_candidate_fill(bad_ts, wallet=WALLET) is None
        assert parse_candidate_fill(_trade(tx="0xa", size=1.0, price=1.5), wallet=WALLET) is None
        assert parse_candidate_fill(_trade(tx="0xa", size=0.0), wallet=WALLET) is None

    def test_exact_six_decimal_and_mismatch(self):
        fills = [TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 1.2345674, 0.4, 10)]
        ok = correlate_transition(
            _transition(new=1.234567),
            fills,
            lower_ts=0,
            upper_ts=100,
            trade_watermark_ts=None,
            detected_at=1,
        )
        assert not isinstance(ok, CorrelationQuarantine)

        mismatch = correlate_transition(
            _transition(new=10.0),
            [TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 9.0, 0.4, 10)],
            lower_ts=0,
            upper_ts=100,
            trade_watermark_ts=None,
            detected_at=1,
        )
        assert isinstance(mismatch, CorrelationQuarantine)
        assert mismatch.code in {"trade_size_mismatch", "partial_coverage"}

    def test_partial_and_ambiguous(self):
        partial = correlate_transition(
            _transition(new=10.0),
            [TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 4.0, 0.4, 10)],
            lower_ts=0,
            upper_ts=100,
            trade_watermark_ts=None,
            detected_at=1,
        )
        assert isinstance(partial, CorrelationQuarantine)
        assert partial.code == "partial_coverage"

        ambiguous = correlate_transition(
            _transition(new=10.0),
            [
                TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 10.0, 0.4, 10),
                TradeFill("0xb", ASSET_YES, CONDITION, "BUY", 6.0, 0.4, 11),
                TradeFill("0xc", ASSET_YES, CONDITION, "BUY", 4.0, 0.4, 12),
            ],
            lower_ts=0,
            upper_ts=100,
            trade_watermark_ts=None,
            detected_at=1,
        )
        assert isinstance(ambiguous, CorrelationQuarantine)
        assert ambiguous.code == "ambiguous_trade_match"


class TestWatermarkUidSafety:
    def test_watermark_prevents_replay(self, settings):
        _seed(settings, size=10.0)
        ts = _now() - 5
        trade = _trade(tx="0xwm", size=5.0, ts=ts)
        first = ingest_wallet(
            settings, WALLET, client=_client([_raw(size=15.0)], trades=[trade])
        )
        assert len(first) == 1
        state = copy_storage.get_wallet_monitor_state(settings, WALLET)
        assert state is not None
        assert state.last_correlated_trade_ts == ts
        second = ingest_wallet(
            settings, WALLET, client=_client([_raw(size=20.0)], trades=[trade])
        )
        assert second == []
        assert list_trade_correlation_quarantine(settings, wallet=WALLET)

    def test_detected_at_not_trade_time(self):
        fills = [TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 10.0, 0.4, 12345)]
        outcome = correlate_transition(
            _transition(new=10.0),
            fills,
            lower_ts=0,
            upper_ts=99999,
            trade_watermark_ts=None,
            detected_at=99999,
        )
        assert not isinstance(outcome, CorrelationQuarantine)
        assert outcome.event.ts == 12345
        assert outcome.event.detected_at == 99999
        assert outcome.event.ts != outcome.event.detected_at

    def test_id_trade_id_ignored(self):
        raw = _trade(tx="0xa", size=10.0)
        raw["id"] = "undocumented-1"
        raw["tradeId"] = "undocumented-2"
        fill = parse_candidate_fill(raw, wallet=WALLET)
        assert fill is not None
        assert "undocumented" not in fill_fingerprint(fill)
        assert (
            parse_candidate_fill(
                {
                    "id": "only-id",
                    "tradeId": "only-trade-id",
                    "asset": ASSET_YES,
                    "conditionId": CONDITION,
                    "side": "BUY",
                    "size": 10,
                    "price": 0.4,
                    "timestamp": 1,
                },
                wallet=WALLET,
            )
            is None
        )

    def test_api_failure_retains_snapshots(self, settings):
        _seed(settings, size=10.0)
        client = MagicMock()
        client.fetch_positions.return_value = [_raw(size=20.0)]
        client.fetch_trades.return_value = ApiFailure(
            code="timeout",
            status_code=None,
            retryable=True,
            request_count=1,
            message="timed out",
        )
        events = ingest_wallet(settings, WALLET, client=client)
        assert events == []
        assert float(load_snapshots_v2_as_models(settings, WALLET)[ASSET_YES].size) == 10.0

    def test_reduce_suppressed_and_quarantine_no_delivery(self, settings):
        _seed(settings, size=100.0)
        events = ingest_wallet(
            settings, WALLET, client=_client([_raw(size=40.0)], trades=[])
        )
        assert events == []
        events2 = ingest_wallet(
            settings, WALLET, client=_client([_raw(size=20.0)], trades=[])
        )
        assert events2 == []
        # Quarantine rows exist for the increase attempt after reduce committed.
        # After reduce to 40, book is 40; increase to 20 is actually another reduce.
        # Seed again and attempt unsigned increase to force quarantine.
        _seed(settings, size=10.0)
        assert (
            ingest_wallet(
                settings, WALLET, client=_client([_raw(size=20.0)], trades=[])
            )
            == []
        )
        assert list_trade_correlation_quarantine(settings, wallet=WALLET)


def _uid_kwargs(fills, **overrides):
    base = {
        "wallet": WALLET,
        "asset": ASSET_YES,
        "event_type": "open",
        "prev_size": 0.0,
        "new_size": 10.0,
        "fills": fills,
    }
    base.update(overrides)
    return base


def _assert_ff1(uid: str) -> None:
    assert uid.startswith("ff1:")
    digest = uid[4:]
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


class TestSha256CorrelatedUid:
    def test_uid_format_ff1_64_hex(self):
        fill = TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 10.0, 0.4, 10)
        uid = build_correlated_event_uid(**_uid_kwargs([fill]))
        _assert_ff1(uid)
        payload = build_correlated_canonical_payload(**_uid_kwargs([fill]))
        assert uid != payload
        assert not uid.startswith("ff:")

    def test_same_canonical_event_same_uid(self):
        fill = TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 10.0, 0.4, 10)
        a = build_correlated_event_uid(**_uid_kwargs([fill]))
        b = build_correlated_event_uid(**_uid_kwargs([fill]))
        assert a == b

    def test_reordered_fills_same_uid(self):
        fills = [
            TradeFill("0xb", ASSET_YES, CONDITION, "BUY", 3.0, 0.5, 20),
            TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 7.0, 0.3, 10),
        ]
        uid_a = build_correlated_event_uid(**_uid_kwargs(fills, new_size=10.0))
        uid_b = build_correlated_event_uid(
            **_uid_kwargs(list(reversed(fills)), new_size=10.0)
        )
        assert uid_a == uid_b

    def test_duplicate_fill_multiplicity_changes_uid(self):
        a = TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 5.0, 0.4, 10)
        b = TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 5.0, 0.4, 10)
        uid1 = build_correlated_event_uid(**_uid_kwargs([a], new_size=5.0))
        uid2 = build_correlated_event_uid(**_uid_kwargs([a, b], new_size=10.0))
        assert uid1 != uid2

    def test_field_changes_alter_uid(self):
        base_fill = TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 10.0, 0.4, 10)
        base = build_correlated_event_uid(**_uid_kwargs([base_fill]))
        assert base != build_correlated_event_uid(
            **_uid_kwargs(
                [base_fill], wallet="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            )
        )
        assert base != build_correlated_event_uid(
            **_uid_kwargs([base_fill], asset=ASSET_NO)
        )
        assert base != build_correlated_event_uid(
            **_uid_kwargs(
                [base_fill], event_type="increase", prev_size=0.0, new_size=10.0
            )
        )
        assert base != build_correlated_event_uid(
            **_uid_kwargs([base_fill], prev_size=1.0, new_size=11.0)
        )
        assert base != build_correlated_event_uid(
            **_uid_kwargs([base_fill], new_size=11.0)
        )
        assert base != build_correlated_event_uid(
            **_uid_kwargs(
                [TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 9.0, 0.4, 10)],
                new_size=9.0,
            )
        )
        assert base != build_correlated_event_uid(
            **_uid_kwargs(
                [TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 10.0, 0.5, 10)]
            )
        )
        assert base != build_correlated_event_uid(
            **_uid_kwargs(
                [TradeFill("0xa", ASSET_YES, CONDITION, "BUY", 10.0, 0.4, 11)]
            )
        )
        assert base != build_correlated_event_uid(
            **_uid_kwargs(
                [TradeFill("0xb", ASSET_YES, CONDITION, "BUY", 10.0, 0.4, 10)]
            )
        )

    def test_id_and_trade_id_do_not_change_uid(self):
        raw = _trade(tx="0xa", size=10.0, price=0.4, ts=10)
        fill_plain = parse_candidate_fill(raw, wallet=WALLET)
        raw_extra = dict(raw)
        raw_extra["id"] = "id-should-not-matter"
        raw_extra["tradeId"] = "trade-id-should-not-matter"
        fill_extra = parse_candidate_fill(raw_extra, wallet=WALLET)
        assert fill_plain is not None and fill_extra is not None
        assert build_correlated_event_uid(
            **_uid_kwargs([fill_plain])
        ) == build_correlated_event_uid(**_uid_kwargs([fill_extra]))

    def test_delimiter_like_characters_no_payload_collision(self):
        uid_a = build_correlated_event_uid(
            wallet="ab",
            asset="c",
            event_type="open",
            prev_size=0.0,
            new_size=1.0,
            fills=[TradeFill("0xa", "c", CONDITION, "BUY", 1.0, 0.4, 1)],
        )
        uid_b = build_correlated_event_uid(
            wallet="a",
            asset="bc",
            event_type="open",
            prev_size=0.0,
            new_size=1.0,
            fills=[TradeFill("0xa", "bc", CONDITION, "BUY", 1.0, 0.4, 1)],
        )
        assert uid_a != uid_b
        payload_a = build_correlated_canonical_payload(
            wallet="ab",
            asset="c",
            event_type="open",
            prev_size=0.0,
            new_size=1.0,
            fills=[TradeFill("0xa", "c", CONDITION, "BUY", 1.0, 0.4, 1)],
        )
        payload_b = build_correlated_canonical_payload(
            wallet="a",
            asset="bc",
            event_type="open",
            prev_size=0.0,
            new_size=1.0,
            fills=[TradeFill("0xa", "bc", CONDITION, "BUY", 1.0, 0.4, 1)],
        )
        assert payload_a != payload_b


class TestCorrelationComplexityBound:
    def test_candidate_count_at_bound_still_works(self):
        n = MAX_CORRELATION_CANDIDATE_FILLS
        fills = [
            TradeFill(f"0x{i:02x}", ASSET_YES, CONDITION, "BUY", 1.0, 0.4, 100 + i)
            for i in range(n)
        ]
        outcome = correlate_transition(
            _transition(new=float(n)),
            fills,
            lower_ts=0,
            upper_ts=99999,
            trade_watermark_ts=None,
            detected_at=99999,
        )
        assert not isinstance(outcome, CorrelationQuarantine)
        assert outcome.event.matched_fill_count == n
        _assert_ff1(outcome.event.event_uid or "")

    def test_candidate_count_above_bound_quarantined(self):
        n = MAX_CORRELATION_CANDIDATE_FILLS + 1
        fills = [
            TradeFill(f"0x{i:02x}", ASSET_YES, CONDITION, "BUY", 1.0, 0.4, 100 + i)
            for i in range(n)
        ]
        outcome = correlate_transition(
            _transition(new=float(n)),
            fills,
            lower_ts=0,
            upper_ts=99999,
            trade_watermark_ts=None,
            detected_at=99999,
        )
        assert isinstance(outcome, CorrelationQuarantine)
        assert outcome.code == "correlation_complexity_exceeded"
        assert outcome.diagnostic["candidate_count"] == n
        assert outcome.diagnostic["limit"] == MAX_CORRELATION_CANDIDATE_FILLS

    def test_complexity_rejection_no_snapshot_commit(self, settings):
        _seed(settings, size=10.0)
        n = MAX_CORRELATION_CANDIDATE_FILLS + 1
        ts = _now() - 5
        trades = [
            _trade(tx=f"0x{i:02x}", size=1.0, ts=ts + (i % 3)) for i in range(n)
        ]
        events = ingest_wallet(
            settings,
            WALLET,
            client=_client([_raw(size=10.0 + float(n))], trades=trades),
        )
        assert events == []
        assert float(load_snapshots_v2_as_models(settings, WALLET)[ASSET_YES].size) == 10.0
        rows = list_trade_correlation_quarantine(settings, wallet=WALLET)
        assert any(r["reason"] == "correlation_complexity_exceeded" for r in rows)
