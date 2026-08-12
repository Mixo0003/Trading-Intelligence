"""V4.6 directional trading intelligence foundation (contracts + validation).

Supports typed LONG/SHORT observations and strategy/evidence labels for:
trend, breakout, momentum, mean reversion, order-book imbalance, plus
volume/volatility evidence presence.

Does **not** invent signal formulas, TP/SL, invalidation, ranking, confidence,
or risk policies. Evaluation fails closed until approved formula/policy objects
exist. Does not change spot paper arbitrage, scanner, or delivery defaults.

Uses only verified public market-data fields already available in-repo
(snapshot bid/ask / reference prices and optional book depth sizes). Volume and
volatility have no verified crypto domain adapters yet — claiming those
evidence types without values fails closed.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from app.crypto.market_snapshots import NormalizedMarketSnapshot
from app.crypto.product_types import ProductType
from app.crypto.venue_registry import CanonicalInstrument, require_v43_venue


class DirectionalSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class DirectionalStrategyType(StrEnum):
    TREND = "trend"
    BREAKOUT = "breakout"
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    ORDER_BOOK_IMBALANCE = "order_book_imbalance"


class DirectionalRejectCode(StrEnum):
    MISSING_SIGNAL_FORMULA = "missing_signal_formula"
    MISSING_RISK_POLICY = "missing_risk_policy"
    MISSING_FRESHNESS_POLICY = "missing_freshness_policy"
    MISSING_REQUIRED_EVIDENCE = "missing_required_evidence"
    MISSING_REQUIRED_SOURCE = "missing_required_source"
    UNSUPPORTED_PRODUCT = "unsupported_product"
    UNSUPPORTED_STRATEGY = "unsupported_strategy"
    CONTRADICTORY_DATA = "contradictory_data"
    INVALID_OBSERVATION = "invalid_observation"
    STALE_DATA = "stale_data"


@dataclass(frozen=True)
class DirectionalReject:
    code: DirectionalRejectCode
    message: str
    symbol: str | None = None
    venue_id: str | None = None
    side: str | None = None
    strategy_type: str | None = None


@dataclass(frozen=True)
class DirectionalEvidencePresence:
    """Explicit presence flags — ``False`` means unavailable, not an outage."""

    reference_price: bool = False
    order_book_depth: bool = False
    volume: bool = False
    volatility: bool = False

    def __post_init__(self) -> None:
        for name in (
            "reference_price",
            "order_book_depth",
            "volume",
            "volatility",
        ):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be bool")


def _identifier(value: str, field_name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field_name} is invalid")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError(f"{field_name} contains control characters")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must have a UTC offset")
    return value.astimezone(timezone.utc)


def _optional_decimal(value: Decimal | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be Decimal or None")
    if value.is_nan() or value.is_infinite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    return value


def _require_non_negative(
    value: Decimal | None, field_name: str
) -> Decimal | None:
    parsed = _optional_decimal(value, field_name)
    if parsed is not None and parsed < 0:
        raise ValueError(f"{field_name} must be non-negative when present")
    return parsed


def _require_positive(value: Decimal | None, field_name: str) -> Decimal | None:
    parsed = _optional_decimal(value, field_name)
    if parsed is not None and parsed <= 0:
        raise ValueError(f"{field_name} must be positive when present")
    return parsed


@dataclass(frozen=True)
class DirectionalEvidence:
    """Verified evidence fields only. Missing capabilities stay explicit."""

    presence: DirectionalEvidencePresence
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    reference_price: Decimal | None = None
    reference_price_label: str = "reference"
    bid_depth_base: Decimal | None = None
    ask_depth_base: Decimal | None = None
    volume: Decimal | None = None
    volatility: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.presence, DirectionalEvidencePresence):
            raise TypeError("presence must be DirectionalEvidencePresence")
        _identifier(self.reference_price_label, "reference_price_label", maximum=64)

        bid = _require_positive(self.best_bid, "best_bid")
        ask = _require_positive(self.best_ask, "best_ask")
        ref = _require_positive(self.reference_price, "reference_price")
        bid_depth = _require_non_negative(self.bid_depth_base, "bid_depth_base")
        ask_depth = _require_non_negative(self.ask_depth_base, "ask_depth_base")
        volume = _require_non_negative(self.volume, "volume")
        volatility = _require_non_negative(self.volatility, "volatility")

        if self.presence.reference_price:
            if ref is None:
                raise ValueError(
                    "reference_price capability requires reference_price"
                )
            if bid is not None and ask is not None and bid > ask:
                raise ValueError("best_bid must not exceed best_ask")
        else:
            if ref is not None:
                raise ValueError(
                    "reference_price must be absent when capability is false"
                )

        if self.presence.order_book_depth:
            if bid_depth is None or ask_depth is None:
                raise ValueError(
                    "order_book_depth capability requires bid_depth_base "
                    "and ask_depth_base"
                )
        else:
            if bid_depth is not None or ask_depth is not None:
                raise ValueError(
                    "depth sizes must be absent when order_book_depth "
                    "capability is false"
                )

        if self.presence.volume:
            if volume is None:
                raise ValueError("volume capability requires volume")
        elif volume is not None:
            raise ValueError("volume must be absent when volume capability is false")

        if self.presence.volatility:
            if volatility is None:
                raise ValueError("volatility capability requires volatility")
        elif volatility is not None:
            raise ValueError(
                "volatility must be absent when volatility capability is false"
            )


@dataclass(frozen=True)
class DirectionalObservation:
    """One directional paper observation (foundation contract only)."""

    instrument: CanonicalInstrument
    side: DirectionalSide
    strategy_type: DirectionalStrategyType
    evidence: DirectionalEvidence
    as_of: datetime
    observed_at: datetime
    snapshot: NormalizedMarketSnapshot | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, CanonicalInstrument):
            raise TypeError("instrument must be CanonicalInstrument")
        if not isinstance(self.side, DirectionalSide):
            raise TypeError("side must be DirectionalSide")
        if not isinstance(self.strategy_type, DirectionalStrategyType):
            raise TypeError("strategy_type must be DirectionalStrategyType")
        if not isinstance(self.evidence, DirectionalEvidence):
            raise TypeError("evidence must be DirectionalEvidence")
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        object.__setattr__(
            self, "observed_at", _utc(self.observed_at, "observed_at")
        )
        if self.observed_at > self.as_of:
            raise ValueError("observed_at must not be after as_of")
        if self.note:
            _identifier(self.note, "note", maximum=500)

        if self.snapshot is not None:
            if not isinstance(self.snapshot, NormalizedMarketSnapshot):
                raise TypeError("snapshot must be NormalizedMarketSnapshot")
            if self.snapshot.instrument.identity_key != self.instrument.identity_key:
                raise ValueError("snapshot instrument must match observation")
            if self.snapshot.product_type != self.instrument.product_type:
                raise ValueError("snapshot product_type contradicts instrument")

        if self.strategy_type is DirectionalStrategyType.ORDER_BOOK_IMBALANCE:
            if not self.evidence.presence.order_book_depth:
                raise ValueError(
                    "order_book_imbalance requires order_book_depth evidence"
                )

    @property
    def signal_age_seconds(self) -> float:
        return max(0.0, (self.as_of - self.observed_at).total_seconds())


def evaluate_directional_opportunity(
    observation: DirectionalObservation,
    *,
    signal_formula: object | None = None,
    risk_policy: object | None = None,
    freshness_policy: object | None = None,
) -> tuple[None, DirectionalReject]:
    """Foundation evaluator: always fail closed until formulas/policies exist.

    Never fabricates a directional signal or guaranteed profit.
    """
    if not isinstance(observation, DirectionalObservation):
        raise TypeError("observation must be DirectionalObservation")

    symbol = observation.instrument.normalized_symbol
    venue_id = observation.instrument.venue_id
    side = observation.side.value
    strategy = observation.strategy_type.value

    # Product gate (also enforced on construction for non-spot).
    if observation.instrument.product_type != ProductType.SPOT:
        venue = require_v43_venue(venue_id)
        if not venue.supports(observation.instrument.product_type):
            return None, DirectionalReject(
                code=DirectionalRejectCode.UNSUPPORTED_PRODUCT,
                message=(
                    "directional opportunity unsupported: futures product "
                    "has no verified adapter in repository"
                ),
                symbol=symbol,
                venue_id=venue_id,
                side=side,
                strategy_type=strategy,
            )

    if not observation.evidence.presence.reference_price:
        return None, DirectionalReject(
            code=DirectionalRejectCode.MISSING_REQUIRED_EVIDENCE,
            message=(
                "directional opportunity unsupported: reference price "
                "evidence is required and missing"
            ),
            symbol=symbol,
            venue_id=venue_id,
            side=side,
            strategy_type=strategy,
        )

    source_reject = require_volume_volatility_sources(observation)
    if source_reject is not None:
        return None, source_reject

    if signal_formula is None:
        return None, DirectionalReject(
            code=DirectionalRejectCode.MISSING_SIGNAL_FORMULA,
            message=(
                f"directional {strategy} opportunity unsupported: no approved "
                "signal formula (V4.6 foundation does not invent thresholds)"
            ),
            symbol=symbol,
            venue_id=venue_id,
            side=side,
            strategy_type=strategy,
        )
    if risk_policy is None:
        return None, DirectionalReject(
            code=DirectionalRejectCode.MISSING_RISK_POLICY,
            message=(
                "directional opportunity unsupported: no approved risk policy "
                "(V4.6 foundation does not invent TP/SL or invalidation)"
            ),
            symbol=symbol,
            venue_id=venue_id,
            side=side,
            strategy_type=strategy,
        )
    if freshness_policy is None:
        return None, DirectionalReject(
            code=DirectionalRejectCode.MISSING_FRESHNESS_POLICY,
            message=(
                "directional opportunity unsupported: no approved freshness "
                "policy (V4.6 foundation does not invent TTLs)"
            ),
            symbol=symbol,
            venue_id=venue_id,
            side=side,
            strategy_type=strategy,
        )
    return None, DirectionalReject(
        code=DirectionalRejectCode.UNSUPPORTED_STRATEGY,
        message=(
            "directional opportunity unsupported: complete directional "
            "engine is not implemented in V4.6 foundation"
        ),
        symbol=symbol,
        venue_id=venue_id,
        side=side,
        strategy_type=strategy,
    )


def require_volume_volatility_sources(
    observation: DirectionalObservation,
    *,
    volume_source: object | None = None,
    volatility_source: object | None = None,
) -> DirectionalReject | None:
    """Fail closed when volume/volatility evidence is claimed without sources."""
    if observation.evidence.presence.volume and volume_source is None:
        return DirectionalReject(
            code=DirectionalRejectCode.MISSING_REQUIRED_SOURCE,
            message=(
                "volume evidence unsupported: no verified volume source/"
                "adapter in repository"
            ),
            symbol=observation.instrument.normalized_symbol,
            venue_id=observation.instrument.venue_id,
            side=observation.side.value,
            strategy_type=observation.strategy_type.value,
        )
    if observation.evidence.presence.volatility and volatility_source is None:
        return DirectionalReject(
            code=DirectionalRejectCode.MISSING_REQUIRED_SOURCE,
            message=(
                "volatility evidence unsupported: no verified volatility "
                "source/adapter in repository"
            ),
            symbol=observation.instrument.normalized_symbol,
            venue_id=observation.instrument.venue_id,
            side=observation.side.value,
            strategy_type=observation.strategy_type.value,
        )
    return None


def format_directional_opportunity_card(
    observation: DirectionalObservation,
    reject: DirectionalReject,
) -> str:
    """Honest paper/read-only directional foundation card."""
    evidence = observation.evidence
    lines = [
        "DIRECTIONAL OPPORTUNITY (foundation)",
        f"Side: {observation.side.value}",
        f"Strategy / evidence type: {observation.strategy_type.value}",
        f"Symbol: {observation.instrument.normalized_symbol}",
        f"Venue: {observation.instrument.venue_id}",
        f"Product type: {observation.instrument.product_type.value}",
    ]
    if evidence.presence.reference_price and evidence.reference_price is not None:
        lines.append(
            f"{evidence.reference_price_label} price: {evidence.reference_price}"
        )
    if evidence.best_bid is not None:
        lines.append(f"Best bid: {evidence.best_bid}")
    if evidence.best_ask is not None:
        lines.append(f"Best ask: {evidence.best_ask}")
    if evidence.presence.order_book_depth:
        lines.append(f"Bid depth (base): {evidence.bid_depth_base}")
        lines.append(f"Ask depth (base): {evidence.ask_depth_base}")
    if evidence.presence.volume:
        lines.append(f"Volume (claimed): {evidence.volume}")
    if evidence.presence.volatility:
        lines.append(f"Volatility (claimed): {evidence.volatility}")
    lines.extend(
        [
            f"Signal age: {observation.signal_age_seconds:.1f}s",
            f"Observed at: {observation.observed_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"As of: {observation.as_of.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
            f"Status: unsupported ({reject.code.value})",
            reject.message,
            "",
            "Public read-only / paper-only. Not guaranteed profit.",
            "Not a trade recommendation. No TP/SL invented.",
        ]
    )
    return "\n".join(lines)
