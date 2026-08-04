"""Telegram formatting for Crypto Intelligence (never claims guaranteed profit)."""

from __future__ import annotations

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.health import summarize_exchange_health
from app.crypto.models import (
    ArbReport,
    ExchangeHealth,
    HealthStatus,
    OverviewReport,
    SpreadQuote,
)

ARB_DISCLAIMER = (
    "Gross price difference before fees, slippage, transfer costs, latency, "
    "and execution risk. Not guaranteed profit and not an executable "
    "arbitrage instruction."
)

_STATUS_LABEL = {
    HealthStatus.OK: "OK",
    HealthStatus.DEGRADED: "DEGRADED",
    HealthStatus.DOWN: "DOWN",
    HealthStatus.UNSUPPORTED: "UNSUPPORTED",
    HealthStatus.DISABLED: "DISABLED",
}


def format_exchanges_health(rows: list[ExchangeHealth], *, enabled: bool) -> str:
    lines = [
        "🏦 Exchanges & Health",
        "Read-only public market-data probes.",
        "No trading keys · no order placement.",
        "",
        f"Summary: {summarize_exchange_health(rows)}",
        "",
    ]
    if not enabled:
        lines.append("Crypto Intelligence is disabled (CRYPTO_INTEL_ENABLED=false).")
        return "\n".join(lines)
    for row in rows:
        latency = f"{row.latency_ms:.0f}ms" if row.latency_ms is not None else "n/a"
        label = _STATUS_LABEL.get(row.status, str(row.status))
        lines.append(f"• {row.exchange_id}: {label} ({latency})")
        if row.detail and row.status != HealthStatus.OK:
            lines.append(f"  {row.detail}")
    return "\n".join(lines)


def format_market_overview(report: OverviewReport, *, max_reject_lines: int = 12) -> str:
    symbols = ", ".join(report.symbols) if report.symbols else "(none)"
    lines = [
        "📊 Market Overview",
        f"Watchlist: {symbols} (spot)",
        "Indicative top-of-book only — not guaranteed profit.",
        "Read-only · public data · 24h stats omitted.",
        "",
    ]
    if not report.enabled:
        lines.append("Crypto Intelligence is disabled (CRYPTO_INTEL_ENABLED=false).")
        return "\n".join(lines)

    if report.rows:
        for row in report.rows:
            lines.append(
                f"• {row.instrument.canonical_symbol} @{row.exchange_id}: "
                f"bid={row.best_bid:.6g} ask={row.best_ask:.6g} mid={row.midpoint:.6g} "
                f"age={row.age_seconds:.1f}s ({row.freshness})"
            )
    else:
        lines.append("No fresh overview rows.")

    if report.rejects:
        lines.append("")
        lines.append(f"Rejected ({len(report.rejects)}):")
        shown = 0
        for item in report.rejects:
            if shown >= max_reject_lines:
                remaining = len(report.rejects) - shown
                lines.append(f"+{remaining} more rejected")
                break
            where = item.exchange_id or "—"
            sym = item.symbol or "—"
            lines.append(f"• {sym} @{where}: {item.code} — {item.message}")
            shown += 1
    return "\n".join(lines)


def format_arbitrage_opportunities(
    report: ArbReport, *, max_reject_lines: int = 12
) -> str:
    symbols = ", ".join(report.symbols) if report.symbols else "(none)"
    lines = [
        "💱 Arbitrage Opportunities",
        f"Watchlist: {symbols} (spot)",
        "Read-only · public top-of-book · gross gaps only.",
        ARB_DISCLAIMER,
        "",
    ]
    if not report.enabled:
        lines.append("Crypto Intelligence is disabled (CRYPTO_INTEL_ENABLED=false).")
        return "\n".join(lines)

    if report.opportunities:
        for opp in report.opportunities:
            lines.append(
                f"• {opp.instrument.canonical_symbol}: "
                f"buy@{opp.buy_exchange_id} ask={opp.buy_ask:.6g} → "
                f"sell@{opp.sell_exchange_id} bid={opp.sell_bid:.6g} | "
                f"gross={opp.gross_spread_abs:.6g} ({opp.gross_spread_pct:.4f}%) "
                f"age={opp.age_seconds:.1f}s health={opp.health_status.value} "
                f"buy_ts={opp.buy_timestamp_ms} sell_ts={opp.sell_timestamp_ms} "
                f"recv_ts={opp.receive_timestamp_ms}"
            )
    else:
        lines.append("No valid positive gross opportunities.")

    if report.rejects:
        lines.append("")
        lines.append(f"Rejected ({len(report.rejects)}):")
        shown = 0
        for item in report.rejects:
            if shown >= max_reject_lines:
                remaining = len(report.rejects) - shown
                lines.append(f"+{remaining} more rejected")
                break
            where = item.exchange_id or "—"
            sym = item.symbol or "—"
            lines.append(f"• {sym} @{where}: {item.code} — {item.message}")
            shown += 1
    return "\n".join(lines)


def format_spread_monitor(
    symbol: str,
    results: list[ApiSuccess[SpreadQuote] | ApiFailure],
    *,
    enabled: bool,
) -> str:
    lines = [
        "↔️ Spread Monitor",
        f"Symbol: {symbol} (spot)",
        "Indicative top-of-book only — not guaranteed profit.",
        "Read-only · public data · fail-closed on stale quotes.",
        "",
    ]
    if not enabled:
        lines.append("Crypto Intelligence is disabled (CRYPTO_INTEL_ENABLED=false).")
        return "\n".join(lines)

    shown = 0
    for item in results:
        if isinstance(item, ApiFailure):
            lines.append(f"• rejected: {item.code} — {item.message}")
            continue
        q = item.data
        lines.append(
            f"• {q.exchange_id}: bid={q.best_bid:.6g} ask={q.best_ask:.6g} "
            f"spread={q.spread_pct:.4f}% age={q.age_seconds:.1f}s ({q.freshness})"
        )
        shown += 1
    if shown == 0:
        lines.append("No fresh executable top-of-book quotes.")
    return "\n".join(lines)
