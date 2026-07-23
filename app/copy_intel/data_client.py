"""Read-only Polymarket Data API, Gamma, and CLOB clients for copy intel."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from app.config.settings import Settings
from app.copy_intel.api_result import ApiFailure, ApiResult, ApiSuccess, unwrap_data_list
from app.copy_intel.models import MarketQuote
from app.copy_intel.order_book import ValidatedOrderBook, parse_validated_order_book
from app.http.retry import get_with_retry, is_retryable_status

logger = logging.getLogger(__name__)

_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Documented trade identity uses transaction hash fields only (not id / tradeId).
_TX_HASH_KEYS = ("transactionHash", "transaction_hash", "txHash")

# Sanitized trade row keys passed to callers (documented / used fields only).
_TRADE_PASSTHROUGH_KEYS = frozenset(
    {
        "transactionHash",
        "transaction_hash",
        "txHash",
        "conditionId",
        "asset",
        "outcome",
        "side",
        "size",
        "price",
        "timestamp",
        "title",
        "slug",
        "type",
        "proxyWallet",
    }
)

_POSITION_REQUIRED_KEYS = ("conditionId", "asset")
_ACTIVITY_REQUIRED_KEYS = ("type",)


def normalize_wallet(address: str) -> str:
    cleaned = address.strip().lower()
    if not _WALLET_RE.match(cleaned):
        raise ValueError("Invalid wallet address; expected 0x-prefixed 40-hex proxy wallet.")
    return cleaned


def is_trade_activity(activity_type: str | None) -> bool:
    """Allowlist: only exact activity type TRADE counts as trade activity."""
    if not activity_type:
        return False
    return str(activity_type).upper() == "TRADE"


def _failure(
    *,
    code: str,
    status_code: int | None,
    retryable: bool,
    request_count: int,
    message: str,
) -> ApiFailure:
    return ApiFailure(
        code=code,
        status_code=status_code,
        retryable=retryable,
        request_count=max(1, int(request_count)),
        message=message,
    )


def _tx_hash_from_raw(raw: dict[str, Any]) -> str | None:
    for key in _TX_HASH_KEYS:
        value = raw.get(key)
        if value:
            return str(value)
    return None


def _sanitize_trade_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Pass through documented fields only; drop undocumented id / tradeId."""
    return {key: raw[key] for key in _TRADE_PASSTHROUGH_KEYS if key in raw}


