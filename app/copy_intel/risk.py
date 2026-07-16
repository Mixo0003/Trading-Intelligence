"""Paper portfolio and exposure risk controls."""

from __future__ import annotations

from app.config.constants import (
    COPY_MAX_ALLOC_PER_MARKET,
    COPY_MAX_ALLOC_PER_WALLET,
    COPY_MAX_ALLOC_TOTAL,
)
from app.config.settings import Settings
from app.copy_intel.data_client import CopyDataClient
from app.database import copy_storage


def assert_read_only(settings: Settings) -> None:
    if not settings.copy_intel_read_only:
        raise RuntimeError("Copy intel must run in read-only mode for Phase 1.")


def check_exposure_limits(
    settings: Settings,
    *,
    wallet: str,
    condition_id: str,
    proposed_notional: float,
) -> tuple[bool, str, float]:
    assert_read_only(settings)
    bankroll = settings.copy_paper_bankroll_usd
    totals = copy_storage.paper_exposure_totals(settings)
    total_cap = bankroll * COPY_MAX_ALLOC_TOTAL
    wallet_cap = bankroll * COPY_MAX_ALLOC_PER_WALLET
    market_cap = bankroll * COPY_MAX_ALLOC_PER_MARKET

    current_total = totals["total"]
    current_wallet = totals["by_wallet"].get(wallet.lower(), 0.0)
    current_market = totals["by_market"].get(condition_id, 0.0)

    remaining_total = max(0.0, total_cap - current_total)
    remaining_wallet = max(0.0, wallet_cap - current_wallet)
    remaining_market = max(0.0, market_cap - current_market)
    allowed = min(proposed_notional, remaining_total, remaining_wallet, remaining_market)

    if allowed <= 0:
        if current_total >= total_cap:
            return False, "total copy sleeve exposure limit reached", 0.0
        if current_wallet >= wallet_cap:
            return False, "per-wallet exposure limit reached", 0.0
        if current_market >= market_cap:
            return False, "per-market exposure limit reached", 0.0
        return False, "exposure limit reached", 0.0

    if allowed < proposed_notional:
        return True, "reduced allocation to fit exposure caps", allowed
    return True, "", allowed


def record_paper_copy(
    settings: Settings,
    *,
    wallet: str,
    condition_id: str,
    market_slug: str | None,
    outcome: str | None,
    notional: float,
    entry_price: float,
) -> None:
    assert_read_only(settings)
    ok, reason, cap = check_exposure_limits(
        settings,
        wallet=wallet,
        condition_id=condition_id,
        proposed_notional=notional,
    )
    if not ok or cap <= 0:
        copy_storage.write_audit_log(
            settings,
            actor="paper_portfolio",
            action="paper_rejected",
            payload={"wallet": wallet, "reason": reason},
        )
        return
    copy_storage.open_paper_position(
        settings,
        wallet=wallet,
        condition_id=condition_id,
        market_slug=market_slug,
        outcome=outcome,
        notional=cap,
        entry_price=entry_price,
    )
    copy_storage.write_audit_log(
        settings,
        actor="paper_portfolio",
        action="paper_open",
        payload={
            "wallet": wallet,
            "condition_id": condition_id,
            "notional": cap,
            "entry_price": entry_price,
        },
    )


def estimate_slippage(
    client: CopyDataClient,
    token_id: str | None,
    *,
    side: str = "BUY",
) -> float | None:
    if not token_id:
        return None
    quote = client.fetch_quote(token_id)
    ref = quote.midpoint
    if ref is None or ref <= 0:
        return None
    executable = quote.best_ask if side == "BUY" else quote.best_bid
    if executable is None:
        depth = quote.ask_depth_usd if side == "BUY" else quote.bid_depth_usd
        if depth < 100:
            return 0.05
        return 0.02
    return abs(executable - ref) / ref
