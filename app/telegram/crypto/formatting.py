"""Telegram formatting for Crypto Intelligence (never claims guaranteed profit)."""

from __future__ import annotations

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.health import summarize_exchange_health
from app.crypto.models import ExchangeHealth, HealthStatus, SpreadQuote

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
