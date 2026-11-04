"""Configuration for paper momentum «Моментум Спід». PAPER-ONLY: no
wallets, no keys, no order placement anywhere in this module.

SPEC AUDIT (2026-10-17, per Michał's spec-fidelity rule — every
value traces to data or is explicitly marked UNFITTED):

- ENTRY TIMING — DURING the move, first seconds: the two provably
  profitable crypto 5m HFT profiles reverse-engineered 2026-10-14
  earn by buying in the first seconds after a Binance move, while
  the Polymarket book still shows the stale price, BEFORE it
  reprices. v1.8 does the opposite (enters at 120-180s remaining,
  long after repricing, at fair prices) and its full official
  record is ~breakeven. This bot tests the HFT shape itself.
- LOOKBACK 15s (trailing): the trigger window for "the move just
  started". Long enough to measure an impulse, short enough that
  the book has not finished repricing when we fire.
- MOVE THRESHOLDS BTC $25 / ETH $1.80 over 15s: scaled DOWN from
  v1.6's measured 60s thresholds (BTC $30 / ETH $2.00 — the values
  that defined v1.6's signal) to the 15s window. A 15s impulse is
  naturally smaller than a 60s move, so the thresholds sit just
  below the 60s ones. These are UNFITTED starting values — no data
  set has isolated a better 15s threshold; the paper run is the
  test, and the audit says so instead of inventing precision.
- MAX ASK 0.85: inherited from v1.6 (its 24h v1 data: asks >0.85
  lost -$56.57). Here it doubles as the stale-price guard: if the
  book has already repriced above 0.85, the edge this strategy
  exists to capture is gone — skip and log, never chase.
- MIN 20s REMAINING: entries stop with 20s left in the window —
  the fill/record path must not straddle the close, and the HFT
  profiles this copies enter early, not at the buzzer.
- SYMBOLS BTC + ETH only: SOL's v1.6 slice was -$25.54 (n=14) on
  the thinnest book; same exclusion as v1.8.
- PANEL OPTIONAL COINS (2026-10-19, «auto BOT» product only): the
  symbols dict also carries SOL/XRP lines PRE-MARKED OFF with the
  panel's "# [panel-off]" comment, so the paper twin never trades
  them; a panel user may enable them on their OWN instance copy.
  Thresholds are the paper «Монети» derivation rounded — the same
  relative 15 s move as ETH $1.80 (≈0.0742% of price on
  2026-10-18): SOL $0.0783 -> 0.08, XRP $0.000974 -> 0.001.
  Gamma slugs sol-/xrp-updown-5m-*, fee category crypto 0.07 —
  identical settlement path to BTC/ETH.
- ONE ENTRY per symbol per window: the stale-price moment happens
  once; re-firing would be chasing, a different strategy.
- FEE 0.07 INSIDE THE SPEND: the official crypto taker fee model
  (live_trading/fees.py), charged inside the $10 spend exactly as
  a live FAK order pays it — live-identical accounting.
- STAKE $10 paper: same as v1.6/v1.8 so results compare 1:1.
- SETTLEMENT: official Gamma resolution ONLY (gamma_official).
  No estimate path exists in this package (the Binance-estimate
  settle is what falsified v1.6's paper record).
- SPEED UPGRADE (2026-10-17 evening; trigger thresholds, max_ask,
  stake, settle — ALL UNCHANGED): reaction speed is now measured,
  not claimed. (1) Feed: Binance aggTrade — per-trade pushes
  instead of ~1/sec miniTicker snapshots (paper_momentum_speed.
  feed), with a REST last-trade fallback if the socket goes
  quiet >6s.
  (2) Book: the CLOB market-websocket cache (paper_momentum_speed.
  bookcache) keeps the current+next windows' books in memory, so
  a trigger walks a cached book with zero fetch latency; a cache
  older than book_stale_secs (2s), uninitialized or disconnected
  falls back to the REST /book fetch and logs the fallback.
  (3) Instrumentation: every trigger attempt records feed_age_ms
  (age of the newest aggTrade at evaluation) and detect_to_fill_ms
  (evaluation -> fill/refusal booked) plus book_source, in the
  run log and in the trade record; an hourly LATENCY SUMMARY line
  reports the trailing hour's trigger count and median/p95
  detect_to_fill_ms.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Sentinel: the process refuses to start if this is ever flipped.
PAPER_ONLY = True


@dataclass
class SymbolSpec:
    binance_pair: str
    move_usd: float
    lookback_secs: int = 15


@dataclass
class SpeedConfig:
    symbols: dict[str, SymbolSpec] = field(
        default_factory=lambda: {
            "BTC": SymbolSpec(binance_pair="btcusdt", move_usd=25.0, lookback_secs=15),
            "ETH": SymbolSpec(binance_pair="ethusdt", move_usd=1.80, lookback_secs=15),
            # [panel-off] "SOL": SymbolSpec(binance_pair="solusdt", move_usd=0.08, lookback_secs=15),
            # [panel-off] "XRP": SymbolSpec(binance_pair="xrpusdt", move_usd=0.001, lookback_secs=15),
        }
    )
    max_ask: float = 0.85          # stale-price guard (see audit)
    # Entry-price FLOOR (owner's order 2026-10-18): the panel price
    # filter is a від–до range, not just a cap. 0.0 = floor off
    # (historical behaviour). A positive floor blocks entries into a
    # near-dead side — e.g. live fills at ask 0.01 late in a window,
    # where one side is already decided and the "entry" is a lottery
    # ticket the strategy never meant to buy.
    min_ask: float = 0.0
    # Minimum fillable shares (owner's order 2026-10-18): when > 0,
    # an entry fires only if the book holds at least this many
    # shares within the allowed price band (min_ask..max_ask).
    # Blocks crumb entries — e.g. a live FAK that found 0.53 shares
    # in a nearly empty book and filled a meaningless position.
    # 0.0 = off (historical behaviour).
    min_entry_shares: float = 0.0
    # Entry window inside the 5-minute market (owner's order
    # 2026-10-18): an entry may fire only when the signal lands
    # entry_from_secs..entry_to_secs seconds after the window
    # opens. ADDITIVE to min_secs_remaining (the tail gate) — both
    # apply. Defaults 0..300 = the whole window = no-op.
    entry_from_secs: float = 0.0
    entry_to_secs: float = 300.0
    # Panel-managed entry filters («auto BOT» → Фільтри). Defaults are
    # the historical behaviour — both directions, all hours — so these
    # fields change nothing until a user sets them on their OWN copy.
    allowed_directions: tuple[str, ...] = ("Up", "Down")
    active_hours_chicago: tuple[int, ...] | None = None  # None = all hours
    min_secs_remaining: float = 20.0
    # Move confirmation (panel filter «⏱ Підтвердження руху
    # (сек)», owner's order 2026-10-19): after a trigger fires,
    # the executor waits this many seconds and enters only if
    # the trailing 15 s move still holds in the same direction
    # (paper_speed_confirm semantics). 0 = OFF — enter at once,
    # the historical behaviour.
    confirm_secs: float = 0.0
    stake_usd: float = 10.0
    fee_rate: float = 0.07        # official crypto taker fee, inside spend
    start_bankroll_usd: float = 1000.0
    loop_poll_secs: float = 1.0    # housekeeping wake cap; the trigger path is feed-event driven
    attempt_cooldown_secs: float = 2.0  # min gap between entry attempts per symbol
    settle_retry_secs: float = 20.0     # Gamma lags a few minutes post-close
    book_stale_secs: float = 2.0        # WS book older than this at trigger -> REST fallback
    latency_summary_secs: float = 3600.0  # hourly LATENCY SUMMARY log line
    trades_log: str = "trades.jsonl"
    state_dir: str = "state"
    report_dir: str = "reports"


DEFAULT_CONFIG = SpeedConfig()
