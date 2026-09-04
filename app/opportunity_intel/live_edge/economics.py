"""Executable book VWAP, conservative edge, and binary expected value."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext

from app.copy_intel.order_book import ValidatedOrderBook, allocate_buy
from app.opportunity_intel.live_edge.models import (
    ExecutableQuote,
    ProbabilityRange,
)
from app.opportunity_intel.live_edge.policy import PROBABILITY_DISPLAY_QUANTUM

_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_ZERO = Decimal("0")


def round_probability_display(value: Decimal) -> Decimal:
    """Whole percentage points as a 0–1 decimal (no fake 94.372% precision)."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("probability must be a finite Decimal")
    with localcontext() as ctx:
        ctx.rounding = ROUND_HALF_UP
        return value.quantize(PROBABILITY_DISPLAY_QUANTUM)


def round_edge_pp_display(edge: Decimal) -> Decimal:
    """Percentage points, whole numbers."""
    if not isinstance(edge, Decimal) or not edge.is_finite():
        raise ValueError("edge must be a finite Decimal")
    with localcontext() as ctx:
        ctx.rounding = ROUND_HALF_UP
        return (edge * _HUNDRED).quantize(Decimal("1"))


def round_money(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.rounding = ROUND_HALF_UP
        return value.quantize(Decimal("0.01"))


def executable_quote_from_book(
    book: ValidatedOrderBook,
    *,
    allocation_usd: Decimal,
    side: str,
    now: datetime | None = None,
) -> ExecutableQuote | None:
    """Buy-side sweep of asks. Last price is never used."""
    if side not in {"YES", "NO"}:
        raise ValueError("side must be YES or NO")
    execution = allocate_buy(book, float(allocation_usd))
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    book_at = datetime.fromtimestamp(book.quote_at, tz=timezone.utc)
    if not execution.available or execution.execution_vwap is None:
        return ExecutableQuote(
            side=side,
            best_ask=Decimal(str(book.best_ask)),
            vwap=Decimal(str(book.best_ask)),
            fully_executable=False,
            allocation_usd=allocation_usd,
            slippage=(
                None
                if execution.slippage is None
                else Decimal(str(execution.slippage))
            ),
            book_at=book_at,
            quote_age_seconds=Decimal(str(max(0.0, (clock - book_at).total_seconds()))),
            visible_ask_depth_usd=Decimal(str(book.visible_ask_depth_usd)),
        )
    return ExecutableQuote(
        side=side,
        best_ask=Decimal(str(book.best_ask)),
        vwap=Decimal(str(execution.execution_vwap)),
        fully_executable=True,
        allocation_usd=allocation_usd,
        slippage=Decimal(str(execution.slippage)),
        book_at=book_at,
        quote_age_seconds=Decimal(str(max(0.0, (clock - book_at).total_seconds()))),
        visible_ask_depth_usd=Decimal(str(book.visible_ask_depth_usd)),
    )


def market_probability(quote: ExecutableQuote) -> Decimal:
    """Probability implied by the executable entry price, not last trade."""
    return quote.vwap if quote.fully_executable else quote.best_ask


def conservative_edge_pp(
    probability: ProbabilityRange, quote: ExecutableQuote
) -> Decimal:
    return probability.conservative - market_probability(quote)


def expected_value_usd(
    *,
    probability: ProbabilityRange,
    quote: ExecutableQuote,
    known_fees_usd: Decimal = _ZERO,
) -> tuple[Decimal, Decimal] | None:
    """Binary EV using conservative P(win). Returns (ev_usd, roi) or None."""
    if not quote.fully_executable or quote.vwap <= _ZERO:
        return None
    capital = quote.allocation_usd
    shares = capital / quote.vwap
    fees = known_fees_usd if known_fees_usd > _ZERO else _ZERO
    profit_if_win = shares * _ONE - capital - fees
    loss_if_lose = -capital
    p_win = probability.conservative
    ev = p_win * profit_if_win + (_ONE - p_win) * loss_if_lose
    roi = ev / capital if capital > _ZERO else _ZERO
    return ev, roi


def format_probability_uk(probability: ProbabilityRange) -> str:
    low_pct = int(round_probability_display(probability.low) * _HUNDRED)
    high_pct = int(round_probability_display(probability.high) * _HUNDRED)
    if low_pct == high_pct:
        return f"{low_pct}%"
    return f"{low_pct}–{high_pct}%"


def format_conservative_uk(probability: ProbabilityRange) -> str:
    pct = int(round_probability_display(probability.conservative) * _HUNDRED)
    return f"{pct}%"
