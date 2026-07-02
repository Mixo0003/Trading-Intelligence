from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PolymarketMarket:
    id: str | None
    slug: str | None
    question: str | None
    description: str
    rules: str
    outcomes: list[Any]
    outcome_prices: list[Any]
    volume: float | None
    liquidity: float | None
    end_date: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class PolymarketEvent:
    id: str | None
    slug: str | None
    title: str | None
    description: str
    markets: list[PolymarketMarket]
    raw: dict[str, Any]
