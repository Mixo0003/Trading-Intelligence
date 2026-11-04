"""Live Second Shot executor — strategy profile: paper «Другий постріл».

STRATEGY (imported, never copied): every strategy constant comes
from paper_speed_second.config.SpeedConfig and the trigger itself
is paper_speed_second.engine.evaluate_trigger, so the live bot
cannot drift from the paper twin it is A/B-tested against:

  - Binance aggTrade feed (paper_speed_second.feed — per-trade
    pushes, REST fallback when the socket goes quiet; byte-identical
    to the paper_momentum_speed chassis the main Speed bot uses);
  - trigger: |spot move| over the trailing 15 s >= BTC $25 /
    ETH $1.80; enter the CURRENT 5-min window in the move's
    direction, buying the side the move points at;
  - UP TO TWO entries per (symbol, window) — THE variant change
    vs the main Speed bot's one. The second entry is a GENUINE
    second decision, exactly as in paper_speed_second/run.py:
    it requires a FRESH trigger evaluation (the same
    evaluate_trigger over the trailing 15 s) performed AFTER the
    first entry's outcome is known (filled). While an entry
    attempt for a (symbol, window) is in flight, no other is
    started for that key; a confirmed zero-fill FAK kill and
    book-refusals do NOT consume an entry slot (retryable while
    the trigger holds), while a venue refusal or a RiskGate
    refusal ends the window (live-Speed semantics — real money is
    never blindly retried). The 2 s attempt cooldown applies
    between attempts. Every journal record carries entry_index
    (1 or 2) so the second shot's quality is measurable
    separately, exactly like the paper journal;
  - entry price band 0.70-0.85 (paper config): best ask above
    max_ask 0.85 -> skip, terminal for the window (stale price
    gone, never chase); best ask below min_trigger_ask 0.70 ->
    skip, RETRYABLE while the trigger holds (the paper WR70
    floor: sub-0.70 entries are this family's structural drag);
  - entries only while >= 20 s remain (min_secs_remaining);
    entries therefore fall in offsets 0-280 s of the window;
  - session / offset / disabled-symbol gates are honoured from
    the paper config exactly as paper_speed_second.run applies
    them — with the shipped config all three are OFF (trade
    hours None, max offset None, no disabled symbols), so the
    bot as configured trades around the clock, nights included;
  - NO move-confirmation filter: paper_speed_second has none
    (confirm_secs does not exist in its config — the fresh-
    trigger rule IS its second-decision mechanism);
  - CHOP FILTER (panel filter, owner order 2026-10-20):
    SpeedConfig.chop_filter_enabled (default False = off) gates
    each fired trigger — the first AND the second entry alike —
    on the Kaufman efficiency ratio of the trailing
    chop_window_secs (1 s closes): ER below chop_er_max means a
    sawtooth market — the trigger is journaled signal_skipped
    (reason "chop_filter") and the window stays open to later
    triggers if the ratio recovers. Same shared gate as the
    main Speed bot (paper_momentum_speed.engine.chop_gate), so
    paper and live cannot drift;
  - settlement: ONLY the official Gamma resolution (the audited
    live helper from live_trading.momentum). Wins/losses are
    booked from ACTUAL fills (contracts and spent amount the
    venue reports), never from estimates.

EXECUTION (through the existing exchange path — exchange.place_buy,
FAK market order whose amount is exactly the stake; the taker fee is
charged ON TOP of the stake — owner rule 2026-10-19, so a position's
cost basis and the budget ledger use stake + fee):

  stake        LiveConfig.second_stake_usd ($10, from the paper
               config; a fully-filled window carries 2 x $10 =
               $20 plus fees — stated plainly);
  risk gates   RiskGate under the bot's OWN ledger key "second"
               (budget/daily state are NEVER mixed with "speed"),
               with per-bot limits LiveConfig.second_daily_loss_stop_usd
               / second_budget_usd — owner-approved 2026-10-20
               ($10 / -$30 / $100; see config.py).

DELIBERATE DIFFERENCE vs the paper twin (execution accounting
only, stated plainly): the paper bot books the 0.07 taker fee
INSIDE the $10 spend; the live module follows the house live
rule (stake = exact order amount, fee on top), so a live entry
debits $10 + fee (~$10.15-0.21) and holds 10/price contracts.
Fill mechanics also differ by nature: paper simulates the fill
on the book 575 ms after the trigger (walk with refusals); live
sends a real FAK order whose venue-reported fill is booked.

ARMING (fail-closed, pair_limit pattern): the REAL exchange is
built ONLY when ALL of these hold: LIVE_TRADING_ENABLED == "1"
AND the per-bot LIVE_SECOND_ENABLED == "1" AND no KILL file AND
all credentials present (see build_second_exchange /
second_armed). In every other state the runner uses the inert
dry-run exchange and every "order" is a journaled simulation.
The per-bot switch is deliberately NOT set anywhere by default:
enabling the module or starting the main Speed bot can never arm
this bot by accident. STOP: the global KILL file
(live_trading/KILL — checked by RiskGate on every entry and by
the exchange at order time) halts new entries immediately;
removing LIVE_SECOND_ENABLED / LIVE_TRADING_ENABLED or stopping
the unit also stops it. Already-open positions still settle on
the official Gamma resolution (settlement is not an entry).

LATENCY LADDER: identical to the main Speed executor (this module
is its chassis): signal_detected_ts / eval_ts / decision_ts /
sign_start_ts / sign_end_ts / submit_ts / response_ts on every
trigger outcome, plus the same speedups (warm HTTP + pre-warmed
SDK client, background market prefetch, priming, buffered
journal, worker-thread venue calls).

BUILT 2026-10-20 on the owner's order "prepare for live, do not
launch". NOT LAUNCHED: no unit enabled, no arm variable set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from paper_speed_second import polymarket as spm
from paper_speed_second.bookcache import ClobBookCache, resolve_asks
from paper_speed_second.config import SpeedConfig
from paper_speed_second.engine import evaluate_trigger
from paper_momentum_speed.engine import chop_gate
from paper_speed_second.feed import AggTradeFeed
from paper_pair_accum.engine import walk_fill

from live_trading import config as cfg_mod
from live_trading import fees
from live_trading.exchange import (
    DryRunExchange, Exchange, ExchangeError, OrderResult,
    PolymarketClobExchange)
from live_trading.journal import Journal
from live_trading.momentum import _official_up_won
from live_trading.risk import RiskGate
from live_trading.state import BudgetLedger, DailyState

logger = logging.getLogger("live_trading.second")

BOT = "second"


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
            target=self._run, name="second-journal", daemon=True)
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
                 max_ask: float,
                 min_ask: float | None = None) -> EntryPlan:
    """Go/no-go for one trigger, from the book at decision time.

    Mirrors paper_speed_second's trigger-time gates: empty book ->
    skip (a live FAK would be killed for nothing); best ask above
    the cap -> skip_cap (the stale price is gone — never chase,
    terminal for the window); best ask below the paper floor
    (min_trigger_ask 0.70) -> skip_below_min (RETRYABLE while the
    trigger holds, exactly like the paper runner's
    skipped_below_min_at_trigger); otherwise walk the depth to
    get the trigger-book VWAP of the actual live stake for the
    latency ladder.
    """
    if not asks:
        return EntryPlan(action="skip_empty_book")
    best = asks[0][0]
    fill = walk_fill(asks, stake_usd, fee_rate, side)
    vwap = fill.vwap if fill is not None else None
    if best > max_ask:
        return EntryPlan(action="skip_cap", best_ask=best, trigger_vwap=vwap)
    if min_ask is not None and best < min_ask:
        return EntryPlan(action="skip_below_min", best_ask=best,
                         trigger_vwap=vwap)
    if fill is None:
        return EntryPlan(action="skip_empty_book", best_ask=best)
    return EntryPlan(action="go", best_ask=best, trigger_vwap=vwap)


# ---------------------------------------------------------------------------
# Second-entry bookkeeping (pure, unit-tested)
# ---------------------------------------------------------------------------

def next_entry_index(filled_count: int,
                     max_entries: int) -> int | None:
    """Entry index the NEXT filled entry in a window would carry.

    filled_count counts FILLED entries only (paper semantics:
    refusals and FAK kills never consume a slot). Returns None
    once the per-window cap is reached — no third shot exists.
    """
    if filled_count < 0 or max_entries <= 0:
        return None
    if filled_count >= max_entries:
        return None
    return filled_count + 1


def entry_allowed(filled_count: int, attempt_in_flight: bool,
                  max_entries: int) -> bool:
    """May a fresh trigger start an entry attempt right now?

    The paper rule, restated for live: an attempt may start only
    when (a) fewer than max_entries entries have FILLED in this
    (symbol, window), and (b) no earlier attempt for the same
    key is still in flight — so entry #2's trigger is always
    evaluated after entry #1 resolved, a genuine second decision,
    never a duplicate fired alongside the first.
    """
    if attempt_in_flight:
        return False
    return next_entry_index(filled_count, max_entries) is not None


def session_open(pcfg, now_ts: float) -> bool:
    """Paper session gate (trade_hours_* in America/Chicago).

    The shipped paper config leaves both bounds None = gate OFF
    (the bot trades around the clock). When bounds are set, a
    trigger outside [start, end) is not entered — the paper
    runner journals it skipped_session_gate, terminal for the
    window; here the loop simply never enters, which for a
    closed session is the same outcome.
    """
    start = getattr(pcfg, "trade_hours_start", None)
    end = getattr(pcfg, "trade_hours_end", None)
    if start is None or end is None:
        return True
    hour = datetime.fromtimestamp(now_ts, ZoneInfo("America/Chicago")).hour
    return start <= hour < end


def config_drift(pcfg, cfg: cfg_mod.LiveConfig) -> list[str]:
    """Fail-closed startup audit (pair_limit pattern): the live
    stake must equal EVERY per-symbol paper stake, and the
    per-window entry cap must be the variant's defining 2."""
    problems: list[str] = []
    for sym, spec in pcfg.symbols.items():
        if abs(float(spec.stake_usd) - float(cfg.second_stake_usd)) > 1e-9:
            problems.append(
                f"{sym} paper stake {spec.stake_usd} != live "
                f"second_stake_usd {cfg.second_stake_usd}")
    if int(pcfg.max_entries_per_window) != 2:
        problems.append(
            f"max_entries_per_window {pcfg.max_entries_per_window} != 2")
    return problems


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
# Arming (fail-closed, pair_limit pattern)
# ---------------------------------------------------------------------------

