"""Trigger + delayed fill + ledger for «Спід — жива затримка».
SIMULATION ONLY.

Clone of paper_momentum_speed.engine with ONE behavioural
change: a trigger does not fill. It records the trigger-time
book (``compute_trigger_record``), schedules an arrival 575 ms
later (``PendingArrival``), and the fill — if any — is executed
against the THEN-CURRENT book (``evaluate_arrival``).

Fill realism (Michał's live-identical standard, 2026-10-17):

  * a paper fill consumes the REAL CLOB ask depth, level by level,
    exactly like a live FAK market order (shared machinery:
    paper_pair_accum.engine.walk_fill) — thin books fill partially,
    empty books are REFUSED and logged, never filled in imagination;
  * the official taker fee (0.07 crypto) comes OUT of the spend,
    never on top;
  * settlement reads ONLY the official Gamma resolution.

Arrival-outcome semantics (mirroring the original at execution
time):
  * filled                    -> trade opens at the ARRIVAL fill;
  * empty/unfillable arrival  -> "refused_at_arrival", RETRYABLE
    (like an original refusal — a later trigger may re-schedule);
  * best ask at arrival > cap -> "refused_above_cap_at_arrival",
    TERMINAL for the window (stale price gone, do not chase —
    the original cap-skip semantics, applied at execution time).

Trigger-time semantics (unchanged from the original):
  * best ask at trigger > cap -> "skipped_cap_at_trigger",
    TERMINAL, no order is "sent" at all;
  * empty/unfillable trigger  -> "refused_at_trigger", RETRYABLE.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field

from paper_pair_accum.engine import Fill, walk_fill

logger = logging.getLogger(__name__)

NOMINAL_DELAY_MS = 575.0


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}ms"


def evaluate_trigger(samples: list[tuple[float, float]], now: float,
                     lookback_secs: float,
                     threshold_usd: float) -> tuple[str, float] | None:
    """Trailing-move trigger over the last `lookback_secs`.

    Identical to paper_momentum_speed.engine.evaluate_trigger.
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

    Identical to paper_momentum_speed.engine.attempt_entry; the
    arrival execution runs exactly these rules on the arrival book.

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


def uncapped_vwap(asks: list[tuple[float, float]], stake_usd: float,
                  fee_rate: float, side: str) -> float | None:
    """VWAP of walking the full `stake_usd` with NO ask cap.

    Returns None when the book cannot fill anything at all
    (walk_fill returns None: empty book / no usable size). A
    partial fill still yields the VWAP of the shares it would
    fill — that is the recorded walk VWAP (design decision,
    stated in the README).
    """
    fill = walk_fill(asks, stake_usd, fee_rate, side)
    return fill.vwap if fill is not None else None


# --- delayed-fill machinery ----------------------------------------------

@dataclass
class TriggerRecord:
    """What the trigger-time book evaluation found."""
    trigger_vwap: float | None      # uncapped $stake walk VWAP; None if unfillable
    trigger_best_ask: float | None
    status: str                     # "scheduled" | "skipped_cap" | "refused"
    reason: str | None              # None when scheduled, else the attempt reason

    @property
    def terminal(self) -> bool:
        return self.status == "skipped_cap"

    @property
    def retryable(self) -> bool:
        return self.status == "refused"


def compute_trigger_record(asks: list[tuple[float, float]], stake_usd: float,
                           fee_rate: float, side: str,
                           max_ask: float) -> TriggerRecord:
    """Evaluate the trigger-time book WITHOUT filling.

    - `trigger_vwap` is computed even when the walk would exceed
      the cap: the full stake is walked uncapped for the record;
      it is None iff the book cannot fill anything at all.
    - best ask > max_ask -> skipped_cap (terminal, no order sent);
    - empty/unfillable   -> refused (retryable, no order sent);
    - otherwise          -> scheduled for arrival execution.
    """
    if not asks:
        return TriggerRecord(trigger_vwap=None, trigger_best_ask=None,
                             status="refused", reason="refused_at_trigger")
    best = asks[0][0]
    vwap = uncapped_vwap(asks, stake_usd, fee_rate, side)
    if best > max_ask:
        return TriggerRecord(trigger_vwap=vwap, trigger_best_ask=best,
                             status="skipped_cap",
                             reason="skipped_cap_at_trigger")
    if vwap is None:
        return TriggerRecord(trigger_vwap=None, trigger_best_ask=best,
                             status="refused", reason="refused_at_trigger")
    return TriggerRecord(trigger_vwap=vwap, trigger_best_ask=best,
                         status="scheduled", reason=None)


