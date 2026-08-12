"""Catalog-driven symbol universe for the V4.3 crypto scanner."""

from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings
from app.crypto.catalog_discovery import discover_spot_catalog
from app.crypto.discovery_models import DiscoveryCompleteness
from app.crypto.models import Instrument
from app.crypto.product_types import ProductType
from app.crypto.venue_registry import V43_EXCHANGE_IDS
from app.database import market_opportunity_storage as moi_storage


@dataclass(frozen=True)
class UniverseInstrument:
    venue_id: str
    instrument: Instrument
    venue_market_id: str
    quality_status: str


@dataclass(frozen=True)
class UniverseBuildResult:
    completeness_by_venue: dict[str, str]
    instruments: tuple[UniverseInstrument, ...]
    symbols: tuple[str, ...]
    rejects: tuple[str, ...]


def _active_only(rows: list[dict]) -> list[dict]:
    return [
        row
        for row in rows
        if str(row.get("quality_status") or "").lower() == "active"
    ]


def build_symbol_universe(
    settings: Settings,
    *,
    selected_exchanges: tuple[str, ...],
    persist: bool = True,
) -> UniverseBuildResult:
    """Build cross-venue spot universe.

    Uses public catalog discovery when the catalog feature flag is on.
    Otherwise falls back to ``settings.crypto_symbols`` (configured allowlist).
    Partial/failed discovery for a venue is never labeled complete.
    """
    venues = tuple(v for v in selected_exchanges if v in V43_EXCHANGE_IDS)
    rejects: list[str] = []
    completeness: dict[str, str] = {}
    by_symbol: dict[str, list[UniverseInstrument]] = {}

    if settings.market_opportunity_catalog_discovery_enabled:
        for venue_id in venues:
            result = discover_spot_catalog(settings, venue_id)
            completeness[venue_id] = result.completeness.value
            if result.completeness is not DiscoveryCompleteness.COMPLETE:
                rejects.append(
                    f"{venue_id}:discovery_{result.completeness.value}"
                )
                continue
            if persist:
                moi_storage.persist_catalog_discovery_result(settings, result)
            for record in result.instruments:
                if record.quality_status != "active":
                    continue
                if record.instrument.product_type is not ProductType.SPOT:
                    continue
                inst = Instrument.from_canonical(
                    record.instrument.normalized_symbol, market_type="spot"
                )
                if inst is None:
                    rejects.append(f"{venue_id}:incompatible_symbol")
                    continue
                entry = UniverseInstrument(
                    venue_id=venue_id,
                    instrument=inst,
                    venue_market_id=record.instrument.venue_market_id,
                    quality_status=record.quality_status,
                )
                by_symbol.setdefault(inst.canonical_symbol, []).append(entry)
    else:
        for venue_id in venues:
            completeness[venue_id] = "allowlist"
        for symbol in settings.crypto_symbols:
            inst = Instrument.from_canonical(symbol, market_type="spot")
            if inst is None:
                rejects.append(f"allowlist:incompatible_symbol:{symbol}")
                continue
            for venue_id in venues:
                entry = UniverseInstrument(
                    venue_id=venue_id,
                    instrument=inst,
                    venue_market_id=f"{inst.base}{inst.quote}",
                    quality_status="active",
                )
                by_symbol.setdefault(inst.canonical_symbol, []).append(entry)

    # Require presence on at least two selected venues.
    kept: list[UniverseInstrument] = []
    symbols: list[str] = []
    for symbol, rows in sorted(by_symbol.items()):
        venue_ids = {row.venue_id for row in rows}
        if len(venue_ids) < 2:
            rejects.append(f"{symbol}:insufficient_venues")
            continue
        symbols.append(symbol)
        kept.extend(rows)

    return UniverseBuildResult(
        completeness_by_venue=completeness,
        instruments=tuple(kept),
        symbols=tuple(symbols),
        rejects=tuple(rejects),
    )


def instruments_for_symbol(
    universe: UniverseBuildResult, symbol: str
) -> tuple[UniverseInstrument, ...]:
    return tuple(
        row for row in universe.instruments if row.instrument.canonical_symbol == symbol
    )
