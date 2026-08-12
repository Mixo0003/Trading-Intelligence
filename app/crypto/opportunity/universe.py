"""Catalog-driven symbol universe for the V4.4 crypto scanner.

When ``market_opportunity_catalog_discovery_enabled`` is true, builds the
cross-venue spot universe from COMPLETE public catalog discovery only.
Partial/failed venues are never treated as complete and never contribute
symbols. Allowlist fallback (``CRYPTO_SYMBOLS``) remains when the catalog
flag is off (V4.3-compatible defaults).
"""

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
    venue_status: str = ""


@dataclass(frozen=True)
class UniverseBuildResult:
    completeness_by_venue: dict[str, str]
    instruments: tuple[UniverseInstrument, ...]
    symbols: tuple[str, ...]
    rejects: tuple[str, ...]


def build_symbol_universe(
    settings: Settings,
    *,
    selected_exchanges: tuple[str, ...],
    persist: bool = True,
) -> UniverseBuildResult:
    """Build cross-venue spot universe for paper arbitrage scanning.

    Catalog-driven path (flag on):
    - Discover each selected V4.3 venue (Bybit excluded by ``V43_EXCHANGE_IDS``).
    - Require ``DiscoveryCompleteness.COMPLETE`` before accepting instruments.
    - Keep only active spot rows with normalized symbols.
    - Persist COMPLETE/PARTIAL results additively; COMPLETE may deactivate
      absent instruments (complete-miss). PARTIAL never delists.
    - Intersect: symbol must appear on at least two selected venues.

    Allowlist path (flag off): configured ``crypto_symbols`` on selected venues.
    """
    venues = tuple(v for v in selected_exchanges if v in V43_EXCHANGE_IDS)
    rejects: list[str] = []
    completeness: dict[str, str] = {}
    by_symbol: dict[str, list[UniverseInstrument]] = {}

    for venue_id in selected_exchanges:
        if venue_id == "bybit":
            rejects.append("bybit:excluded_from_v43_v44_universe")

    if not venues:
        return UniverseBuildResult(
            completeness_by_venue=completeness,
            instruments=(),
            symbols=(),
            rejects=tuple(rejects) or ("universe:no_v43_venues_selected",),
        )

    if settings.market_opportunity_catalog_discovery_enabled:
        complete_venues = 0
        for venue_id in venues:
            result = discover_spot_catalog(settings, venue_id)
            completeness[venue_id] = result.completeness.value
            if persist:
                # Persist all outcomes for audit; complete-miss only on COMPLETE.
                moi_storage.persist_catalog_discovery_result(settings, result)
            if result.completeness is not DiscoveryCompleteness.COMPLETE:
                rejects.append(
                    f"{venue_id}:discovery_{result.completeness.value}"
                )
                continue
            complete_venues += 1
            for record in result.instruments:
                if record.quality_status != "active":
                    rejects.append(
                        f"{venue_id}:inactive_or_unknown:{record.instrument.venue_market_id}"
                    )
                    continue
                if record.instrument.product_type is not ProductType.SPOT:
                    rejects.append(
                        f"{venue_id}:unsupported_product:{record.instrument.product_type.value}"
                    )
                    continue
                inst = Instrument.from_canonical(
                    record.instrument.normalized_symbol, market_type="spot"
                )
                if inst is None:
                    rejects.append(
                        f"{venue_id}:incompatible_symbol:{record.instrument.normalized_symbol}"
                    )
                    continue
                if (
                    inst.base != record.instrument.base
                    or inst.quote != record.instrument.quote
                ):
                    rejects.append(
                        f"{venue_id}:contradictory_symbol:{record.instrument.venue_market_id}"
                    )
                    continue
                entry = UniverseInstrument(
                    venue_id=venue_id,
                    instrument=inst,
                    venue_market_id=record.instrument.venue_market_id,
                    quality_status=record.quality_status,
                    venue_status=str(record.venue_status or ""),
                )
                by_symbol.setdefault(inst.canonical_symbol, []).append(entry)
        if complete_venues < 2:
            rejects.append("universe:insufficient_complete_venues")
            return UniverseBuildResult(
                completeness_by_venue=completeness,
                instruments=(),
                symbols=(),
                rejects=tuple(rejects),
            )
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

    # Common-symbol intersection: require presence on at least two selected venues.
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
