from dataclasses import dataclass


@dataclass(frozen=True)
class UserMarketFilters:
    telegram_id: int
    polymarket_enabled: bool
    weather_enabled: bool
    min_edge: float
    notifications_enabled: bool