def second_armed(cfg: cfg_mod.LiveConfig) -> bool:
    """True only when EVERY arm condition holds (fail-closed)."""
    return (cfg_mod.live_enabled()
            and cfg_mod.second_enabled()
            and not cfg_mod.kill_switch_engaged(cfg)
            and cfg_mod.credentials_present())


def build_second_exchange(cfg: cfg_mod.LiveConfig) -> Exchange:
    """Real exchange ONLY when fully armed (global + per-bot
    switch + no KILL + credentials); the inert dry-run otherwise,
    so a default run can never place a real order. Mirrors
    pair_limit's build_pair_order_exchange — the generic
    exchange.build_exchange must NOT be used for this bot: it
    knows nothing about the per-bot switch."""
    if second_armed(cfg):
        return PolymarketClobExchange(cfg)
    return DryRunExchange(cfg)


# ---------------------------------------------------------------------------
# Open-position persistence (settlement must survive a restart)
# ---------------------------------------------------------------------------

def _save_positions(path: str, open_pos: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(open_pos.values()), f)
    os.replace(tmp, path)


def _load_positions(path: str) -> dict[tuple[str, int, int], dict]:
    """Open positions keyed (symbol, window_start, entry_index).

    A window can hold TWO positions for one symbol (entries 1
    and 2) — the key must carry entry_index or the second fill
    would overwrite the first. Rows written before entry_index
    existed default to index 1.
    """
    if not os.path.exists(path):
        return {}
    try:
        rows = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    out: dict[tuple[str, int, int], dict] = {}
    for r in rows if isinstance(rows, list) else []:
        try:
            out[(r["symbol"], int(r["window_start"]),
                 int(r.get("entry_index", 1)))] = r
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


