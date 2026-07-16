"""Read-only Polymarket Data API, Gamma, and CLOB clients for copy intel."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config.settings import Settings
from app.copy_intel.models import MarketQuote
from app.http.retry import get_with_retry

logger = logging.getLogger(__name__)

_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_NON_TRADE_ACTIVITY = frozenset(
    {"DEPOSIT", "WITHDRAW", "TRANSFER", "SPLIT", "MERGE", "REDEEM", "REWARD"}
)


def normalize_wallet(address: str) -> str:
    cleaned = address.strip().lower()
    if not _WALLET_RE.match(cleaned):
        raise ValueError("Invalid wallet address; expected 0x-prefixed 40-hex proxy wallet.")
    return cleaned


def is_trade_activity(activity_type: str | None) -> bool:
    if not activity_type:
        return False
    return activity_type.upper() not in _NON_TRADE_ACTIVITY


class CopyDataClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def fetch_positions(self, wallet: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._data_get("/positions", wallet, limit=limit)

    def fetch_trades(self, wallet: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._data_get("/trades", wallet, limit=limit)

    def fetch_activity(self, wallet: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._data_get("/activity", wallet, limit=limit)

    def fetch_closed_positions(self, wallet: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._data_get("/closed-positions", wallet, limit=limit)

    def fetch_portfolio_value(self, wallet: str) -> float | None:
        data = self._data_get("/value", wallet, limit=1)
        if not data:
            return None
        item = data[0] if isinstance(data, list) else data
        value = item.get("value")
        return float(value) if value is not None else None

    def fetch_market_by_condition(self, condition_id: str) -> dict[str, Any] | None:
        url = f"{self._settings.polymarket_gamma_base_url}/markets"
        response = get_with_retry(url, params={"condition_ids": condition_id}, timeout=20)
        if response.status_code != 200:
            logger.warning("Gamma market lookup failed for %s: %s", condition_id, response.status_code)
            return None
        items = response.json()
        if not items:
            return None
        return items[0]

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
        response = get_with_retry(url, params=params, timeout=30)
        if response.status_code != 200:
            logger.warning("Leaderboard fetch failed: %s", response.status_code)
            return []
        payload = response.json()
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
        base = self._settings.polymarket_clob_base_url.rstrip("/")
        midpoint = self._clob_price(f"{base}/midpoint", token_id)
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

    def _data_get(self, path: str, wallet: str, *, limit: int) -> list[dict[str, Any]]:
        wallet = normalize_wallet(wallet)
        url = f"{self._settings.polymarket_data_base_url.rstrip('/')}{path}"
        response = get_with_retry(url, params={"user": wallet, "limit": limit}, timeout=30)
        if response.status_code != 200:
            logger.warning("Data API %s failed for %s: %s", path, wallet[:10], response.status_code)
            return []
        payload = response.json()
        if isinstance(payload, list):
            return payload
        return []

    def _clob_price(self, url: str, token_id: str) -> float | None:
        response = get_with_retry(url, params={"token_id": token_id}, timeout=15)
        if response.status_code != 200:
            return None
        data = response.json()
        price = data.get("mid") or data.get("price")
        return float(price) if price is not None else None

    def _clob_book(self, url: str, token_id: str) -> dict[str, float | None]:
        response = get_with_retry(url, params={"token_id": token_id}, timeout=15)
        if response.status_code != 200:
            return {"best_bid": None, "best_ask": None, "bid_depth": 0.0, "ask_depth": 0.0}
        data = response.json()
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        bid_depth = sum(float(b["price"]) * float(b["size"]) for b in bids[:5])
        ask_depth = sum(float(a["price"]) * float(a["size"]) for a in asks[:5])
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
