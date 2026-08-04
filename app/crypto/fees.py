"""Static public spot fee schedules — missing fees fail closed for arb paths."""

from __future__ import annotations

from app.crypto.api_result import ApiFailure, ApiSuccess
from app.crypto.models import FeeSchedule, MarketType

# Indicative public spot taker/maker rates (fraction). Not a trading guarantee.
_SPOT_FEES: dict[str, tuple[float, float]] = {
    "binance": (0.001, 0.001),
    "bybit": (0.001, 0.001),
    "okx": (0.0008, 0.001),
    "bingx": (0.001, 0.001),
    "mexc": (0.0, 0.0005),
    "kucoin": (0.001, 0.001),
}


def get_spot_fee_schedule(
    exchange_id: str,
    *,
    market_type: MarketType = "spot",
) -> ApiSuccess[FeeSchedule] | ApiFailure:
    if market_type != "spot":
        return ApiFailure(
            code="unsupported_market_type",
            status_code=None,
            retryable=False,
            request_count=0,
            message=f"Unsupported market type: {market_type}",
        )
    rates = _SPOT_FEES.get(exchange_id)
    if rates is None:
        return ApiFailure(
            code="missing_fee_schedule",
            status_code=None,
            retryable=False,
            request_count=0,
            message=f"No fee schedule for exchange {exchange_id}",
        )
    maker, taker = rates
    return ApiSuccess(
        data=FeeSchedule(
            exchange_id=exchange_id,
            market_type="spot",
            maker_rate=maker,
            taker_rate=taker,
        )
    )
