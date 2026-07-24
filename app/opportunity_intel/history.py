"""Public CLOB price-history reads and deterministic movement windows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Callable, Iterable

import requests

from app.config.settings import Settings
from app.http.retry import get_with_retry, is_retryable_status
from app.opportunity_intel.discovery import ResultStatus
from app.opportunity_intel.models import ObservationMode, OpportunityDiagnosticCode
from app.opportunity_intel.settings import (
    NORMAL_HISTORY_15M_TOLERANCE_SECONDS,
    NORMAL_HISTORY_1H_TOLERANCE_SECONDS,
    NORMAL_HISTORY_5M_TOLERANCE_SECONDS,
    NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS,
    STRICT_LIVE_HISTORY_15M_TOLERANCE_SECONDS,
    STRICT_LIVE_HISTORY_1H_TOLERANCE_SECONDS,
    STRICT_LIVE_HISTORY_5M_TOLERANCE_SECONDS,
    STRICT_LIVE_LATEST_HISTORY_MAXIMUM_AGE_SECONDS,
)

HISTORY_TIMEOUT_SECONDS = 15
HISTORY_MAX_ASSETS_PER_BATCH = 100
_ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"


class HistoryDirection(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"


@dataclass(frozen=True)
class HistoryPoint:
    at: datetime
    price: Decimal

    def __post_init__(self) -> None:
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("history timestamp must be timezone-aware")
        object.__setattr__(self, "at", self.at.astimezone(timezone.utc))
        if (
            not isinstance(self.price, Decimal)
            or not self.price.is_finite()
            or not Decimal("0") <= self.price <= Decimal("1")
        ):
            raise ValueError("history price must be a Decimal between 0 and 1")


@dataclass(frozen=True)
class HistoryReadResult:
    asset_id: str
    status: ResultStatus
    points: tuple[HistoryPoint, ...]
    diagnostic_code: OpportunityDiagnosticCode | None = None
    http_status_category: str | None = None

    @property
    def complete(self) -> bool:
        return self.status == ResultStatus.COMPLETE


@dataclass(frozen=True)
class MovementResult:
    asset_id: str
    status: ResultStatus
    freshness: FreshnessStatus
    diagnostic_code: OpportunityDiagnosticCode | None
    latest_price: Decimal | None = None
    latest_at: datetime | None = None
    price_5m: Decimal | None = None
    price_15m: Decimal | None = None
    price_1h: Decimal | None = None
    change_5m_pp: Decimal | None = None
    change_15m_pp: Decimal | None = None
    change_1h_pp: Decimal | None = None
    absolute_change_5m_pp: Decimal | None = None
    absolute_change_15m_pp: Decimal | None = None
    absolute_change_1h_pp: Decimal | None = None
    direction: HistoryDirection | None = None


class PublicPriceHistoryClient:
    """No-auth client for official CLOB ``/prices-history``."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_url: str | None = None,
        request_get: Callable[..., requests.Response] = get_with_retry,
        timeout_seconds: int = HISTORY_TIMEOUT_SECONDS,
    ) -> None:
        configured = (
            base_url
            if base_url is not None
            else settings.polymarket_clob_base_url if settings is not None else None
        )
        if not configured:
            raise ValueError("public CLOB base URL is required")
        self.base_url = str(configured).rstrip("/")
        self.request_get = request_get
        self.timeout_seconds = int(timeout_seconds)
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")

    def fetch(
        self,
        asset_id: str,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> HistoryReadResult:
        if not isinstance(asset_id, str) or not _ASSET_RE.fullmatch(asset_id):
            return _history_failure(
                asset_id=str(asset_id),
                status=ResultStatus.PERMANENT_FAILURE,
                code=OpportunityDiagnosticCode.HISTORY_REQUEST_FAILED,
            )
        start = _utc_timestamp(start_at, "start_at")
        end = _utc_timestamp(end_at, "end_at")
        if start >= end:
            raise ValueError("start_at must be before end_at")
        try:
            response = self.request_get(
                f"{self.base_url}/prices-history",
                params={
                    "market": asset_id,
                    "startTs": int(start.timestamp()),
                    "endTs": int(end.timestamp()),
                    "fidelity": 1,
                },
                headers={
                    "User-Agent": "Trading Intelligence Bot",
                    "Accept": "application/json",
                },
                timeout=self.timeout_seconds,
            )
            status_code = int(response.status_code)
            response.raise_for_status()
            payload = response.json()
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ):
            return _history_failure(
                asset_id=asset_id,
                status=ResultStatus.RETRYABLE_FAILURE,
                code=OpportunityDiagnosticCode.HISTORY_REQUEST_FAILED,
                category="network",
            )
        except requests.exceptions.HTTPError as exc:
            response_value = exc.response
            status_code = (
                int(response_value.status_code)
                if response_value is not None
                else None
            )
            status = (
                ResultStatus.RETRYABLE_FAILURE
                if is_retryable_status(status_code)
                else ResultStatus.PERMANENT_FAILURE
            )
            category = (
                "retryable_http"
                if status == ResultStatus.RETRYABLE_FAILURE
                else "permanent_http"
            )
            return _history_failure(
                asset_id=asset_id,
                status=status,
                code=OpportunityDiagnosticCode.HISTORY_REQUEST_FAILED,
                category=category,
            )
        except (ValueError, TypeError, requests.exceptions.JSONDecodeError):
            return _history_failure(
                asset_id=asset_id,
                status=ResultStatus.PERMANENT_FAILURE,
                code=OpportunityDiagnosticCode.HISTORY_RESPONSE_INVALID,
            )

        if not isinstance(payload, dict) or not isinstance(payload.get("history"), list):
            return _history_failure(
                asset_id=asset_id,
                status=ResultStatus.PERMANENT_FAILURE,
                code=OpportunityDiagnosticCode.HISTORY_RESPONSE_INVALID,
                category=_http_category(status_code),
            )
        parsed = _parse_points(payload["history"])
        if parsed is None:
            return _history_failure(
                asset_id=asset_id,
                status=ResultStatus.PERMANENT_FAILURE,
                code=OpportunityDiagnosticCode.HISTORY_POINT_INVALID,
                category=_http_category(status_code),
            )
        if not parsed:
            return _history_failure(
                asset_id=asset_id,
                status=ResultStatus.RETRYABLE_FAILURE,
                code=OpportunityDiagnosticCode.HISTORY_MISSING,
                category=_http_category(status_code),
            )
        return HistoryReadResult(
            asset_id=asset_id,
            status=ResultStatus.COMPLETE,
            points=parsed,
            http_status_category=_http_category(status_code),
        )

    def fetch_many(
        self,
        asset_ids: Iterable[str],
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> dict[str, HistoryReadResult]:
        """Read each token independently so one failure cannot poison its peers."""
        ordered = tuple(dict.fromkeys(asset_ids))
        if not 1 <= len(ordered) <= HISTORY_MAX_ASSETS_PER_BATCH:
            raise ValueError(
                f"asset count must be between 1 and {HISTORY_MAX_ASSETS_PER_BATCH}"
            )
        return {
            asset_id: self.fetch(asset_id, start_at=start_at, end_at=end_at)
            for asset_id in ordered
        }


def select_movement(
    history: HistoryReadResult,
    *,
    mode: ObservationMode,
    now: datetime,
) -> MovementResult:
    """Select nearest points relative to the latest fresh history timestamp."""
    if history.status != ResultStatus.COMPLETE:
        return MovementResult(
            asset_id=history.asset_id,
            status=history.status,
            freshness=FreshnessStatus.INCOMPLETE,
            diagnostic_code=history.diagnostic_code,
        )
    current = _utc_timestamp(now, "now")
    points = history.points
    if not points:
        return _movement_failure(
            history.asset_id,
            ResultStatus.RETRYABLE_FAILURE,
            FreshnessStatus.INCOMPLETE,
            OpportunityDiagnosticCode.HISTORY_MISSING,
        )
    latest = points[-1]
    latest_age = (current - latest.at).total_seconds()
    if mode == ObservationMode.NORMAL:
        maximum_age = NORMAL_LATEST_HISTORY_MAXIMUM_AGE_SECONDS
        tolerances = (
            NORMAL_HISTORY_5M_TOLERANCE_SECONDS,
            NORMAL_HISTORY_15M_TOLERANCE_SECONDS,
            NORMAL_HISTORY_1H_TOLERANCE_SECONDS,
        )
    elif mode == ObservationMode.LIVE_STRICT:
        maximum_age = STRICT_LIVE_LATEST_HISTORY_MAXIMUM_AGE_SECONDS
        tolerances = (
            STRICT_LIVE_HISTORY_5M_TOLERANCE_SECONDS,
            STRICT_LIVE_HISTORY_15M_TOLERANCE_SECONDS,
            STRICT_LIVE_HISTORY_1H_TOLERANCE_SECONDS,
        )
    else:  # pragma: no cover - enum exhaustiveness
        raise ValueError("unsupported observation mode")
    if latest_age < -5:
        return _movement_failure(
            history.asset_id,
            ResultStatus.PERMANENT_FAILURE,
            FreshnessStatus.INCOMPLETE,
            OpportunityDiagnosticCode.HISTORY_POINT_INVALID,
        )
    if latest_age > maximum_age:
        return _movement_failure(
            history.asset_id,
            ResultStatus.RETRYABLE_FAILURE,
            FreshnessStatus.STALE,
            OpportunityDiagnosticCode.HISTORY_LATEST_STALE,
        )

    comparison_points: list[HistoryPoint] = []
    for offset_seconds, tolerance_seconds in zip(
        (300, 900, 3600), tolerances, strict=True
    ):
        target = latest.at - timedelta(seconds=offset_seconds)
        selected = _nearest_point(
            points[:-1],
            target=target,
            tolerance_seconds=tolerance_seconds,
        )
        if selected is None:
            return _movement_failure(
                history.asset_id,
                ResultStatus.RETRYABLE_FAILURE,
                FreshnessStatus.INCOMPLETE,
                OpportunityDiagnosticCode.HISTORY_TARGET_MISSING,
            )
        comparison_points.append(selected)

    changes = tuple(
        (latest.price - point.price) * Decimal("100")
        for point in comparison_points
    )
    strongest_index = max(
        range(len(changes)),
        key=lambda index: (abs(changes[index]), -index),
    )
    strongest = changes[strongest_index]
    direction = (
        HistoryDirection.UP
        if strongest > 0
        else HistoryDirection.DOWN
        if strongest < 0
        else HistoryDirection.FLAT
    )
    return MovementResult(
        asset_id=history.asset_id,
        status=ResultStatus.COMPLETE,
        freshness=FreshnessStatus.FRESH,
        diagnostic_code=None,
        latest_price=latest.price,
        latest_at=latest.at,
        price_5m=comparison_points[0].price,
        price_15m=comparison_points[1].price,
        price_1h=comparison_points[2].price,
        change_5m_pp=changes[0],
        change_15m_pp=changes[1],
        change_1h_pp=changes[2],
        absolute_change_5m_pp=abs(changes[0]),
        absolute_change_15m_pp=abs(changes[1]),
        absolute_change_1h_pp=abs(changes[2]),
        direction=direction,
    )


def _parse_points(raw_points: list[Any]) -> tuple[HistoryPoint, ...] | None:
    by_timestamp: dict[int, Decimal] = {}
    for raw in raw_points:
        if not isinstance(raw, dict):
            return None
        timestamp = _epoch_seconds(raw.get("t"))
        price = _price(raw.get("p"))
        if timestamp is None or price is None:
            return None
        existing = by_timestamp.get(timestamp)
        if existing is not None and existing != price:
            return None
        by_timestamp[timestamp] = price
    return tuple(
        HistoryPoint(
            at=datetime.fromtimestamp(timestamp, tz=timezone.utc),
            price=by_timestamp[timestamp],
        )
        for timestamp in sorted(by_timestamp)
    )


def _epoch_seconds(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    integer = int(parsed)
    if integer <= 0 or integer > 253402300799:
        return None
    return integer


def _price(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("1"):
        return None
    return parsed


def _nearest_point(
    points: tuple[HistoryPoint, ...],
    *,
    target: datetime,
    tolerance_seconds: int,
) -> HistoryPoint | None:
    eligible = [
        point
        for point in points
        if abs((point.at - target).total_seconds()) <= tolerance_seconds
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda point: (
            abs((point.at - target).total_seconds()),
            -point.at.timestamp(),
        ),
    )


def _history_failure(
    *,
    asset_id: str,
    status: ResultStatus,
    code: OpportunityDiagnosticCode,
    category: str | None = None,
) -> HistoryReadResult:
    return HistoryReadResult(
        asset_id=asset_id,
        status=status,
        points=(),
        diagnostic_code=code,
        http_status_category=category,
    )


def _movement_failure(
    asset_id: str,
    status: ResultStatus,
    freshness: FreshnessStatus,
    code: OpportunityDiagnosticCode,
) -> MovementResult:
    return MovementResult(
        asset_id=asset_id,
        status=status,
        freshness=freshness,
        diagnostic_code=code,
    )


def _utc_timestamp(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError(f"{name} must have a UTC offset")
    return value.astimezone(timezone.utc)


def _http_category(status_code: int) -> str:
    if 200 <= status_code < 300:
        return "success"
    if status_code == 429:
        return "rate_limited"
    if 400 <= status_code < 500:
        return "client_error"
    if status_code >= 500:
        return "server_error"
    return "other"
