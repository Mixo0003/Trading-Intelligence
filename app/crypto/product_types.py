"""Canonical product types for Crypto / V4.3 multi-asset foundation.

``unsupported`` is an explicit product classification. It must never be treated
as an exchange failure, venue outage, or health DOWN condition.
"""

from __future__ import annotations

from enum import StrEnum


class ProductType(StrEnum):
    """Normalized exchange product classification."""

    SPOT = "spot"
    PERPETUAL = "perpetual"
    DATED_FUTURE = "dated_future"
    UNSUPPORTED = "unsupported"


# Product families that a venue may declare as supported or unsupported.
# ``UNSUPPORTED`` is a resolution status, not a registrable family.
PRODUCT_FAMILIES: frozenset[ProductType] = frozenset(
    {
        ProductType.SPOT,
        ProductType.PERPETUAL,
        ProductType.DATED_FUTURE,
    }
)


def require_product_family(product_type: ProductType) -> ProductType:
    """Return ``product_type`` when it is a registrable family; else raise."""
    if not isinstance(product_type, ProductType):
        raise TypeError("product_type must be ProductType")
    if product_type not in PRODUCT_FAMILIES:
        raise ValueError(
            f"product family must be spot, perpetual, or dated_future "
            f"(got {product_type.value!r})"
        )
    return product_type
