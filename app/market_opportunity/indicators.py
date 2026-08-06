"""Deterministic indicator helpers for V4.2 WTI Opportunity Intelligence."""

from __future__ import annotations

from decimal import Decimal
from statistics import mean, pstdev
from typing import Sequence

from app.market_opportunity.sources.eia import DailyBar, InventoryPoint


class IndicatorError(ValueError):
    """Insufficient or invalid series for an indicator."""


def _closes(bars: Sequence[DailyBar]) -> list[Decimal]:
    return [b.close for b in bars]


def ret_1d(bars: Sequence[DailyBar]) -> Decimal:
    """One-session simple return of the last close vs prior close."""
    if len(bars) < 2:
        raise IndicatorError("ret_1d requires at least 2 bars")
    prev = bars[-2].close
    if prev == 0:
        raise IndicatorError("ret_1d prior close is zero")
    return (bars[-1].close - prev) / prev


def ret_5d(bars: Sequence[DailyBar]) -> Decimal:
    """Five-session simple return of the last close vs close 5 sessions earlier."""
    if len(bars) < 6:
        raise IndicatorError("ret_5d requires at least 6 bars")
    prev = bars[-6].close
    if prev == 0:
        raise IndicatorError("ret_5d prior close is zero")
    return (bars[-1].close - prev) / prev


def sma(bars: Sequence[DailyBar], window: int) -> Decimal:
    """Simple moving average of closes over ``window`` ending at the last bar."""
    if window < 1:
        raise IndicatorError("sma window must be >= 1")
    if len(bars) < window:
        raise IndicatorError(f"sma({window}) requires at least {window} bars")
    closes = _closes(bars)[-window:]
    return sum(closes, Decimal("0")) / Decimal(window)


def atr14(bars: Sequence[DailyBar]) -> Decimal:
    """V4.2 spot-ATR: mean of the last 14 absolute close-to-close changes.

    When high=low=close (EIA RWTC proxy), classic True Range collapses; this
    definition keeps ATR nonzero from daily print changes.
    """
    if len(bars) < 15:
        raise IndicatorError("atr14 requires at least 15 bars")
    changes = [
        abs(bars[i].close - bars[i - 1].close) for i in range(len(bars) - 14, len(bars))
    ]
    if len(changes) != 14:
        raise IndicatorError("atr14 internal window error")
    return sum(changes, Decimal("0")) / Decimal(14)


def prev_20_high_low(bars: Sequence[DailyBar]) -> tuple[Decimal, Decimal]:
    """Max/min of the prior 20 closes excluding the current (last) bar."""
    if len(bars) < 21:
        raise IndicatorError("prev_20_high_low requires at least 21 bars")
    prior = _closes(bars)[-21:-1]
    if len(prior) != 20:
        raise IndicatorError("prev_20_high_low internal window error")
    return max(prior), min(prior)


def inventory_wow_zscore(
    points: Sequence[InventoryPoint],
    *,
    window: int = 26,
) -> Decimal:
    """Week-over-week change z-score over ``window`` WoW observations.

    Needs ``window + 1`` inventory points. Suppresses (raises) if stdev is 0.
    Uses population stdev of the last ``window`` WoW deltas; z is computed for
    the most recent WoW delta against that window's mean/stdev.
    """
    if window < 2:
        raise IndicatorError("inventory z-score window must be >= 2")
    if len(points) < window + 1:
        raise IndicatorError(
            f"inventory_wow_zscore requires at least {window + 1} points"
        )
    recent = list(points)[-(window + 1) :]
    deltas = [recent[i].value - recent[i - 1].value for i in range(1, len(recent))]
    if len(deltas) != window:
        raise IndicatorError("inventory_wow_zscore internal window error")
    # Convert to float for pstdev stability; return Decimal z.
    float_deltas = [float(d) for d in deltas]
    sigma = pstdev(float_deltas)
    if sigma == 0.0:
        raise IndicatorError("inventory_wow_zscore stdev is zero")
    mu = mean(float_deltas)
    z = (float_deltas[-1] - mu) / sigma
    return Decimal(str(z))
