"""Public market-data exchange adapters (no trading / no keys)."""

from __future__ import annotations

from app.config.settings import Settings
from app.crypto.exchanges.base import PublicMarketDataClient, PublicOrderBookClient
from app.crypto.exchanges.binance import BinancePublicClient
from app.crypto.exchanges.bingx import BingxPublicClient
from app.crypto.exchanges.bybit import BybitPublicClient
from app.crypto.exchanges.kucoin import KucoinPublicClient
from app.crypto.exchanges.mexc import MexcPublicClient
from app.crypto.exchanges.okx import OkxPublicClient
from app.crypto.venue_registry import V43_EXCHANGE_IDS


def build_clients(settings: Settings) -> dict[str, PublicMarketDataClient]:
    return {
        "binance": BinancePublicClient(settings),
        "bybit": BybitPublicClient(settings),
        "okx": OkxPublicClient(settings),
        "bingx": BingxPublicClient(settings),
        "mexc": MexcPublicClient(settings),
        "kucoin": KucoinPublicClient(settings),
    }


def build_v43_clients(settings: Settings) -> dict[str, PublicOrderBookClient]:
    """V4.3 scanner universe only — Bybit intentionally excluded."""
    all_clients = build_clients(settings)
    out: dict[str, PublicOrderBookClient] = {}
    for venue_id in V43_EXCHANGE_IDS:
        client = all_clients[venue_id]
        out[venue_id] = client  # type: ignore[assignment]
    return out


__all__ = [
    "build_clients",
    "build_v43_clients",
    "PublicMarketDataClient",
    "PublicOrderBookClient",
]
