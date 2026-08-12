"""V4.5 basis and funding foundation contracts (validation only).

Does not implement futures arbitrage, funding-arbitrage execution, APR/ROI,
liquidation math, LONG/SHORT, TP/SL, news signals, or real trading.

When a required fee policy, funding formula, or verified source is missing,
evaluation fails closed with an explicit unsupported reject code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from app.crypto.market_snapshots import NormalizedMarketSnapshot
from app.crypto.product_types import ProductType
from app.crypto.venue_registry import CanonicalInstrument


class FoundationRejectCode(StrEnum):
    """Fail-closed codes for unsupported V4.5 foundation evaluation."""

    MISSING_FEE_POLICY = "missing_fee_policy"
    MISSING_FUNDING_FORMULA = "missing_funding_formula"
    MISSING_FRESHNESS_POLICY = "missing_freshness_policy"
    MISSING_REQUIRED_SOURCE = "missing_required_source"
    UNSUPPORTED_PRODUCT = "unsupported_product"
    CONTRADICTORY_DATA = "contradictory_data"
    INVALID_SNAPSHOT = "invalid_snapshot"


@dataclass(frozen=True)
class FoundationReject:
    code: FoundationRejectCode
    message: str
    symbol: str | None = None
    venue_id: str | None = None


@dataclass(frozen=True)
class BasisObservation:
    """Spot ↔ derivative or futures ↔ futures relationship observation.

    Stores verified prices only. Does not compute tradable edge or profit.
    """

    left: NormalizedMarketSnapshot
    right: NormalizedMarketSnapshot
    relationship: str  # "spot_perpetual" | "futures_futures"
    as_of: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.left, NormalizedMarketSnapshot):
            raise TypeError("left must be NormalizedMarketSnapshot")
        if not isinstance(self.right, NormalizedMarketSnapshot):
            raise TypeError("right must be NormalizedMarketSnapshot")
        if self.relationship not in {"spot_perpetual", "futures_futures"}:
            raise ValueError(
                "relationship must be spot_perpetual or futures_futures"
            )
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(timezone.utc))

        if self.left.instrument.normalized_symbol != (
            self.right.instrument.normalized_symbol
        ):
            raise ValueError("basis legs must share normalized_symbol")
        if self.left.instrument.base != self.right.instrument.base:
            raise ValueError("basis legs must share base asset")
        if self.left.instrument.quote != self.right.instrument.quote:
            raise ValueError("basis legs must share quote asset")

        if self.relationship == "spot_perpetual":
            types = {
                self.left.product_type,
                self.right.product_type,
            }
            if types != {ProductType.SPOT, ProductType.PERPETUAL}:
                raise ValueError(
                    "spot_perpetual requires one spot and one perpetual leg"
                )
        else:
            futures_types = {ProductType.PERPETUAL, ProductType.DATED_FUTURE}
            if self.left.product_type not in futures_types:
                raise ValueError("futures_futures left leg must be a futures product")
            if self.right.product_type not in futures_types:
                raise ValueError(
                    "futures_futures right leg must be a futures product"
                )
            if self.left.product_type == self.right.product_type == ProductType.PERPETUAL:
                if self.left.instrument.venue_id == self.right.instrument.venue_id:
                    raise ValueError(
                        "same-venue perpetual/perpetual basis is contradictory"
                    )

    @property
    def raw_basis(self) -> Decimal | None:
        """Right mid-or-mark minus left mid-or-mark when both sides have prices.

        Returns ``None`` when either side lacks a verified price — never invents.
        """
        left_px = _reference_price(self.left)
        right_px = _reference_price(self.right)
        if left_px is None or right_px is None:
            return None
        return right_px - left_px


@dataclass(frozen=True)
class FundingObservation:
    """Verified funding-rate observation for one perpetual instrument."""

    instrument: CanonicalInstrument
    snapshot: NormalizedMarketSnapshot
    as_of: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, CanonicalInstrument):
            raise TypeError("instrument must be CanonicalInstrument")
        if not isinstance(self.snapshot, NormalizedMarketSnapshot):
            raise TypeError("snapshot must be NormalizedMarketSnapshot")
        if self.instrument.identity_key != self.snapshot.instrument.identity_key:
            raise ValueError("funding instrument must match snapshot instrument")
        if self.instrument.product_type != ProductType.PERPETUAL:
            raise ValueError("funding observations require perpetual instruments")
        if self.snapshot.product_type != ProductType.PERPETUAL:
            raise ValueError("funding snapshot must be perpetual")
        if not self.snapshot.presence.funding:
            raise ValueError("funding observation requires funding capability")
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(timezone.utc))
        if self.snapshot.funding_rate is None or self.snapshot.funding_time is None:
            raise ValueError("funding observation requires rate and timestamp")


def _reference_price(snapshot: NormalizedMarketSnapshot) -> Decimal | None:
    if snapshot.presence.mark_price and snapshot.mark_price is not None:
        return snapshot.mark_price
    if snapshot.presence.bid_ask and snapshot.best_bid is not None and snapshot.best_ask is not None:
        return (snapshot.best_bid + snapshot.best_ask) / Decimal("2")
    if snapshot.presence.index_price and snapshot.index_price is not None:
        return snapshot.index_price
    return None


def evaluate_basis_opportunity(
    observation: BasisObservation,
    *,
    fee_policy: object | None = None,
    freshness_policy: object | None = None,
) -> tuple[None, FoundationReject]:
    """Foundation evaluator: always fail closed until policies are approved.

    No guaranteed-profit, APR, or executable futures arb is computed here.
    """
    if not isinstance(observation, BasisObservation):
        raise TypeError("observation must be BasisObservation")
    if fee_policy is None:
        return None, FoundationReject(
            code=FoundationRejectCode.MISSING_FEE_POLICY,
            message=(
                "basis opportunity unsupported: no approved fee policy "
                "(V4.5 foundation does not invent fee schedules)"
            ),
            symbol=observation.left.instrument.normalized_symbol,
        )
    if freshness_policy is None:
        return None, FoundationReject(
            code=FoundationRejectCode.MISSING_FRESHNESS_POLICY,
            message=(
                "basis opportunity unsupported: no approved freshness policy "
                "(V4.5 foundation does not invent TTLs)"
            ),
            symbol=observation.left.instrument.normalized_symbol,
        )
    return None, FoundationReject(
        code=FoundationRejectCode.UNSUPPORTED_PRODUCT,
        message=(
            "basis opportunity unsupported: complete futures/basis engine "
            "is not implemented in V4.5 foundation"
        ),
        symbol=observation.left.instrument.normalized_symbol,
    )


def evaluate_funding_opportunity(
    observation: FundingObservation,
    *,
    fee_policy: object | None = None,
    funding_formula: object | None = None,
    freshness_policy: object | None = None,
) -> tuple[None, FoundationReject]:
    """Foundation evaluator: always fail closed until formula/policies exist."""
    if not isinstance(observation, FundingObservation):
        raise TypeError("observation must be FundingObservation")
    if funding_formula is None:
        return None, FoundationReject(
            code=FoundationRejectCode.MISSING_FUNDING_FORMULA,
            message=(
                "funding opportunity unsupported: no approved funding formula "
                "(V4.5 foundation does not invent APR/ROI)"
            ),
            symbol=observation.instrument.normalized_symbol,
            venue_id=observation.instrument.venue_id,
        )
    if fee_policy is None:
        return None, FoundationReject(
            code=FoundationRejectCode.MISSING_FEE_POLICY,
            message=(
                "funding opportunity unsupported: no approved fee policy"
            ),
            symbol=observation.instrument.normalized_symbol,
            venue_id=observation.instrument.venue_id,
        )
    if freshness_policy is None:
        return None, FoundationReject(
            code=FoundationRejectCode.MISSING_FRESHNESS_POLICY,
            message=(
                "funding opportunity unsupported: no approved freshness policy"
            ),
            symbol=observation.instrument.normalized_symbol,
            venue_id=observation.instrument.venue_id,
        )
    return None, FoundationReject(
        code=FoundationRejectCode.UNSUPPORTED_PRODUCT,
        message=(
            "funding opportunity unsupported: complete funding-arbitrage "
            "engine is not implemented in V4.5 foundation"
        ),
        symbol=observation.instrument.normalized_symbol,
        venue_id=observation.instrument.venue_id,
    )


def format_foundation_status_card(
    *,
    title: str,
    reject: FoundationReject,
) -> str:
    """Honest paper/read-only status card for unsupported foundation paths."""
    lines = [
        title,
        "",
        "Status: unsupported (foundation only)",
        f"Reason: {reject.code.value}",
        reject.message,
        "",
        "Read-only / paper-only analysis.",
        "Not real trading. Not guaranteed profit.",
        "No LONG/SHORT, TP/SL, or news signals.",
    ]
    if reject.symbol:
        lines.insert(3, f"Symbol: {reject.symbol}")
    if reject.venue_id:
        lines.insert(4 if reject.symbol else 3, f"Venue: {reject.venue_id}")
    return "\n".join(lines)
