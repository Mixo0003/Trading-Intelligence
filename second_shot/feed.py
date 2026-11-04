"""Per-trade Binance spot feed for «Моментум Спід». READ-ONLY.

Speed upgrade (2026-10-17): replaces the ~1/sec miniTicker
snapshots (paper_momentum_v16.feed) with the aggTrade stream —
every aggregated trade pushes an event, so the rolling 15s buffer
sees a move as it forms, not at the next 1s snapshot. Buffer
semantics are unchanged: an ascending deque of (receive_ts, price)
per symbol, exactly what engine.evaluate_trigger consumes.

Latency honesty: alongside each sample the feed tracks the
EXCHANGE trade timestamp of the newest sample, so the runner can
log feed_age_ms = now - newest_trade_ts — how old the freshest
information in the trigger actually was when it fired.

REST fallback: if the websocket goes quiet for more than
REST_QUIET_SECS (dropped socket between reconnects, dead venue),
a side task polls the venue's last aggregate trade once a second
so the buffer never goes blind. REST samples carry the trade's
own exchange timestamp, so feed_age stays honest (age of the last
actual trade), never a flattering ~0.

No API keys. Host note (measured 2026-10-17): the bot runs on
the Stockholm VPS, and there the global stream.binance.com feed
is the only usable one — binance.us aggTrade delivered ZERO
messages in repeated 10s probes and its REST showed last trades
~9s stale (a thin venue cannot feed a 15s trigger), while
stream.binance.com delivered ~5 msg/s combined with a ~140ms
median trade age at arrival. From a US IP binance.com is
geo-blocked (HTTP 451, the v1.6 feed's caveat) — this service is
VPS-only in production.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque

import requests
import websockets

logger = logging.getLogger(__name__)

_STREAM_URL = "wss://stream.binance.com:9443/stream"
_REST_TRADES_URL = "https://api.binance.com/api/v3/aggTrades"
_BUFFER_MAXLEN = 60000          # per-trade pushes need far more room than 1/sec snapshots
REST_QUIET_SECS = 6.0           # ws silence before the REST fallback engages
_REST_POLL_SECS = 1.0


def parse_agg_trade(data: object) -> tuple[str, float, float] | None:
    """(pair_lower, price, trade_ts_secs) from one aggTrade payload.

    Returns None for anything that is not a usable aggTrade event.
    """
    if not isinstance(data, dict) or data.get("e") != "aggTrade":
        return None
    pair = str(data.get("s", "")).lower()
    try:
        price = float(data.get("p"))
        ts_ms = float(data.get("T") or data.get("E"))
    except (TypeError, ValueError):
        return None
    if not pair or price <= 0:
        return None
    return pair, price, ts_ms / 1000.0


def parse_last_agg_trade(rows: object) -> tuple[float, float] | None:
    """(price, trade_ts_secs) from a REST aggTrades limit=1 reply."""
    if not isinstance(rows, list) or not rows:
        return None
    last = rows[-1]
    try:
        price = float(last["p"])
        ts_ms = float(last["T"])
    except (KeyError, TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return price, ts_ms / 1000.0


class AggTradeFeed:
    """Rolling per-trade price buffer per symbol (Binance aggTrade)."""

    def __init__(self, symbols: dict[str, str]) -> None:
        # symbols: {"BTC": "btcusdt", ...}
        self._symbols = symbols
        self.prices: dict[str, deque[tuple[float, float]]] = {
            s: deque(maxlen=_BUFFER_MAXLEN) for s in symbols
        }
        self._trade_ts: dict[str, float] = {}    # exchange ts of newest sample
        self._source: dict[str, str] = {}        # "ws" | "rest" of newest sample
        self._last_msg_ts = 0.0                  # last ws message, any symbol
        self._updated = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._rest_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._last_msg_ts = time.time()  # grace period before fallback can engage
        streams = "/".join(f"{p}@aggTrade" for p in self._symbols.values())
        self._task = asyncio.create_task(self._run(streams))
        self._rest_task = asyncio.create_task(self._rest_fallback())
        logger.info("binance aggTrade feed connecting: %s", streams)

    async def stop(self) -> None:
        for task in (self._task, self._rest_task):
            if task:
                task.cancel()

    # -- update signalling (event-driven trigger loop) ----------------

    def wait_update(self):  # awaitable; pair with clear_update()
        return self._updated.wait()

    def clear_update(self) -> None:
        self._updated.clear()

    # -- sample intake -------------------------------------------------

    def note_trade(self, pair: str, price: float, trade_ts: float,
                   source: str = "ws", recv_ts: float | None = None) -> bool:
        """Append one trade sample. Returns False for unknown pairs."""
        matched = False
        for sym, want in self._symbols.items():
            if want == pair:
                self.prices[sym].append(
                    (time.time() if recv_ts is None else recv_ts, price))
                self._trade_ts[sym] = trade_ts
                self._source[sym] = source
                matched = True
        if matched:
            self._updated.set()
        return matched

    async def _run(self, streams: str) -> None:
        while True:
            try:
                async with websockets.connect(
                    f"{_STREAM_URL}?streams={streams}", ping_interval=20
                ) as ws:
                    logger.info("binance aggTrade websocket connected")
                    async for raw in ws:
                        self._last_msg_ts = time.time()
                        try:
                            data = json.loads(raw).get("data", {})
                        except json.JSONDecodeError:
                            continue
                        parsed = parse_agg_trade(data)
                        if parsed is not None:
                            self.note_trade(*parsed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # reconnect, never die silently
                logger.warning("binance aggTrade feed reconnect in 5s: %s", exc)
                await asyncio.sleep(5)

    async def _rest_fallback(self) -> None:
        engaged = False
        while True:
            await asyncio.sleep(_REST_POLL_SECS)
            quiet = time.time() - self._last_msg_ts > REST_QUIET_SECS
            if not quiet:
                if engaged:
                    engaged = False
                    logger.info("binance aggTrade feed live again — "
                                "REST fallback disengaged")
                continue
            if not engaged:
                engaged = True
                logger.warning("binance aggTrade feed quiet >%.0fs — "
                               "REST price fallback engaged", REST_QUIET_SECS)
            for sym, pair in self._symbols.items():
                try:
                    r = await asyncio.to_thread(
                        requests.get, _REST_TRADES_URL,
                        params={"symbol": pair.upper(), "limit": 1},
                        timeout=5)
                    parsed = parse_last_agg_trade(r.json())
                except Exception:
                    continue
                if parsed is None:
                    continue
                price, trade_ts = parsed
                self.note_trade(pair, price, trade_ts, source="rest")

    # -- reads ----------------------------------------------------------

    def last_price(self, sym: str) -> float | None:
        dq = self.prices[sym]
        return dq[-1][1] if dq else None

    def newest_trade_ts(self, sym: str) -> float | None:
        return self._trade_ts.get(sym)

    def feed_age_ms(self, sym: str, now: float) -> float | None:
        """Age of the newest sample's exchange timestamp, in ms."""
        ts = self._trade_ts.get(sym)
        if ts is None:
            return None
        return max(0.0, (now - ts) * 1000.0)
