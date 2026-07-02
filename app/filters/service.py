from app.config.settings import Settings
from app.database.storage import get_connection
from app.filters.models import UserMarketFilters


def get_user_filters(settings: Settings, telegram_id: int) -> UserMarketFilters:
    with get_connection(settings) as connection:
        row = connection.execute(
            """
            SELECT market_source, category, min_edge, notifications_enabled
            FROM user_filters
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()

    if row is None:
        return UserMarketFilters(
            telegram_id=telegram_id,
            polymarket_enabled=True,
            weather_enabled=True,
            min_edge=0.05,
            notifications_enabled=False,
        )

    return UserMarketFilters(
        telegram_id=telegram_id,
        polymarket_enabled=row["market_source"] == "polymarket",
        weather_enabled=row["category"] == "weather",
        min_edge=float(row["min_edge"]),
        notifications_enabled=bool(row["notifications_enabled"]),
    )


def should_scan_polymarket_weather(filters: UserMarketFilters) -> bool:
    return filters.polymarket_enabled and filters.weather_enabled
