"""Public Binance spot last price for crypto near-resolution locking — not a probability model."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.config.constants import DEFAULT_BINANCE_PUBLIC_BASE_URL
from app.opportunity_intel.live_edge.http_json import fetch_json
from app.opportunity_intel.live_edge.models import (
    LiveDataProvenance,
    LiveEdgeCategory,
    LiveStateSnapshot,
    MarketIdentity,
)

_SYMBOLS = {
    "bitcoin": "BTCUSDT",
    "btc": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "eth": "ETHUSDT",
}


class BinanceSpotProvider:
    def snapshots_for(
        self,
        market,
        identity: MarketIdentity,
        *,
        now: datetime,
    ) -> tuple[LiveStateSnapshot, ...]:
        if identity.category != LiveEdgeCategory.CRYPTO:
            return ()
        text = f"{market.question} {market.event_title or ''}".lower()
        symbol = None
        for needle, code in _SYMBOLS.items():
            if needle in text:
                symbol = code
                break
        if symbol is None:
            return ()
        url = f"{DEFAULT_BINANCE_PUBLIC_BASE_URL.rstrip('/')}/api/v3/ticker/price?symbol={symbol}"
        payload = fetch_json(
            url, cache_key=f"binance:{symbol}", cache_ttl=8.0, provider="binance-spot"
        )
        if not isinstance(payload, dict):
            return ()
        price = _decimal(payload.get("price"))
        if price is None:
            return ()
        return (
            LiveStateSnapshot(
                sport="crypto",
                status="spot",
                participants=(symbol,),
                extra={"spot_usd": str(price), "symbol": symbol, "outcome_confirmed": False},
                provenance=LiveDataProvenance(
                    provider="binance-spot",
                    event_identifier=symbol,
                    source_locator=url,
                    fetched_at=now,
                    source_timestamp=now,
                    reliability="official_exchange_public",
                ),
            ),
        )


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None