@dataclass
class PendingArrival:
    """A trigger whose fill is scheduled for trigger_ts + delay."""
    window_start: int
    symbol: str
    side: str            # "Up" or "Down"
    trigger_ts: float
    due_ts: float
    trigger_vwap: float | None
    trigger_best_ask: float | None
    trigger_delta_usd: float
    lookback_secs: int
    stake_usd: float
    delay_ms: float = NOMINAL_DELAY_MS
    feed_age_ms: float | None = None
    trigger_book_source: str | None = None
    spot_at_trigger: float | None = None
    entry_index: int = 1

    def is_due(self, now: float) -> bool:
        return now >= self.due_ts


def make_pending(record: TriggerRecord, *, window_start: int, symbol: str,
                 side: str, trigger_ts: float, delay_ms: float,
                 trigger_delta_usd: float, lookback_secs: int,
                 stake_usd: float, feed_age_ms: float | None = None,
                 trigger_book_source: str | None = None,
                 spot_at_trigger: float | None = None,
                 entry_index: int = 1) -> PendingArrival:
    """Build the pending arrival for a "scheduled" trigger record."""
    return PendingArrival(
        window_start=window_start, symbol=symbol, side=side,
        trigger_ts=trigger_ts, due_ts=trigger_ts + delay_ms / 1000.0,
        trigger_vwap=record.trigger_vwap,
        trigger_best_ask=record.trigger_best_ask,
        trigger_delta_usd=trigger_delta_usd, lookback_secs=lookback_secs,
        stake_usd=stake_usd, delay_ms=delay_ms, feed_age_ms=feed_age_ms,
        trigger_book_source=trigger_book_source,
        spot_at_trigger=spot_at_trigger, entry_index=entry_index,
    )


@dataclass
class ArrivalOutcome:
    """Classification of executing the fill on the arrival book."""
    status: str          # "filled" | "refused" (retryable, live "killed")
    reason: str          # "filled" | "refused_at_arrival" | "refused_above_cap_at_arrival"
    fill: Fill | None = None
    best_ask: float | None = None
    arrival_vwap: float | None = None
    depth_below_cap_usd: float | None = None    # ask depth priced <= max_ask at arrival
    depth_below_limit_usd: float | None = None  # ask depth priced <= venue limit at arrival

    @property
    def filled(self) -> bool:
        return self.status == "filled"

    @property
    def terminal(self) -> bool:
        """Only a fill ends the window. Every refusal (including an
        above-cap arrival) is retryable, mirroring the live bot:
        a venue-killed order is retried while the trigger holds,
        and the next trigger re-applies the max_ask gate."""
        return self.status == "filled"

    @property
    def retryable(self) -> bool:
        return self.status == "refused"


def evaluate_arrival(asks: list[tuple[float, float]], stake_usd: float,
                     fee_rate: float, side: str,
                     max_ask: float,
                     limit_price: float | None = None) -> ArrivalOutcome:
    """Execute the fill on the ARRIVAL book, live-identically.

    The live venue order carries max_price = the trigger-time best
    ask (live_trading/exchange.py place_buy), so only arrival depth
    priced <= `limit_price` (default: max_ask) can fill. The walk
    consumes that depth level by level with the official fee inside
    the spend (paper_pair_accum walk_fill). No fillable depth ->
    refused and RETRYABLE (the live "killed" outcome). When the
    arrival best ask sits above max_ask the reason is recorded as
    refused_above_cap_at_arrival (the stale price is gone), but the
    outcome stays retryable exactly as live re-gates the next
    trigger attempt.

    `arrival_vwap` is the fill VWAP when filled; on an above-cap
    refusal it is the uncapped-walk VWAP (what the price had
    become — the gap measurement); None when nothing can fill.
    """
    record_vwap = uncapped_vwap(asks, stake_usd, fee_rate, side) if asks else None
    best = asks[0][0] if asks else None
    depth_cap = (sum(p * s for p, s in asks if p <= max_ask)
                 if asks else 0.0)
    limit = max_ask if limit_price is None else limit_price
    depth_limit = (sum(p * s for p, s in asks if p <= limit)
                   if asks else 0.0)
    fillable = [(p, s) for p, s in asks if p <= limit]
    fill = walk_fill(fillable, stake_usd, fee_rate, side) if fillable else None
    if fill is not None:
        return ArrivalOutcome(status="filled", reason="filled",
                              fill=fill, best_ask=best,
                              arrival_vwap=fill.vwap,
                              depth_below_cap_usd=depth_cap,
                              depth_below_limit_usd=depth_limit)
    if best is not None and best > max_ask:
        return ArrivalOutcome(status="refused",
                              reason="refused_above_cap_at_arrival",
                              best_ask=best, arrival_vwap=record_vwap,
                              depth_below_cap_usd=depth_cap,
                              depth_below_limit_usd=depth_limit)
    return ArrivalOutcome(status="refused", reason="refused_at_arrival",
                          best_ask=best, arrival_vwap=None,
                          depth_below_cap_usd=depth_cap,
                          depth_below_limit_usd=depth_limit)


