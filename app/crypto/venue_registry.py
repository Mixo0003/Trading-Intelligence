"""Venue registry, capabilities, and canonical instrument identity.

Verified capability facts (V4.3–V4.5):
- Five Market Intelligence venues expose public spot ``fetch_ticker``,
  ``probe_health``, spot catalog discovery, and public L2 order books
  (see ``app.crypto.catalog_discovery`` / exchange adapters).
- Funding and open interest remain unimplemented (capability ``False``).
- Perpetual and dated futures remain ``unsupported`` (product classification),
  never as venue health failures — V4.5 foundation does not add futures HTTP
  adapters or claim futures catalog completeness.

Bybit remains in Crypto V3 (``EXCHANGE_IDS`` / ``build_clients``) and is
intentionally absent from this registry.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.crypto.product_types import (
    PRODUCT_FAMILIES,
    ProductType,
    require_product_family,
)

# Market Intelligence registry order (locked). Bybit is excluded.
V43_EXCHANGE_IDS: tuple[str, ...] = (
    "binance",
    "bingx",
    "mexc",
    "okx",
    "kucoin",
)


def _identifier(value: str, field_name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field_name} is invalid")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError(f"{field_name} contains control characters")
    return value


@dataclass(frozen=True)
class MarketDataCapabilities:
    """Public market-data capability flags for one venue product family.

    Flags describe implemented foundation support, not live probe results.
    ``False`` means the capability is not available in this codebase yet —
    it is not an exchange outage.
    """

    market_catalog: bool
    ticker: bool
    order_book: bool
    funding: bool
    open_interest: bool

    def __post_init__(self) -> None:
        for name in (
            "market_catalog",
            "ticker",
            "order_book",
            "funding",
            "open_interest",
        ):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be bool")


# Spot ticker + catalog discovery + public L2 depth for V4.3 venues.
# Funding / OI remain unimplemented.
_VERIFIED_SPOT_CAPABILITIES = MarketDataCapabilities(
    market_catalog=True,
    ticker=True,
    order_book=True,
    funding=False,
    open_interest=False,
)


@dataclass(frozen=True)
class VenueProductCapability:
    """Capability declaration for one product family at one venue.

    When ``supported`` is False, the product resolves to
    :attr:`ProductType.UNSUPPORTED`. That resolution is not a venue failure.
    """

    product_family: ProductType
    supported: bool
    capabilities: MarketDataCapabilities | None = None

    def __post_init__(self) -> None:
        require_product_family(self.product_family)
        if not isinstance(self.supported, bool):
            raise TypeError("supported must be bool")
        if self.supported:
            if self.capabilities is None:
                raise ValueError(
                    "supported product family requires MarketDataCapabilities"
                )
            if not isinstance(self.capabilities, MarketDataCapabilities):
                raise TypeError("capabilities must be MarketDataCapabilities")
        elif self.capabilities is not None:
            raise ValueError(
                "unsupported product family must not declare capabilities"
            )

    @property
    def resolved_product_type(self) -> ProductType:
        """Effective product type: family when supported, else unsupported."""
        if self.supported:
            return self.product_family
        return ProductType.UNSUPPORTED

    @classmethod
    def supported_with(
        cls,
        product_family: ProductType,
        capabilities: MarketDataCapabilities,
    ) -> VenueProductCapability:
        return cls(
            product_family=require_product_family(product_family),
            supported=True,
            capabilities=capabilities,
        )

    @classmethod
    def unsupported(cls, product_family: ProductType) -> VenueProductCapability:
        return cls(
            product_family=require_product_family(product_family),
            supported=False,
            capabilities=None,
        )


@dataclass(frozen=True)
class VenueRegistration:
    """Canonical Market Intelligence exchange registry entry (capability-aware)."""

    venue_id: str
    display_name: str
    products: tuple[VenueProductCapability, ...]

    def __post_init__(self) -> None:
        _identifier(self.venue_id, "venue_id", maximum=64)
        _identifier(self.display_name, "display_name", maximum=128)
        if not self.products:
            raise ValueError("products must not be empty")
        seen: set[ProductType] = set()
        for entry in self.products:
            if not isinstance(entry, VenueProductCapability):
                raise TypeError("products must contain VenueProductCapability")
            if entry.product_family in seen:
                raise ValueError(
                    f"duplicate product family {entry.product_family.value!r}"
                )
            seen.add(entry.product_family)
        missing = PRODUCT_FAMILIES - seen
        if missing:
            names = ", ".join(sorted(item.value for item in missing))
            raise ValueError(
                f"venue must declare all product families; missing: {names}"
            )

    def product(self, product_family: ProductType) -> VenueProductCapability:
        family = require_product_family(product_family)
        for entry in self.products:
            if entry.product_family == family:
                return entry
        raise KeyError(family.value)

    def supports(self, product_family: ProductType) -> bool:
        return self.product(product_family).supported

    def supported_product_families(self) -> tuple[ProductType, ...]:
        """Product families declared supported for this venue (registry order)."""
        return tuple(
            entry.product_family for entry in self.products if entry.supported
        )

    def unsupported_product_families(self) -> tuple[ProductType, ...]:
        """Product families declared unsupported (not a venue outage)."""
        return tuple(
            entry.product_family for entry in self.products if not entry.supported
        )


@dataclass(frozen=True)
class CanonicalInstrument:
    """Unambiguous venue instrument identity for V4.3.

    Identity key: ``(venue_id, product_type, venue_market_id)``.

    This is additive to the V3 spot ``Instrument`` model and does not replace it.
    """

    venue_id: str
    product_type: ProductType
    venue_market_id: str
    base: str
    quote: str
    normalized_symbol: str
    display_name: str = ""
    contract_expiry: str | None = None
    # Optional authoritative constraints when a verified source provides them.
    # Not persisted as separate catalog columns in this foundation slice.
    settlement: str | None = None
    tick_size: str | None = None
    quantity_step: str | None = None
    contract_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.product_type, ProductType):
            raise TypeError("product_type must be ProductType")
        _identifier(self.venue_id, "venue_id", maximum=64)
        _identifier(self.venue_market_id, "venue_market_id", maximum=256)
        _identifier(self.base, "base", maximum=64)
        _identifier(self.quote, "quote", maximum=64)
        _identifier(self.normalized_symbol, "normalized_symbol", maximum=128)
        if self.display_name:
            _identifier(self.display_name, "display_name", maximum=256)
        if self.contract_expiry is not None:
            _identifier(self.contract_expiry, "contract_expiry", maximum=64)
            if self.product_type == ProductType.SPOT:
                raise ValueError("spot instruments must not set contract_expiry")
        if (
            self.product_type == ProductType.DATED_FUTURE
            and self.contract_expiry is None
        ):
            raise ValueError("dated_future instruments require contract_expiry")
        if self.product_type == ProductType.SPOT and self.contract_code is not None:
            raise ValueError("spot instruments must not set contract_code")
        for field_name in ("settlement", "tick_size", "quantity_step", "contract_code"):
            value = getattr(self, field_name)
            if value is not None:
                _identifier(value, field_name, maximum=64)

    @property
    def identity_key(self) -> tuple[str, str, str]:
        return (self.venue_id, self.product_type.value, self.venue_market_id)


def _spot_supported() -> VenueProductCapability:
    return VenueProductCapability.supported_with(
        ProductType.SPOT, _VERIFIED_SPOT_CAPABILITIES
    )


def _default_products() -> tuple[VenueProductCapability, ...]:
    """Verified spot support; perpetual/dated futures unsupported (V4.5)."""
    return (
        _spot_supported(),
        VenueProductCapability.unsupported(ProductType.PERPETUAL),
        VenueProductCapability.unsupported(ProductType.DATED_FUTURE),
    )


def _venue(venue_id: str, display_name: str) -> VenueRegistration:
    return VenueRegistration(
        venue_id=venue_id,
        display_name=display_name,
        products=_default_products(),
    )


_V43_REGISTRY: dict[str, VenueRegistration] = {
    entry.venue_id: entry
    for entry in (
        _venue("binance", "Binance"),
        _venue("bingx", "BingX"),
        _venue("mexc", "MEXC"),
        _venue("okx", "OKX"),
        _venue("kucoin", "KuCoin"),
    )
}


def is_v43_venue(venue_id: str) -> bool:
    return venue_id in _V43_REGISTRY


def get_v43_venue(venue_id: str) -> VenueRegistration | None:
    return _V43_REGISTRY.get(venue_id)


def require_v43_venue(venue_id: str) -> VenueRegistration:
    entry = get_v43_venue(venue_id)
    if entry is None:
        raise KeyError(f"venue not in V4.3 registry: {venue_id!r}")
    return entry


def iter_v43_venues() -> tuple[VenueRegistration, ...]:
    """Return V4.3 venues in locked registry order."""
    return tuple(_V43_REGISTRY[venue_id] for venue_id in V43_EXCHANGE_IDS)


def v43_exchange_ids() -> tuple[str, ...]:
    return V43_EXCHANGE_IDS
