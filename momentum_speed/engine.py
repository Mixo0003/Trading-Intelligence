"""Trigger + fill + ledger for «Моментум Спід». SIMULATION ONLY.

Fill realism (Michał's live-identical standard, 2026-10-17):

  * a paper fill consumes the REAL CLOB ask depth, level by level,
    exactly like a live FAK market order (shared machinery:
    paper_pair_accum.engine.walk_fill) — thin books fill partially,
    empty books are REFUSED and logged, never filled in imagination;
  * the official taker fee (0.07 crypto) comes OUT of the spend,
    never on top;
  * settlement reads ONLY the official Gamma resolution.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field

from paper_pair_accum.engine import Fill, walk_fill

logger = logging.getLogger(__name__)


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}ms"


def evaluate_trigger(samples: list[tuple[float, float]], now: float,
                     lookback_secs: float,
                     threshold_usd: float) -> tuple[str, float] | None:
    """Trailing-move trigger over the last `lookback_secs`.

    `samples` is an ascending [(ts, price)] series. Returns
    ("Up"|"Down", delta_usd) when the move from the latest sample
    at-or-before (now - lookback_secs) to the newest sample reaches
    `threshold_usd` in size; None when the move is smaller (drift)
    or when no sample is old enough to measure against yet.
    """
    if len(samples) < 2:
        return None
    cutoff = now - lookback_secs
    ref: float | None = None
    for ts, px in reversed(samples):
        if ts <= cutoff:
            ref = px
            break
    if ref is None:
        return None
    delta = samples[-1][1] - ref
    if abs(delta) < threshold_usd:
        return None
    return ("Up" if delta > 0 else "Down"), delta


@dataclass
class EntryOutcome:
    status: str                 # "filled" | "refused" | "skipped_cap"
    fill: Fill | None = None
    best_ask: float | None = None


def attempt_entry(asks: list[tuple[float, float]], stake_usd: float,
                  fee_rate: float, side: str,
                  max_ask: float) -> EntryOutcome:
    """One live-identical buy attempt against the real book.

    - empty book -> "refused" (a live FAK order would be killed);
    - best ask above max_ask -> "skipped_cap" (book already
      repriced; the stale-price edge is gone — do not chase);
    - otherwise walk the depth FAK-style: whatever the book can
      fill, fills (possibly partially); nothing fillable at all
      -> "refused".
    """
    if not asks:
        return EntryOutcome(status="refused")
    best = asks[0][0]
    if best > max_ask:
        return EntryOutcome(status="skipped_cap", best_ask=best)
    fill = walk_fill(asks, stake_usd, fee_rate, side)
    if fill is None:
        return EntryOutcome(status="refused", best_ask=best)
    return EntryOutcome(status="filled", fill=fill, best_ask=best)


@dataclass
class SpeedTrade:
    window_start: int
    symbol: str
    side: str            # "Up" or "Down"
    entry_vwap: float    # average price paid per share (excl. fee)
    best_ask: float
    stake_usd: float     # requested spend
    spent_usd: float     # actual all-in spend (contracts + fee)
    contracts: float
    fee_usd: float
    levels_used: int
    entry_ts: float
    trigger_delta_usd: float
    lookback_secs: int
    spot_at_entry: float | None = None
    # latency instrumentation (speed upgrade 2026-10-17): feed_age_ms
    # = age of the newest aggTrade in the trigger at evaluation time;
    # detect_to_fill_ms = trigger evaluation -> fill/refusal booked;
    # book_source = "ws_cache" | "rest_fallback".
    feed_age_ms: float | None = None
    detect_to_fill_ms: float | None = None
    book_source: str | None = None
    won: bool | None = None
    pnl_usd: float | None = None
    exit_ts: float | None = None
    settlement_source: str | None = None   # must end up "gamma_official"
    official_winner: str | None = None     # "Up"/"Down" per Gamma


class LatencyStats:
    """Rolling detect->fill measurements for the hourly summary.

    The speed claim of this bot is these numbers, not adjectives:
    every trigger attempt records how long evaluation-to-booking
    actually took on this machine, and the runner logs the trailing
    hour's median / p95.
    """

    def __init__(self) -> None:
        self._samples: list[tuple[float, float]] = []  # (ts, detect_ms)

    def record(self, detect_to_fill_ms: float, ts: float | None = None) -> None:
        self._samples.append(
            (time.time() if ts is None else ts, float(detect_to_fill_ms)))

    def summary(self, now: float, window_secs: float = 3600.0
                ) -> tuple[int, float | None, float | None]:
        """(count, median_ms, p95_ms) over the trailing window."""
        vals = sorted(ms for ts, ms in self._samples
                      if now - ts <= window_secs)
        if not vals:
            return 0, None, None
        n = len(vals)
        mid = n // 2
        median = vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0
        p95 = vals[max(0, math.ceil(0.95 * n) - 1)]
        return n, median, p95

    def prune(self, now: float, window_secs: float = 3600.0) -> None:
        self._samples = [(ts, ms) for ts, ms in self._samples
                         if now - ts <= window_secs]


def resolve_official(trade: SpeedTrade, winner: str) -> None:
    """Book a trade from the OFFICIAL Gamma winner ('Up'/'Down').

    Live-identical accounting (fee already inside the spend):
    win  -> contracts * $1.00 - spent
    loss -> -spent
    """
    won = (winner == trade.side)
    trade.won = won
    trade.official_winner = winner
    trade.settlement_source = "gamma_official"
    trade.exit_ts = time.time()
    payout = trade.contracts * 1.0 if won else 0.0
    trade.pnl_usd = payout - trade.spent_usd
    logger.info(
        "PAPER EXIT  %s %s official=%s won=%s pnl=%+.2f",
        trade.symbol, trade.side, winner, won, trade.pnl_usd,
    )


@dataclass
class SpeedLedger:
    start_bankroll: float
    trades: list[SpeedTrade] = field(default_factory=list)
    stale_skips: int = 0
    fill_refusals: int = 0

    def open(self, trade: SpeedTrade) -> None:
        self.trades.append(trade)
        logger.info(
            "PAPER ENTER %s %s %s @ vwap %.3f (best %.3f) spent=%.2f "
            "contracts=%.2f fee=%.2f levels=%d trigger=%+.2f/%ds "
            "feed_age=%s detect_to_fill=%s book=%s",
            trade.symbol, trade.side, trade.window_start,
            trade.entry_vwap, trade.best_ask, trade.spent_usd,
            trade.contracts, trade.fee_usd, trade.levels_used,
            trade.trigger_delta_usd, trade.lookback_secs,
            _ms(trade.feed_age_ms), _ms(trade.detect_to_fill_ms),
            trade.book_source or "n/a",
        )

    def closed(self) -> list[SpeedTrade]:
        return [t for t in self.trades if t.pnl_usd is not None]

    def net_pnl(self) -> float:
        return sum(t.pnl_usd for t in self.closed())

    def win_rate(self) -> float:
        c = self.closed()
        return sum(1 for t in c if t.won) / len(c) if c else 0.0

    def append_jsonl(self, path: str, trade: SpeedTrade) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(trade)) + "\n")
