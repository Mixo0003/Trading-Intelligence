"""Polymarket taker fee from public CLOB /fee-rate. Never invent a rate."""

from __future__ import annotations

from decimal import Decimal

from app.config.constants import POLYMARKET_CLOB_BASE_URL
from app.opportunity_intel.live_edge.http_json import fetch_json


def fetch_taker_fee_usd(
    token_id: str,
    *,
    capital_usd: Decimal,
    executable_price: Decimal,
) -> tuple[Decimal | None, str]:
    """Return (fee_usd, note). fee_usd is None when the public endpoint is unverified."""
    if not token_id or capital_usd <= 0 or executable_price <= 0:
        return None, "Fee estimate unavailable"
    url = f"{POLYMARKET_CLOB_BASE_URL.rstrip('/')}/fee-rate?token_id={token_id}"
    payload = fetch_json(url, cache_key=f"clob-fee:{token_id}", cache_ttl=60.0)
    if not isinstance(payload, dict):
        return None, "Fee estimate unavailable"
    raw = payload.get("base_fee")
    if raw is None:
        return None, "Fee estimate unavailable"
    try:
        base_fee = Decimal(str(raw))
    except Exception:
        return None, "Fee estimate unavailable"
    if not base_fee.is_finite() or base_fee < 0:
        return None, "Fee estimate unavailable"
    shares = capital_usd / executable_price
    fee_rate = base_fee / Decimal("10000") if base_fee > 1 else base_fee
    fee = shares * fee_rate * executable_price * (Decimal("1") - executable_price)
    if fee < 0:
        return None, "Fee estimate unavailable"
    return fee, f"taker fee from CLOB /fee-rate (base_fee={base_fee})"