async def _warm_loop(exchange) -> None:
    """Keep one authenticated CLOB connection pooled and warm.

    The audit of 2026-10-18 found the executor restored after the
    crash-loop never warmed the connection: order flights paid fresh
    setup inside the submit path (median 352 ms vs 279 ms primed).
    warm_connection is a read-only balance/allowance read on the same
    transport; failures are logged at debug and never break the loop.
    """
    while True:
        await asyncio.sleep(15.0)
        try:
            await asyncio.to_thread(exchange.warm_connection)
        except Exception:
            logger.debug("connection warm failed", exc_info=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_second(cfg: cfg_mod.LiveConfig,
                     journal: Journal, state: DailyState,
                     duration_mins: float = 0.0) -> None:
    pcfg = SpeedConfig()  # paper_speed_second parameters (module docstring)
    stake = cfg.second_stake_usd
    fee_rate = fees.fee_rate_for(BOT)
    exchange = build_second_exchange(cfg)
    budget = BudgetLedger(cfg.budget_state_path)
    # Per-bot risk limits ride a copied config (pair_limit pattern):
    # the shared RiskGate arithmetic stays untouched for other bots.
    gate_cfg = replace(cfg,
                       daily_loss_stop_usd=cfg.second_daily_loss_stop_usd,
                       budget_usd=cfg.second_budget_usd)
    gate = RiskGate(gate_cfg, state, budget)
    drift = config_drift(pcfg, cfg)
    if drift:
        journal.log("config_mismatch", bot=BOT, problems=drift)
        raise RuntimeError(f"second-shot config drift: {drift}")
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
    warm_task = asyncio.create_task(_warm_loop(exchange))
    primed: set[tuple[str, int]] = set()

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

    open_pos = _load_positions(cfg.second_positions_path)
    if open_pos:
        logger.info("resumed %d pending second positions", len(open_pos))
    # Filled-entry counts per (symbol, window) — the paper rule:
    # the second shot may fire only while fewer than
    # max_entries_per_window entries have FILLED, and only on a
    # fresh trigger evaluated after the previous entry resolved.
    # Resumed open positions count as already-filled entries.
    filled_counts: dict[tuple[str, int], int] = {}
    for (_s, _w, _i) in open_pos.keys():
        filled_counts[(_s, _w)] = filled_counts.get((_s, _w), 0) + 1
    in_flight: set[tuple[str, int]] = set()
    cap_skipped: set[tuple[str, int]] = set()
    dead_windows: set[tuple[str, int]] = set()
    last_attempt: dict[str, float] = {}
    last_settle_try: dict[tuple[str, int], float] = {}
    attempts: dict[tuple[str, int], int] = {}
    # No move-confirmation filter: paper_speed_second has none.
    chop_enabled = bool(getattr(pcfg, "chop_filter_enabled", False))
    chop_window_secs = int(getattr(pcfg, "chop_window_secs", 300) or 300)
    chop_er_max = float(getattr(pcfg, "chop_er_max", 0.25) or 0.25)
    chop_journaled: set[tuple[str, int]] = set()
    deadline = time.time() + duration_mins * 60 if duration_mins > 0 else None
    bj.log("runner_started", bot=BOT, exchange=exchange.name,
           stake_usd=stake, live_armed=cfg_mod.live_enabled(),
           second_armed=second_armed(cfg),
           max_entries_per_window=pcfg.max_entries_per_window,
           min_trigger_ask=pcfg.min_trigger_ask,
           max_ask=pcfg.max_ask,
           chop_filter_enabled=chop_enabled,
           chop_window_secs=chop_window_secs,
           chop_er_max=chop_er_max,
           event_ts=time.time())

    async def _enter(sym: str, ws: int, direction: str,
                     eval_ts: float, detected_ts: float | None,
                     feed_age: float | None,
                     entry_index: int = 1) -> None:
        """The post-signal entry path: book walk -> decide -> risk
        gate -> place. Called once per entry attempt; entry_index
        (1 or 2) is journaled on every record so the second shot's
        quality stays measurable separately, exactly like paper."""
        key = (sym, ws)
        window = window_tokens.get(key)
        if not window:
            # Prefetch runs two windows ahead; a miss is a
            # discovery failure — journal once, fail closed.
            dead_windows.add(key)
            bj.log("signal_skipped", bot=BOT, symbol=sym,
                   reason="no_window_prefetched",
                   window_start=ws, entry_index=entry_index,
                   **ladder_fields(
                       signal_detected_ts=detected_ts,
                       eval_ts=eval_ts, decision_ts=time.time(),
                       feed_age_ms=feed_age),
                   event_ts=eval_ts)
            return
        asset = window["up_token"] if direction == "Up" \
            else window["down_token"]
        asks, book_source = await asyncio.to_thread(
            resolve_asks, cache, asset, pcfg.book_stale_secs,
            warm.book_asks)
        plan = decide_entry(asks, stake, fee_rate, direction,
                            pcfg.max_ask, pcfg.min_trigger_ask)
        if plan.action != "go":
            reason = {"skip_cap": "ask_above_max",
                      "skip_below_min": "ask_below_min"}.get(
                          plan.action, "empty_book")
            bj.log("signal_skipped", bot=BOT, symbol=sym,
                   reason=reason, window_start=ws,
                   entry_index=entry_index,
                   max_ask=pcfg.max_ask,
                   **ladder_fields(
                       signal_detected_ts=detected_ts,
                       eval_ts=eval_ts, decision_ts=time.time(),
                       feed_age_ms=feed_age,
                       trigger_vwap=plan.trigger_vwap,
                       trigger_best_ask=plan.best_ask,
                       book_source=book_source),
                   event_ts=eval_ts)
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
                   entry_index=entry_index,
                   **ladder_fields(
                       signal_detected_ts=detected_ts,
                       eval_ts=eval_ts, decision_ts=decision_ts,
                       feed_age_ms=feed_age,
                       trigger_vwap=plan.trigger_vwap,
                       trigger_best_ask=plan.best_ask,
                       book_source=book_source),
                   event_ts=eval_ts)
            if decision.reason == "kill_switch":
                bj.log("kill_switch_engaged", bot=BOT,
                       event_ts=eval_ts)
            filled_counts[key] = pcfg.max_entries_per_window
            # a gate refusal ends the window
            return

        # Local order preparation, in-process (expected
        # fill + arguments) — the sign stage this module
        # controls. exp_* were computed before the gate.
        sign_start_ts = time.time()
        meta = {"bot": BOT, "symbol": sym, "window_start": ws,
                "entry_index": entry_index}
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
                   entry_index=entry_index,
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
                   event_ts=eval_ts)
            # Ambiguous outcome — never blindly retry real money.
            filled_counts[key] = pcfg.max_entries_per_window
            # refusal ends the window (never blindly retry real money)
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
                   entry_index=entry_index,
                   stake_usd=stake, status=result.status,
                   detail=result.detail, **ladder,
                   event_ts=eval_ts)
            return
        if outcome.kind == "refused":
            bj.log("order_refused", bot=BOT, symbol=sym,
                   reason="venue_refused", window_start=ws,
                   entry_index=entry_index,
                   status=result.status, detail=result.detail,
                   **ladder, event_ts=eval_ts)
            filled_counts[key] = pcfg.max_entries_per_window
            # refusal ends the window (never blindly retry real money)
            return
        bj.log("order_simulated" if outcome.kind == "simulated"
               else "order_placed",
               bot=BOT, symbol=sym, side=direction,
               window_start=ws, entry_index=entry_index,
               stake_usd=stake,
               spent_usd=round(outcome.spent_usd, 5),
               cost_usd=round(outcome.cost_usd, 5),
               filled_contracts=round(outcome.contracts, 4),
               avg_fill_price=outcome.avg_fill_price,
               expected_contracts=round(exp_contracts, 4),
               fee_usd=outcome.fee_usd, status=result.status,
               order_id=result.order_id, **ladder,
               event_ts=eval_ts)
        if outcome.kind != "simulated":
            budget.record_open(BOT, outcome.cost_usd)
        open_pos[(sym, ws, entry_index)] = {
            "symbol": sym, "window_start": ws,
            "entry_index": entry_index,
            "side": direction, "slug": window.get("slug"),
            "contracts": outcome.contracts,
            "spent_usd": outcome.spent_usd,
            "cost_usd": outcome.cost_usd,
            "stake_usd": stake, "fee_usd": outcome.fee_usd,
            "entry_ask": plan.best_ask, "entry_ts": eval_ts,
            "simulated": outcome.kind == "simulated",
        }
        _save_positions(cfg.second_positions_path, open_pos)
        filled_counts[key] = filled_counts.get(key, 0) + 1

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
            filled_counts = {k: v for k, v in filled_counts.items()
                             if k[1] >= ws - 300}
            in_flight = {k for k in in_flight if k[1] >= ws - 300}
            cap_skipped = {k for k in cap_skipped if k[1] >= ws - 300}
            dead_windows = {k for k in dead_windows if k[1] >= ws - 300}
            attempts = {k: v for k, v in attempts.items()
                        if k[1] >= ws - 300}

            # Prime each fresh window's market metadata once, as soon
            # as the prefetch task knows its tokens (current + next
            # window), so the first order never pays metadata setup
            # inside its submit flight. One attempt per window/symbol;
            # a prime failure is journaled and never retried in-window.
            for (psym, pw), pwin in list(window_tokens.items()):
                if pw < ws or (psym, pw) in primed:
                    continue
                primed.add((psym, pw))
                for ptok in (pwin["up_token"], pwin["down_token"]):
                    try:
                        pms = await asyncio.to_thread(
                            exchange.prime_market, token_id=ptok,
                            size_usd=stake, max_price=pcfg.max_ask)
                        bj.log("market_primed", bot=BOT, symbol=psym,
                               window_start=pw, prime_ms=pms,
                               event_ts=time.time())
                    except Exception as exc:
                        bj.log("prime_failed", bot=BOT, symbol=psym,
                               window_start=pw, error=str(exc)[:200],
                               event_ts=time.time())
            primed = {k for k in primed if k[1] >= ws - 300}

            # 1) settle finished windows — OFFICIAL Gamma only.
            for key in list(open_pos.keys()):
                sym, w, eidx = key
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
                _save_positions(cfg.second_positions_path, open_pos)
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
                        BOT, pnl, gate_cfg.daily_loss_stop_usd)
                    budget.record_settlement(BOT, pnl, cost)
                bj.log("settlement", bot=BOT, symbol=sym, window_start=w,
                       entry_index=eidx, side=pos["side"],
                       official_winner="Up" if up_won else "Down",
                       won=won, pnl_usd=pnl, simulated=simulated,
                       daily_pnl=state.realized_pnl(BOT),
                       budget_cumulative_pnl=budget.cumulative_pnl(BOT),
                       settlement_source="gamma_official")
                if stopped:
                    bj.log("daily_stop_hit", bot=BOT,
                           daily_pnl=state.realized_pnl(BOT),
                           limit=-abs(gate_cfg.daily_loss_stop_usd))

            # 2) the speed trigger — event-driven, like the paper twin.
            secs_remaining = ws + 300.0 - now
            if secs_remaining >= pcfg.min_secs_remaining:
                for sym, spec in pcfg.symbols.items():
                    key = (sym, ws)
                    idx = next_entry_index(
                        filled_counts.get(key, 0),
                        pcfg.max_entries_per_window)
                    if idx is None or key in cap_skipped \
                            or key in dead_windows \
                            or key in in_flight \
                            or not entry_allowed(
                                filled_counts.get(key, 0),
                                key in in_flight,
                                pcfg.max_entries_per_window):
                        continue
                    if sym in pcfg.disabled_symbols:
                        continue
                    if not session_open(pcfg, now):
                        continue
                    if pcfg.max_entry_offset_secs is not None \
                            and now - ws > pcfg.max_entry_offset_secs:
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
                           window_start=ws, entry_index=idx,
                           secs_remaining=round(secs_remaining, 1),
                           signal_detected_ts=detected_ts,
                           feed_age_ms=feed_age, attempt=attempts[key],
                           event_ts=eval_ts)

                    if chop_enabled:
                        er = chop_gate(samples, now, chop_window_secs,
                                       chop_er_max)
                        if er is not None:
                            if key not in chop_journaled:
                                chop_journaled.add(key)
                                bj.log("signal_skipped", bot=BOT,
                                       symbol=sym, reason="chop_filter",
                                       window_start=ws, side=direction,
                                       entry_index=idx,
                                       efficiency_ratio=round(er, 4),
                                       chop_er_max=chop_er_max,
                                       chop_window_secs=chop_window_secs,
                                       **ladder_fields(
                                           signal_detected_ts=detected_ts,
                                           eval_ts=eval_ts,
                                           decision_ts=time.time(),
                                           feed_age_ms=feed_age),
                                       event_ts=eval_ts)
                            continue

                    in_flight.add(key)
                    try:
                        await _enter(sym, ws, direction, eval_ts,
                                     detected_ts, feed_age,
                                     entry_index=idx)
                    finally:
                        in_flight.discard(key)

            # sleep until the next feed event (housekeeping cap)
            try:
                await asyncio.wait_for(feed.wait_update(),
                                       timeout=pcfg.loop_poll_secs)
            except asyncio.TimeoutError:
                pass
    finally:
        prefetch_task.cancel()
        warm_task.cancel()
        await feed.stop()
        await cache.stop()
        bj.log("runner_stopped", bot=BOT,
               daily_pnl=state.realized_pnl(BOT), event_ts=time.time())
        bj.stop()
        warm.close()