# --- attempts journal ------------------------------------------------------

@dataclass
class AttemptRecord:
    """One line of attempts.jsonl — one per trigger outcome."""
    ts: float
    symbol: str
    window_start: int
    side: str
    trigger_vwap: float | None
    trigger_best_ask: float | None
    arrival_vwap: float | None
    arrival_best_ask: float | None
    delay_ms: float = NOMINAL_DELAY_MS
    measured_delay_ms: float | None = None
    filled: bool = False
    reason: str = ""
    # scaling-test journal fields (paper_scale_* only):
    stake_usd: float = 0.0                 # stake of this attempt's symbol
    offset_secs: float | None = None       # record ts - window_start
    depth_usd_at_or_below_cap_at_arrival: float | None = None
    depth_usd_at_or_below_limit_at_arrival: float | None = None
    filled_usd: float = 0.0                # actual all-in spend when filled
    fill_ratio: float = 0.0                # filled_usd / stake_usd
    fill_vwap: float | None = None         # entry price when filled
    entry_index: int = 1                    # 1st/2nd entry in the window (this variant)


def attempt_record_for_trigger(record: TriggerRecord, *, ts: float,
                               symbol: str, window_start: int, side: str,
                               delay_ms: float = NOMINAL_DELAY_MS,
                               stake_usd: float = 0.0
                               ) -> AttemptRecord:
    """Attempt line for a trigger that never sent an order
    (skipped_cap_at_trigger / refused_at_trigger): no arrival,
    so arrival_* and measured_delay_ms are null."""
    assert record.reason is not None
    return AttemptRecord(
        ts=ts, symbol=symbol, window_start=window_start, side=side,
        trigger_vwap=record.trigger_vwap,
        trigger_best_ask=record.trigger_best_ask,
        arrival_vwap=None, arrival_best_ask=None, delay_ms=delay_ms,
        measured_delay_ms=None, filled=False, reason=record.reason,
        stake_usd=stake_usd, offset_secs=ts - window_start,
    )


def attempt_record_for_arrival(pending: PendingArrival,
                               outcome: ArrivalOutcome,
                               arrival_ts: float) -> AttemptRecord:
    """Attempt line for an executed arrival. measured_delay_ms is
    the ACTUAL arrival_ts - trigger_ts, not the nominal delay."""
    filled_usd = outcome.fill.spend_usd if outcome.fill is not None else 0.0
    return AttemptRecord(
        ts=arrival_ts, symbol=pending.symbol,
        window_start=pending.window_start, side=pending.side,
        trigger_vwap=pending.trigger_vwap,
        trigger_best_ask=pending.trigger_best_ask,
        arrival_vwap=outcome.arrival_vwap, arrival_best_ask=outcome.best_ask,
        delay_ms=pending.delay_ms,
        measured_delay_ms=(arrival_ts - pending.trigger_ts) * 1000.0,
        filled=outcome.filled, reason=outcome.reason,
        stake_usd=pending.stake_usd,
        offset_secs=arrival_ts - pending.window_start,
        depth_usd_at_or_below_cap_at_arrival=outcome.depth_below_cap_usd,
        depth_usd_at_or_below_limit_at_arrival=outcome.depth_below_limit_usd,
        filled_usd=filled_usd,
        fill_ratio=(filled_usd / pending.stake_usd
                    if pending.stake_usd > 0 else 0.0),
        fill_vwap=(outcome.fill.vwap if outcome.fill is not None else None),
        entry_index=pending.entry_index,
    )


