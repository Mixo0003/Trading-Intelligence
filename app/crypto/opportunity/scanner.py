"""V4.3 crypto opportunity scanner orchestration."""

from __future__ import annotations

from app.config.settings import Settings
from app.crypto.api_result import ApiFailure
from app.crypto.exchanges import build_v43_clients
from app.crypto.freshness import now_ms
from app.crypto.models import OrderBookSnapshot
from app.crypto.opportunity.models import ScanReject, ScanReport
from app.crypto.opportunity.netting import evaluate_cross_venue_opportunity
from app.crypto.opportunity.settings import CryptoOpportunitySettings
from app.crypto.opportunity.universe import (
    build_symbol_universe,
    instruments_for_symbol,
)
from app.crypto.risk import assert_crypto_read_only, crypto_feature_enabled

# Bound on-demand Top Opportunities work (fail-closed budget).
MAX_SYMBOLS_PER_SCAN = 8


def scan_crypto_opportunities(
    settings: Settings,
    *,
    user_settings: CryptoOpportunitySettings,
) -> ScanReport:
    assert_crypto_read_only(settings)
    if not crypto_feature_enabled(settings):
        return ScanReport(enabled=False, opportunities=(), rejects=())
    if not getattr(settings, "crypto_opportunity_scanner_enabled", False):
        return ScanReport(
            enabled=False,
            opportunities=(),
            rejects=(
                ScanReject(
                    code="scanner_disabled",
                    message="Crypto opportunity scanner disabled",
                ),
            ),
        )

    universe = build_symbol_universe(
        settings, selected_exchanges=user_settings.selected_exchanges
    )
    rejects: list[ScanReject] = [
        ScanReject(code="universe", message=msg) for msg in universe.rejects
    ]
    clients = build_v43_clients(settings)
    receive = now_ms()
    found = []

    for symbol in universe.symbols[:MAX_SYMBOLS_PER_SCAN]:
        rows = instruments_for_symbol(universe, symbol)
        books: dict[str, OrderBookSnapshot] = {}
        for row in rows:
            if row.venue_id not in user_settings.selected_exchanges:
                continue
            client = clients.get(row.venue_id)
            if client is None:
                rejects.append(
                    ScanReject(
                        code="venue_missing",
                        message="Venue client missing",
                        symbol=symbol,
                        buy_venue=row.venue_id,
                    )
                )
                continue
            result = client.fetch_order_book(row.instrument)
            if isinstance(result, ApiFailure):
                rejects.append(
                    ScanReject(
                        code=result.code,
                        message=result.message,
                        symbol=symbol,
                        buy_venue=row.venue_id,
                    )
                )
                continue
            books[row.venue_id] = result.data

        opportunity, reject = evaluate_cross_venue_opportunity(
            books=books,
            paper_allocation_usd=user_settings.paper_allocation_usd,
            maximum_slippage=user_settings.maximum_slippage,
            maximum_signal_age_seconds=float(
                min(
                    user_settings.maximum_signal_age_seconds,
                    settings.crypto_max_quote_age_seconds,
                )
            ),
            receive_ms=receive,
        )
        if reject is not None:
            rejects.append(reject)
        if opportunity is not None:
            # Depth floor: both legs must support allocation.
            if (
                opportunity.executable_liquidity_usd
                < float(user_settings.minimum_executable_depth_usd)
            ):
                rejects.append(
                    ScanReject(
                        code="insufficient_executable_depth",
                        message="Executable depth below user minimum",
                        symbol=symbol,
                        buy_venue=opportunity.buy_venue,
                        sell_venue=opportunity.sell_venue,
                    )
                )
                continue
            if opportunity.net_profit_usd < float(user_settings.minimum_net_profit_usd):
                continue
            if opportunity.roi < float(user_settings.minimum_roi):
                continue
            found.append(opportunity)

    found.sort(
        key=lambda row: (
            -row.net_profit_usd,
            -row.roi,
            row.signal_age_seconds,
        )
    )
    return ScanReport(
        enabled=True,
        opportunities=tuple(found),
        rejects=tuple(rejects),
        diagnostics=tuple(
            f"{vid}:{state}" for vid, state in universe.completeness_by_venue.items()
        ),
    )
