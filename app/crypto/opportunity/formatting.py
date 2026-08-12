"""CRYPTO OPPORTUNITY Telegram card formatting (spot paper arb only)."""

from __future__ import annotations

from app.crypto.opportunity.models import PaperNetOpportunity, ScanReport


def _money(value: float) -> str:
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def format_crypto_opportunity_card(opportunity: PaperNetOpportunity) -> str:
    checked = opportunity.checked_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    return "\n".join(
        [
            "CRYPTO OPPORTUNITY",
            "Strategy: spot cross-exchange paper arbitrage",
            "",
            f"Symbol: {opportunity.pair}",
            f"Product type: {opportunity.product_type}",
            f"BUY venue: {opportunity.buy_venue}",
            f"SELL venue: {opportunity.sell_venue}",
            f"Buy executable price: {_money(opportunity.buy_vwap)}",
            f"Sell executable price: {_money(opportunity.sell_vwap)}",
            f"Best ask (top): {_money(opportunity.buy_ask)}",
            f"Best bid (top): {_money(opportunity.sell_bid)}",
            f"Executable quantity: {_money(opportunity.executable_base)}",
            f"Paper allocation: ${opportunity.paper_allocation_usd}",
            f"Trading fees: ${_money(opportunity.trading_fees_usd)}",
            f"Estimated slippage: {_pct(opportunity.estimated_slippage)}",
            f"Gross paper profit: ${_money(opportunity.gross_spread_usd)}",
            f"Net paper profit: ${_money(opportunity.net_profit_usd)}",
            f"ROI: {_pct(opportunity.roi)}",
            f"Freshness (signal age): {opportunity.signal_age_seconds:.1f}s",
            f"Checked at: {checked}",
            "",
            "Public read-only / paper-only. Not guaranteed profit.",
            "Not a trade recommendation. Not LONG/SHORT, TP/SL, futures, funding, or news.",
        ]
    )


def format_top_crypto_opportunities(report: ScanReport, *, limit: int = 3) -> str:
    if not report.enabled:
        return (
            "📈 New Opportunities\n\n"
            "Scanner disabled.\n"
            "Enable CRYPTO_INTEL_ENABLED and CRYPTO_OPPORTUNITY_SCANNER_ENABLED "
            "to view spot cross-exchange paper arbitrage.\n"
            "Supported venues when enabled: Binance, BingX, MEXC, OKX, KuCoin.\n"
            "Public read-only / paper-only."
        )
    rows = report.opportunities[:limit]
    if not rows:
        return (
            "📈 New Opportunities\n\n"
            "No qualifying executable spot paper opportunities right now.\n"
            "Venues in scope: Binance, BingX, MEXC, OKX, KuCoin.\n"
            "Public read-only / paper-only."
        )
    header = (
        "📈 New Opportunities\n"
        "Spot cross-exchange paper arbitrage only.\n"
        "Each card shows the buy and sell venues used for that opportunity.\n"
    )
    cards = [format_crypto_opportunity_card(row) for row in rows]
    return header + "\n\n---\n\n".join(cards)


def format_opportunity_history(
    *,
    deliveries: list[dict],
    last_sent: list[dict],
) -> str:
    lines = [
        "🕘 Opportunity History",
        "Delivery and last-sent records for your account only.",
        "On-demand New Opportunities views are not logged here.",
        "",
    ]
    if not deliveries and not last_sent:
        lines.append("No history yet.")
        lines.append("Records appear after delivery attempts (auto-delivery stays off by default).")
        return "\n".join(lines)

    if deliveries:
        lines.append("Deliveries:")
        for row in deliveries[:15]:
            lines.append(
                f"• {row['status']} | {row['opportunity_key']} | {row['updated_at']}"
            )
        lines.append("")
    if last_sent:
        lines.append("Last sent (cooldown):")
        for row in last_sent[:15]:
            lines.append(f"• {row['opportunity_key']} | {row['sent_at']}")
    return "\n".join(lines)
