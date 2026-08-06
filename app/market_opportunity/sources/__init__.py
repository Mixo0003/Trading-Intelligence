"""Market Opportunity Intelligence source adapters (V4.2)."""

from __future__ import annotations

from app.market_opportunity.sources.eia import (
    EIA_RWTC_SERIES_ID,
    EIA_WTI_INVENTORY_SERIES_ID,
    WTI_CUSHING_DISPLAY_NAME,
    WTI_CUSHING_MARKET_ID,
    WTI_CUSHING_SOURCE_ID,
    WTI_CUSHING_SYMBOL,
    WTI_CUSHING_VENUE,
    DailyBar,
    InventoryPoint,
    parse_inventory_wcestus1,
    parse_rwtc_daily,
)
from app.market_opportunity.sources.fred import (
    FRED_DCOILWTICO_SERIES_ID,
    FredObservation,
    lookup_same_period,
    parse_dcoilwtico_observations,
)

__all__ = [
    "DailyBar",
    "EIA_RWTC_SERIES_ID",
    "EIA_WTI_INVENTORY_SERIES_ID",
    "FRED_DCOILWTICO_SERIES_ID",
    "FredObservation",
    "InventoryPoint",
    "WTI_CUSHING_DISPLAY_NAME",
    "WTI_CUSHING_MARKET_ID",
    "WTI_CUSHING_SOURCE_ID",
    "WTI_CUSHING_SYMBOL",
    "WTI_CUSHING_VENUE",
    "lookup_same_period",
    "parse_dcoilwtico_observations",
    "parse_inventory_wcestus1",
    "parse_rwtc_daily",
]
