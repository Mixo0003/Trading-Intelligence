"""Composite trader quality scoring."""

from __future__ import annotations

import math

from app.copy_intel.models import TraderMetrics


def score_trader(metrics: TraderMetrics) -> float:
    """Return 0-100 composite score; never rank on raw PnL alone."""
    if not metrics.eligible:
        return max(0.0, 20.0 - metrics.wash_score * 20.0)

    win_component = metrics.win_rate * 100.0
    consistency = _consistency_score(metrics)
    recency = _recency_score(metrics)
    drawdown_penalty = max(0.0, 100.0 - metrics.max_drawdown * 2.0)
    diversification = max(0.0, (1.0 - metrics.category_concentration) * 100.0)
    execution = max(0.0, 100.0 - metrics.wash_score * 100.0)

    composite = (
        0.25 * win_component
        + 0.20 * consistency
        + 0.20 * recency
        + 0.15 * drawdown_penalty
        + 0.10 * diversification
        + 0.10 * execution
    )
    return round(min(100.0, max(0.0, composite)), 1)


def _consistency_score(metrics: TraderMetrics) -> float:
    if metrics.resolved_count < 5:
        return 30.0
    ratio = metrics.recent_pnl_30d / max(1.0, abs(metrics.lifetime_pnl))
    variance_penalty = abs(ratio - 0.5) * 40.0
    return max(0.0, 80.0 - variance_penalty)


def _recency_score(metrics: TraderMetrics) -> float:
    if metrics.lifetime_pnl == 0:
        return 50.0
    ratio = metrics.recent_pnl_30d / abs(metrics.lifetime_pnl)
    normalized = math.tanh(ratio) * 50.0 + 50.0
    return max(0.0, min(100.0, normalized))
