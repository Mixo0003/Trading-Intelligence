"""V2.2.2 Checkpoint 8 — COPY paper ordering, execution VWAP entry, atomic idempotency."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from app.copy_intel.api_result import ApiSuccess
from app.copy_intel.models import CopyRecommendation, TradeEvent
from app.copy_intel.order_book import parse_validated_order_book
from app.copy_intel.paper_commit import (
    CODE_CONFLICT,
    CODE_EXPOSURE_CHANGED,
    CODE_EXPOSURE_ZERO,
    CODE_INVALID_EXECUTION_VWAP,
    CODE_MISSING_EXECUTION_VWAP,
    CODE_NOT_COPY,
    commit_copy_paper_for_recommendation,
    prepare_pending_recommendation,
)
from app.copy_intel.recommendation import (
    REASON_AUTHORITATIVE_EVENT_REQUIRED,
    build_recommendation,
)
from app.copy_intel.risk import assert_read_only
from app.database import copy_storage
from app.database.storage import get_connection, initialize_database
from tests.conftest import make_test_settings

WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ASSET = "token-yes-111"
CONDITION = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
UID = "ff1:" + ("ab" * 32)


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "cp8_paper.db"),
        copy_paper_bankroll_usd=10_000.0,
        copy_min_liquidity_usd=200.0,
        copy_max_slippage_pct=0.25,
        copy_max_event_age_seconds=600,
        copy_max_trade_age_min=30,
        copy_min_resolved_trades=0,
    )
    initialize_database(s)
    return s


class FakeMetrics:
    eligible = True
    ineligible_reason = None


def _event(**overrides) -> TradeEvent:
    now = 1_700_000_000
    base = dict(
        wallet=WALLET,
        event_type="open",
        condition_id=CONDITION,
        asset=ASSET,
        outcome="Yes",
        side="BUY",
        price=0.50,
        size=10.0,
        tx_hash="0xabc",
        ts=now - 10,
        title="Sanitized market",
        slug="sanitized",
        dedup_key="dk",
        event_uid=UID,
        trader_vwap=0.50,
        first_fill_ts=now - 20,
        last_fill_ts=now - 10,
        detected_at=now - 5,
        matched_fill_count=1,
    )
    base.update(overrides)
    return TradeEvent(**base)


def _book_payload(*, timestamp: float, asks=None):
    return {
        "asset_id": ASSET,
        "timestamp": str(int(timestamp)),
        "hash": "bookhash",
        "bids": [{"price": "0.49", "size": "5000"}],
        "asks": asks
        or [
            {"price": "0.51", "size": "5000"},
            {"price": "0.52", "size": "5000"},
        ],
        "min_order_size": "1",
    }


def _validated(payload, *, received_at: float):
    result = parse_validated_order_book(
        payload, requested_asset=ASSET, received_at=received_at, request_count=1
    )
    assert isinstance(result, ApiSuccess)
    return result.data


def _patch_metrics(monkeypatch):
    monkeypatch.setattr(
        "app.copy_intel.recommendation.compute_trader_metrics",
        lambda *a, **k: FakeMetrics(),
    )
    monkeypatch.setattr(
        "app.copy_intel.recommendation.score_trader",
        lambda *a, **k: 80.0,
    )


def _client_with_book(book):
    client = MagicMock()
    client.fetch_validated_order_book.return_value = ApiSuccess(data=book)
    return client


def _copy_rec(settings, monkeypatch, **event_overrides) -> CopyRecommendation:
    now = 1_700_000_000.0
    book = _validated(_book_payload(timestamp=now - 5), received_at=now)
    _patch_metrics(monkeypatch)
    client = _client_with_book(book)
    rec = build_recommendation(
        settings, _event(**event_overrides), client=client, now=now
    )
    assert rec.action == "COPY"
    assert rec.execution_vwap is not None
    return rec


def _manual_copy_rec(**overrides) -> CopyRecommendation:
    base = dict(
        wallet=WALLET,
        trader_label="lab",
        market_title="Sanitized market",
        market_slug="sanitized",
        outcome="Yes",
        event_type="open",
        entry_price=0.50,
        current_price=0.50,
        price_drift=0.0,
        slippage_est=0.02,
        trader_score=80.0,
        max_alloc=300.0,
        action="COPY",
        reason="copy_eligible",
        event_uid=UID,
        first_fill_ts=1_699_999_980,
        last_fill_ts=1_699_999_990,
        detected_at=1_699_999_995,
        trader_vwap=0.50,
        midpoint=0.50,
        best_bid=0.49,
        best_ask=0.51,
        execution_vwap=0.51,
        slippage=0.02,
        proposed_allocation_usd=300.0,
        paper_bankroll_usd=10_000.0,
        paper_bankroll_share=0.03,
        resulting_position_size=588.2352941176,
        quote_at=1_699_999_995.0,
        reason_codes=("copy_eligible",),
    )
    base.update(overrides)
    return CopyRecommendation(**base)


class TestPaperEligibility:
    def test_final_copy_creates_exactly_one_paper_row(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert result.ok and result.inserted
        assert copy_storage.count_open_paper_positions(settings) == 1
        assert copy_storage.count_recommendations_for_event_uid(settings, UID) == 1

    def test_watch_creates_no_paper_row(self, settings):
        rec = _manual_copy_rec(action="WATCH", execution_vwap=0.51)
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert result.ok is False
        assert result.code == CODE_NOT_COPY
        assert copy_storage.count_open_paper_positions(settings) == 0

    def test_skip_creates_no_paper_row(self, settings):
        rec = _manual_copy_rec(action="SKIP", execution_vwap=0.51)
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert result.code == CODE_NOT_COPY
        assert copy_storage.count_open_paper_positions(settings) == 0

    def test_quarantined_event_creates_no_paper(self, settings, monkeypatch):
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings,
            _event(trader_vwap=None, first_fill_ts=None, last_fill_ts=None),
            client=MagicMock(),
            now=1_700_000_000.0,
        )
        assert rec.action == "SKIP"
        assert REASON_AUTHORITATIVE_EVENT_REQUIRED in rec.reason_codes
        include, paper = prepare_pending_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert include is False  # SKIP never enters pending delivery
        assert paper is None
        assert copy_storage.count_open_paper_positions(settings) == 0


class TestEntryPrice:
    def test_missing_execution_vwap_rejected(self, settings):
        rec = _manual_copy_rec(execution_vwap=None)
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert result.code == CODE_MISSING_EXECUTION_VWAP
        assert copy_storage.count_open_paper_positions(settings) == 0

    @pytest.mark.parametrize("bad", [0.0, -0.1, float("nan"), float("inf"), 1.01])
    def test_invalid_execution_vwap_rejected(self, settings, bad):
        rec = _manual_copy_rec(execution_vwap=bad)
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert result.code == CODE_INVALID_EXECUTION_VWAP
        assert copy_storage.count_open_paper_positions(settings) == 0

    def test_fallback_half_never_used(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)
        # Force none on legacy fields; commit must still use execution_vwap.
        rec2 = replace(rec, entry_price=None, current_price=None)
        result = commit_copy_paper_for_recommendation(
            settings, rec2, condition_id=CONDITION, asset=ASSET
        )
        assert result.ok
        row = copy_storage.get_paper_position_by_source_event_uid(settings, UID)
        assert row is not None
        assert float(row["entry_price"]) == pytest.approx(float(rec.execution_vwap))
        assert float(row["entry_price"]) != pytest.approx(0.5) or float(rec.execution_vwap) == 0.5

    def test_trader_vwap_midpoint_best_ask_not_used_as_entry(self, settings):
        rec = _manual_copy_rec(
            trader_vwap=0.40,
            midpoint=0.45,
            best_ask=0.60,
            execution_vwap=0.51,
        )
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert result.ok
        row = copy_storage.get_paper_position_by_source_event_uid(settings, UID)
        assert float(row["entry_price"]) == pytest.approx(0.51)
        assert float(row["entry_price"]) != pytest.approx(0.40)
        assert float(row["entry_price"]) != pytest.approx(0.45)
        assert float(row["entry_price"]) != pytest.approx(0.60)

    def test_entry_price_equals_execution_vwap_exactly(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert result.ok
        row = copy_storage.get_paper_position_by_source_event_uid(settings, UID)
        assert float(row["entry_price"]) == float(rec.execution_vwap)


class TestOrderingAndExposure:
    def test_paper_write_only_after_final_copy(self, settings, monkeypatch):
        now = 1_700_000_000.0
        book = _validated(_book_payload(timestamp=now - 5), received_at=now)
        _patch_metrics(monkeypatch)
        client = _client_with_book(book)
        rec = build_recommendation(settings, _event(), client=client, now=now)
        assert rec.action == "COPY"
        assert copy_storage.count_open_paper_positions(settings) == 0
        commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert copy_storage.count_open_paper_positions(settings) == 1

    def test_zero_capacity_at_commit_rolls_back(self, settings):
        # Exhaust total sleeve (15% of 10k = 1500) via other wallets/markets.
        copy_storage.open_paper_position(
            settings,
            wallet="0xcccccccccccccccccccccccccccccccccccccccc",
            condition_id="other-market",
            market_slug="other",
            outcome="Yes",
            notional=1500.0,
            entry_price=0.5,
            asset="other-asset",
            source_event_uid="seed-full",
        )
        rec = _manual_copy_rec(proposed_allocation_usd=10.0, max_alloc=10.0)
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert result.code == CODE_EXPOSURE_ZERO
        assert copy_storage.count_recommendations_for_event_uid(settings, UID) == 0
        assert copy_storage.get_paper_position_by_source_event_uid(settings, UID) is None

    def test_reduced_capacity_at_commit_no_resize(self, settings):
        # Per-wallet cap 3% of 10k = 300. Seed 200 → remaining 100 < proposed 300.
        copy_storage.open_paper_position(
            settings,
            wallet=WALLET,
            condition_id="other-cond",
            market_slug="other",
            outcome="No",
            notional=200.0,
            entry_price=0.4,
            asset="other-asset-2",
            source_event_uid="seed-partial",
        )
        rec = _manual_copy_rec(proposed_allocation_usd=300.0, max_alloc=300.0)
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert result.code == CODE_EXPOSURE_CHANGED
        assert copy_storage.get_paper_position_by_source_event_uid(settings, UID) is None
        assert copy_storage.count_recommendations_for_event_uid(settings, UID) == 0


class TestAtomicity:
    def test_recommendation_and_paper_atomic_success(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert result.ok and result.inserted
        assert copy_storage.count_recommendations_for_event_uid(settings, UID) == 1
        assert copy_storage.count_open_paper_positions(settings) == 1

    def test_failure_after_recommendation_rolls_back_both(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)

        def hook(stage: str) -> None:
            if stage == "after_recommendation":
                raise RuntimeError("injected after recommendation")

        copy_storage._PAPER_COMMIT_TEST_HOOK = hook
        try:
            result = commit_copy_paper_for_recommendation(
                settings, rec, condition_id=CONDITION, asset=ASSET
            )
            assert result.ok is False
        finally:
            copy_storage._PAPER_COMMIT_TEST_HOOK = None
        assert copy_storage.count_recommendations_for_event_uid(settings, UID) == 0
        assert copy_storage.count_open_paper_positions(settings) == 0

    def test_failure_during_paper_rolls_back_recommendation(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)

        def hook(stage: str) -> None:
            if stage == "after_paper":
                raise RuntimeError("injected after paper")

        copy_storage._PAPER_COMMIT_TEST_HOOK = hook
        try:
            result = commit_copy_paper_for_recommendation(
                settings, rec, condition_id=CONDITION, asset=ASSET
            )
            assert result.ok is False
        finally:
            copy_storage._PAPER_COMMIT_TEST_HOOK = None
        assert copy_storage.count_recommendations_for_event_uid(settings, UID) == 0
        assert copy_storage.count_open_paper_positions(settings) == 0


class TestIdempotency:
    def test_first_insert_succeeds_second_identical_idempotent(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)
        first = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        second = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert first.ok and first.inserted
        assert second.ok and not second.inserted
        assert second.code == "copy_paper_idempotent" or second.delivery_eligible
        assert copy_storage.count_open_paper_positions(settings) == 1

    def test_concurrent_duplicate_attempts_one_row(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)
        results: list = []

        def worker() -> None:
            results.append(
                commit_copy_paper_for_recommendation(
                    settings, rec, condition_id=CONDITION, asset=ASSET
                )
            )

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert copy_storage.count_open_paper_positions(settings) == 1
        assert sum(1 for r in results if r.ok) >= 1
        assert sum(1 for r in results if r.inserted) <= 1

    def test_conflicting_duplicate_payload_fails_closed(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)
        assert commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        ).ok
        conflict = replace(
            rec,
            execution_vwap=float(rec.execution_vwap) + 0.02,
            proposed_allocation_usd=float(rec.proposed_allocation_usd) + 5.0,
            max_alloc=float(rec.max_alloc) + 5.0,
        )
        result = commit_copy_paper_for_recommendation(
            settings, conflict, condition_id=CONDITION, asset=ASSET
        )
        assert result.ok is False
        assert result.code == CODE_CONFLICT
        row = copy_storage.get_paper_position_by_source_event_uid(settings, UID)
        assert float(row["entry_price"]) == pytest.approx(float(rec.execution_vwap))
        assert float(row["notional"]) == pytest.approx(float(rec.proposed_allocation_usd))

    def test_same_event_cannot_overwrite_committed_price_allocation(
        self, settings, monkeypatch
    ):
        rec = _copy_rec(settings, monkeypatch)
        commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        overwrite = replace(rec, execution_vwap=0.99, proposed_allocation_usd=1.0, max_alloc=1.0)
        result = commit_copy_paper_for_recommendation(
            settings, overwrite, condition_id=CONDITION, asset=ASSET
        )
        assert result.code == CODE_CONFLICT
        row = copy_storage.get_paper_position_by_source_event_uid(settings, UID)
        assert float(row["entry_price"]) == pytest.approx(float(rec.execution_vwap))
        assert float(row["notional"]) != pytest.approx(1.0)


class TestValidationAndAudit:
    def test_storage_lock_error_no_partial_row(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)

        def hook(stage: str) -> None:
            if stage == "before_recommendation":
                raise sqlite_error()

        def sqlite_error():
            import sqlite3

            raise sqlite3.OperationalError("database is locked")

        copy_storage._PAPER_COMMIT_TEST_HOOK = hook
        try:
            result = commit_copy_paper_for_recommendation(
                settings, rec, condition_id=CONDITION, asset=ASSET
            )
            assert result.ok is False
        finally:
            copy_storage._PAPER_COMMIT_TEST_HOOK = None
        assert copy_storage.count_open_paper_positions(settings) == 0
        assert copy_storage.count_recommendations_for_event_uid(settings, UID) == 0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"event_uid": None},
            {"event_uid": ""},
            {"last_fill_ts": None, "first_fill_ts": None},
        ],
    )
    def test_invalid_uid_or_timestamp_rejected(self, settings, kwargs):
        rec = _manual_copy_rec(**kwargs)
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert result.ok is False
        assert copy_storage.count_open_paper_positions(settings) == 0

    def test_invalid_asset_rejected(self, settings):
        rec = _manual_copy_rec()
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=""
        )
        assert result.ok is False
        assert copy_storage.count_open_paper_positions(settings) == 0

    def test_audit_envelope_contains_authoritative_values(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)
        commit_copy_paper_for_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        with get_connection(settings) as conn:
            row = conn.execute(
                "SELECT envelope_json, entry_price, max_alloc FROM copy_recommendations WHERE event_uid = ?",
                (UID,),
            ).fetchone()
            audit = conn.execute(
                "SELECT payload_json FROM copy_audit_log WHERE action = 'copy_paper_open' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        envelope = json.loads(row["envelope_json"])
        assert envelope["execution_vwap"] == rec.execution_vwap
        assert envelope["proposed_allocation_usd"] == rec.proposed_allocation_usd
        assert envelope["asset"] == ASSET
        assert envelope["authoritative_trade_ts"] == rec.last_fill_ts
        assert float(row["entry_price"]) == pytest.approx(float(rec.execution_vwap))
        payload = json.loads(audit["payload_json"])
        assert payload["execution_vwap"] == rec.execution_vwap
        assert payload["source_event_uid"] == UID
        assert "SELECT" not in (audit["payload_json"] or "")


class TestSchedulerGate:
    def test_scheduler_calls_paper_write_only_for_copy(self, settings):
        watch = _manual_copy_rec(action="WATCH", event_uid=UID + "w")
        skip = _manual_copy_rec(action="SKIP", event_uid=UID + "s")
        copy = _manual_copy_rec(action="COPY", event_uid=UID + "c")

        with patch(
            "app.copy_intel.paper_commit.commit_copy_paper_for_recommendation"
        ) as mocked:
            mocked.return_value = MagicMock(ok=True, code="copy_paper_committed")
            include_w, _ = prepare_pending_recommendation(
                settings, watch, condition_id=CONDITION, asset=ASSET
            )
            include_s, _ = prepare_pending_recommendation(
                settings, skip, condition_id=CONDITION, asset=ASSET
            )
            include_c, _ = prepare_pending_recommendation(
                settings, copy, condition_id=CONDITION, asset=ASSET
            )
            assert include_w and include_c
            assert include_s is False
            assert mocked.call_count == 1

    def test_failed_paper_write_blocks_delivery_eligibility(self, settings):
        rec = _manual_copy_rec(execution_vwap=None)
        include, result = prepare_pending_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert include is False
        assert result is not None
        assert result.delivery_eligible is False

    def test_successful_idempotent_retry_remains_safe(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)
        first = prepare_pending_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        second = prepare_pending_recommendation(
            settings, rec, condition_id=CONDITION, asset=ASSET
        )
        assert first[0] is True and first[1].ok
        assert second[0] is True and second[1].ok
        assert copy_storage.count_open_paper_positions(settings) == 1

    def test_no_telegram_send_in_paper_commit_path(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)
        with patch("telegram.Bot.send_message", create=True) as send:
            commit_copy_paper_for_recommendation(
                settings, rec, condition_id=CONDITION, asset=ASSET
            )
            prepare_pending_recommendation(
                settings, rec, condition_id=CONDITION, asset=ASSET
            )
            send.assert_not_called()

    def test_no_authenticated_trading_api_call(self, settings, monkeypatch):
        rec = _copy_rec(settings, monkeypatch)
        with patch("app.copy_intel.data_client.CopyDataClient") as client_cls:
            commit_copy_paper_for_recommendation(
                settings, rec, condition_id=CONDITION, asset=ASSET
            )
            client_cls.assert_not_called()

    def test_read_only_guard_still_enforced(self):
        settings = make_test_settings(copy_intel_read_only=False)
        with pytest.raises(RuntimeError):
            assert_read_only(settings)
        rec = _manual_copy_rec()
        with pytest.raises(RuntimeError):
            commit_copy_paper_for_recommendation(
                settings, rec, condition_id=CONDITION, asset=ASSET
            )
