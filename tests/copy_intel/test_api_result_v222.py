"""V2.2.2 Checkpoint 2 — typed API boundary and fail-closed CopyDataClient."""

from __future__ import annotations

import pytest
import requests

from app.copy_intel.api_result import ApiFailure, ApiSuccess, unwrap_data_list
from app.copy_intel.data_client import CopyDataClient, is_trade_activity
from tests.conftest import make_test_settings

WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN = "token-yes-111"


@pytest.fixture
def settings(tmp_path):
    return make_test_settings(database_path=str(tmp_path / "api.db"))


def _resp(status_code: int, payload, *, as_text: bool = False):
    class Resp:
        def __init__(self) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self):
            if as_text:
                raise ValueError("No JSON object could be decoded")
            if isinstance(self._payload, BaseException):
                raise self._payload
            return self._payload

    return Resp()


class TestApiResultModel:
    def test_success_empty_vs_non_empty(self):
        empty = ApiSuccess(data=[], request_count=1)
        filled = ApiSuccess(data=[{"conditionId": "c1"}], request_count=1)
        assert empty.ok is True
        assert empty.empty is True
        assert empty.non_empty is False
        assert filled.ok is True
        assert filled.empty is False
        assert filled.non_empty is True

    def test_failure_fields_are_sanitized_shape(self):
        failure = ApiFailure(
            code="http_error",
            status_code=503,
            retryable=True,
            request_count=3,
            message="http status 503",
        )
        assert failure.ok is False
        payload = failure.__dict__
        assert set(payload) == {"code", "status_code", "retryable", "request_count", "message"}
        assert "body" not in payload
        assert "raw" not in payload

    def test_unwrap_compat(self):
        assert unwrap_data_list(ApiSuccess(data=[{"a": 1}])) == [{"a": 1}]
        assert unwrap_data_list(ApiFailure("x", 500, True, 1, "m")) == []
        assert unwrap_data_list([{"a": 1}]) == [{"a": 1}]


