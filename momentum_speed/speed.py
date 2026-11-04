"""Live Speed executor — strategy profile: paper momentum «Спід».

STRATEGY (imported, never copied): every strategy constant comes
from paper_momentum_speed.config.SpeedConfig and the trigger itself
is paper_momentum_speed.engine.evaluate_trigger, so the live bot
cannot drift from the paper twin it is A/B-tested against:

  - Binance aggTrade feed (paper_momentum_speed.feed — per-trade
    pushes, REST fallback when the socket goes quiet);
  - trigger: |spot move| over the trailing 15 s >= BTC $25 /
    ETH $1.80; enter the CURRENT 5-min window in the move's
    direction, buying the side the move points at;
  - one entry per (symbol, window); entries stop with 20 s left;
  - max ask 0.85, evaluated on the live book at decision time —
    a book that already repriced is skipped, never chased;
  - settlement: ONLY the official Gamma resolution (the audited
    live helper from live_trading.momentum). Wins/losses are booked
    from ACTUAL fills (contracts and spent amount the venue reports),
    never from the fee-split estimate.

MOVE CONFIRMATION (panel filter, owner's order 2026-10-19):
SpeedConfig.confirm_secs > 0 parks each fired trigger as a
PendingConfirm; after that many seconds the trailing 15 s move is
recomputed and the entry proceeds only if it still holds in the
same direction (journaled `move_not_confirmed` otherwise) — the
paper_speed_confirm semantics, implemented as a pending set the
main loop matures on later ticks, never a sleep on the signal
path. 0 = off (enter at once).

EXECUTION (through the existing exchange path — exchange.place_buy,
FAK market order whose amount is exactly the stake; the taker fee is
charged ON TOP of the stake — owner rule 2026-10-19, so a position's
cost basis and the budget ledger use stake + fee):

  stake        LiveConfig.speed_stake_usd ($2 phase-1 default).
  risk gates   RiskGate, same discipline as momentum: kill-switch,
               daily realised-loss stop -$15 (blocks new entries
               only), per-bot hard budget $100 under the bot's OWN
               ledger key "speed".

THE LATENCY LADDER (the whole point of the micro-test): every
trigger outcome (order_placed / order_simulated / order_killed /
order_refused / signal_skipped) journals these stage timestamps:

  signal_detected_ts  exchange ts of the aggTrade that completed
                      the trigger (Binance clock);
  eval_ts             local time the trigger evaluation ran;
  decision_ts         local time the go/no-go was complete (book
                      walked, cap checked, RiskGate answered);
  sign_start_ts /     bracket the LOCAL order preparation done
  sign_end_ts         in-process on the signal path (fee split,
                      order arguments — no subprocess, no disk);
  submit_ts           the place_buy call was dispatched;
  response_ts         the venue/SDK response was in hand.

  The EIP-712 signing itself happens inside the unified SDK's
  place_market_order call, so its duration is part of
  submit->response (submit_flight_ms) — this module does not fake a
  finer split it cannot observe. feed_age_ms (paper-identical) says
  how old the triggering trade was when evaluated; detect_to_fill
  numbers in the paper bot have their live counterpart here:
  eval->response = (response_ts - eval_ts).

SPEEDUPS (all applied; the startup journal carries a
"session_probe" record with the measured connection numbers):

  1. warm HTTP everywhere it matters: the unified-SDK client is
     constructed ONCE per process and cached by the exchange, so
     its transport keeps connections alive across orders; when the
     module is armed the client is additionally PRE-WARMED during
     feed warmup (account_snapshot) so no signal ever pays client
     construction. This module's own reads run over a persistent
     keep-alive session (WarmHttp) — the CLOB book REST fallback
     never pays a fresh TLS handshake either;
  2. market/token ids for the current AND next windows are
     pre-fetched in the background loop — the signal path never
     resolves a market;
  3. order construction + signing in-process on the signal path
     (see ladder above); the blocking venue call runs in a worker
     thread so the feed/book event loop never stalls behind it;
  4. the trigger walks the websocket book cache (shared with the
     paper design); REST is only the logged fallback;
  5. journal writes are buffered through a writer thread
     (BufferedJournal) — file I/O never sits on the signal path.

DELIBERATE DIFFERENCES vs the paper twin (execution only):
  - a venue/exchange refusal (an error whose fill outcome is
    ambiguous) ends the window — a real order is never blindly
    retried; a CONFIRMED zero-fill FAK kill retries while the
    trigger holds, exactly like a paper book refusal;
  - open positions persist to state/speed_positions.json and are
    settled on the official resolution after a restart; the budget
    ledger keeps their stakes counted either way (conservative).

Nothing here can place a real order unless the exchange handed in
is the armed real one (see exchange.build_exchange) — with the
default dry-run exchange every "order" is a journaled simulation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

import requests

from paper_momentum_speed import polymarket as spm
from paper_momentum_speed.bookcache import ClobBookCache, resolve_asks
from paper_momentum_speed.config import SpeedConfig
from paper_momentum_speed.engine import evaluate_trigger
from paper_momentum_speed.feed import AggTradeFeed
from paper_pair_accum.engine import walk_fill

from live_trading import config as cfg_mod
from live_trading import fees
from live_trading.exchange import Exchange, ExchangeError, OrderResult
from live_trading.journal import Journal
from live_trading.momentum import _official_up_won
from live_trading.risk import RiskGate
from live_trading.state import BudgetLedger, DailyState

logger = logging.getLogger("live_trading.speed")

BOT = "speed"


# ---------------------------------------------------------------------------
# Warm keep-alive HTTP (book fallback + connection probes)
# ---------------------------------------------------------------------------

class WarmHttp:
    """Persistent keep-alive session for this bot's own HTTP reads.

    One requests.Session with a pooled adapter: the first request
    pays DNS+TLS, every later one reuses the connection. Used for
    the CLOB /book REST fallback (the trigger path's last resort)
    and for the startup connection probe. The ORDER path's warmth
    comes from the exchange's cached SDK client (see module docs).
    """

    def __init__(self, cfg: cfg_mod.LiveConfig) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4, pool_maxsize=8, max_retries=0)
        self.session.mount("https://", adapter)

    def close(self) -> None:
        self.session.close()

    def book_asks(self, asset_id: str) -> list[tuple[float, float]]:
        """Ask depth [(price, size)] best-first via the warm session.

        Parse-identical to paper_momentum_v18.polymarket.book_asks;
        any failure returns [] (the caller treats it as an empty,
        unfillable book — fail-closed).
        """
        try:
            r = self.session.get(self.cfg.clob_host + "/book",
                                 params={"token_id": asset_id}, timeout=5)
            r.raise_for_status()
            asks = r.json().get("asks") or []
        except (requests.RequestException, ValueError):
            return []
        levels: list[tuple[float, float]] = []
        for a in asks:
            try:
                levels.append((float(a["price"]), float(a["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        levels.sort(key=lambda x: x[0])
        return levels

    def time_probe_ms(self, repeats: int = 3) -> float | None:
        """Last of `repeats` GET /time round trips on the warm session.

        /time is a no-side-effect CLOB endpoint: this measures
        connection reuse + venue responsiveness, a PROXY for the
        submit flight — never an order measurement.
        """
        last: float | None = None
        for _ in range(max(1, repeats)):
            try:
                t0 = time.time()
                r = self.session.get(self.cfg.clob_host + "/time",
                                     timeout=5)
                r.raise_for_status()
                last = (time.time() - t0) * 1000.0
            except requests.RequestException:
                return None
        return last

    def cold_probe_ms(self) -> float | None:
        """One GET /time on a FRESH session (full DNS+TLS+request)."""
        try:
            t0 = time.time()
            r = requests.get(self.cfg.clob_host + "/time", timeout=5)
            r.raise_for_status()
            return (time.time() - t0) * 1000.0
        except requests.RequestException:
            return None


# ---------------------------------------------------------------------------
# Buffered journal — file I/O off the signal path
# ---------------------------------------------------------------------------

class BufferedJournal:
    """Queue-backed writer over the shared live Journal.

    log() only enqueues (microseconds); a daemon thread performs
    the actual appends to decisions.jsonl in call order. Every
    record carries an explicit event_ts (enqueue time) — the
    wrapped Journal's own "ts" is the flush time and must not be
    read as the event time; the stage timestamps in the record
    fields are the authoritative ladder.
    """

    def __init__(self, journal: Journal) -> None:
        self._journal = journal
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="speed-journal", daemon=True)
        self._thread.start()

    def log(self, event: str, *, event_ts: float | None = None,
            **fields) -> None:
        record = {"event_ts": time.time() if event_ts is None else event_ts}
        record.update(fields)
        self._q.put((event, record))

    def flush(self) -> None:
        """Block until everything queued so far is on disk."""
        self._q.join()

    def stop(self) -> None:
        self.flush()
        self._q.put(None)
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                self._q.task_done()
                return
            event, fields = item
            try:
                self._journal.log(event, **fields)
            except Exception:  # a journal failure must not kill trading
                logger.exception("buffered journal write failed: %s", event)
            self._q.task_done()


# ---------------------------------------------------------------------------
# Pure decision helpers (unit-tested)
# ---------------------------------------------------------------------------

@dataclass
class EntryPlan:
    action: str                    # "go" | "skip_cap" | "skip_empty_book"
    best_ask: float | None = None
    trigger_vwap: float | None = None   # walked VWAP of the live stake


def decide_entry(asks: list[tuple[float, float]], stake_usd: float,
                 fee_rate: float, side: str,
                 max_ask: float) -> EntryPlan:
    """Go/no-go for one trigger, from the book at decision time.

    Mirrors paper engine.attempt_entry's gates: empty book -> skip
    (a live FAK would be killed for nothing); best ask above the
    cap -> skip (the stale price is gone — never chase); otherwise
    walk the depth to get the trigger-book VWAP of the actual live
    stake for the latency ladder.
    """
    if not asks:
        return EntryPlan(action="skip_empty_book")
    best = asks[0][0]
    fill = walk_fill(asks, stake_usd, fee_rate, side)
    vwap = fill.vwap if fill is not None else None
    if best > max_ask:
        return EntryPlan(action="skip_cap", best_ask=best, trigger_vwap=vwap)
    if fill is None:
        return EntryPlan(action="skip_empty_book", best_ask=best)
    return EntryPlan(action="go", best_ask=best, trigger_vwap=vwap)


@dataclass
class FillOutcome:
    kind: str                      # "filled"|"killed"|"refused"|"simulated"
    contracts: float = 0.0
    spent_usd: float = 0.0         # notional spent (making amount, no fee)
    fee_usd: float = 0.0           # taker fee charged ON TOP of the notional
    cost_usd: float = 0.0          # total debit: spent_usd + fee_usd
    avg_fill_price: float | None = None   # pure price: spent / contracts


def classify_fill(result: OrderResult, stake_usd: float,
                  fee_rate: float = 0.0) -> FillOutcome:
    """Map an exchange OrderResult onto the journal's fill taxonomy.

    filled    — venue accepted and reports contracts taken;
    killed    — venue accepted the FAK but took ZERO contracts
                (the book ran away between decision and arrival);
    refused   — venue rejected the order outright;
    simulated — dry-run exchange (nothing was sent).
    spent_usd is the venue-reported notional (making amount) when
    present, else the stake (the order amount — since 2026-10-19 the
    stake IS the amount, fee on top). fee_usd is the taker fee on the
    filled contracts at their average fill price (for simulated fills,
    the dry-run's expected fee); cost_usd = spent + fee is the total
    debit and the position's cost basis for settlement and ledgers.
    """
    if result.simulated:
        shares = result.shares
        spent = result.spent_usd if result.spent_usd is not None \
            else stake_usd
        fee = result.fee_usd
        return FillOutcome(
            kind="simulated", contracts=shares, spent_usd=spent,
            fee_usd=fee, cost_usd=spent + fee,
            avg_fill_price=(spent / shares) if shares > 0 else None)
    if result.status != "placed":
        return FillOutcome(kind="refused")
    shares = result.shares or 0.0
    if shares <= 0:
        return FillOutcome(kind="killed")
    spent = result.spent_usd if result.spent_usd is not None else stake_usd
    avg = spent / shares
    fee = fees.taker_fee_usd(shares, avg, fee_rate)
    return FillOutcome(kind="filled", contracts=shares, spent_usd=spent,
                       fee_usd=fee, cost_usd=spent + fee,
                       avg_fill_price=avg)


def ladder_fields(*, signal_detected_ts: float | None, eval_ts: float,
                  decision_ts: float | None = None,
                  sign_start_ts: float | None = None,
                  sign_end_ts: float | None = None,
                  submit_ts: float | None = None,
                  response_ts: float | None = None,
                  feed_age_ms: float | None = None,
                  trigger_vwap: float | None = None,
                  trigger_best_ask: float | None = None,
                  book_source: str | None = None) -> dict:
    """The latency-ladder field set for one trigger outcome record.

    Millisecond deltas are computed ONLY between local-clock
    timestamps (the aggTrade ts runs on Binance's clock — mixing
    clocks would fake precision). Field names deliberately avoid
    the journal sanitizer's secret markers so nothing is dropped.
    """
    def _delta(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return round((a - b) * 1000.0, 3)

    return {
        "signal_detected_ts": signal_detected_ts,
        "eval_ts": eval_ts,
        "decision_ts": decision_ts,
        "sign_start_ts": sign_start_ts,
        "sign_end_ts": sign_end_ts,
        "submit_ts": submit_ts,
        "response_ts": response_ts,
        "feed_age_ms": feed_age_ms,
        "detect_to_decision_ms": _delta(decision_ts, eval_ts),
        "prep_ms": _delta(sign_end_ts, sign_start_ts),
        "submit_flight_ms": _delta(response_ts, submit_ts),
        "eval_to_response_ms": _delta(response_ts, eval_ts),
        "trigger_book_vwap": trigger_vwap,
        "trigger_best_ask": trigger_best_ask,
        "book_source": book_source,
    }


# ---------------------------------------------------------------------------
# Move confirmation (panel filter «⏱ Підтвердження руху», 2026-10-19)
# ---------------------------------------------------------------------------

@dataclass
class PendingConfirm:
    """A fired trigger parked for the move-confirmation wait.

    Mirrors paper_speed_confirm's PendingConfirmation: at the due
    time the trailing 15 s move is recomputed and the entry
    proceeds only if it still reaches the symbol threshold in the
    SAME direction; otherwise the attempt is journaled
    `move_not_confirmed` and the window stays open to later
    triggers. confirm_secs = 0 disables the filter entirely (no
    signal ever parks — the historical behaviour).
    """
    symbol: str
    window_start: int
    side: str
    trigger_ts: float            # eval_ts of the original signal
    due_ts: float                # trigger_ts + confirm_secs
    trigger_move_usd: float
    detected_ts: float | None
    feed_age_ms: float | None
    attempt: int


def confirmation_outcome(trig, side: str) -> tuple[bool, float | None]:
    """Verdict of the confirmation re-check: (confirmed, move).

    `trig` is evaluate_trigger's return at confirmation time —
    (direction, signed move) or None when the trailing move no
    longer reaches the threshold in either direction. Confirmed
    only when the direction is unchanged; the recomputed move is
    reported either way (None when there is no trigger at all).
    """
    if trig is None:
        return False, None
    direction, delta = trig
    return direction == side, delta


def confirm_late(window_start: int, confirm_ts: float,
                 min_secs_remaining: float) -> bool:
    """True when the confirmation matures too close to the window
    close for an entry (the tail gate applied at confirmation
    time — paper's skipped_confirm_late)."""
    return window_start + 300.0 - confirm_ts < min_secs_remaining

# ---------------------------------------------------------------------------
# Open-position persistence (settlement must survive a restart)
# ---------------------------------------------------------------------------

def _save_positions(path: str, open_pos: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(open_pos.values()), f)
    os.replace(tmp, path)


def _load_positions(path: str) -> dict[tuple[str, int], dict]:
    if not os.path.exists(path):
        return {}
    try:
        rows = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    out: dict[tuple[str, int], dict] = {}
    for r in rows if isinstance(rows, list) else []:
        try:
            out[(r["symbol"], int(r["window_start"]))] = r
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def _refresh_window_tokens(cache: ClobBookCache, window_tokens: dict,
                                 symbols, now: float) -> None:
    """Background market/token resolution: current + next windows.

    Runs OUTSIDE the signal path (a dedicated prefetch task /
    startup), so a trigger never waits on Gamma. Also keeps the
    book cache subscribed to exactly the live windows' tokens.
    """
    ws = spm.window_start_ts(now)
    for w in (ws, ws + 300):
        for sym in symbols:
            if (sym, w) not in window_tokens:
                win = await asyncio.to_thread(spm.get_window, sym, w)
                if win:
                    window_tokens[(sym, w)] = win
    tokens: set[str] = set()
    for (sym, w), win in window_tokens.items():
        if w >= ws:
            tokens.add(win["up_token"])
            tokens.add(win["down_token"])
    cache.set_tokens(tokens)
    for key in list(window_tokens):
        if key[1] < ws - 300:
            del window_tokens[key]


async def _prefetch_loop(cache: ClobBookCache, window_tokens: dict,
                         symbols) -> None:
    """Keep current+next window markets resolved, forever off-path.

    A dedicated task (not the signal loop): the 2026-10-18 dry-run
    caught an inline-at-rollover refresh delaying a trigger
    evaluation by ~0.8 s of feed age; the signal loop must never
    await Gamma, not even between windows.
    """
    while True:
        try:
            await _refresh_window_tokens(cache, window_tokens, symbols,
                                         time.time())
        except Exception:  # a discovery hiccup must not kill the loop
            logger.exception("window prefetch failed")
        await asyncio.sleep(5.0)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_speed(cfg: cfg_mod.LiveConfig, exchange: Exchange,
                    journal: Journal, state: DailyState,
                    duration_mins: float = 0.0) -> None:
    pcfg = SpeedConfig()  # paper strategy parameters (see module docstring)
    stake = cfg.speed_stake_usd
    fee_rate = fees.fee_rate_for(BOT)
    budget = BudgetLedger(cfg.budget_state_path)
    gate = RiskGate(cfg, state, budget)
    bj = BufferedJournal(journal)
    warm = WarmHttp(cfg)

    feed = AggTradeFeed({s: spec.binance_pair
                         for s, spec in pcfg.symbols.items()})
    await feed.start()
    cache = ClobBookCache()
    await cache.start()
    window_tokens: dict[tuple[str, int], dict] = {}
    await _refresh_window_tokens(cache, window_tokens, pcfg.symbols,
                                 time.time())
    prefetch_task = asyncio.create_task(
        _prefetch_loop(cache, window_tokens, pcfg.symbols))

    # Startup evidence, off the signal path: connection costs with a
    # cold vs warm session (proxy — see WarmHttp.time_probe_ms), and,
    # when armed, pre-warming of the SDK client so the first signal
    # never pays client construction.
    probe = await asyncio.to_thread(
        lambda: {"cold_ms": warm.cold_probe_ms(),
                 "warm_ms": warm.time_probe_ms()})
    bj.log("session_probe", bot=BOT, probe="GET /time (connection "
           "setup proxy, not an order)", **probe)
    if exchange.name == "polymarket-clob":
        t0 = time.time()
        try:
            await asyncio.to_thread(exchange.account_snapshot)
            bj.log("client_prewarmed", bot=BOT,
                   elapsed_ms=round((time.time() - t0) * 1000.0, 1))
        except Exception as exc:  # surfaced, never hidden; orders re-assert
            bj.log("client_prewarm_failed", bot=BOT, detail=str(exc))

    logger.info("waiting 20s for feed warmup (15s lookback buffer)...")
    await asyncio.sleep(20)

    open_pos = _load_positions(cfg.speed_positions_path)
    if open_pos:
        logger.info("resumed %d pending speed positions", len(open_pos))
    entered: set[tuple[str, int]] = set(open_pos.keys())
    cap_skipped: set[tuple[str, int]] = set()
    dead_windows: set[tuple[str, int]] = set()
    last_attempt: dict[str, float] = {}
    last_settle_try: dict[tuple[str, int], float] = {}
    attempts: dict[tuple[str, int], int] = {}
    # Move-confirmation filter (panel «⏱ Підтвердження руху»):
    # 0 = off (enter at once, the historical behaviour). Read once
    # at start — a panel change restarts the service, like every
    # other filter.
    confirm_secs = float(getattr(pcfg, "confirm_secs", 0.0) or 0.0)
    pending_confirms: dict[tuple[str, int], PendingConfirm] = {}
    deadline = time.time() + duration_mins * 60 if duration_mins > 0 else None
    bj.log("runner_started", bot=BOT, exchange=exchange.name,
           stake_usd=stake, live_armed=cfg_mod.live_enabled(),
           confirm_secs=confirm_secs,
           event_ts=time.time())

    async def _enter(sym: str, ws: int, direction: str,
                     eval_ts: float, detected_ts: float | None,
                     feed_age: float | None,
                     cfields: dict | None = None) -> None:
        """The post-signal entry path: book walk -> decide -> risk
        gate -> place. Shared by the immediate path (a fresh
        trigger) and the move-confirmation path (a matured
        PendingConfirm re-enters here on the CURRENT book, exactly
        like a fresh trigger). `cfields` carries the confirmation
        journal fields (confirm_secs / trigger_move_usd /
        confirm_move_usd) merged into every record this attempt
        produces; None on the immediate path."""
        key = (sym, ws)
        cfields = cfields or {}
        window = window_tokens.get(key)
        if not window:
            # Prefetch runs two windows ahead; a miss is a
            # discovery failure — journal once, fail closed.
            dead_windows.add(key)
            bj.log("signal_skipped", bot=BOT, symbol=sym,
                   reason="no_window_prefetched",
                   window_start=ws,
                   **ladder_fields(
                       signal_detected_ts=detected_ts,
                       eval_ts=eval_ts, decision_ts=time.time(),
                       feed_age_ms=feed_age),
                   **cfields, event_ts=eval_ts)
            return
        asset = window["up_token"] if direction == "Up" \
            else window["down_token"]
        asks, book_source = await asyncio.to_thread(
            resolve_asks, cache, asset, pcfg.book_stale_secs,
            warm.book_asks)
        plan = decide_entry(asks, stake, fee_rate, direction,
                            pcfg.max_ask)
        if plan.action != "go":
            reason = "ask_above_max" \
                if plan.action == "skip_cap" else "empty_book"
            bj.log("signal_skipped", bot=BOT, symbol=sym,
                   reason=reason, window_start=ws,
                   max_ask=pcfg.max_ask,
                   **ladder_fields(
                       signal_detected_ts=detected_ts,
                       eval_ts=eval_ts, decision_ts=time.time(),
                       feed_age_ms=feed_age,
                       trigger_vwap=plan.trigger_vwap,
                       trigger_best_ask=plan.best_ask,
                       book_source=book_source),
                   **cfields, event_ts=eval_ts)
            if plan.action == "skip_cap":
                cap_skipped.add(key)  # stale price gone
            return

        # Expected fill under fee-on-top semantics: the
        # gate's budget check runs on the FULL debit
        # (stake + expected fee), not the bare stake.
        exp_contracts, exp_fee = fees.order_fill(
            stake, plan.best_ask, fee_rate)
        decision = gate.evaluate(
            BOT, stake, total_spend_usd=stake + exp_fee)
        decision_ts = time.time()
        if not decision.allowed:
            bj.log("order_refused", bot=BOT, symbol=sym,
                   reason=decision.reason, window_start=ws,
                   **ladder_fields(
                       signal_detected_ts=detected_ts,
                       eval_ts=eval_ts, decision_ts=decision_ts,
                       feed_age_ms=feed_age,
                       trigger_vwap=plan.trigger_vwap,
                       trigger_best_ask=plan.best_ask,
                       book_source=book_source),
                   **cfields, event_ts=eval_ts)
            if decision.reason == "kill_switch":
                bj.log("kill_switch_engaged", bot=BOT,
                       **cfields, event_ts=eval_ts)
            entered.add(key)  # a gate refusal ends the window
            return

        # Local order preparation, in-process (expected
        # fill + arguments) — the sign stage this module
        # controls. exp_* were computed before the gate.
        sign_start_ts = time.time()
        meta = {"bot": BOT, "symbol": sym, "window_start": ws}
        sign_end_ts = time.time()
        # The venue call blocks (~100-300 ms): worker thread,
        # so the feed/book loop never stalls behind it.
        submit_ts = time.time()
        try:
            result = await asyncio.to_thread(
                exchange.place_buy, token_id=asset,
                price=plan.best_ask, size_usd=stake, meta=meta)
        except ExchangeError as exc:
            bj.log("order_refused", bot=BOT, symbol=sym,
                   reason=str(exc), window_start=ws,
                   **ladder_fields(
                       signal_detected_ts=detected_ts,
                       eval_ts=eval_ts, decision_ts=decision_ts,
                       sign_start_ts=sign_start_ts,
                       sign_end_ts=sign_end_ts,
                       submit_ts=submit_ts,
                       response_ts=time.time(),
                       feed_age_ms=feed_age,
                       trigger_vwap=plan.trigger_vwap,
                       trigger_best_ask=plan.best_ask,
                       book_source=book_source),
                   **cfields, event_ts=eval_ts)
            # Ambiguous outcome — never blindly retry real money.
            entered.add(key)
            return
        response_ts = time.time()
        outcome = classify_fill(result, stake, fee_rate)
        ladder = ladder_fields(
            signal_detected_ts=detected_ts, eval_ts=eval_ts,
            decision_ts=decision_ts,
            sign_start_ts=sign_start_ts,
            sign_end_ts=sign_end_ts, submit_ts=submit_ts,
            response_ts=response_ts, feed_age_ms=feed_age,
            trigger_vwap=plan.trigger_vwap,
            trigger_best_ask=plan.best_ask,
            book_source=book_source)
        if outcome.kind == "killed":
            # Confirmed zero fill — retry while the trigger
            # holds, exactly like a paper book refusal.
            bj.log("order_killed", bot=BOT, symbol=sym,
                   side=direction, window_start=ws,
                   stake_usd=stake, status=result.status,
                   detail=result.detail, **ladder,
                   **cfields, event_ts=eval_ts)
            return
        if outcome.kind == "refused":
            bj.log("order_refused", bot=BOT, symbol=sym,
                   reason="venue_refused", window_start=ws,
                   status=result.status, detail=result.detail,
                   **ladder, **cfields, event_ts=eval_ts)
            entered.add(key)
            return
        bj.log("order_simulated" if outcome.kind == "simulated"
               else "order_placed",
               bot=BOT, symbol=sym, side=direction,
               window_start=ws, stake_usd=stake,
               spent_usd=round(outcome.spent_usd, 5),
               cost_usd=round(outcome.cost_usd, 5),
               filled_contracts=round(outcome.contracts, 4),
               avg_fill_price=outcome.avg_fill_price,
               expected_contracts=round(exp_contracts, 4),
               fee_usd=outcome.fee_usd, status=result.status,
               order_id=result.order_id, **ladder,
               **cfields, event_ts=eval_ts)
        if outcome.kind != "simulated":
            budget.record_open(BOT, outcome.cost_usd)
        open_pos[key] = {
            "symbol": sym, "window_start": ws,
            "side": direction, "slug": window.get("slug"),
            "contracts": outcome.contracts,
            "spent_usd": outcome.spent_usd,
            "cost_usd": outcome.cost_usd,
            "stake_usd": stake, "fee_usd": outcome.fee_usd,
            "entry_ask": plan.best_ask, "entry_ts": eval_ts,
            "simulated": outcome.kind == "simulated",
        }
        _save_positions(cfg.speed_positions_path, open_pos)
        entered.add(key)

    try:
        while True:
            if deadline and time.time() >= deadline:
                break
            feed.clear_update()
            now = time.time()
            ws = spm.window_start_ts(now)

            # Market/token resolution lives in the prefetch task —
            # this loop only ever READS window_tokens, so a trigger
            # evaluation can never block on Gamma.
            entered = {k for k in entered if k[1] >= ws - 300}
            cap_skipped = {k for k in cap_skipped if k[1] >= ws - 300}
            dead_windows = {k for k in dead_windows if k[1] >= ws - 300}
            attempts = {k: v for k, v in attempts.items()
                        if k[1] >= ws - 300}

            # 1) settle finished windows — OFFICIAL Gamma only.
            for key in list(open_pos.keys()):
                sym, w = key
                if now < w + 300:
                    continue
                if now - last_settle_try.get(key, 0.0) \
                        < pcfg.settle_retry_secs:
                    continue
                last_settle_try[key] = now
                pos = open_pos[key]
                up_won = await asyncio.to_thread(
                    _official_up_won, cfg, pos.get("slug"))
                if up_won is None:
                    if now - (w + 300) > 600 and not pos.get(
                            "pending_logged"):
                        pos["pending_logged"] = True
                        bj.log("settlement_pending", bot=BOT, symbol=sym,
                               window_start=w)
                    continue
                open_pos.pop(key)
                _save_positions(cfg.speed_positions_path, open_pos)
                contracts = float(pos["contracts"])
                # Cost basis = total debit (notional + fee on top).
                # Positions opened before the 2026-10-19 fee-on-top
                # change have no cost_usd: their spent_usd is what
                # record_open registered then, so releasing exactly
                # that keeps the ledger consistent.
                cost = float(pos.get("cost_usd", pos["spent_usd"]))
                won = up_won if pos["side"] == "Up" else not up_won
                pnl = round(contracts - cost, 2) if won \
                    else round(-cost, 2)
                simulated = bool(pos.get("simulated"))
                stopped = False
                if not simulated:
                    stopped = state.record_settlement(
                        BOT, pnl, cfg.daily_loss_stop_usd)
                    budget.record_settlement(BOT, pnl, cost)
                bj.log("settlement", bot=BOT, symbol=sym, window_start=w,
                       side=pos["side"],
                       official_winner="Up" if up_won else "Down",
                       won=won, pnl_usd=pnl, simulated=simulated,
                       daily_pnl=state.realized_pnl(BOT),
                       budget_cumulative_pnl=budget.cumulative_pnl(BOT),
                       settlement_source="gamma_official")
                if stopped:
                    bj.log("daily_stop_hit", bot=BOT,
                           daily_pnl=state.realized_pnl(BOT),
                           limit=-abs(cfg.daily_loss_stop_usd))

            # 1b) due move confirmations — the persistence
            #     re-check (panel filter «⏱ Підтвердження руху»).
            #     The trailing 15 s move is recomputed NOW; the
            #     entry proceeds on the current book only if the
            #     move still reaches the threshold in the same
            #     direction (paper_speed_confirm semantics). A
            #     failed check leaves the window open to later
            #     triggers. Never a sleep: pendings mature here, on
            #     ordinary loop ticks.
            if pending_confirms:
                pending_confirms = {
                    k: p for k, p in pending_confirms.items()
                    if k[1] >= ws - 300}
                for pkey in [k for k, p in pending_confirms.items()
                             if now >= p.due_ts]:
                    pend = pending_confirms.pop(pkey)
                    sym_c, w_c = pkey
                    spec_c = pcfg.symbols[sym_c]
                    confirm_ts = time.time()
                    trig2 = evaluate_trigger(
                        list(feed.prices[sym_c]), confirm_ts,
                        spec_c.lookback_secs, spec_c.move_usd)
                    confirmed, conf_delta = confirmation_outcome(
                        trig2, pend.side)
                    cfields = {
                        "confirm_secs": confirm_secs,
                        "trigger_move_usd": round(
                            pend.trigger_move_usd, 4),
                        "confirm_move_usd": (
                            round(conf_delta, 4)
                            if conf_delta is not None else None),
                    }
                    if confirm_late(w_c, confirm_ts,
                                    pcfg.min_secs_remaining):
                        bj.log("signal_skipped", bot=BOT, symbol=sym_c,
                               reason="confirm_late",
                               window_start=w_c, side=pend.side,
                               **cfields, event_ts=confirm_ts)
                        continue
                    if not confirmed:
                        bj.log("move_not_confirmed", bot=BOT,
                               symbol=sym_c, side=pend.side,
                               window_start=w_c, **cfields,
                               event_ts=confirm_ts)
                        continue
                    await _enter(
                        sym_c, w_c, pend.side, confirm_ts,
                        feed.newest_trade_ts(sym_c),
                        feed.feed_age_ms(sym_c, confirm_ts),
                        cfields=cfields)

            # 2) the speed trigger — event-driven, like the paper twin.
            secs_remaining = ws + 300.0 - now
            if secs_remaining >= pcfg.min_secs_remaining:
                for sym, spec in pcfg.symbols.items():
                    key = (sym, ws)
                    if key in entered or key in cap_skipped \
                            or key in open_pos or key in dead_windows \
                            or key in pending_confirms:
                        continue
                    samples = list(feed.prices[sym])
                    trig = evaluate_trigger(samples, now,
                                            spec.lookback_secs,
                                            spec.move_usd)
                    if trig is None:
                        continue
                    direction, delta = trig
                    eval_ts = time.time()
                    detected_ts = feed.newest_trade_ts(sym)
                    feed_age = feed.feed_age_ms(sym, eval_ts)
                    if now - last_attempt.get(sym, 0.0) \
                            < pcfg.attempt_cooldown_secs:
                        continue
                    last_attempt[sym] = now
                    attempts[key] = attempts.get(key, 0) + 1
                    bj.log("signal_seen", bot=BOT, symbol=sym,
                           side=direction, move_usd=round(delta, 4),
                           window_start=ws,
                           secs_remaining=round(secs_remaining, 1),
                           signal_detected_ts=detected_ts,
                           feed_age_ms=feed_age, attempt=attempts[key],
                           event_ts=eval_ts)

                    if confirm_secs > 0:
                        # Park the trigger for the confirmation
                        # wait — the entry decision is deferred to
                        # step 1b, never a sleep on the signal path.
                        pending_confirms[key] = PendingConfirm(
                            symbol=sym, window_start=ws,
                            side=direction, trigger_ts=eval_ts,
                            due_ts=eval_ts + confirm_secs,
                            trigger_move_usd=delta,
                            detected_ts=detected_ts,
                            feed_age_ms=feed_age,
                            attempt=attempts[key])
                        bj.log("confirm_pending", bot=BOT, symbol=sym,
                               side=direction,
                               move_usd=round(delta, 4),
                               window_start=ws,
                               confirm_secs=confirm_secs,
                               due_ts=eval_ts + confirm_secs,
                               signal_detected_ts=detected_ts,
                               feed_age_ms=feed_age,
                               attempt=attempts[key],
                               event_ts=eval_ts)
                        continue
                    await _enter(sym, ws, direction, eval_ts,
                                 detected_ts, feed_age)

            # sleep until the next feed event (housekeeping cap)
            try:
                await asyncio.wait_for(feed.wait_update(),
                                       timeout=pcfg.loop_poll_secs)
            except asyncio.TimeoutError:
                pass
    finally:
        prefetch_task.cancel()
        await feed.stop()
        await cache.stop()
        bj.log("runner_stopped", bot=BOT,
               daily_pnl=state.realized_pnl(BOT), event_ts=time.time())
        bj.stop()
        warm.close()