def _validate_trade_row(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return "trade_row_not_object"
    if _tx_hash_from_raw(raw) is None:
        return "trade_missing_transaction_hash"
    side = (raw.get("side") or "").upper()
    if side and side not in {"BUY", "SELL"}:
        return "trade_invalid_side"
    return None


def _validate_position_row(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return "position_row_not_object"
    if not raw.get("conditionId"):
        return "position_missing_condition_id"
    asset = raw.get("asset")
    if asset is None or str(asset).strip() == "":
        return "position_missing_asset"
    size_raw = raw.get("size")
    if size_raw is not None:
        try:
            size_val = float(size_raw)
        except (TypeError, ValueError):
            return "position_invalid_size"
        if size_val != size_val or size_val in (float("inf"), float("-inf")) or size_val < 0:
            return "position_non_finite_or_negative_size"
    return None


def _validate_activity_row(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return "activity_row_not_object"
    for key in _ACTIVITY_REQUIRED_KEYS:
        if raw.get(key) is None or raw.get(key) == "":
            return "activity_missing_type"
    return None


def _validate_closed_row(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return "closed_row_not_object"
    if not raw.get("conditionId"):
        return "closed_missing_condition_id"
    return None


class CopyDataClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def fetch_positions(
        self,
        wallet: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> ApiResult[list[dict[str, Any]]]:
        extra: dict[str, Any] | None = None
        if offset:
            extra = {"offset": max(0, int(offset))}
        return self._data_get(
            "/positions",
            wallet,
            limit=limit,
            extra_params=extra,
            row_validator=_validate_position_row,
        )

    def fetch_trades(
        self,
        wallet: str,
        *,
        limit: int = 100,
        offset: int = 0,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> ApiResult[list[dict[str, Any]]]:
        extra: dict[str, Any] = {"takerOnly": "false"}
        if offset:
            extra["offset"] = max(0, int(offset))
        if start_ts is not None:
            extra["start"] = int(start_ts)
        if end_ts is not None:
            extra["end"] = int(end_ts)
        return self._data_get(
            "/trades",
            wallet,
            limit=limit,
            extra_params=extra,
            row_validator=_validate_trade_row,
            row_transform=_sanitize_trade_row,
        )

    def fetch_activity(self, wallet: str, *, limit: int = 100) -> ApiResult[list[dict[str, Any]]]:
        return self._data_get(
            "/activity",
            wallet,
            limit=limit,
            row_validator=_validate_activity_row,
        )

    def fetch_closed_positions(self, wallet: str, *, limit: int = 200) -> ApiResult[list[dict[str, Any]]]:
        return self._data_get(
            "/closed-positions",
            wallet,
            limit=limit,
            row_validator=_validate_closed_row,
        )

    def fetch_portfolio_value(self, wallet: str) -> float | None:
        result = self._data_get("/value", wallet, limit=1, row_validator=None)
        if isinstance(result, ApiFailure):
            return None
        data = result.data
        if not data:
            return None
        item = data[0] if isinstance(data, list) else data
        if not isinstance(item, dict):
            return None
        value = item.get("value")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def fetch_market_by_condition(self, condition_id: str) -> dict[str, Any] | None:
        url = f"{self._settings.polymarket_gamma_base_url}/markets"
        try:
            response = get_with_retry(url, params={"condition_ids": condition_id}, timeout=20)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            logger.warning("Gamma market lookup timeout/connection for condition")
            return None
        if response.status_code != 200:
            logger.warning("Gamma market lookup failed: status=%s", response.status_code)
            return None
        try:
            items = response.json()
        except ValueError:
            logger.warning("Gamma market lookup returned invalid JSON")
            return None
        if not isinstance(items, list) or not items:
            return None
        first = items[0]
        return first if isinstance(first, dict) else None

    def fetch_leaderboard(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        time_period: str | None = None,
        order_by: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch public leaderboard rows (paginated)."""
        url = f"{self._settings.polymarket_data_base_url.rstrip('/')}/v1/leaderboard"
        params: dict[str, Any] = {"limit": max(1, limit), "offset": max(0, offset)}
        if time_period:
            params["timePeriod"] = time_period
        if order_by:
            params["orderBy"] = order_by
        if category:
            params["category"] = category
        try:
            response = get_with_retry(url, params=params, timeout=30)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            logger.warning("Leaderboard fetch timeout/connection error")
            return []
        if response.status_code != 200:
            logger.warning("Leaderboard fetch failed: status=%s", response.status_code)
            return []
        try:
            payload = response.json()
        except ValueError:
            logger.warning("Leaderboard fetch returned invalid JSON")
            return []
        return payload if isinstance(payload, list) else []

    def fetch_leaderboard_pages(
        self,
        *,
        page_size: int = 25,
        max_pages: int = 3,
        time_period: str | None = None,
        order_by: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(max_pages):
            batch = self.fetch_leaderboard(
                limit=page_size,
                offset=page * page_size,
                time_period=time_period,
                order_by=order_by,
                category=category,
            )
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < page_size:
                break
        return rows

    def fetch_quote(self, token_id: str) -> MarketQuote:
        """Legacy quote helper (midpoint + book). Prefer ``fetch_validated_order_book``."""
        base = self._settings.polymarket_clob_base_url.rstrip("/")
        midpoint = self._clob_mid_price(f"{base}/midpoint", token_id)
        book = self._clob_book(f"{base}/book", token_id)
        best_bid = book.get("best_bid")
        best_ask = book.get("best_ask")
        return MarketQuote(
            token_id=token_id,
            midpoint=midpoint,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_depth_usd=book.get("bid_depth", 0.0),
            ask_depth_usd=book.get("ask_depth", 0.0),
        )

    def fetch_validated_order_book(
        self, token_id: str
    ) -> ApiResult[ValidatedOrderBook]:
        """Fetch one CLOB /book snapshot and validate fail-closed.

        Midpoint, depth, and later allocation metrics must use this single snapshot.
        Does not call /midpoint and does not refetch on stale.
        """
        base = self._settings.polymarket_clob_base_url.rstrip("/")
        url = f"{base}/book"
        request_count = 1
        try:
            response = get_with_retry(url, params={"token_id": token_id}, timeout=15)
            request_count = max(1, int(getattr(response, "request_count", 1) or 1))
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.RequestException,
        ):
            logger.warning("CLOB book request failed for validated snapshot")
            return ApiFailure(
                code="request_error",
                status_code=None,
                retryable=True,
                request_count=request_count,
                message="book request failed",
            )
        received_at = time.time()
        status = int(response.status_code)
        if status != 200:
            logger.warning("CLOB book failed: status=%s", status)
            return ApiFailure(
                code="http_error",
                status_code=status,
                retryable=is_retryable_status(status),
                request_count=request_count,
                message=f"http status {status}",
            )
        try:
            payload = response.json()
        except ValueError:
            logger.warning("CLOB book invalid JSON")
            return ApiFailure(
                code="invalid_json",
                status_code=status,
                retryable=False,
                request_count=request_count,
                message="invalid json body",
            )
        return parse_validated_order_book(
            payload,
            requested_asset=token_id,
            received_at=received_at,
            request_count=request_count,
        )

    def _data_get(
        self,
        path: str,
        wallet: str,
        *,
        limit: int,
        extra_params: dict[str, Any] | None = None,
        row_validator=None,
        row_transform=None,
    ) -> ApiResult[list[dict[str, Any]]]:
        wallet = normalize_wallet(wallet)
        url = f"{self._settings.polymarket_data_base_url.rstrip('/')}{path}"
        params: dict[str, Any] = {"user": wallet, "limit": limit}
        if extra_params:
            params.update(extra_params)

        request_count = 1
        try:
            response = get_with_retry(url, params=params, timeout=30)
            # get_with_retry may perform multiple attempts; surface at least 1.
            request_count = max(1, int(getattr(response, "request_count", 1) or 1))
        except requests.exceptions.Timeout:
            logger.warning("Data API %s timeout for %s", path, wallet[:10])
            return _failure(
                code="timeout",
                status_code=None,
                retryable=True,
                request_count=request_count,
                message="request timed out",
            )
        except requests.exceptions.ConnectionError:
            logger.warning("Data API %s connection error for %s", path, wallet[:10])
            return _failure(
                code="connection_error",
                status_code=None,
                retryable=True,
                request_count=request_count,
                message="connection error",
            )
        except requests.exceptions.RequestException:
            logger.warning("Data API %s request error for %s", path, wallet[:10])
            return _failure(
                code="request_error",
                status_code=None,
                retryable=True,
                request_count=request_count,
                message="request failed",
            )

        status = int(response.status_code)
        if status != 200:
            # Never log or persist response bodies.
            logger.warning(
                "Data API %s failed for %s: status=%s",
                path,
                wallet[:10],
                status,
            )
            return _failure(
                code="http_error",
                status_code=status,
                retryable=is_retryable_status(status),
                request_count=request_count,
                message=f"http status {status}",
            )

        try:
            payload = response.json()
        except ValueError:
            logger.warning("Data API %s invalid JSON for %s", path, wallet[:10])
            return _failure(
                code="invalid_json",
                status_code=status,
                retryable=False,
                request_count=request_count,
                message="invalid json body",
            )

        if not isinstance(payload, list):
            logger.warning("Data API %s wrong top-level type for %s", path, wallet[:10])
            return _failure(
                code="wrong_type",
                status_code=status,
                retryable=False,
                request_count=request_count,
                message="expected top-level list",
            )

        rows: list[dict[str, Any]] = []
        for item in payload:
            if row_validator is not None:
                reason = row_validator(item)
                if reason is not None:
                    logger.warning(
                        "Data API %s malformed row for %s: %s",
                        path,
                        wallet[:10],
                        reason,
                    )
                    return _failure(
                        code="malformed_row",
                        status_code=status,
                        retryable=False,
                        request_count=request_count,
                        message=reason,
                    )
            assert isinstance(item, dict)
            if row_transform is not None:
                rows.append(row_transform(item))
            else:
                rows.append(item)

        return ApiSuccess(data=rows, request_count=request_count)

    def _clob_mid_price(self, url: str, token_id: str) -> float | None:
        try:
            response = get_with_retry(url, params={"token_id": token_id}, timeout=15)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.RequestException):
            logger.warning("CLOB midpoint request failed")
            return None
        if response.status_code != 200:
            logger.warning("CLOB midpoint failed: status=%s", response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            logger.warning("CLOB midpoint invalid JSON")
            return None
        if not isinstance(data, dict):
            logger.warning("CLOB midpoint wrong top-level type")
            return None
        # Documented CLOB midpoint field.
        price = data.get("mid_price")
        if price is None:
            return None
        try:
            return float(price)
        except (TypeError, ValueError):
            return None

    def _clob_book(self, url: str, token_id: str) -> dict[str, float | None]:
        empty = {"best_bid": None, "best_ask": None, "bid_depth": 0.0, "ask_depth": 0.0}
        try:
            response = get_with_retry(url, params={"token_id": token_id}, timeout=15)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.RequestException):
            logger.warning("CLOB book request failed")
            return empty
        if response.status_code != 200:
            return empty
        try:
            data = response.json()
        except ValueError:
            logger.warning("CLOB book invalid JSON")
            return empty
        if not isinstance(data, dict):
            return empty
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not isinstance(bids, list):
            bids = []
        if not isinstance(asks, list):
            asks = []
        best_bid = float(bids[0]["price"]) if bids and isinstance(bids[0], dict) and "price" in bids[0] else None
        best_ask = float(asks[0]["price"]) if asks and isinstance(asks[0], dict) and "price" in asks[0] else None
        bid_depth = sum(float(b["price"]) * float(b["size"]) for b in bids[:5] if isinstance(b, dict))
        ask_depth = sum(float(a["price"]) * float(a["size"]) for a in asks[:5] if isinstance(a, dict))
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
        }


def build_dedup_key(
    wallet: str,
    *,
    tx_hash: str | None,
    asset: str | None,
    side: str | None,
    size: float | None,
    ts: int | None,
    event_type: str,
) -> str:
    return "|".join(
        [
            wallet.lower(),
            event_type,
            tx_hash or "",
            asset or "",
            side or "",
            f"{size:.6f}" if size is not None else "",
            str(ts or ""),
        ]
    )


# Re-export for callers/tests.
__all__ = [
    "CopyDataClient",
    "build_dedup_key",
    "is_trade_activity",
    "normalize_wallet",
    "unwrap_data_list",
]
