from dataclasses import dataclass

from app.config.settings import Settings
from app.signals.scan_service import scan_weather_markets
from app.signals.generator import WeatherSignal


@dataclass(frozen=True)
class ScanResult:
    total_markets: int
    signals: list[WeatherSignal]


_BET_TIERS = frozenset({
    "SMALL BET",
    "NORMAL BET",
    "STRONG OPPORTUNITY",
})


def scan_polymarket_weather_markets(
    settings: Settings,
    limit: int = 100,
    min_edge: float = 0.05,
) -> ScanResult:
    progress = scan_weather_markets(settings, limit=limit)
    signals: list[WeatherSignal] = []
    for item in progress.results:
        if item.recommendation_tier not in _BET_TIERS:
            continue
        if item.edge is None or item.edge < min_edge:
            continue
        signals.append(
            WeatherSignal(
                decision=item.recommendation_tier,
                probability=item.model_probability,
                confidence_score=item.confidence_score,
                confidence_label=item.confidence,
                edge=item.edge,
                ev=item.ev,
                kelly_fraction=item.kelly_fraction,
                recommendation_tier=item.recommendation_tier,
                market_probability=item.implied_probability,
                message=item.message,
            )
        )
    return ScanResult(total_markets=progress.discovered, signals=signals)
