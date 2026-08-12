"""Fee-aware depth allocation netting for cross-venue paper opportunities."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.crypto.api_result import ApiFailure
from app.crypto.fees import get_spot_fee_schedule
from app.crypto.models import OrderBookSnapshot
from app.crypto.opportunity.identity import attach_identity
from app.crypto.opportunity.models import PaperNetOpportunity, ScanReject
from app.crypto.order_book import age_seconds, allocate_buy_usd, allocate_sell_base
from app.crypto.product_types import ProductType


def evaluate_cross_venue_opportunity(
    *,
    books: dict[str, OrderBookSnapshot],
    paper_allocation_usd: Decimal,
    maximum_slippage: Decimal,
    maximum_signal_age_seconds: float,
    receive_ms: int,
) -> tuple[PaperNetOpportunity | None, ScanReject | None]:
    """Pick best buy-ask / sell-bid venue pair for one symbol's books."""
    if len(books) < 2:
        return None, ScanReject(
            code="insufficient_venues",
            message="Need books from at least two venues",
        )

    allocation = float(paper_allocation_usd)
    checked_at = datetime.now(timezone.utc)
    best: PaperNetOpportunity | None = None

    venues = sorted(books.keys())
    for buy_venue in venues:
        buy_book = books[buy_venue]
        buy_alloc = allocate_buy_usd(buy_book, allocation)
        if not buy_alloc.available or buy_alloc.filled_base is None:
            continue
        if age_seconds(buy_book, receive_ms=receive_ms) > maximum_signal_age_seconds:
            continue
        buy_fee = get_spot_fee_schedule(buy_venue, market_type="spot")
        if isinstance(buy_fee, ApiFailure):
            return None, ScanReject(
                code="missing_fee_schedule",
                message=buy_fee.message,
                buy_venue=buy_venue,
                symbol=buy_book.instrument.canonical_symbol,
            )
        for sell_venue in venues:
            if sell_venue == buy_venue:
                continue
            sell_book = books[sell_venue]
            if (
                buy_book.instrument.canonical_symbol
                != sell_book.instrument.canonical_symbol
            ):
                return None, ScanReject(
                    code="incompatible_instruments",
                    message="Symbol mismatch across venues",
                    symbol=buy_book.instrument.canonical_symbol,
                    buy_venue=buy_venue,
                    sell_venue=sell_venue,
                )
            if buy_book.instrument.market_type != "spot":
                return None, ScanReject(
                    code="unsupported_product",
                    message="Only spot is supported",
                    symbol=buy_book.instrument.canonical_symbol,
                )
            if age_seconds(sell_book, receive_ms=receive_ms) > maximum_signal_age_seconds:
                continue
            sell_alloc = allocate_sell_base(sell_book, buy_alloc.filled_base)
            if not sell_alloc.available or sell_alloc.proceeds_usd is None:
                continue
            sell_fee = get_spot_fee_schedule(sell_venue, market_type="spot")
            if isinstance(sell_fee, ApiFailure):
                return None, ScanReject(
                    code="missing_fee_schedule",
                    message=sell_fee.message,
                    sell_venue=sell_venue,
                    symbol=sell_book.instrument.canonical_symbol,
                )

            buy_cost = allocation
            sell_proceeds = float(sell_alloc.proceeds_usd)
            buy_fee_usd = buy_cost * float(buy_fee.data.taker_rate)
            sell_fee_usd = sell_proceeds * float(sell_fee.data.taker_rate)
            fees = buy_fee_usd + sell_fee_usd
            gross = sell_proceeds - buy_cost
            net = gross - fees
            slippage = float(buy_alloc.slippage or 0.0) + float(
                sell_alloc.slippage or 0.0
            )
            if slippage > float(maximum_slippage):
                continue
            if net <= 0:
                continue
            roi = net / allocation if allocation > 0 else 0.0
            age = max(
                age_seconds(buy_book, receive_ms=receive_ms),
                age_seconds(sell_book, receive_ms=receive_ms),
            )
            candidate = PaperNetOpportunity(
                opportunity_key="",
                opportunity_uid="",
                pair=buy_book.instrument.canonical_symbol,
                product_type=ProductType.SPOT.value,
                buy_venue=buy_venue,
                sell_venue=sell_venue,
                buy_ask=float(buy_alloc.best_ask or buy_book.asks[0].price),
                sell_bid=float(sell_alloc.best_bid or sell_book.bids[0].price),
                buy_vwap=float(buy_alloc.execution_vwap or 0.0),
                sell_vwap=float(sell_alloc.execution_vwap or 0.0),
                gross_spread_usd=gross,
                buy_fee_usd=buy_fee_usd,
                sell_fee_usd=sell_fee_usd,
                trading_fees_usd=fees,
                estimated_slippage=slippage,
                paper_allocation_usd=paper_allocation_usd,
                executable_base=float(buy_alloc.filled_base),
                executable_liquidity_usd=min(
                    buy_alloc.visible_depth_usd, sell_alloc.visible_depth_usd
                ),
                net_profit_usd=net,
                roi=roi,
                signal_age_seconds=age,
                checked_at=checked_at,
            )
            candidate = attach_identity(candidate)
            if best is None or candidate.net_profit_usd > best.net_profit_usd:
                best = candidate
            elif (
                best is not None
                and candidate.net_profit_usd == best.net_profit_usd
                and candidate.roi > best.roi
            ):
                best = candidate

    if best is None:
        return None, ScanReject(
            code="not_profitable_or_insufficient_depth",
            message="No profitable executable cross-venue pair",
            symbol=next(iter(books.values())).instrument.canonical_symbol,
        )
    return best, None
