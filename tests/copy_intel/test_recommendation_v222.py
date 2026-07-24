"""V2.2.2 Checkpoint 7 — trustworthy recommendation pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.copy_intel.api_result import ApiFailure, ApiSuccess
from app.copy_intel.models import TradeEvent
from app.copy_intel.order_book import (
    ValidatedOrderBook,
    allocate_buy,
    parse_validated_order_book,
)
from app.copy_intel.recommendation import (
    REASON_AUTHORITATIVE_EVENT_REQUIRED,
    REASON_BELOW_MIN_LIQUIDITY,
    REASON_COPY_ELIGIBLE,
    REASON_EVENT_TOO_OLD,
    REASON_EXPOSURE_CAPACITY_ZERO,
    REASON_INSUFFICIENT_DEPTH,
    REASON_QUOTE_STALE,
    REASON_QUOTE_UNAVAILABLE,
    REASON_REQUEST_BUDGET_EXCEEDED,
    REASON_SLIPPAGE_LIMIT_EXCEEDED,
    REASON_SLIPPAGE_UNAVAILABLE,
    build_recommendation,
    is_recommendation_deliverable,
)
from app.copy_intel.risk import assert_read_only
from app.database import copy_storage
from app.database.storage import initialize_database
from tests.conftest import make_test_settings

WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ASSET = "token-yes-111"
CONDITION = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
UID = "ff1:" + ("ab" * 32)


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "cp7_rec.db"),
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
        matched_fill_count=1,
        matched_fill_size=10.0,
        detected_at=now,
        prev_size=0.0,
        new_size=10.0,
    )
    base.update(overrides)
    return TradeEvent(**base)


def _book_payload(
    *,
    timestamp: float = 1_700_000_000 - 5,
    asks=None,
    bids=None,
    asset_id: str = ASSET,
    min_order_size: str | None = "1",
):
    return {
        "asset_id": asset_id,
        "timestamp": timestamp,
        "hash": "hash-1",
        "bids": bids or [{"price": "0.49", "size": "5000"}],
        "asks": asks
        or [
            {"price": "0.51", "size": "5000"},
            {"price": "0.52", "size": "5000"},
        ],
        "min_order_size": min_order_size,
    }


def _validated(payload, *, received_at: float) -> ValidatedOrderBook:
    result = parse_validated_order_book(
        payload, requested_asset=ASSET, received_at=received_at
    )
    assert isinstance(result, ApiSuccess)
    return result.data


def _client_with_book(book: ValidatedOrderBook | ApiFailure, *, calls: list | None = None):
    client = MagicMock()
    call_log = calls if calls is not None else []

    def fetch(token_id: str):
        call_log.append(token_id)
        if isinstance(book, list):
            # sequence of responses for refetch tests
            idx = min(len(call_log) - 1, len(book) - 1)
            item = book[idx]
            return item if isinstance(item, (ApiSuccess, ApiFailure)) else ApiSuccess(data=item)
        if isinstance(book, ApiFailure):
            return book
        return ApiSuccess(data=book, request_count=1)

    client.fetch_validated_order_book.side_effect = fetch
    return client


def _patch_metrics(monkeypatch, *, score: float = 80.0, eligible: bool = True):
    metrics = FakeMetrics()
    metrics.eligible = eligible
    monkeypatch.setattr(
        "app.copy_intel.recommendation.compute_trader_metrics",
        lambda *a, **k: metrics,
    )
    monkeypatch.setattr(
        "app.copy_intel.recommendation.score_trader", lambda m: score
    )


class TestCopyHappyPath:
    def test_complete_valid_event_produces_copy(self, settings, monkeypatch):
        now = 1_700_000_000.0
        book = _validated(_book_payload(timestamp=now - 5), received_at=now)
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings,
            _event(last_fill_ts=int(now - 10), first_fill_ts=int(now - 20), detected_at=int(now)),
            client=_client_with_book(book),
            now=now,
            min_liquidity_usd=200.0,
        )
        assert rec.action == "COPY"
        assert REASON_COPY_ELIGIBLE in rec.reason_codes

    def test_copy_contains_authoritative_fields(self, settings, monkeypatch):
        now = 1_700_000_000.0
        book = _validated(_book_payload(timestamp=now - 5), received_at=now)
        _patch_metrics(monkeypatch)
        event = _event()
        rec = build_recommendation(
            settings, event, client=_client_with_book(book), now=now
        )
        assert rec.action == "COPY"
        assert rec.event_type == "open"
        assert rec.first_fill_ts == event.first_fill_ts
        assert rec.last_fill_ts == event.last_fill_ts
        assert rec.detected_at == event.detected_at
        assert rec.event_age_seconds == pytest.approx(now - event.last_fill_ts)
        assert rec.trader_vwap == pytest.approx(0.50)
        assert rec.midpoint == pytest.approx(book.midpoint)
        assert rec.best_ask == pytest.approx(book.best_ask)
        assert rec.best_bid == pytest.approx(book.best_bid)
        assert rec.execution_vwap is not None
        assert rec.slippage is not None
        assert rec.visible_ask_depth_usd == pytest.approx(book.visible_ask_depth_usd)
        assert rec.proposed_allocation_usd is not None
        assert rec.paper_bankroll_usd == pytest.approx(10_000.0)
        assert rec.paper_bankroll_share == pytest.approx(
            rec.proposed_allocation_usd / 10_000.0
        )
        assert rec.resulting_position_size is not None
        assert rec.size_delta == pytest.approx(10.0)
        assert rec.matched_fill_count == 1
        assert rec.quote_at == pytest.approx(book.quote_at)
        assert rec.quote_age_seconds == pytest.approx(book.quote_age_seconds)
        assert rec.reason_codes

    def test_avg_price_never_used_as_trader_vwap(self, settings, monkeypatch):
        now = 1_700_000_000.0
        book = _validated(_book_payload(timestamp=now - 5), received_at=now)
        _patch_metrics(monkeypatch)
        # Position-style avgPrice must not override authoritative trader_vwap.
        event = _event(price=0.99, trader_vwap=0.50)
        rec = build_recommendation(
            settings, event, client=_client_with_book(book), now=now
        )
        assert rec.trader_vwap == pytest.approx(0.50)
        assert rec.entry_price == pytest.approx(0.50)
        assert rec.trader_vwap != pytest.approx(0.99)

    def test_legacy_spread_heuristic_not_used_as_slippage(self, settings, monkeypatch):
        now = 1_700_000_000.0
        book = _validated(
            _book_payload(
                timestamp=now - 5,
                bids=[{"price": "0.40", "size": "100"}],
                asks=[
                    {"price": "0.50", "size": "100"},  # $50
                    {"price": "0.70", "size": "5000"},
                ],
            ),
            received_at=now,
        )
        _patch_metrics(monkeypatch)
        monkeypatch.setattr(
            "app.copy_intel.risk.estimate_slippage",
            lambda *a, **k: 0.999,
        )
        rec = build_recommendation(
            settings,
            _event(trader_vwap=0.50),
            client=_client_with_book(book),
            now=now,
            min_liquidity_usd=50.0,
        )
        assert rec.action in {"COPY", "WATCH"}
        assert rec.slippage == pytest.approx(
            (rec.execution_vwap - rec.midpoint) / rec.midpoint
        )
        assert rec.slippage != pytest.approx(0.999)
        assert rec.spread == pytest.approx(book.spread)
        assert abs((rec.slippage or 0) - (rec.spread or 0)) > 1e-6

    def test_legacy_depth_proxy_not_used(self, settings, monkeypatch):
        now = 1_700_000_000.0
        # Depth $600 — above 200/500, below 1000.
        asks = [{"price": "0.51", "size": "1177"}]  # ~$600
        book = _validated(
            _book_payload(timestamp=now - 5, asks=asks), received_at=now
        )
        _patch_metrics(monkeypatch)
        client = _client_with_book(book)
        ok = build_recommendation(
            settings, _event(), client=client, now=now, min_liquidity_usd=200.0
        )
        bad = build_recommendation(
            settings,
            _event(event_uid=UID[:-1] + "c"),
            client=client,
            now=now,
            min_liquidity_usd=1000.0,
        )
        assert ok.action == "COPY"
        assert bad.action == "SKIP"
        assert REASON_BELOW_MIN_LIQUIDITY in bad.reason_codes


class TestHardGates:
    def test_missing_authoritative_timestamp(self, settings, monkeypatch):
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings,
            _event(first_fill_ts=None, last_fill_ts=None),
            client=_client_with_book(ApiFailure("x", None, False, 1, "m")),
            now=1_700_000_000.0,
        )
        assert rec.action == "SKIP"
        assert REASON_AUTHORITATIVE_EVENT_REQUIRED in rec.reason_codes
        assert not is_recommendation_deliverable("FOLLOW_AND_WATCH", rec.action)

    def test_event_exactly_at_max_age_accepted(self, settings, monkeypatch):
        now = 1_700_000_000.0
        max_age = 600
        book = _validated(_book_payload(timestamp=now - 5), received_at=now)
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings,
            _event(last_fill_ts=int(now - max_age), first_fill_ts=int(now - max_age - 1)),
            client=_client_with_book(book),
            now=now,
            max_event_age_seconds=max_age,
        )
        assert rec.action == "COPY"

    def test_event_above_max_age_rejected(self, settings, monkeypatch):
        now = 1_700_000_000.0
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings,
            _event(last_fill_ts=int(now - 601), first_fill_ts=int(now - 602)),
            client=_client_with_book(ApiFailure("x", None, False, 1, "m")),
            now=now,
            max_event_age_seconds=600,
        )
        assert rec.action == "SKIP"
        assert REASON_EVENT_TOO_OLD in rec.reason_codes

    def test_stricter_owner_age_wins(self, settings, monkeypatch):
        now = 1_700_000_000.0
        _patch_metrics(monkeypatch)
        # Global 600s would allow age 400; owner 300 rejects.
        rec = build_recommendation(
            settings,
            _event(last_fill_ts=int(now - 400), first_fill_ts=int(now - 401)),
            client=_client_with_book(ApiFailure("x", None, False, 1, "m")),
            now=now,
            max_event_age_seconds=300,
        )
        assert rec.action == "SKIP"
        assert REASON_EVENT_TOO_OLD in rec.reason_codes

    def test_missing_quote_fails_closed(self, settings, monkeypatch):
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings,
            _event(),
            client=_client_with_book(
                ApiFailure("http_error", 503, True, 1, "down")
            ),
            now=1_700_000_000.0,
        )
        assert rec.action == "SKIP"
        assert REASON_QUOTE_UNAVAILABLE in rec.reason_codes

    def test_stale_quote_fails_closed(self, settings, monkeypatch):
        now = 1_700_000_000.0
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings,
            _event(),
            client=_client_with_book(
                ApiFailure("stale_quote", 200, False, 1, "stale")
            ),
            now=now,
        )
        assert rec.action == "SKIP"
        assert rec.reason_codes[0] in {REASON_QUOTE_STALE, REASON_QUOTE_UNAVAILABLE}

    def test_wrong_asset_quote_fails_closed(self, settings, monkeypatch):
        now = 1_700_000_000.0
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings,
            _event(),
            client=_client_with_book(
                ApiFailure("asset_mismatch", 200, False, 1, "asset")
            ),
            now=now,
        )
        assert rec.action == "SKIP"
        assert REASON_QUOTE_UNAVAILABLE in rec.reason_codes

    def test_insufficient_depth_fails_closed(self, settings, monkeypatch):
        now = 1_700_000_000.0
        # Depth $250 — above min liquidity 200, below $300 exposure-capped alloc.
        book = _validated(
            _book_payload(
                timestamp=now - 5,
                asks=[{"price": "0.50", "size": "500"}],  # $250
            ),
            received_at=now,
        )
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings, _event(), client=_client_with_book(book), now=now
        )
        assert rec.action == "SKIP"
        assert REASON_INSUFFICIENT_DEPTH in rec.reason_codes

    def test_exact_depth_allocation_succeeds(self, settings, monkeypatch):
        now = 1_700_000_000.0
        book = _validated(
            _book_payload(
                timestamp=now - 5,
                bids=[{"price": "0.49", "size": "100"}],
                asks=[{"price": "0.50", "size": "600"}],  # $300
            ),
            received_at=now,
        )
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings, _event(trader_vwap=0.495), client=_client_with_book(book), now=now
        )
        assert rec.action == "COPY"
        assert rec.proposed_allocation_usd == pytest.approx(300.0)
        assert rec.execution_vwap == pytest.approx(0.50)

    def test_execution_vwap_and_slippage_formula(self, settings, monkeypatch):
        now = 1_700_000_000.0
        book = _validated(_book_payload(timestamp=now - 5), received_at=now)
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings, _event(), client=_client_with_book(book), now=now
        )
        exe = allocate_buy(book, rec.proposed_allocation_usd or 0)
        assert rec.execution_vwap == pytest.approx(exe.execution_vwap)
        assert rec.slippage == pytest.approx(
            (rec.execution_vwap - rec.midpoint) / rec.midpoint
        )

    def test_slippage_unavailable_fails_closed(self, settings, monkeypatch):
        now = 1_700_000_000.0
        book = _validated(_book_payload(timestamp=now - 5), received_at=now)
        _patch_metrics(monkeypatch)

        def boom(*a, **k):
            from app.copy_intel.order_book import AllocationExecution

            return AllocationExecution(
                book=book,
                allocation_usd=1.0,
                best_bid=book.best_bid,
                best_ask=book.best_ask,
                midpoint=book.midpoint,
                spread=book.spread,
                visible_ask_depth_usd=book.visible_ask_depth_usd,
                quote_age_seconds=book.quote_age_seconds,
                filled_shares=None,
                execution_vwap=0.5,
                slippage=None,
                available=False,
                unavailable_code="invalid_midpoint",
            )

        monkeypatch.setattr("app.copy_intel.recommendation.allocate_buy", boom)
        rec = build_recommendation(
            settings, _event(), client=_client_with_book(book), now=now
        )
        assert rec.action == "SKIP"
        assert REASON_SLIPPAGE_UNAVAILABLE in rec.reason_codes

    def test_slippage_above_limit_watch(self, settings, monkeypatch):
        now = 1_700_000_000.0
        # Wide book so allocation VWAP walks far from mid.
        book = _validated(
            _book_payload(
                timestamp=now - 5,
                bids=[{"price": "0.10", "size": "10"}],
                asks=[
                    {"price": "0.80", "size": "100"},
                    {"price": "0.90", "size": "1000"},
                ],
            ),
            received_at=now,
        )
        settings = make_test_settings(
            database_path=settings.database_path,
            copy_paper_bankroll_usd=10_000.0,
            copy_min_liquidity_usd=50.0,
            copy_max_slippage_pct=0.01,
            copy_max_event_age_seconds=600,
            copy_max_trade_age_min=30,
            copy_min_resolved_trades=0,
        )
        initialize_database(settings)
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings, _event(), client=_client_with_book(book), now=now
        )
        assert rec.action == "WATCH"
        assert REASON_SLIPPAGE_LIMIT_EXCEEDED in rec.reason_codes

    def test_min_liquidity_200_500_1000_differ(self, settings, monkeypatch):
        now = 1_700_000_000.0
        asks = [{"price": "0.51", "size": "1177"}]  # ~$600 depth
        book = _validated(
            _book_payload(timestamp=now - 5, asks=asks), received_at=now
        )
        _patch_metrics(monkeypatch)
        client = _client_with_book(book)

        def run(floor: float, uid_suffix: str):
            return build_recommendation(
                settings,
                _event(event_uid=UID[:-1] + uid_suffix),
                client=client,
                now=now,
                min_liquidity_usd=floor,
            )

        r200 = run(200.0, "1")
        r500 = run(500.0, "2")
        r1000 = run(1000.0, "3")
        assert r200.action == "COPY"
        assert r500.action == "COPY"
        assert r1000.action == "SKIP"
        assert REASON_BELOW_MIN_LIQUIDITY in r1000.reason_codes

    def test_exposure_capacity_zero(self, settings, monkeypatch):
        now = 1_700_000_000.0
        book = _validated(_book_payload(timestamp=now - 5), received_at=now)
        _patch_metrics(monkeypatch)
        monkeypatch.setattr(
            "app.copy_intel.recommendation.check_exposure_limits",
            lambda *a, **k: (False, "full", 0.0),
        )
        rec = build_recommendation(
            settings, _event(), client=_client_with_book(book), now=now
        )
        assert rec.action == "SKIP"
        assert REASON_EXPOSURE_CAPACITY_ZERO in rec.reason_codes

    def test_proposed_allocation_exposure_capped_and_share(self, settings, monkeypatch):
        now = 1_700_000_000.0
        book = _validated(_book_payload(timestamp=now - 5), received_at=now)
        _patch_metrics(monkeypatch)
        monkeypatch.setattr(
            "app.copy_intel.recommendation.check_exposure_limits",
            lambda *a, **k: (True, "reduced", 150.0),
        )
        rec = build_recommendation(
            settings, _event(), client=_client_with_book(book), now=now
        )
        assert rec.proposed_allocation_usd == pytest.approx(150.0)
        assert rec.paper_bankroll_share == pytest.approx(150.0 / 10_000.0)
        assert "trader_bankroll" not in rec.reason
        assert getattr(rec, "trader_bankroll", None) is None


class TestDeliveryFilter:
    def test_follow_only_and_follow_and_watch(self):
        assert is_recommendation_deliverable("FOLLOW_ONLY", "COPY") is True
        assert is_recommendation_deliverable("FOLLOW_ONLY", "WATCH") is False
        assert is_recommendation_deliverable("FOLLOW_ONLY", "SKIP") is False
        assert is_recommendation_deliverable("FOLLOW_AND_WATCH", "COPY") is True
        assert is_recommendation_deliverable("FOLLOW_AND_WATCH", "WATCH") is True
        assert is_recommendation_deliverable("FOLLOW_AND_WATCH", "SKIP") is False

    def test_skip_never_deliverable(self):
        for mode in ("FOLLOW_ONLY", "FOLLOW_AND_WATCH"):
            assert is_recommendation_deliverable(mode, "SKIP") is False


class TestQuoteRefetch:
    def test_stale_refetch_replaces_quote_fields(self, settings, monkeypatch):
        from app.copy_intel.order_book import BookLevel

        now = 1_700_000_000.0
        deep_asks = (BookLevel(0.51, 5000.0), BookLevel(0.52, 5000.0))
        deep_bids = (BookLevel(0.49, 5000.0),)
        old = ValidatedOrderBook(
            asset_id=ASSET,
            book_hash="old",
            quote_at=now - 40,
            received_at=now - 39,
            quote_age_seconds=1.0,
            bids=deep_bids,
            asks=deep_asks,
            best_bid=0.49,
            best_ask=0.51,
            midpoint=0.50,
            spread=0.02,
            visible_ask_depth_usd=0.51 * 5000 + 0.52 * 5000,
            visible_bid_depth_usd=0.49 * 5000,
            min_order_size=1.0,
        )
        fresh = _validated(
            _book_payload(timestamp=now - 2, asks=[{"price": "0.51", "size": "5000"}]),
            received_at=now,
        )
        calls: list = []
        responses = [ApiSuccess(data=old), ApiSuccess(data=fresh)]
        client = MagicMock()

        def fetch(token_id: str):
            calls.append(token_id)
            return responses[len(calls) - 1]

        client.fetch_validated_order_book.side_effect = fetch
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings, _event(), client=client, now=now, min_liquidity_usd=200.0
        )
        assert len(calls) == 2
        assert rec.action == "COPY"
        assert rec.quote_at == pytest.approx(fresh.quote_at)
        assert rec.best_ask == pytest.approx(fresh.best_ask)
        assert rec.midpoint == pytest.approx(fresh.midpoint)
        assert rec.visible_ask_depth_usd == pytest.approx(fresh.visible_ask_depth_usd)

    def test_failed_refetch_fails_closed(self, settings, monkeypatch):
        from app.copy_intel.order_book import BookLevel

        now = 1_700_000_000.0
        old = ValidatedOrderBook(
            asset_id=ASSET,
            book_hash="old",
            quote_at=now - 40,
            received_at=now - 39,
            quote_age_seconds=1.0,
            bids=(BookLevel(0.49, 5000.0),),
            asks=(BookLevel(0.51, 5000.0),),
            best_bid=0.49,
            best_ask=0.51,
            midpoint=0.50,
            spread=0.02,
            visible_ask_depth_usd=10_000.0,
            visible_bid_depth_usd=400.0,
            min_order_size=1.0,
        )
        client = MagicMock()
        client.fetch_validated_order_book.side_effect = [
            ApiSuccess(data=old),
            ApiFailure("timeout", None, True, 1, "t"),
        ]
        _patch_metrics(monkeypatch)
        rec = build_recommendation(settings, _event(), client=client, now=now)
        assert rec.action == "SKIP"
        assert REASON_QUOTE_UNAVAILABLE in rec.reason_codes
        assert client.fetch_validated_order_book.call_count == 2

    def test_no_more_than_one_refetch(self, settings, monkeypatch):
        from app.copy_intel.order_book import BookLevel

        now = 1_700_000_000.0
        old = ValidatedOrderBook(
            asset_id=ASSET,
            book_hash="old",
            quote_at=now - 40,
            received_at=now - 39,
            quote_age_seconds=1.0,
            bids=(BookLevel(0.49, 5000.0),),
            asks=(BookLevel(0.51, 5000.0),),
            best_bid=0.49,
            best_ask=0.51,
            midpoint=0.50,
            spread=0.02,
            visible_ask_depth_usd=10_000.0,
            visible_bid_depth_usd=400.0,
            min_order_size=1.0,
        )
        still_stale = ValidatedOrderBook(
            asset_id=ASSET,
            book_hash="stale2",
            quote_at=now - 50,
            received_at=now,
            quote_age_seconds=50.0,
            bids=old.bids,
            asks=old.asks,
            best_bid=0.49,
            best_ask=0.51,
            midpoint=0.50,
            spread=0.02,
            visible_ask_depth_usd=10_000.0,
            visible_bid_depth_usd=400.0,
            min_order_size=1.0,
        )
        client = MagicMock()
        client.fetch_validated_order_book.side_effect = [
            ApiSuccess(data=old),
            ApiSuccess(data=still_stale),
            ApiSuccess(data=still_stale),
        ]
        _patch_metrics(monkeypatch)
        rec = build_recommendation(settings, _event(), client=client, now=now)
        assert client.fetch_validated_order_book.call_count == 2
        assert rec.action == "SKIP"
        assert REASON_QUOTE_STALE in rec.reason_codes

    def test_request_budget_exhaustion(self, settings, monkeypatch):
        from app.copy_intel.order_book import BookLevel

        now = 1_700_000_000.0
        old = ValidatedOrderBook(
            asset_id=ASSET,
            book_hash="old",
            quote_at=now - 40,
            received_at=now - 39,
            quote_age_seconds=1.0,
            bids=(BookLevel(0.49, 5000.0),),
            asks=(BookLevel(0.51, 5000.0),),
            best_bid=0.49,
            best_ask=0.51,
            midpoint=0.50,
            spread=0.02,
            visible_ask_depth_usd=10_000.0,
            visible_bid_depth_usd=400.0,
            min_order_size=1.0,
        )
        monkeypatch.setattr(
            "app.copy_intel.recommendation.MAX_BOOK_REQUESTS_PER_EVALUATION", 1
        )
        client = MagicMock()
        client.fetch_validated_order_book.return_value = ApiSuccess(data=old)
        _patch_metrics(monkeypatch)
        rec = build_recommendation(settings, _event(), client=client, now=now)
        assert rec.action == "SKIP"
        assert REASON_REQUEST_BUDGET_EXCEEDED in rec.reason_codes
        assert client.fetch_validated_order_book.call_count == 1


class TestPersistenceAndGuards:
    def test_copy_build_defers_persistence_until_paper_commit(self, settings, monkeypatch):
        now = 1_700_000_000.0
        book = _validated(_book_payload(timestamp=now - 5), received_at=now)
        _patch_metrics(monkeypatch)
        client = _client_with_book(book)
        event = _event()
        first = build_recommendation(settings, event, client=client, now=now)
        second = build_recommendation(settings, event, client=client, now=now)
        assert first.action == "COPY"
        assert second.action == "COPY"
        # Checkpoint 8: COPY envelope+paper are atomic; build alone writes neither.
        assert copy_storage.count_recommendations_for_event_uid(settings, UID) == 0
        assert copy_storage.count_open_paper_positions(settings) == 0

    def test_quarantined_event_no_copy_watch(self, settings, monkeypatch):
        _patch_metrics(monkeypatch)
        rec = build_recommendation(
            settings,
            _event(trader_vwap=None, first_fill_ts=None, last_fill_ts=None),
            client=MagicMock(),
            now=1_700_000_000.0,
        )
        assert rec.action == "SKIP"
        assert REASON_AUTHORITATIVE_EVENT_REQUIRED in rec.reason_codes

    def test_read_only_guard_still_enforced(self):
        settings = make_test_settings(copy_intel_read_only=True)
        assert_read_only(settings)
        with pytest.raises(RuntimeError):
            assert_read_only(make_test_settings(copy_intel_read_only=False))
