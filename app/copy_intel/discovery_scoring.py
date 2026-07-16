"""Performance / risk / confidence scoring for discovered traders."""

from __future__ import annotations

import math

from app.copy_intel.models import (
    CopyUserSettings,
    DataConfidence,
    DiscoveryDecision,
    DiscoveryMetrics,
    DiscoveryScore,
    RiskLevel,
)


def score_discovery_trader(
    metrics: DiscoveryMetrics,
    user_settings: CopyUserSettings,
) -> DiscoveryScore:
    rejected, reject_reason = _hard_reject(metrics, user_settings)
    performance = _performance_score(metrics)
    risk = _risk_score(metrics, user_settings)
    confidence = _data_confidence(metrics)
    final = _final_score(performance, risk, confidence, metrics)
    risk_level = _risk_level(risk, metrics)
    decision, reason = _decision(
        metrics,
        user_settings,
        performance=performance,
        risk=risk,
        confidence=confidence,
        final=final,
        rejected=rejected,
        reject_reason=reject_reason,
    )
    return DiscoveryScore(
        wallet=metrics.wallet,
        label=metrics.label,
        performance_score=round(performance, 1),
        risk_score=round(risk, 1),
        data_confidence=confidence,
        final_score=round(final, 1),
        risk_level=risk_level,
        decision=decision,
        reason=reason,
        metrics=metrics,
        rejected=rejected,
        reject_reason=reject_reason,
    )


def _hard_reject(
    metrics: DiscoveryMetrics,
    user_settings: CopyUserSettings,
) -> tuple[bool, str | None]:
    if metrics.resolved_count < max(3, user_settings.min_resolved_markets // 4):
        return True, "insufficient history"
    if metrics.active_days is not None and metrics.active_days < 3:
        return True, "very new account"
    if metrics.wash_score > 0.35:
        return True, "suspicious rapid back-and-forth activity"
    if "realized_pnl" in metrics.incomplete_metrics and metrics.realized_pnl is None:
        return True, "incomplete trade history"
    if metrics.active_days is not None and metrics.active_days >= 1:
        # Inactive: no activity in span check handled via resolved + recent
        if metrics.recent_pnl_30d is None and metrics.resolved_count < user_settings.min_resolved_markets:
            return True, "inactive account"
    return False, None


def _performance_score(metrics: DiscoveryMetrics) -> float:
    win = (metrics.win_rate or 0.0) * 100.0
    resolved_boost = min(30.0, metrics.resolved_count * 1.2)
    roi_component = 50.0
    if metrics.roi is not None:
        roi_component = max(0.0, min(100.0, 50.0 + metrics.roi * 80.0))
    recent = 50.0
    if metrics.recent_pnl_30d is not None and metrics.lifetime_pnl:
        ratio = metrics.recent_pnl_30d / max(1.0, abs(metrics.lifetime_pnl))
        recent = max(0.0, min(100.0, 50.0 + math.tanh(ratio) * 40.0))
    # Cap raw PnL influence heavily.
    pnl_cap = 20.0
    if metrics.realized_pnl is not None:
        pnl_cap = min(20.0, max(0.0, math.log1p(max(0.0, metrics.realized_pnl)) * 2.0))
    one_hit_penalty = 0.0
    if metrics.single_win_contribution is not None:
        one_hit_penalty = max(0.0, (metrics.single_win_contribution - 0.35) * 80.0)
    score = (
        0.30 * win
        + 0.25 * roi_component
        + 0.20 * recent
        + 0.15 * resolved_boost
        + 0.10 * (50.0 + pnl_cap)
        - one_hit_penalty
    )
    return max(0.0, min(100.0, score))


def _risk_score(metrics: DiscoveryMetrics, user_settings: CopyUserSettings) -> float:
    """Higher is safer."""
    score = 100.0
    if metrics.max_drawdown is not None:
        score -= min(50.0, metrics.max_drawdown * 100.0)
        if metrics.max_drawdown > user_settings.max_drawdown_pct:
            score -= 15.0
    if metrics.category_concentration is not None:
        score -= max(0.0, (metrics.category_concentration - 0.3) * 60.0)
        if metrics.category_concentration > user_settings.max_category_concentration:
            score -= 10.0
    if metrics.market_concentration is not None:
        score -= max(0.0, (metrics.market_concentration - 0.25) * 50.0)
    if metrics.single_win_contribution is not None:
        score -= max(0.0, (metrics.single_win_contribution - 0.3) * 70.0)
        if metrics.single_win_contribution > user_settings.max_single_win_contribution:
            score -= 12.0
    if metrics.liquidity_quality is not None:
        if metrics.liquidity_quality < 0.35:
            score -= 20.0
        elif metrics.liquidity_quality < 0.5:
            score -= 10.0
    score -= metrics.wash_score * 40.0
    return max(0.0, min(100.0, score))


def _data_confidence(metrics: DiscoveryMetrics) -> DataConfidence:
    missing = len(metrics.incomplete_metrics)
    if metrics.resolved_count >= 30 and missing <= 1:
        return "High"
    if metrics.resolved_count >= 10 and missing <= 4:
        return "Medium"
    return "Low"


def _final_score(
    performance: float,
    risk: float,
    confidence: DataConfidence,
    metrics: DiscoveryMetrics,
) -> float:
    conf_w = {"Low": 0.7, "Medium": 0.9, "High": 1.0}[confidence]
    # Raw PnL does not dominate: performance already caps PnL; blend with risk.
    base = 0.45 * performance + 0.45 * risk + 0.10 * min(100.0, metrics.resolved_count * 2.5)
    return max(0.0, min(100.0, base * conf_w))


def _risk_level(risk_score: float, metrics: DiscoveryMetrics) -> RiskLevel:
    if risk_score >= 70 and (metrics.max_drawdown or 0) <= 0.35:
        return "Low"
    if risk_score >= 45:
        return "Medium"
    return "High"


def _decision(
    metrics: DiscoveryMetrics,
    user_settings: CopyUserSettings,
    *,
    performance: float,
    risk: float,
    confidence: DataConfidence,
    final: float,
    rejected: bool,
    reject_reason: str | None,
) -> tuple[DiscoveryDecision, str]:
    if rejected:
        return "AVOID", reject_reason or "rejected by quality filters"

    if (
        metrics.resolved_count < user_settings.min_resolved_markets
        or (metrics.active_days is not None and metrics.active_days < user_settings.min_active_days)
        or confidence == "Low"
        or (metrics.max_drawdown is not None and metrics.max_drawdown > user_settings.max_drawdown_pct)
        or (
            metrics.single_win_contribution is not None
            and metrics.single_win_contribution > user_settings.max_single_win_contribution
        )
        or (
            metrics.category_concentration is not None
            and metrics.category_concentration > user_settings.max_category_concentration
        )
        or (metrics.liquidity_quality is not None and metrics.liquidity_quality < 0.3)
    ):
        if final >= 55 and confidence != "Low":
            return "WATCH", "meets watch criteria but not full FOLLOW gates"
        return "AVOID", "fails sample, drawdown, concentration, or confidence gates"

    if final >= 70 and performance >= 55 and risk >= 55 and confidence in {"Medium", "High"}:
        return "FOLLOW", "stable performance with acceptable risk and confidence"
    if final >= 50:
        return "WATCH", "promising but needs more consistency or liquidity"
    return "AVOID", "score below watch threshold"