def append_attempt_jsonl(path: str, record: AttemptRecord) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record)) + "\n")


# --- trades ------------------------------------------------------------------

@dataclass
class SpeedTrade:
    window_start: int
    symbol: str
    side: str            # "Up" or "Down"
    entry_vwap: float    # average price paid per share (excl. fee) — the ARRIVAL fill
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
    # latency instrumentation (inherited from paper_momentum_speed):
    # feed_age_ms = age of the newest aggTrade in the trigger at
    # evaluation time; detect_to_fill_ms = arrival evaluation ->
    # fill/refusal booked; book_source = the ARRIVAL book's source,
    # "ws_cache" | "rest_fallback".
    feed_age_ms: float | None = None
    detect_to_fill_ms: float | None = None
    book_source: str | None = None
    # delayed-fill fields (this package only): trigger_vwap = the
    # uncapped walk VWAP on the trigger-time book; arrival_vwap =
    # the walk VWAP on the arrival book (= entry_vwap when filled);
    # delay_ms = the nominal delay (575); measured_delay_ms = the
    # actual arrival_ts - trigger_ts.
    trigger_vwap: float | None = None
    arrival_vwap: float | None = None
    limit_price: float | None = None   # venue max_price = trigger best ask
    delay_ms: float = NOMINAL_DELAY_MS
    measured_delay_ms: float | None = None
    won: bool | None = None
    pnl_usd: float | None = None
    exit_ts: float | None = None
    settlement_source: str | None = None   # must end up "gamma_official"
    official_winner: str | None = None     # "Up"/"Down" per Gamma
    entry_index: int = 1                   # 1st/2nd entry in the window (this variant)


def build_filled_trade(pending: PendingArrival, outcome: ArrivalOutcome,
                       arrival_ts: float, *,
                       spot_at_entry: float | None = None,
                       detect_to_fill_ms: float | None = None,
                       book_source: str | None = None) -> SpeedTrade:
    """Open the trade at the ARRIVAL fill — never the trigger price."""
    fill = outcome.fill
    assert fill is not None, "build_filled_trade needs a filled outcome"
    return SpeedTrade(
        window_start=pending.window_start, symbol=pending.symbol,
        side=pending.side, entry_vwap=fill.vwap, best_ask=outcome.best_ask,
        stake_usd=pending.stake_usd, spent_usd=fill.spend_usd,
        contracts=fill.contracts, fee_usd=fill.fee_usd,
        levels_used=fill.levels_used, entry_ts=arrival_ts,
        trigger_delta_usd=pending.trigger_delta_usd,
        lookback_secs=pending.lookback_secs, spot_at_entry=spot_at_entry,
        feed_age_ms=pending.feed_age_ms, detect_to_fill_ms=detect_to_fill_ms,
        book_source=book_source, trigger_vwap=pending.trigger_vwap,
        arrival_vwap=outcome.arrival_vwap,
        limit_price=pending.trigger_best_ask, delay_ms=pending.delay_ms,
        measured_delay_ms=(arrival_ts - pending.trigger_ts) * 1000.0,
        entry_index=pending.entry_index,
    )


class LatencyStats:
    """Rolling detect->fill measurements for the hourly summary.

    Identical to paper_momentum_speed.engine.LatencyStats.
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

    Identical to paper_momentum_speed.engine.resolve_official.
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
            "PAPER ENTER (delayed) %s %s %s @ arrival vwap %.3f "
            "(trigger vwap %s, best %.3f) spent=%.2f contracts=%.2f "
            "fee=%.2f levels=%d trigger=%+.2f/%ds feed_age=%s "
            "detect_to_fill=%s book=%s delay=%s measured_delay=%s",
            trade.symbol, trade.side, trade.window_start,
            trade.entry_vwap,
            "n/a" if trade.trigger_vwap is None else f"{trade.trigger_vwap:.3f}",
            trade.best_ask, trade.spent_usd,
            trade.contracts, trade.fee_usd, trade.levels_used,
            trade.trigger_delta_usd, trade.lookback_secs,
            _ms(trade.feed_age_ms), _ms(trade.detect_to_fill_ms),
            trade.book_source or "n/a",
            _ms(trade.delay_ms), _ms(trade.measured_delay_ms),
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

    def append_attempt(self, path: str, record: AttemptRecord) -> None:
        append_attempt_jsonl(path, record)
