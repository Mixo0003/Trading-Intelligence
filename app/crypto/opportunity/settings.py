"""Per-user V4.3 crypto opportunity settings (safe defaults)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.crypto.product_types import ProductType
from app.crypto.venue_registry import V43_EXCHANGE_IDS
from app.opportunity_intel.settings import (
    ALLOCATION_PRESETS_USD,
    BALANCED_COOLDOWN_SECONDS,
    BALANCED_MAXIMUM_BOOK_AGE_SECONDS,
    BALANCED_MAXIMUM_SLIPPAGE,
    BALANCED_PAPER_ALLOCATION_USD,
    COOLDOWN_SECONDS_BOUNDS,
    MAXIMUM_SLIPPAGE_BOUNDS,
)

RISK_PREFERENCE_BALANCED = "balanced"
EVIDENCE_PUBLIC_BOOK_AND_FEES = "public_book_and_fees"


@dataclass(frozen=True)
class CryptoOpportunitySettings:
    revision: int = 0
    selected_exchanges: tuple[str, ...] = V43_EXCHANGE_IDS
    selected_product_types: tuple[str, ...] = (ProductType.SPOT.value,)
    paper_allocation_usd: Decimal = BALANCED_PAPER_ALLOCATION_USD
    minimum_net_profit_usd: Decimal = Decimal("0")
    minimum_roi: Decimal = Decimal("0")
    maximum_slippage: Decimal = BALANCED_MAXIMUM_SLIPPAGE
    minimum_executable_depth_usd: Decimal = BALANCED_PAPER_ALLOCATION_USD
    maximum_signal_age_seconds: int = BALANCED_MAXIMUM_BOOK_AGE_SECONDS
    risk_preference: str = RISK_PREFERENCE_BALANCED
    evidence_requirements: str = EVIDENCE_PUBLIC_BOOK_AND_FEES
    delivery_enabled: bool = False
    cooldown_seconds: int = BALANCED_COOLDOWN_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if not self.selected_exchanges:
            raise ValueError("selected_exchanges must not be empty")
        allowed = set(V43_EXCHANGE_IDS)
        for venue in self.selected_exchanges:
            if venue not in allowed:
                raise ValueError(f"exchange not in V4.3 universe: {venue}")
        if "bybit" in self.selected_exchanges:
            raise ValueError("bybit is excluded from V4.3 scanner")
        if self.selected_product_types != (ProductType.SPOT.value,):
            raise ValueError("V4.3 scanner supports spot product type only")
        if self.paper_allocation_usd not in ALLOCATION_PRESETS_USD:
            raise ValueError("paper_allocation_usd must be an approved preset")
        if self.minimum_net_profit_usd < 0 or self.minimum_roi < 0:
            raise ValueError("minimum profit/ROI floors must be non-negative")
        if not (
            MAXIMUM_SLIPPAGE_BOUNDS[0]
            <= self.maximum_slippage
            <= MAXIMUM_SLIPPAGE_BOUNDS[1]
        ):
            raise ValueError("maximum_slippage out of approved bounds")
        if self.minimum_executable_depth_usd < self.paper_allocation_usd:
            raise ValueError(
                "minimum_executable_depth_usd must be >= paper_allocation_usd"
            )
        if self.maximum_signal_age_seconds < 1:
            raise ValueError("maximum_signal_age_seconds must be positive")
        if self.risk_preference != RISK_PREFERENCE_BALANCED:
            raise ValueError("unsupported risk_preference")
        if self.evidence_requirements != EVIDENCE_PUBLIC_BOOK_AND_FEES:
            raise ValueError("unsupported evidence_requirements")
        if not (
            COOLDOWN_SECONDS_BOUNDS[0]
            <= self.cooldown_seconds
            <= COOLDOWN_SECONDS_BOUNDS[1]
        ):
            raise ValueError("cooldown_seconds out of approved bounds")


def balanced_crypto_settings(*, revision: int = 0) -> CryptoOpportunitySettings:
    return CryptoOpportunitySettings(revision=revision)


@dataclass(frozen=True)
class CryptoOpportunitySubscription:
    telegram_id: int
    settings: CryptoOpportunitySettings
