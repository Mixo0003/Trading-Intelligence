from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import requests

from app.opportunity_intel.discovery import ResultStatus
from app.opportunity_intel.history import (
    FreshnessStatus,
    HistoryDirection,
    PublicPriceHistoryClient,
    select_movement,
)
from app.opportunity_intel.models import (
    ObservationMode,
    OpportunityDiagnosticCode,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
LATEST = NOW - timedelta(seconds=30)
_DEFAULT_PAYLOAD = object()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, json_error=False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        if self._json_error:
            raise ValueError("invalid json")
        return self._payload


def _points(
    *,
    latest_at: datetime = LATEST,
    prices=("0.45", "0.50", "0.55", "0.60"),
):
    offsets = (3600, 900, 300, 0)
    return [
        {
            "t": int((latest_at - timedelta(seconds=offset)).timestamp()),
            "p": price,
        }
        for offset, price in zip(offsets, prices, strict=True)
    ]


def _client_with_payload(payload, status=200):
    calls = []

    def request_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(status, payload)

    return (
        PublicPriceHistoryClient(
            base_url="https://clob.polymarket.com",
            request_get=request_get,
        ),
        calls,
    )


def _read(payload=_DEFAULT_PAYLOAD):
    client, _ = _client_with_payload(
        {"history": _points()} if payload is _DEFAULT_PAYLOAD else payload
    )
    return client.fetch(
        "asset-1",
        start_at=NOW - timedelta(hours=2),
        end_at=NOW,
    )


def test_public_history_request_is_bounded_and_has_no_authentication():
    client, calls = _client_with_payload({"history": _points()})
    result = client.fetch(
        "asset-1",
        start_at=NOW - timedelta(hours=2),
        end_at=NOW,
    )
    assert result.status == ResultStatus.COMPLETE
    url, kwargs = calls[0]
    assert url == "https://clob.polymarket.com/prices-history"
    assert kwargs["params"] == {
        "market": "asset-1",
        "startTs": int((NOW - timedelta(hours=2)).timestamp()),
        "endTs": int(NOW.timestamp()),
        "fidelity": 1,
    }
    assert kwargs["timeout"] == 15
    assert "Authorization" not in kwargs["headers"]


def test_exact_normal_5m_15m_1h_selection_and_signed_changes():
    movement = select_movement(
        _read(),
        mode=ObservationMode.NORMAL,
        now=NOW,
    )
    assert movement.status == ResultStatus.COMPLETE
    assert movement.freshness == FreshnessStatus.FRESH
    assert movement.latest_price == Decimal("0.60")
    assert movement.price_5m == Decimal("0.55")
    assert movement.price_15m == Decimal("0.50")
    assert movement.price_1h == Decimal("0.45")
    assert movement.change_5m_pp == Decimal("5.00")
    assert movement.change_15m_pp == Decimal("10.00")
    assert movement.change_1h_pp == Decimal("15.00")
    assert movement.absolute_change_1h_pp == Decimal("15.00")
    assert movement.direction == HistoryDirection.UP


def test_exact_strict_live_selection():
    movement = select_movement(
        _read(),
        mode=ObservationMode.LIVE_STRICT,
        now=NOW,
    )
    assert movement.status == ResultStatus.COMPLETE
    assert movement.freshness == FreshnessStatus.FRESH
    assert movement.price_5m == Decimal("0.55")
    assert movement.price_15m == Decimal("0.50")
    assert movement.price_1h == Decimal("0.45")


def test_normal_tolerance_accepts_point_strict_live_rejects_same_point():
    points = _points()
    points[1]["t"] += 150  # 15m target +2m30s: normal ±3m, live ±2m.
    history = _read({"history": points})
    normal = select_movement(history, mode=ObservationMode.NORMAL, now=NOW)
    strict = select_movement(history, mode=ObservationMode.LIVE_STRICT, now=NOW)
    assert normal.status == ResultStatus.COMPLETE
    assert strict.status == ResultStatus.RETRYABLE_FAILURE
    assert strict.diagnostic_code == OpportunityDiagnosticCode.HISTORY_TARGET_MISSING


def test_missing_target_point_is_isolated_failure():
    points = _points()
    del points[1]
    movement = select_movement(
        _read({"history": points}),
        mode=ObservationMode.NORMAL,
        now=NOW,
    )
    assert movement.status == ResultStatus.RETRYABLE_FAILURE
    assert movement.freshness == FreshnessStatus.INCOMPLETE
    assert movement.diagnostic_code == OpportunityDiagnosticCode.HISTORY_TARGET_MISSING


@pytest.mark.parametrize(
    ("mode", "latest_age_seconds"),
    [
        (ObservationMode.NORMAL, 121),
        (ObservationMode.LIVE_STRICT, 61),
    ],
)
def test_stale_latest_point_rejected(mode, latest_age_seconds):
    latest = NOW - timedelta(seconds=latest_age_seconds)
    movement = select_movement(
        _read({"history": _points(latest_at=latest)}),
        mode=mode,
        now=NOW,
    )
    assert movement.status == ResultStatus.RETRYABLE_FAILURE
    assert movement.freshness == FreshnessStatus.STALE
    assert movement.diagnostic_code == OpportunityDiagnosticCode.HISTORY_LATEST_STALE


def test_duplicate_identical_timestamp_is_deduplicated():
    points = _points()
    points.insert(1, dict(points[0]))
    result = _read({"history": points})
    assert result.status == ResultStatus.COMPLETE
    assert len(result.points) == 4


def test_duplicate_conflicting_timestamp_is_rejected():
    points = _points()
    conflict = dict(points[0])
    conflict["p"] = "0.46"
    points.append(conflict)
    result = _read({"history": points})
    assert result.status == ResultStatus.PERMANENT_FAILURE
    assert result.diagnostic_code == OpportunityDiagnosticCode.HISTORY_POINT_INVALID


def test_reordered_points_are_sorted_deterministically():
    points = _points()
    result = _read({"history": [points[3], points[1], points[0], points[2]]})
    assert result.status == ResultStatus.COMPLETE
    assert list(result.points) == sorted(result.points, key=lambda point: point.at)
    movement = select_movement(result, mode=ObservationMode.NORMAL, now=NOW)
    assert movement.change_1h_pp == Decimal("15.00")


@pytest.mark.parametrize(
    "bad_point",
    [
        {"t": "not-a-time", "p": "0.5"},
        {"t": 1.5, "p": "0.5"},
        {"t": -1, "p": "0.5"},
        {"t": int(LATEST.timestamp()), "p": "NaN"},
        {"t": int(LATEST.timestamp()), "p": "1.01"},
        {"t": int(LATEST.timestamp()), "p": "-0.01"},
        {"t": int(LATEST.timestamp()), "p": True},
        "not-an-object",
    ],
)
def test_malformed_timestamp_or_price_rejected(bad_point):
    result = _read({"history": [bad_point]})
    assert result.status == ResultStatus.PERMANENT_FAILURE
    assert result.points == ()
    assert result.diagnostic_code == OpportunityDiagnosticCode.HISTORY_POINT_INVALID


def test_down_direction_and_decimal_precision_are_preserved():
    precise = ("0.600000009", "0.600000007", "0.600000003", "0.123456789")
    movement = select_movement(
        _read({"history": _points(prices=precise)}),
        mode=ObservationMode.NORMAL,
        now=NOW,
    )
    assert movement.latest_price == Decimal("0.123456789")
    assert movement.direction == HistoryDirection.DOWN
    assert movement.change_5m_pp == Decimal("-47.654321400")


@pytest.mark.parametrize(
    ("status_code", "expected_status", "category"),
    [
        (429, ResultStatus.RETRYABLE_FAILURE, "retryable_http"),
        (503, ResultStatus.RETRYABLE_FAILURE, "retryable_http"),
        (400, ResultStatus.PERMANENT_FAILURE, "permanent_http"),
        (403, ResultStatus.PERMANENT_FAILURE, "permanent_http"),
    ],
)
def test_retryable_versus_permanent_http_failure(
    status_code, expected_status, category
):
    client, _ = _client_with_payload({}, status=status_code)
    result = client.fetch(
        "asset-1",
        start_at=NOW - timedelta(hours=2),
        end_at=NOW,
    )
    assert result.status == expected_status
    assert result.diagnostic_code == OpportunityDiagnosticCode.HISTORY_REQUEST_FAILED
    assert result.http_status_category == category


def test_network_timeout_is_retryable():
    def timeout(*_args, **_kwargs):
        raise requests.exceptions.ReadTimeout("timeout")

    client = PublicPriceHistoryClient(
        base_url="https://clob.polymarket.com",
        request_get=timeout,
    )
    result = client.fetch(
        "asset-1",
        start_at=NOW - timedelta(hours=2),
        end_at=NOW,
    )
    assert result.status == ResultStatus.RETRYABLE_FAILURE
    assert result.http_status_category == "network"


def test_batch_partial_failure_isolated_per_asset():
    def request_get(_url, **kwargs):
        asset = kwargs["params"]["market"]
        if asset == "asset-bad":
            return FakeResponse(503, {})
        return FakeResponse(200, {"history": _points()})

    client = PublicPriceHistoryClient(
        base_url="https://clob.polymarket.com",
        request_get=request_get,
    )
    results = client.fetch_many(
        ["asset-good", "asset-bad", "asset-good-2"],
        start_at=NOW - timedelta(hours=2),
        end_at=NOW,
    )
    assert results["asset-good"].status == ResultStatus.COMPLETE
    assert results["asset-bad"].status == ResultStatus.RETRYABLE_FAILURE
    assert results["asset-good-2"].status == ResultStatus.COMPLETE


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"history": "not-a-list"},
    ],
)
def test_malformed_response_is_permanent(payload):
    result = _read(payload)
    assert result.status == ResultStatus.PERMANENT_FAILURE
    assert result.diagnostic_code == OpportunityDiagnosticCode.HISTORY_RESPONSE_INVALID


def test_empty_history_is_retryable_and_history_alone_creates_no_signal():
    result = _read({"history": []})
    assert result.status == ResultStatus.RETRYABLE_FAILURE
    assert result.points == ()
    assert result.diagnostic_code == OpportunityDiagnosticCode.HISTORY_MISSING
    movement = select_movement(
        result,
        mode=ObservationMode.NORMAL,
        now=NOW,
    )
    assert movement.status == ResultStatus.RETRYABLE_FAILURE
    assert not hasattr(movement, "opportunity_uid")
