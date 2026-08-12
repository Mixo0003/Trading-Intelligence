"""V4.3 public market-catalog discovery contracts.

Venue I/O boundary types only — not a second opportunity model.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from app.crypto.product_types import ProductType, require_product_family
from app.crypto.venue_registry import CanonicalInstrument


class DiscoveryCompleteness(StrEnum):
    """Catalog discovery outcome for one venue × product family."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    EMPTY = "empty"


class DiscoveryDiagnosticCode(StrEnum):
    HTTP_ERROR = "http_error"
    HTTP_STATUS = "http_status"
    INVALID_JSON = "invalid_json"
    INVALID_PAYLOAD = "invalid_payload"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    MALFORMED_ROW = "malformed_row"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    DUPLICATE_INSTRUMENT = "duplicate_instrument"
    UNSUPPORTED_PRODUCT = "unsupported_product"
    UNKNOWN_CONTINUATION = "unknown_continuation"
    PAGINATION_STOPPED = "pagination_stopped"
    FEATURE_DISABLED = "feature_disabled"
    READ_ONLY_VIOLATION = "read_only_violation"


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


@dataclass(frozen=True)
class DiscoveryDiagnostic:
    code: DiscoveryDiagnosticCode
    message: str
    venue_market_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, DiscoveryDiagnosticCode):
            raise TypeError("code must be DiscoveryDiagnosticCode")
        _identifier(self.message, "message", maximum=1000)
        if self.venue_market_id is not None:
            _identifier(self.venue_market_id, "venue_market_id", maximum=256)


@dataclass(frozen=True)
class PaginationState:
    """Pagination progress for one discovery attempt.

    Spot catalog endpoints used in V4.3 are single-response (no cursor).
    ``mode='none'`` means one logical page with no continuation token.
    """

    mode: str  # "none" | "cursor" | "page"
    pages_fetched: int
    exhausted: bool
    next_cursor: str | None = None
    stopped_reason: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"none", "cursor", "page"}:
            raise ValueError("pagination mode must be none, cursor, or page")
        if not isinstance(self.pages_fetched, int) or self.pages_fetched < 0:
            raise ValueError("pages_fetched must be a non-negative int")
        if not isinstance(self.exhausted, bool):
            raise TypeError("exhausted must be bool")
        if self.next_cursor is not None:
            _identifier(self.next_cursor, "next_cursor", maximum=512)
        if self.stopped_reason is not None:
            _identifier(self.stopped_reason, "stopped_reason", maximum=256)


@dataclass(frozen=True)
class DiscoveryProvenance:
    source_id: str
    endpoint_path: str
    fetched_at: datetime
    request_count: int = 0
    note: str = ""

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id", maximum=128)
        if self.endpoint_path:
            _identifier(self.endpoint_path, "endpoint_path", maximum=256)
        elif not isinstance(self.endpoint_path, str):
            raise TypeError("endpoint_path must be str")
        object.__setattr__(self, "fetched_at", _utc(self.fetched_at, "fetched_at"))
        if not isinstance(self.request_count, int) or self.request_count < 0:
            raise ValueError("request_count must be a non-negative int")
        if self.note:
            _identifier(self.note, "note", maximum=500)


@dataclass(frozen=True)
class DiscoveredInstrumentRecord:
    """One normalized catalog row with provenance timestamps."""

    instrument: CanonicalInstrument
    venue_status: str | None
    quality_status: str  # active | inactive | unknown
    discovered_at: datetime
    last_seen_at: datetime
    source_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, CanonicalInstrument):
            raise TypeError("instrument must be CanonicalInstrument")
        if self.quality_status not in {"active", "inactive", "unknown"}:
            raise ValueError("quality_status must be active, inactive, or unknown")
        if self.venue_status is not None:
            _identifier(self.venue_status, "venue_status", maximum=64)
        _identifier(self.source_id, "source_id", maximum=128)
        object.__setattr__(
            self, "discovered_at", _utc(self.discovered_at, "discovered_at")
        )
        object.__setattr__(
            self, "last_seen_at", _utc(self.last_seen_at, "last_seen_at")
        )


@dataclass(frozen=True)
class CatalogDiscoveryResult:
    """Capability-aware discovery result for one venue × product family."""

    session_id: str
    venue_id: str
    product_family: ProductType
    completeness: DiscoveryCompleteness
    instruments: tuple[DiscoveredInstrumentRecord, ...]
    pagination: PaginationState
    diagnostics: tuple[DiscoveryDiagnostic, ...]
    provenance: DiscoveryProvenance
    started_at: datetime
    finished_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.session_id, "session_id", maximum=128)
        _identifier(self.venue_id, "venue_id", maximum=64)
        require_product_family(self.product_family)
        if not isinstance(self.completeness, DiscoveryCompleteness):
            raise TypeError("completeness must be DiscoveryCompleteness")
        if not isinstance(self.pagination, PaginationState):
            raise TypeError("pagination must be PaginationState")
        if not isinstance(self.provenance, DiscoveryProvenance):
            raise TypeError("provenance must be DiscoveryProvenance")
        object.__setattr__(self, "started_at", _utc(self.started_at, "started_at"))
        object.__setattr__(
            self, "finished_at", _utc(self.finished_at, "finished_at")
        )
        if self.completeness == DiscoveryCompleteness.COMPLETE and (
            self.pagination.stopped_reason is not None
            or not self.pagination.exhausted
        ):
            raise ValueError(
                "complete discovery requires exhausted pagination without stop reason"
            )
        seen: set[tuple[str, str, str]] = set()
        for row in self.instruments:
            if not isinstance(row, DiscoveredInstrumentRecord):
                raise TypeError("instruments must contain DiscoveredInstrumentRecord")
            key = row.instrument.identity_key
            if key in seen:
                raise ValueError(f"duplicate instrument identity in result: {key}")
            seen.add(key)
            if row.instrument.venue_id != self.venue_id:
                raise ValueError("instrument venue_id must match discovery venue_id")
            if row.instrument.product_type != self.product_family:
                raise ValueError(
                    "instrument product_type must match discovery product_family"
                )
