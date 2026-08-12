"""Normalized market snapshot contracts for V4.5 foundation.

Accepts missing capabilities explicitly. Does not fetch venue data and does not
invent freshness TTLs, funding formulas, or fee policies.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.crypto.product_types import ProductType, require_product_family
from app.crypto.venue_registry import CanonicalInstrument


def _identifier(value: str, field_name: str, *, maximum: int = 512) -> str:
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


def _require_positive_price(value: Decimal | None, field_name: str) -> Decimal | None:
    parsed = _optional_decimal(value, field_name)
    if parsed is not None and parsed <= 0:
        raise ValueError(f"{field_name} must be positive when present")
    return parsed


@dataclass(frozen=True)
class SnapshotProvenance:
    """Where a snapshot observation came from."""

    source_id: str
    endpoint_path: str
    observed_at: datetime
    request_count: int = 0
    note: str = ""

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id", maximum=128)
        if self.endpoint_path:
            _identifier(self.endpoint_path, "endpoint_path", maximum=256)
        elif not isinstance(self.endpoint_path, str):
            raise TypeError("endpoint_path must be str")
        object.__setattr__(
            self, "observed_at", _utc(self.observed_at, "observed_at")
        )
        if not isinstance(self.request_count, int) or self.request_count < 0:
            raise ValueError("request_count must be a non-negative int")
        if self.note:
            _identifier(self.note, "note", maximum=500)


@dataclass(frozen=True)
class SnapshotCapabilityPresence:
    """Explicit presence flags for optional snapshot fields.

    ``False`` means the capability/field is unavailable for this observation —
    not an exchange outage. Callers must not invent values for missing fields.
    """

    bid_ask: bool = False
    mark_price: bool = False
    index_price: bool = False
    funding: bool = False

    def __post_init__(self) -> None:
        for name in ("bid_ask", "mark_price", "index_price", "funding"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be bool")


@dataclass(frozen=True)
class NormalizedMarketSnapshot:
    """Product-aware public market observation (foundation contract only)."""

    instrument: CanonicalInstrument
    product_type: ProductType
    presence: SnapshotCapabilityPresence
    provenance: SnapshotProvenance
    as_of: datetime
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    funding_rate: Decimal | None = None
    funding_time: datetime | None = None
    contract_expiry: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, CanonicalInstrument):
            raise TypeError("instrument must be CanonicalInstrument")
        family = require_product_family(self.product_type)
        if self.instrument.product_type != family:
            raise ValueError(
                "product_type must match instrument.product_type "
                f"(got {family.value!r} vs {self.instrument.product_type.value!r})"
            )
        if not isinstance(self.presence, SnapshotCapabilityPresence):
            raise TypeError("presence must be SnapshotCapabilityPresence")
        if not isinstance(self.provenance, SnapshotProvenance):
            raise TypeError("provenance must be SnapshotProvenance")
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        if self.provenance.observed_at > self.as_of:
            raise ValueError("observed_at must not be after as_of")

        bid = _require_positive_price(self.best_bid, "best_bid")
        ask = _require_positive_price(self.best_ask, "best_ask")
        mark = _require_positive_price(self.mark_price, "mark_price")
        index = _require_positive_price(self.index_price, "index_price")
        funding = _optional_decimal(self.funding_rate, "funding_rate")

        if self.presence.bid_ask:
            if bid is None or ask is None:
                raise ValueError("bid_ask capability requires best_bid and best_ask")
            if bid > ask:
                raise ValueError("best_bid must not exceed best_ask")
        else:
            if bid is not None or ask is not None:
                raise ValueError(
                    "best_bid/best_ask must be absent when bid_ask capability is false"
                )

        if self.presence.mark_price:
            if mark is None:
                raise ValueError("mark_price capability requires mark_price")
        elif mark is not None:
            raise ValueError(
                "mark_price must be absent when mark_price capability is false"
            )

        if self.presence.index_price:
            if index is None:
                raise ValueError("index_price capability requires index_price")
        elif index is not None:
            raise ValueError(
                "index_price must be absent when index_price capability is false"
            )

        if self.presence.funding:
            if funding is None:
                raise ValueError("funding capability requires funding_rate")
            if self.funding_time is None:
                raise ValueError("funding capability requires funding_time")
            object.__setattr__(
                self, "funding_time", _utc(self.funding_time, "funding_time")
            )
        else:
            if funding is not None or self.funding_time is not None:
                raise ValueError(
                    "funding_rate/funding_time must be absent when funding "
                    "capability is false"
                )

        if family == ProductType.SPOT and self.presence.funding:
            raise ValueError("spot snapshots must not claim funding capability")

        if self.contract_expiry is not None:
            _identifier(self.contract_expiry, "contract_expiry", maximum=64)
            if family == ProductType.SPOT:
                raise ValueError("spot snapshots must not set contract_expiry")
        if family == ProductType.DATED_FUTURE and self.contract_expiry is None:
            raise ValueError("dated_future snapshots require contract_expiry")
        if (
            self.contract_expiry is not None
            and self.instrument.contract_expiry is not None
            and self.contract_expiry != self.instrument.contract_expiry
        ):
            raise ValueError("contract_expiry contradicts instrument.contract_expiry")

    def age_seconds(self) -> float:
        """Seconds between provenance.observed_at and as_of (never negative)."""
        return max(
            0.0,
            (self.as_of - self.provenance.observed_at).total_seconds(),
        )


def parse_decimal_text(value: str, field_name: str) -> Decimal:
    """Parse a verified decimal string; reject malformed input."""
    text = _identifier(value, field_name, maximum=64)
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} is not a valid decimal") from exc
    if parsed.is_nan() or parsed.is_infinite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    return parsed
