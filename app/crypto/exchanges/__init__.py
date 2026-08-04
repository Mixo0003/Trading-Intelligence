"""Public market-data exchange adapters (no trading / no keys)."""

from __future__ import annotations

from app.config.settings import Settings
from app.crypto.exchanges.base import PublicMarketDataClient
from app.crypto.exchanges.binance import BinancePublicClient
from app.crypto.exchanges.bingx import BingxPublicClient
from app.crypto.exchanges.bybit import BybitPublicClient
from app.crypto.exchanges.kucoin import KucoinPublicClient
from app.crypto.exchanges.mexc import MexcPublicClient
from app.crypto.exchanges.okx import OkxPublicClient


def build_clients(settings: Settings) -> dict[str, PublicMarketDataClient]:
    return {
        "binance": BinancePublicClient(settings),
        "bybit": BybitPublicClient(settings),
        "okx": OkxPublicClient(settings),
        "bingx": BingxPublicClient(settings),
        "mexc": MexcPublicClient(settings),
        "kucoin": KucoinPublicClient(settings),
    }


__all__ = ["build_clients", "PublicMarketDataClient"]
