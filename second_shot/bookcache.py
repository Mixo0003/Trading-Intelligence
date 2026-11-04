"""In-memory Polymarket CLOB ask books fed by the market websocket.

Speed upgrade (2026-10-17): the trigger path must never wait on a
REST round-trip for the book. This cache subscribes to the public
CLOB market channel (wss://ws-subscriptions-clob.polymarket.com/
ws/market) for the current and next 5m windows' Up/Down tokens
(BTC + ETH) and keeps every token's levels in memory, so a trigger
walks a cached book with zero fetch latency.

Trust rules (the cache must never silently lie):
  * a token's book counts only after a full `book` snapshot has
    arrived on the CURRENT connection; `price_change` deltas for an
    uninitialized token are ignored, and a disconnect un-initializes
    every token until fresh snapshots land;
  * deltas apply by side: BUY levels are bids, SELL levels are asks
    (the CLOB book's two sides);
  * a crossed book (best ask <= best bid) is treated as corrupt and
    reported as absent;
  * freshness is explicit: asks() returns the age of the newest
    applied update, and the caller (resolve_asks) falls back to the
    REST /book fetch — logging the fallback — whenever the cache is
    stale (> stale_secs), absent or disconnected.

Read-only: no authentication, no order placement.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import websockets

logger = logging.getLogger(__name__)

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_PING_SECS = 10.0          # market channel wants an app-level "PING"
_RECONNECT_SECS = 2.0


@dataclass
class TokenBook:
    asks: dict[float, float] = field(default_factory=dict)
    bids: dict[float, float] = field(default_factory=dict)
    last_update: float = 0.0
    initialized: bool = False    # full snapshot applied on this connection


def _levels(raw: object) -> dict[float, float]:
    out: dict[float, float] = {}
    for lv in raw or []:
        try:
            price = float(lv["price"])
            size = float(lv["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and size > 0:
            out[price] = size
    return out


def apply_market_message(books: dict[str, TokenBook], msg: object,
                         recv_ts: float) -> str | None:
    """Apply one market-channel message to `books` (pure, testable).

    Returns the applied event type ("book" / "price_change") or None.
    """
    if not isinstance(msg, dict):
        return None
    event = msg.get("event_type")
    if event == "book":
        token = msg.get("asset_id")
        if not token:
            return None
        book = books.setdefault(token, TokenBook())
        book.asks = _levels(msg.get("asks"))
        book.bids = _levels(msg.get("bids"))
        book.last_update = recv_ts
        book.initialized = True
        return "book"
    if event == "price_change":
        touched = False
        for ch in msg.get("price_changes") or []:
            if not isinstance(ch, dict):
                continue
            book = books.get(ch.get("asset_id") or "")
            if book is None or not book.initialized:
                continue  # deltas are meaningless before a snapshot
            side = ch.get("side")
            if side == "BUY":
                levels = book.bids
            elif side == "SELL":
                levels = book.asks
            else:
                continue
            try:
                price = float(ch.get("price"))
                size = float(ch.get("size"))
            except (TypeError, ValueError):
                continue
            if not 0.0 < price < 1.0:
                continue
            if size > 0:
                levels[price] = size
            else:
                levels.pop(price, None)
            book.last_update = recv_ts
            touched = True
        return "price_change" if touched else None
    return None


def resolve_asks(cache: "ClobBookCache", token: str, stale_secs: float,
                 rest_fetch, now: float | None = None
                 ) -> tuple[list[tuple[float, float]], str]:
    """(asks, book_source) for one trigger attempt.

    The WS cache wins when it holds a fresh (<= stale_secs),
    initialized, uncrossed book for the token; anything else falls
    back to the REST fetch — the caller logs the fallback.
    """
    got = cache.asks(token, time.time() if now is None else now)
    if got is not None:
        asks, age = got
        if age <= stale_secs:
            return asks, "ws_cache"
    return rest_fetch(token), "rest_fallback"


class ClobBookCache:
    """Live CLOB book cache over the market websocket channel."""

    def __init__(self) -> None:
        self.books: dict[str, TokenBook] = {}
        self._desired: set[str] = set()
        self._generation = 0        # bumped whenever the token set changes
        self._task: asyncio.Task | None = None
        self.connected = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def set_tokens(self, tokens: set[str]) -> None:
        tokens = set(tokens)
        if tokens != self._desired:
            self._desired = tokens
            self._generation += 1

    def asks(self, token: str, now: float
             ) -> tuple[list[tuple[float, float]], float] | None:
        """(sorted ask levels, age_secs) or None when untrusted/absent."""
        book = self.books.get(token)
        if book is None or not book.initialized:
            return None
        if book.asks and book.bids and min(book.asks) <= max(book.bids):
            return None  # crossed book — corrupt state, do not trust it
        return sorted(book.asks.items()), now - book.last_update

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(_PING_SECS)
            await ws.send("PING")

    async def _run(self) -> None:
        while True:
            if not self._desired:
                await asyncio.sleep(0.5)
                continue
            generation = self._generation
            try:
                async with websockets.connect(CLOB_WS_URL,
                                              ping_interval=None) as ws:
                    await ws.send(json.dumps({
                        "assets_ids": sorted(self._desired),
                        "type": "market",
                    }))
                    self.connected = True
                    logger.info("clob book cache connected — subscribed "
                                "%d tokens", len(self._desired))
                    pinger = asyncio.create_task(self._pinger(ws))
                    try:
                        while generation == self._generation:
                            try:
                                raw = await asyncio.wait_for(
                                    ws.recv(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue
                            if raw == "PONG":
                                continue
                            try:
                                msg = json.loads(raw)
                            except (ValueError, TypeError):
                                continue
                            # the channel batches initial snapshots
                            # as a JSON list — apply every element
                            items = msg if isinstance(msg, list) else [msg]
                            recv_ts = time.time()
                            for item in items:
                                apply_market_message(self.books, item, recv_ts)
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # reconnect, never die silently
                logger.warning("clob book cache reconnect in %.0fs: %s",
                               _RECONNECT_SECS, exc)
            finally:
                self.connected = False
                for book in self.books.values():
                    book.initialized = False
            await asyncio.sleep(_RECONNECT_SECS)
