"""Configuration for paper momentum «Спід — ДРУГИЙ ПОСТРІЛ»
(paper_speed_second). PAPER-ONLY: no wallets, no keys, no
order placement anywhere.

SPEC AUDIT (Michał's spec-fidelity rule — every value traces to
data or is explicitly marked):

VARIANT DEFINITION (ordered by Michał 2026-10-18 ~23:37 CDT):
- «Другий постріл» = live Speed (BTC+ETH, live thresholds
  BTC $25 / ETH $1.80 over 15 s) with ONE change: up to TWO
  entries per symbol per window instead of one.
  MAX_ENTRIES_PER_WINDOW = 2 (variant definition).
- SECOND-ENTRY RULE: after the first entry FILLS, a later
  trigger evaluation in the same window (trailing 15 s move
  still/again >= threshold — the same evaluate_trigger the
  first entry used) may schedule a second $10 arrival under
  the identical 575 ms / venue-limit / refusal rules. While
  an arrival is pending for a (symbol, window) no other is
  scheduled, so entry #2's trigger is always evaluated after
  entry #1 resolved — a genuine second decision, not a
  duplicate of the first. The 2 s attempt cooldown also
  applies between attempts.
- JOURNAL: every attempt and trade row carries entry_index
  (1 or 2) so the second shot's quality is measurable
  separately from the first.

LIVE-MIRRORED PARAMETERS (source: live_trading/speed.py defaults,
identical to the paper_scale_* chassis this package is cloned
from; verified 2026-10-18):
- LOOKBACK 15 s, Binance aggTrade feed.
- STAKE $10.00 per entry (each of the two entries is its own
  $10 — a fully-filled window therefore carries $20, stated
  plainly).
- Entries only while >= 20 s remain (min_secs_remaining = 20).
- MAX ASK (trigger gate) 0.85.
- FEE 0.07 INSIDE THE SPEND (official crypto taker model).
- SETTLEMENT: official Gamma resolution ONLY (gamma_official).
- ARRIVAL MODEL 575 ms, venue limit = trigger best ask,
  refusals retryable while the trigger holds.
- DAILY STOP / BUDGET: DISABLED deliberately (measurement
  uncensored, same as the scale test).

JOURNAL: attempts.jsonl + trades.jsonl, chassis schema plus
entry_index.

PURPOSE / SERIES WARNING: launched 2026-10-19. Its PnL series
is its own and must NEVER be merged with any other Speed series.
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
    stake_usd: float = 10.0


@dataclass
class SpeedConfig:
    symbols: dict[str, SymbolSpec] = field(
        default_factory=lambda: {
            "BTC": SymbolSpec(binance_pair="btcusdt", move_usd=25.0, lookback_secs=15, stake_usd=10.00),
            "ETH": SymbolSpec(binance_pair="ethusdt", move_usd=1.80, lookback_secs=15, stake_usd=10.00),
        }
    )
    max_ask: float = 0.85          # stale-price guard (see audit)
    min_secs_remaining: float = 20.0
    max_entries_per_window: int = 2  # THE variant change (see audit)
    stake_usd: float = 10.00     # legacy field; per-symbol stakes on SymbolSpec govern
    fee_rate: float = 0.07        # official crypto taker fee, inside spend
    start_bankroll_usd: float = 1000.0
    loop_poll_secs: float = 1.0    # housekeeping wake cap; the trigger path is feed-event driven
    attempt_cooldown_secs: float = 2.0  # min gap between entry attempts per symbol
    settle_retry_secs: float = 20.0     # Gamma lags a few minutes post-close
    book_stale_secs: float = 2.0        # WS book older than this at trigger -> REST fallback
    latency_summary_secs: float = 3600.0  # hourly LATENCY SUMMARY log line
    delay_ms: float = 575.0         # measured live chain midpoint (see audit)
    # --- WR70 tuning gates (2026-10-19; see CHANGES-WR70-2026-10-19.md) ---
    # Entry-selection ONLY: fill model, fees and Gamma settlement
    # are untouched. None / () = gate off.
    trade_hours_start: int | None = None   # Chicago hour, inclusive
    trade_hours_end: int | None = None     # Chicago hour, exclusive
    max_entry_offset_secs: float | None = None
    min_trigger_ask: float | None = 0.70
    disabled_symbols: tuple[str, ...] = ()
    # --- Chop filter (owner order 2026-10-20, the same gate as
    # Momentum Speed) ---
    # Kaufman efficiency ratio over the trailing chop_window_secs
    # of feed prices, resampled to 1 s closes: ER = |net move| /
    # total path. ER below chop_er_max means lots of motion,
    # little displacement (a sawtooth market): entries are
    # blocked and journaled (reason "chop_filter"). The live
    # executor applies the SHARED gate
    # (paper_momentum_speed.engine.chop_gate), so Second Shot,
    # the main Speed bot and the paper A/B cannot drift. All
    # three values are UNFITTED starting points taken from the
    # classic Kaufman definition, NOT fitted to this bot's
    # history; the paper A/B (paper_speed_chop, on Momentum) is
    # the reference test. Default OFF: no behaviour changes
    # until the filter is explicitly enabled (panel toggle).
    chop_filter_enabled: bool = False
    chop_window_secs: int = 300
    chop_er_max: float = 0.25

    trades_log: str = "trades.jsonl"
    attempts_log: str = "attempts.jsonl"
    state_dir: str = "state"
    report_dir: str = "reports"


DEFAULT_CONFIG = SpeedConfig()
