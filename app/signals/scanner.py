from dataclasses import dataclass

from app.config.settings import Settings
from app.polymarket.client import PolymarketClient
from app.polymarket.markets import get_weather_markets
from app.signals.engine import analyze_weather_market
from app.signals.generator import WeatherSignal


@dataclass(frozen=True)
class ScanResult:
    total_markets: int
    signals: list[WeatherSignal]


def _passes_min_edge(
    signal: WeatherSignal,
    min_edge: float,
) -> bool:
    if signal.edge is None:
        return False

    return signal.edge >= min_edge


def scan_polymarket_weather_markets(
    settings: Settings,
    limit: int = 50,
    min_edge: float = 0.05,
) -> ScanResult:
    client = PolymarketClient(settings)
    markets = get_weather_markets(client, limit=limit)

    signals: list[WeatherSignal] = []

    for market in markets:
        try:
            signal = analyze_weather_market(settings=settings, market=market)

            if signal.decision != "POSSIBLE VALUE":
                continue

            if not _passes_min_edge(signal, min_edge=min_edge):
                continue

            signals.append(signal)

        except Exception:
            continue

    return ScanResult(
        total_markets=len(markets),
        signals=signals,
    )
