"""V2.2.2 Checkpoint 6 — validated order book, depth, execution VWAP, slippage.

Sanitized fixtures and temporary resources only.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.copy_intel.api_result import ApiFailure, ApiSuccess
from app.copy_intel.data_client import CopyDataClient
from app.copy_intel.order_book import (
    MAX_QUOTE_AGE_SECONDS,
    allocate_buy,
    parse_validated_order_book,
)
from tests.conftest import make_test_settings

ASSET = "token-yes-111"
OTHER_ASSET = "token-no-222"


@pytest.fixture
def settings(tmp_path):
    return make_test_settings(database_path=str(tmp_path / "cp6_book.db"))


def _book(
    *,
    asset_id: str = ASSET,
    timestamp: float | str = 970.0,
    book_hash: str = "hash-abc",
    bids=None,
    asks=None,
    min_order_size=None,
    include_bids: bool = True,
    include_asks: bool = True,
    extra: dict | None = None,
):
    payload: dict = {
        "asset_id": asset_id,
        "timestamp": timestamp,
        "hash": book_hash,
    }
    if include_bids:
        payload["bids"] = (
            bids
            if bids is not None
            else [{"price": "0.40", "size": "100"}]
        )
    if include_asks:
        payload["asks"] = (
            asks
            if asks is not None
            else [{"price": "0.50", "size": "100"}]
        )
    if min_order_size is not None:
        payload["min_order_size"] = min_order_size
    if extra:
        payload.update(extra)
    return payload


def _parse(payload, *, received_at: float = 1000.0, asset: str = ASSET):
    return parse_validated_order_book(
        payload, requested_asset=asset, received_at=received_at
    )


class TestValidatedBookBasics:
    def test_valid_one_level_book(self):
        result = _parse(_book())
        assert isinstance(result, ApiSuccess)
        book = result.data
        assert book.best_bid == pytest.approx(0.40)
        assert book.best_ask == pytest.approx(0.50)
        assert book.midpoint == pytest.approx(0.45)
        assert book.spread == pytest.approx(0.10)
        assert book.visible_ask_depth_usd == pytest.approx(50.0)
        assert book.quote_at == pytest.approx(970.0)
        assert book.received_at == pytest.approx(1000.0)
        assert book.quote_age_seconds == pytest.approx(30.0)

    def test_valid_multi_level_book(self):
        result = _parse(
            _book(
                bids=[
                    {"price": "0.39", "size": "10"},
                    {"price": "0.40", "size": "20"},
                ],
                asks=[
                    {"price": "0.50", "size": "10"},
                    {"price": "0.52", "size": "20"},
                ],
            )
        )
        assert isinstance(result, ApiSuccess)
        book = result.data
        assert book.best_bid == pytest.approx(0.40)
        assert book.best_ask == pytest.approx(0.50)
        assert book.visible_ask_depth_usd == pytest.approx(0.50 * 10 + 0.52 * 20)

    def test_wrong_asset_rejected(self):
        result = _parse(_book(asset_id=OTHER_ASSET))
        assert isinstance(result, ApiFailure)
        assert result.code == "asset_mismatch"

    def test_missing_hash_rejected(self):
        payload = _book()
        payload["hash"] = ""
        result = _parse(payload)
        assert isinstance(result, ApiFailure)
        assert result.code == "missing_hash"

    def test_missing_timestamp_rejected(self):
        payload = _book()
        del payload["timestamp"]
        result = _parse(payload)
        assert isinstance(result, ApiFailure)
        assert result.code == "missing_timestamp"


class TestTimestampNormalization:
    def test_seconds_timestamp_accepted(self):
        result = _parse(_book(timestamp=985.0), received_at=1000.0)
        assert isinstance(result, ApiSuccess)
        assert result.data.quote_at == pytest.approx(985.0)

    def test_milliseconds_timestamp_accepted(self):
        result = _parse(_book(timestamp=985_000), received_at=1000.0)
        assert isinstance(result, ApiSuccess)
        assert result.data.quote_at == pytest.approx(985.0)
        assert result.data.quote_age_seconds == pytest.approx(15.0)

    def test_ambiguous_timestamp_rejected(self):
        # Small absolute times where both unit interpretations fit the window.
        result = _parse(_book(timestamp=20), received_at=25.0)
        assert isinstance(result, ApiFailure)
        assert result.code == "ambiguous_timestamp"

    def test_future_timestamp_rejected(self):
        result = _parse(_book(timestamp=1100.0), received_at=1000.0)
        assert isinstance(result, ApiFailure)
        assert result.code == "future_timestamp"

    def test_quote_age_exactly_30s_accepted(self):
        result = _parse(_book(timestamp=970.0), received_at=1000.0)
        assert isinstance(result, ApiSuccess)
        assert result.data.quote_age_seconds == pytest.approx(MAX_QUOTE_AGE_SECONDS)

    def test_quote_age_above_30s_rejected(self):
        result = _parse(_book(timestamp=969.0), received_at=1000.0)
        assert isinstance(result, ApiFailure)
        assert result.code == "stale_quote"


class TestBookStructureValidation:
    def test_missing_bids_rejected(self):
        result = _parse(_book(include_bids=False))
        assert isinstance(result, ApiFailure)
        assert result.code == "missing_bids"

    def test_missing_asks_rejected(self):
        result = _parse(_book(include_asks=False))
        assert isinstance(result, ApiFailure)
        assert result.code == "missing_asks"

    def test_non_list_bids_asks_rejected(self):
        bad_bids = _parse(_book(bids="not-a-list"))  # type: ignore[arg-type]
        assert isinstance(bad_bids, ApiFailure)
        assert bad_bids.code == "invalid_bids_type"
        bad_asks = _parse(_book(asks={"price": "0.5"}))  # type: ignore[arg-type]
        assert isinstance(bad_asks, ApiFailure)
        assert bad_asks.code == "invalid_asks_type"

    def test_zero_negative_nonfinite_price_rejected(self):
        for price in ("0", "-0.1", "nan", "inf"):
            result = _parse(_book(asks=[{"price": price, "size": "10"}]))
            assert isinstance(result, ApiFailure)
            assert result.code == "invalid_asks_price"

    def test_zero_negative_nonfinite_size_rejected(self):
        for size in ("0", "-5", "nan", "-inf"):
            result = _parse(_book(asks=[{"price": "0.5", "size": size}]))
            assert isinstance(result, ApiFailure)
            assert result.code == "invalid_asks_size"

    def test_crossed_or_locked_book_rejected(self):
        locked = _parse(
            _book(
                bids=[{"price": "0.50", "size": "1"}],
                asks=[{"price": "0.50", "size": "1"}],
            )
        )
        assert isinstance(locked, ApiFailure)
        assert locked.code == "crossed_or_locked_book"
        crossed = _parse(
            _book(
                bids=[{"price": "0.55", "size": "1"}],
                asks=[{"price": "0.50", "size": "1"}],
            )
        )
        assert isinstance(crossed, ApiFailure)
        assert crossed.code == "crossed_or_locked_book"

    def test_unsorted_asks_normalized_ascending(self):
        result = _parse(
            _book(
                asks=[
                    {"price": "0.55", "size": "2"},
                    {"price": "0.50", "size": "1"},
                ]
            )
        )
        assert isinstance(result, ApiSuccess)
        assert [level.price for level in result.data.asks] == [0.50, 0.55]
        assert result.data.best_ask == pytest.approx(0.50)

    def test_unsorted_bids_normalized_descending(self):
        result = _parse(
            _book(
                bids=[
                    {"price": "0.30", "size": "2"},
                    {"price": "0.40", "size": "1"},
                ]
            )
        )
        assert isinstance(result, ApiSuccess)
        assert [level.price for level in result.data.bids] == [0.40, 0.30]
        assert result.data.best_bid == pytest.approx(0.40)


class TestDerivedMetrics:
    def test_best_bid_ask_midpoint_depth(self):
        result = _parse(
            _book(
                bids=[{"price": "0.42", "size": "5"}],
                asks=[
                    {"price": "0.48", "size": "10"},
                    {"price": "0.50", "size": "4"},
                ],
            )
        )
        assert isinstance(result, ApiSuccess)
        book = result.data
        assert book.best_bid == pytest.approx(0.42)
        assert book.best_ask == pytest.approx(0.48)
        assert book.midpoint == pytest.approx((0.42 + 0.48) / 2)
        assert book.visible_ask_depth_usd == pytest.approx(0.48 * 10 + 0.50 * 4)


class TestAllocationSweep:
    def _ok_book(self, **kwargs):
        result = _parse(_book(**kwargs))
        assert isinstance(result, ApiSuccess)
        return result.data

    def test_allocation_fully_filled_one_level(self):
        book = self._ok_book(asks=[{"price": "0.50", "size": "100"}])
        exe = allocate_buy(book, 25.0)
        assert exe.available is True
        assert exe.filled_shares == pytest.approx(50.0)
        assert exe.execution_vwap == pytest.approx(0.50)
        assert exe.slippage == pytest.approx((0.50 - 0.45) / 0.45)

    def test_allocation_spans_multiple_levels(self):
        book = self._ok_book(
            asks=[
                {"price": "0.50", "size": "10"},  # $5
                {"price": "0.60", "size": "10"},  # $6
            ]
        )
        # Spend $5 + $3 of second level.
        exe = allocate_buy(book, 8.0)
        assert exe.available is True
        assert exe.filled_shares == pytest.approx(10.0 + 5.0)
        assert exe.execution_vwap == pytest.approx(8.0 / 15.0)

    def test_partial_final_level_calculation(self):
        book = self._ok_book(
            asks=[
                {"price": "0.40", "size": "5"},  # $2
                {"price": "0.50", "size": "10"},
            ],
            bids=[{"price": "0.30", "size": "1"}],
        )
        exe = allocate_buy(book, 3.0)
        assert exe.available is True
        # $2 at 0.40 → 5 shares; $1 at 0.50 → 2 shares.
        assert exe.filled_shares == pytest.approx(7.0)
        assert exe.execution_vwap == pytest.approx(3.0 / 7.0)

    def test_exact_depth_allocation_succeeds(self):
        book = self._ok_book(asks=[{"price": "0.50", "size": "20"}])
        depth = book.visible_ask_depth_usd
        exe = allocate_buy(book, depth)
        assert exe.available is True
        assert exe.execution_vwap == pytest.approx(0.50)
        assert exe.filled_shares == pytest.approx(20.0)

    def test_insufficient_depth_unavailable(self):
        book = self._ok_book(asks=[{"price": "0.50", "size": "10"}])
        exe = allocate_buy(book, 100.0)
        assert exe.available is False
        assert exe.execution_vwap is None
        assert exe.slippage is None
        assert exe.unavailable_code == "insufficient_depth"

    def test_execution_vwap_and_slippage_correct(self):
        book = self._ok_book(
            bids=[{"price": "0.40", "size": "1"}],
            asks=[{"price": "0.50", "size": "100"}],
        )
        exe = allocate_buy(book, 10.0)
        assert exe.available is True
        assert exe.execution_vwap == pytest.approx(0.50)
        assert exe.midpoint == pytest.approx(0.45)
        assert exe.slippage == pytest.approx((0.50 - 0.45) / 0.45)

    def test_spread_and_slippage_remain_distinct(self):
        book = self._ok_book(
            bids=[{"price": "0.40", "size": "1"}],
            asks=[
                {"price": "0.50", "size": "2"},
                {"price": "0.60", "size": "100"},
            ],
        )
        exe = allocate_buy(book, 5.0)  # walks into second level
        assert exe.available is True
        assert exe.spread == pytest.approx(0.10)  # ask−bid only
        assert exe.slippage is not None
        assert exe.slippage != pytest.approx(exe.spread)
        # Slippage includes walking the book beyond top-of-book ask.
        assert exe.execution_vwap is not None
        assert exe.execution_vwap > book.best_ask or exe.execution_vwap == book.best_ask

    def test_min_order_size_failure_rejected(self):
        book = self._ok_book(
            asks=[{"price": "0.50", "size": "100"}],
            min_order_size="10",
        )
        exe = allocate_buy(book, 1.0)  # 2 shares < 10
        assert exe.available is False
        assert exe.unavailable_code == "below_min_order_size"
        assert exe.execution_vwap is None

    def test_allocation_zero_negative_nonfinite_rejected(self):
        book = self._ok_book()
        for bad in (0.0, -1.0, float("nan"), float("inf")):
            exe = allocate_buy(book, bad)
            assert exe.available is False
            assert exe.unavailable_code == "invalid_allocation"
            assert exe.execution_vwap is None
            assert exe.slippage is None

    def test_no_partial_fill_result_accepted(self):
        book = self._ok_book(asks=[{"price": "0.50", "size": "10"}])
        exe = allocate_buy(book, book.visible_ask_depth_usd + 0.01)
        assert exe.available is False
        assert exe.filled_shares is None
        assert exe.execution_vwap is None

    def test_one_snapshot_supplies_all_derived_fields(self):
        book = self._ok_book(
            bids=[{"price": "0.40", "size": "5"}],
            asks=[
                {"price": "0.50", "size": "10"},
                {"price": "0.55", "size": "10"},
            ],
        )
        exe = allocate_buy(book, 7.0)
        assert exe.available is True
        # All fields come from the same ValidatedOrderBook instance.
        assert exe.book is book
        assert exe.best_bid == book.best_bid
        assert exe.best_ask == book.best_ask
        assert exe.midpoint == book.midpoint
        assert exe.spread == book.spread
        assert exe.visible_ask_depth_usd == book.visible_ask_depth_usd
        assert exe.quote_age_seconds == book.quote_age_seconds
        assert exe.execution_vwap is not None
        assert exe.slippage is not None


class TestFetchValidatedOrderBook:
    def test_single_book_request_no_midpoint(self, settings, monkeypatch):
        calls: list[str] = []
        received = {"t": 1000.0}

        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            payload = _book(timestamp=990.0)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = payload
            resp.request_count = 1
            return resp

        monkeypatch.setattr("app.copy_intel.data_client.get_with_retry", fake_get)
        monkeypatch.setattr("app.copy_intel.data_client.time.time", lambda: received["t"])
        result = CopyDataClient(settings).fetch_validated_order_book(ASSET)
        assert isinstance(result, ApiSuccess)
        assert len(calls) == 1
        assert calls[0].endswith("/book")
        assert all("/midpoint" not in url for url in calls)
        assert result.data.best_bid == pytest.approx(0.40)
        assert result.data.midpoint == pytest.approx(0.45)

    def test_invalid_json_returns_failure(self, settings, monkeypatch):
        def fake_get(url, params=None, timeout=None):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.side_effect = ValueError("bad")
            resp.request_count = 1
            return resp

        monkeypatch.setattr("app.copy_intel.data_client.get_with_retry", fake_get)
        result = CopyDataClient(settings).fetch_validated_order_book(ASSET)
        assert isinstance(result, ApiFailure)
        assert result.code == "invalid_json"
