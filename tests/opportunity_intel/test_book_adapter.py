"""V2.2.3 Checkpoint 3 — Opportunity public book adapter over V2.2.2 validator."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.copy_intel.api_result import ApiSuccess
from app.copy_intel.order_book import parse_validated_order_book
from app.opportunity_intel.book import (
    BookAdapterFailure,
    BookAdapterSuccess,
    PublicOrderBookClient,
    adapt_validated_book,
    map_book_failure_code,
    max_book_age_seconds,
    validate_public_book_payload,
)
from app.opportunity_intel.discovery import ResultStatus
from app.opportunity_intel.models import ObservationMode, OpportunityDiagnosticCode

ASSET = "token-yes-opp-1"
OTHER = "token-no-opp-2"


def _payload(
    *,
    asset_id: str = ASSET,
    timestamp: float = 990.0,
    bids=None,
    asks=None,
    include_bids: bool = True,
    include_asks: bool = True,
):
    body: dict = {
        "asset_id": asset_id,
        "timestamp": timestamp,
        "hash": "hash-opp-1",
    }
    if include_bids:
        body["bids"] = bids if bids is not None else [{"price": "0.48", "size": "200"}]
    if include_asks:
        body["asks"] = asks if asks is not None else [{"price": "0.52", "size": "2000"}]
    return body


def test_successful_reuse_of_existing_validator():
    payload = _payload()
    direct = parse_validated_order_book(
        payload, requested_asset=ASSET, received_at=1000.0
    )
    adapted = validate_public_book_payload(
        payload,
        requested_asset=ASSET,
        mode=ObservationMode.NORMAL,
        received_at=1000.0,
    )
    assert isinstance(direct, ApiSuccess)
    assert isinstance(adapted, BookAdapterSuccess)
    assert adapted.book.book_hash == direct.data.book_hash
    assert float(adapted.midpoint) == pytest.approx(direct.data.midpoint)
    assert adapted.absolute_spread == adapted.best_ask - adapted.best_bid
    assert adapted.relative_spread == adapted.absolute_spread / adapted.midpoint


def test_asset_mismatch():
    result = validate_public_book_payload(
        _payload(asset_id=OTHER),
        requested_asset=ASSET,
        mode=ObservationMode.NORMAL,
        received_at=1000.0,
    )
    assert isinstance(result, BookAdapterFailure)
    assert result.diagnostic_code == OpportunityDiagnosticCode.BOOK_ASSET_MISMATCH


def test_empty_book_missing_sides():
    result = validate_public_book_payload(
        _payload(include_bids=False, include_asks=False),
        requested_asset=ASSET,
        mode=ObservationMode.NORMAL,
        received_at=1000.0,
    )
    assert isinstance(result, BookAdapterFailure)
    assert result.diagnostic_code == OpportunityDiagnosticCode.BOOK_EMPTY


def test_empty_book_zero_levels_not_future_timestamp():
    result = validate_public_book_payload(
        _payload(bids=[], asks=[], timestamp=1785191932003),
        requested_asset=ASSET,
        mode=ObservationMode.NORMAL,
        received_at=1000.0,
    )
    assert isinstance(result, BookAdapterFailure)
    assert result.source_code == "empty_book"
    assert result.diagnostic_code == OpportunityDiagnosticCode.BOOK_EMPTY


def test_one_sided_book():
    result = validate_public_book_payload(
        _payload(asks=[]),
        requested_asset=ASSET,
        mode=ObservationMode.NORMAL,
        received_at=1000.0,
    )
    assert isinstance(result, BookAdapterFailure)
    assert result.diagnostic_code == OpportunityDiagnosticCode.BOOK_ONE_SIDED


def test_crossed_book():
    result = validate_public_book_payload(
        _payload(
            bids=[{"price": "0.60", "size": "10"}],
            asks=[{"price": "0.50", "size": "10"}],
        ),
        requested_asset=ASSET,
        mode=ObservationMode.NORMAL,
        received_at=1000.0,
    )
    assert isinstance(result, BookAdapterFailure)
    assert result.diagnostic_code == OpportunityDiagnosticCode.BOOK_CROSSED


def test_malformed_prices_sizes():
    result = validate_public_book_payload(
        _payload(asks=[{"price": "bad", "size": "10"}]),
        requested_asset=ASSET,
        mode=ObservationMode.NORMAL,
        received_at=1000.0,
    )
    assert isinstance(result, BookAdapterFailure)
    assert result.diagnostic_code == OpportunityDiagnosticCode.BOOK_INVALID


def test_stale_normal_book():
    # Age 31s exceeds normal 30s max.
    result = validate_public_book_payload(
        _payload(timestamp=969.0),
        requested_asset=ASSET,
        mode=ObservationMode.NORMAL,
        received_at=1000.0,
    )
    assert isinstance(result, BookAdapterFailure)
    assert result.diagnostic_code == OpportunityDiagnosticCode.BOOK_STALE


def test_stale_strict_live_book_accepted_by_copy_default_but_rejected_by_adapter():
    # Age 15s is OK for Copy default 30s, but STRICT live max is 10s.
    payload = _payload(timestamp=985.0)
    copy_default = parse_validated_order_book(
        payload, requested_asset=ASSET, received_at=1000.0
    )
    assert isinstance(copy_default, ApiSuccess)
    strict = validate_public_book_payload(
        payload,
        requested_asset=ASSET,
        mode=ObservationMode.LIVE_STRICT,
        received_at=1000.0,
    )
    assert isinstance(strict, BookAdapterFailure)
    assert strict.diagnostic_code == OpportunityDiagnosticCode.BOOK_STALE
    assert max_book_age_seconds(ObservationMode.LIVE_STRICT) == 10.0


def test_one_snapshot_used_for_all_metrics():
    result = validate_public_book_payload(
        _payload(),
        requested_asset=ASSET,
        mode=ObservationMode.NORMAL,
        received_at=1000.0,
    )
    assert isinstance(result, BookAdapterSuccess)
    book = result.book
    again = adapt_validated_book(
        book, requested_asset=ASSET, mode=ObservationMode.NORMAL
    )
    assert isinstance(again, BookAdapterSuccess)
    assert again.book is book
    assert again.best_bid == result.best_bid
    assert again.visible_ask_depth_usd == result.visible_ask_depth_usd


def test_public_client_uses_unauthenticated_book_endpoint():
    response = MagicMock()
    response.status_code = 200
    response.request_count = 1
    response.json.return_value = _payload(timestamp=995.0)

    def fake_get(url, **kwargs):
        assert url.endswith("/book")
        assert kwargs["params"] == {"token_id": ASSET}
        assert "Authorization" not in (kwargs.get("headers") or {})
        return response

    client = PublicOrderBookClient(
        base_url="https://clob.polymarket.com",
        request_get=fake_get,
    )
    result = client.fetch(
        ASSET, mode=ObservationMode.NORMAL, received_at=1000.0
    )
    assert isinstance(result, BookAdapterSuccess)
    assert result.status == ResultStatus.COMPLETE


def test_public_client_retries_small_future_timestamp():
    response = MagicMock()
    response.status_code = 200
    response.request_count = 1
    response.json.return_value = _payload(timestamp=1005.0)

    client = PublicOrderBookClient(
        base_url="https://clob.polymarket.com",
        request_get=lambda url, **kwargs: response,
    )
    result = client.fetch(
        ASSET, mode=ObservationMode.NORMAL, received_at=1000.0
    )
    assert isinstance(result, BookAdapterSuccess)
    assert result.book.best_ask == pytest.approx(0.52)


def test_public_client_rejects_large_future_timestamp():
    response = MagicMock()
    response.status_code = 200
    response.request_count = 1
    response.json.return_value = _payload(timestamp=1200.0)

    client = PublicOrderBookClient(
        base_url="https://clob.polymarket.com",
        request_get=lambda url, **kwargs: response,
    )
    result = client.fetch(
        ASSET, mode=ObservationMode.NORMAL, received_at=1000.0
    )
    assert isinstance(result, BookAdapterFailure)
    assert result.source_code == "future_timestamp"


def test_map_book_failure_codes():
    assert map_book_failure_code("asset_mismatch") == (
        OpportunityDiagnosticCode.BOOK_ASSET_MISMATCH
    )
    assert map_book_failure_code("crossed_or_locked_book") == (
        OpportunityDiagnosticCode.BOOK_CROSSED
    )
    assert map_book_failure_code("request_error") == (
        OpportunityDiagnosticCode.BOOK_UNAVAILABLE
    )
