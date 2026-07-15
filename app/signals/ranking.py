from app.signals.market_analysis import MarketAnalysis


def data_quality_score(item: MarketAnalysis) -> float:
    score = 0.0
    if item.station_code:
        score += 0.4
    if item.resolution_source:
        score += 0.2
    if item.forecast_display:
        score += 0.2
    if item.yes_price is not None:
        score += 0.2
    return score


def rank_key(item: MarketAnalysis) -> tuple:
    """Rank by EV, confidence, data quality, liquidity, then US priority (tie-breaker only)."""
    ev = item.ev if item.ev is not None else -999.0
    liquidity = item.liquidity if item.liquidity is not None else 0.0
    us_boost = 0.001 if item.is_priority_us_city else 0.0
    return (
        ev + us_boost,
        item.confidence_score,
        data_quality_score(item),
        liquidity,
    )


def sort_analyses(analyses: list[MarketAnalysis]) -> list[MarketAnalysis]:
    return sorted(analyses, key=rank_key, reverse=True)
