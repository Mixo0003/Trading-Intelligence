"""Authoritative source registry for V4.9 verified news/economic ingest.

Only sources with verified public adapters already in this repository are
registered. Unknown publishers and invented endpoints are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.crypto.opportunity.news_fundamentals import NewsSourceType
from app.market_opportunity.sources.eia import (
    EIA_RWTC_ROUTE,
    EIA_RWTC_SERIES_ID,
    EIA_WTI_INVENTORY_ROUTE,
    EIA_WTI_INVENTORY_SERIES_ID,
    WTI_CUSHING_SOURCE_ID,
)
from app.market_opportunity.sources.fred import (
    FRED_DCOILWTICO_SERIES_ID,
    FRED_OBSERVATIONS_ROUTE,
)


class RegistrySeriesKind(StrEnum):
    """Series kinds aligned with MOI freshness helpers — do not invent new ones."""

    EIA_DAILY_SPOT = "eia_daily_spot"
    EIA_WEEKLY_INVENTORY = "eia_weekly_inventory"
    FRED_DAILY_CROSSCHECK = "fred_daily_crosscheck"


FRED_SOURCE_ID = "FRED"


@dataclass(frozen=True)
class RegisteredSource:
    """One verified official source entry."""

    source_id: str
    publisher: str
    source_type: NewsSourceType
    endpoint_url: str
    series_id: str
    series_kind: RegistrySeriesKind
    asset_label: str
    note: str = ""


# Frozen registry — only in-repo verified EIA/FRED routes.
_REGISTERED_SOURCES: tuple[RegisteredSource, ...] = (
    RegisteredSource(
        source_id=WTI_CUSHING_SOURCE_ID,
        publisher="U.S. Energy Information Administration",
        source_type=NewsSourceType.OFFICIAL,
        endpoint_url=EIA_RWTC_ROUTE,
        series_id=EIA_RWTC_SERIES_ID,
        series_kind=RegistrySeriesKind.EIA_DAILY_SPOT,
        asset_label="WTI_CUSHING",
        note="EIA RWTC Cushing spot — verified MOI adapter",
    ),
    RegisteredSource(
        source_id=WTI_CUSHING_SOURCE_ID,
        publisher="U.S. Energy Information Administration",
        source_type=NewsSourceType.OFFICIAL,
        endpoint_url=EIA_WTI_INVENTORY_ROUTE,
        series_id=EIA_WTI_INVENTORY_SERIES_ID,
        series_kind=RegistrySeriesKind.EIA_WEEKLY_INVENTORY,
        asset_label="WTI_CUSHING",
        note="EIA WCESTUS1 inventory — verified MOI adapter",
    ),
    RegisteredSource(
        source_id=FRED_SOURCE_ID,
        publisher="Federal Reserve Bank of St. Louis (FRED)",
        source_type=NewsSourceType.OFFICIAL,
        endpoint_url=FRED_OBSERVATIONS_ROUTE,
        series_id=FRED_DCOILWTICO_SERIES_ID,
        series_kind=RegistrySeriesKind.FRED_DAILY_CROSSCHECK,
        asset_label="WTI_CUSHING",
        note="FRED DCOILWTICO cross-check — verified MOI adapter",
    ),
)


@dataclass(frozen=True)
class AuthoritativeSourceRegistry:
    """Lookup registry for verified official sources only."""

    sources: tuple[RegisteredSource, ...] = _REGISTERED_SOURCES

    def get(
        self, *, source_id: str, series_id: str
    ) -> RegisteredSource | None:
        for entry in self.sources:
            if entry.source_id == source_id and entry.series_id == series_id:
                return entry
        return None

    def require(
        self, *, source_id: str, series_id: str
    ) -> RegisteredSource:
        entry = self.get(source_id=source_id, series_id=series_id)
        if entry is None:
            raise KeyError(
                f"source not in authoritative registry: "
                f"source_id={source_id!r} series_id={series_id!r}"
            )
        return entry

    def contains_source_id(self, source_id: str) -> bool:
        return any(entry.source_id == source_id for entry in self.sources)

    def iter_sources(self) -> tuple[RegisteredSource, ...]:
        return self.sources


def default_authoritative_registry() -> AuthoritativeSourceRegistry:
    return AuthoritativeSourceRegistry()
