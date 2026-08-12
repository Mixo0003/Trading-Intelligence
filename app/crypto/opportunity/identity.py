"""Stable crypto opportunity identity and material fingerprint."""

from __future__ import annotations

import hashlib

from app.crypto.opportunity.models import PaperNetOpportunity


def build_opportunity_key(
    *,
    pair: str,
    buy_venue: str,
    sell_venue: str,
    product_type: str,
    direction: str = "buy_low_sell_high",
) -> str:
    return (
        f"crypto_v1:{pair}:{buy_venue}:{sell_venue}:{product_type}:{direction}"
    )


def material_fingerprint(opportunity: PaperNetOpportunity) -> str:
    raw = (
        f"{opportunity.buy_vwap:.6f}|{opportunity.sell_vwap:.6f}|"
        f"{opportunity.executable_base:.8f}|{opportunity.net_profit_usd:.4f}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_opportunity_uid(opportunity_key: str, fingerprint: str) -> str:
    digest = hashlib.sha256(
        f"{opportunity_key}|{fingerprint}".encode("utf-8")
    ).hexdigest()[:24]
    return f"crypto_opp_{digest}"


def attach_identity(opportunity: PaperNetOpportunity) -> PaperNetOpportunity:
    key = build_opportunity_key(
        pair=opportunity.pair,
        buy_venue=opportunity.buy_venue,
        sell_venue=opportunity.sell_venue,
        product_type=opportunity.product_type,
        direction=opportunity.direction,
    )
    # Temporary object for fingerprint uses key placeholders.
    provisional = PaperNetOpportunity(
        opportunity_key=key,
        opportunity_uid="",
        pair=opportunity.pair,
        product_type=opportunity.product_type,
        buy_venue=opportunity.buy_venue,
        sell_venue=opportunity.sell_venue,
        buy_ask=opportunity.buy_ask,
        sell_bid=opportunity.sell_bid,
        buy_vwap=opportunity.buy_vwap,
        sell_vwap=opportunity.sell_vwap,
        gross_spread_usd=opportunity.gross_spread_usd,
        buy_fee_usd=opportunity.buy_fee_usd,
        sell_fee_usd=opportunity.sell_fee_usd,
        trading_fees_usd=opportunity.trading_fees_usd,
        estimated_slippage=opportunity.estimated_slippage,
        paper_allocation_usd=opportunity.paper_allocation_usd,
        executable_base=opportunity.executable_base,
        executable_liquidity_usd=opportunity.executable_liquidity_usd,
        net_profit_usd=opportunity.net_profit_usd,
        roi=opportunity.roi,
        signal_age_seconds=opportunity.signal_age_seconds,
        checked_at=opportunity.checked_at,
        direction=opportunity.direction,
    )
    fp = material_fingerprint(provisional)
    uid = build_opportunity_uid(key, fp)
    return PaperNetOpportunity(
        opportunity_key=key,
        opportunity_uid=uid,
        pair=opportunity.pair,
        product_type=opportunity.product_type,
        buy_venue=opportunity.buy_venue,
        sell_venue=opportunity.sell_venue,
        buy_ask=opportunity.buy_ask,
        sell_bid=opportunity.sell_bid,
        buy_vwap=opportunity.buy_vwap,
        sell_vwap=opportunity.sell_vwap,
        gross_spread_usd=opportunity.gross_spread_usd,
        buy_fee_usd=opportunity.buy_fee_usd,
        sell_fee_usd=opportunity.sell_fee_usd,
        trading_fees_usd=opportunity.trading_fees_usd,
        estimated_slippage=opportunity.estimated_slippage,
        paper_allocation_usd=opportunity.paper_allocation_usd,
        executable_base=opportunity.executable_base,
        executable_liquidity_usd=opportunity.executable_liquidity_usd,
        net_profit_usd=opportunity.net_profit_usd,
        roi=opportunity.roi,
        signal_age_seconds=opportunity.signal_age_seconds,
        checked_at=opportunity.checked_at,
        direction=opportunity.direction,
    )