class TestFailClosedDataGet:
    def test_non_200_returns_failure_not_empty_list(self, settings, monkeypatch):
        monkeypatch.setattr(
            "app.copy_intel.data_client.get_with_retry",
            lambda *a, **k: _resp(503, {"error": "no"}),
        )
        result = CopyDataClient(settings).fetch_positions(WALLET)
        assert isinstance(result, ApiFailure)
        assert result.code == "http_error"
        assert result.status_code == 503
        assert result.retryable is True
        assert result != []

    def test_timeout_returns_failure(self, settings, monkeypatch):
        def boom(*a, **k):
            raise requests.exceptions.Timeout("timed out")

        monkeypatch.setattr("app.copy_intel.data_client.get_with_retry", boom)
        result = CopyDataClient(settings).fetch_positions(WALLET)
        assert isinstance(result, ApiFailure)
        assert result.code == "timeout"
        assert result.retryable is True

    def test_invalid_json_returns_failure(self, settings, monkeypatch):
        monkeypatch.setattr(
            "app.copy_intel.data_client.get_with_retry",
            lambda *a, **k: _resp(200, None, as_text=True),
        )
        result = CopyDataClient(settings).fetch_positions(WALLET)
        assert isinstance(result, ApiFailure)
        assert result.code == "invalid_json"

    def test_wrong_top_level_type_returns_failure(self, settings, monkeypatch):
        monkeypatch.setattr(
            "app.copy_intel.data_client.get_with_retry",
            lambda *a, **k: _resp(200, {"items": []}),
        )
        result = CopyDataClient(settings).fetch_positions(WALLET)
        assert isinstance(result, ApiFailure)
        assert result.code == "wrong_type"

    def test_successful_empty_list(self, settings, monkeypatch):
        monkeypatch.setattr(
            "app.copy_intel.data_client.get_with_retry",
            lambda *a, **k: _resp(200, []),
        )
        result = CopyDataClient(settings).fetch_positions(WALLET)
        assert isinstance(result, ApiSuccess)
        assert result.empty is True
        assert result.data == []

    def test_malformed_position_row_fails_closed(self, settings, monkeypatch):
        monkeypatch.setattr(
            "app.copy_intel.data_client.get_with_retry",
            lambda *a, **k: _resp(200, [{"asset": "t1", "size": 1}]),
        )
        result = CopyDataClient(settings).fetch_positions(WALLET)
        assert isinstance(result, ApiFailure)
        assert result.code == "malformed_row"
        assert "condition" in result.message

    def test_trade_without_tx_hash_fails_closed_even_with_id(self, settings, monkeypatch):
        monkeypatch.setattr(
            "app.copy_intel.data_client.get_with_retry",
            lambda *a, **k: _resp(
                200,
                [
                    {
                        "id": "undocumented",
                        "tradeId": "also-undocumented",
                        "conditionId": "c1",
                        "side": "BUY",
                        "size": 1,
                    }
                ],
            ),
        )
        result = CopyDataClient(settings).fetch_trades(WALLET)
        assert isinstance(result, ApiFailure)
        assert result.code == "malformed_row"
        assert "transaction_hash" in result.message

    def test_valid_trade_strips_undocumented_ids(self, settings, monkeypatch):
        monkeypatch.setattr(
            "app.copy_intel.data_client.get_with_retry",
            lambda *a, **k: _resp(
                200,
                [
                    {
                        "id": "undocumented",
                        "tradeId": "also-undocumented",
                        "transactionHash": "0xabc",
                        "conditionId": "c1",
                        "side": "BUY",
                        "size": 1,
                        "price": 0.4,
                    }
                ],
            ),
        )
        result = CopyDataClient(settings).fetch_trades(WALLET)
        assert isinstance(result, ApiSuccess)
        assert result.non_empty is True
        row = result.data[0]
        assert "id" not in row
        assert "tradeId" not in row
        assert row["transactionHash"] == "0xabc"

    def test_no_raw_body_in_failure_or_logs_message(self, settings, monkeypatch, caplog):
        secret_body = {"error": "secret-token-should-not-leak", "raw": "BODY"}
        monkeypatch.setattr(
            "app.copy_intel.data_client.get_with_retry",
            lambda *a, **k: _resp(500, secret_body),
        )
        with caplog.at_level("WARNING"):
            result = CopyDataClient(settings).fetch_positions(WALLET)
        assert isinstance(result, ApiFailure)
        assert "secret-token" not in result.message
        assert "secret-token" not in caplog.text
        assert "BODY" not in caplog.text


class TestContracts:
    def test_trades_sends_taker_only_false(self, settings, monkeypatch):
        captured: list[dict] = []

        def fake_get(url, **kwargs):
            captured.append(dict(kwargs.get("params") or {}))
            return _resp(200, [])

        monkeypatch.setattr("app.copy_intel.data_client.get_with_retry", fake_get)
        result = CopyDataClient(settings).fetch_trades(WALLET, limit=25)
        assert isinstance(result, ApiSuccess)
        assert captured[0].get("takerOnly") == "false"
        assert captured[0].get("user") == WALLET
        assert captured[0].get("limit") == 25

    def test_midpoint_reads_mid_price(self, settings, monkeypatch):
        def fake_get(url, **kwargs):
            if url.rstrip("/").endswith("/midpoint"):
                return _resp(200, {"mid_price": "0.61"})
            return _resp(200, {"bids": [], "asks": []})

        monkeypatch.setattr("app.copy_intel.data_client.get_with_retry", fake_get)
        quote = CopyDataClient(settings).fetch_quote(TOKEN)
        assert quote.midpoint == pytest.approx(0.61)

    def test_activity_allowlist_trade_only(self):
        assert is_trade_activity("TRADE") is True
        assert is_trade_activity("trade") is True
        assert is_trade_activity("REDEEM") is False
        assert is_trade_activity("MAKER_REBATE") is False
        assert is_trade_activity("SOMETHING_NEW") is False
        assert is_trade_activity(None) is False
        assert is_trade_activity("") is False
