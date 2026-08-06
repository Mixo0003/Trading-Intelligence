"""EIA Open Data APIv2 adapters for V4.2 WTI Opportunity Intelligence.

Primary instrument: EIA WTI Cushing Spot (RWTC), daily.
Inventory: U.S. Ending Stocks excluding SPR of Crude Oil (WCESTUS1), weekly.

Parse helpers accept pre-fetched JSON dicts so unit tests stay offline.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests

from app.http.retry import request_with_retry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frozen instrument identity (V4.2 Final Plan)
# ---------------------------------------------------------------------------
WTI_CUSHING_SOURCE_ID = "EIA"
WTI_CUSHING_VENUE = "EIA"
WTI_CUSHING_MARKET_ID = "RWTC"
WTI_CUSHING_SYMBOL = "WTI_CUSHING"
WTI_CUSHING_DISPLAY_NAME = "EIA WTI Cushing Spot — Daily"

# ---------------------------------------------------------------------------
# Verified EIA RWTC price series metadata (operator-confirmed)
# ---------------------------------------------------------------------------
EIA_RWTC_SERIES_ID = "RWTC"
EIA_RWTC_TITLE = "Cushing, OK WTI Spot Price FOB (Dollars per Barrel)"
EIA_RWTC_FREQUENCY = "daily"
EIA_RWTC_UNITS = "$/BBL"
EIA_RWTC_ROUTE = "https://api.eia.gov/v2/petroleum/pri/spt/data/"

# ---------------------------------------------------------------------------
# Verified EIA inventory series metadata (frozen — do not change without
# re-verifying official EIA APIv2 metadata)
# ---------------------------------------------------------------------------
EIA_WTI_INVENTORY_SERIES_ID = "WCESTUS1"
EIA_WTI_INVENTORY_TITLE = (
    "U.S. Ending Stocks excluding SPR of Crude Oil (Thousand Barrels)"
)
EIA_WTI_INVENTORY_FREQUENCY = "weekly"
EIA_WTI_INVENTORY_UNITS = "MBBL"  # Thousand Barrels
EIA_WTI_INVENTORY_ROUTE = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
EIA_WTI_INVENTORY_PROCESS_NAME = "Ending Stocks Excluding SPR"

# source_event_at convention: period calendar date at 12:00 America/New_York → UTC
_NY = ZoneInfo("America/New_York")

_DEFAULT_TIMEOUT = 20
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN_SECONDS = 10 * 60
_MAX_ATTEMPTS = 3

_breaker_lock = threading.Lock()
_breaker_failures: dict[str, int] = {}
_breaker_disabled_until: dict[str, float] = {}


class EIAError(ValueError):
    """EIA adapter failure (parse, HTTP, or circuit open)."""


class _HttpSession(Protocol):
    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | int | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class DailyBar:
    """Daily OHLC proxy from EIA RWTC (single value/day → OHLC equal value)."""

    period: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    value: Decimal


@dataclass(frozen=True)
class InventoryPoint:
    """Weekly inventory observation."""

    period: date
    value: Decimal


def period_to_source_event_at(period: date) -> datetime:
    """Map EIA period date to timezone-aware source_event_at (UTC).

    Convention: noon America/New_York on the period date, converted to UTC.
    """
    local = datetime(period.year, period.month, period.day, 12, 0, tzinfo=_NY)
    return local.astimezone(timezone.utc)


def _parse_period(raw: Any) -> date:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if not text:
        raise EIAError("missing EIA period")
    # EIA weekly periods may be YYYY-MM-DD; daily same.
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise EIAError(f"invalid EIA period: {raw!r}") from exc


def _parse_decimal(raw: Any, *, field: str) -> Decimal:
    if raw is None or raw == "":
        raise EIAError(f"missing EIA {field}")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise EIAError(f"invalid EIA {field}: {raw!r}") from exc
    if not value.is_finite():
        raise EIAError(f"non-finite EIA {field}: {raw!r}")
    return value


def _extract_data_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise EIAError("EIA payload must be a dict")
    response = payload.get("response", payload)
    if not isinstance(response, dict):
        raise EIAError("EIA response must be an object")
    data = response.get("data")
    if data is None:
        raise EIAError("EIA response missing data")
    if not isinstance(data, list):
        raise EIAError("EIA response.data must be a list")
    return [row for row in data if isinstance(row, dict)]


def parse_rwtc_daily(payload: dict[str, Any]) -> list[DailyBar]:
    """Parse EIA petroleum/pri/spt RWTC daily JSON into DailyBar list (ascending)."""
    rows = _extract_data_rows(payload)
    bars: list[DailyBar] = []
    for row in rows:
        series = str(row.get("series") or "").strip().upper()
        if series and series != EIA_RWTC_SERIES_ID:
            continue
        try:
            period = _parse_period(row.get("period"))
            value = _parse_decimal(row.get("value"), field="value")
        except EIAError:
            continue
        bars.append(
            DailyBar(
                period=period,
                open=value,
                high=value,
                low=value,
                close=value,
                value=value,
            )
        )
    bars.sort(key=lambda b: b.period)
    # Deduplicate by period (keep last)
    dedup: dict[date, DailyBar] = {b.period: b for b in bars}
    return [dedup[k] for k in sorted(dedup)]


def parse_inventory_wcestus1(payload: dict[str, Any]) -> list[InventoryPoint]:
    """Parse EIA petroleum/stoc/wstk WCESTUS1 weekly JSON (ascending)."""
    rows = _extract_data_rows(payload)
    points: list[InventoryPoint] = []
    for row in rows:
        series = str(row.get("series") or "").strip().upper()
        if series and series != EIA_WTI_INVENTORY_SERIES_ID:
            continue
        try:
            period = _parse_period(row.get("period"))
            value = _parse_decimal(row.get("value"), field="value")
        except EIAError:
            continue
        points.append(InventoryPoint(period=period, value=value))
    points.sort(key=lambda p: p.period)
    dedup: dict[date, InventoryPoint] = {p.period: p for p in points}
    return [dedup[k] for k in sorted(dedup)]


def _breaker_tripped(key: str) -> bool:
    with _breaker_lock:
        return time.time() < _breaker_disabled_until.get(key, 0.0)


def _breaker_note_failure(key: str) -> None:
    with _breaker_lock:
        _breaker_failures[key] = _breaker_failures.get(key, 0) + 1
        if _breaker_failures[key] >= _BREAKER_THRESHOLD:
            _breaker_disabled_until[key] = time.time() + _BREAKER_COOLDOWN_SECONDS
            _breaker_failures[key] = 0
            logger.warning(
                "EIA circuit open for %s (%ss cooldown)",
                key,
                _BREAKER_COOLDOWN_SECONDS,
            )


def _breaker_note_success(key: str) -> None:
    with _breaker_lock:
        _breaker_failures[key] = 0
        _breaker_disabled_until.pop(key, None)


def reset_circuit_breakers() -> None:
    """Test helper: clear EIA circuit state."""
    with _breaker_lock:
        _breaker_failures.clear()
        _breaker_disabled_until.clear()


def _get_json(
    url: str,
    *,
    params: dict[str, Any],
    api_key: str,
    session: _HttpSession | None,
    breaker_key: str,
) -> dict[str, Any]:
    if not api_key or not str(api_key).strip():
        raise EIAError("EIA_API_KEY is required")
    if _breaker_tripped(breaker_key):
        raise EIAError(f"EIA circuit open for {breaker_key}")

    query = dict(params)
    query["api_key"] = str(api_key).strip()
    headers = {
        "Accept": "application/json",
        "User-Agent": "TradingIntelligence/4.2-MOI",
    }
    try:
        if session is not None:
            response = session.get(
                url, params=query, timeout=_DEFAULT_TIMEOUT, headers=headers
            )
        else:
            response = request_with_retry(
                "GET",
                url,
                params=query,
                timeout=_DEFAULT_TIMEOUT,
                max_attempts=_MAX_ATTEMPTS,
                headers=headers,
            )
    except (requests.RequestException, OSError) as exc:
        _breaker_note_failure(breaker_key)
        raise EIAError(f"EIA HTTP failure: {type(exc).__name__}") from exc

    status = getattr(response, "status_code", None)
    if status != 200:
        _breaker_note_failure(breaker_key)
        raise EIAError(f"EIA HTTP status {status}")
    try:
        payload = response.json()
    except ValueError as exc:
        _breaker_note_failure(breaker_key)
        raise EIAError("EIA response is not JSON") from exc
    if not isinstance(payload, dict):
        _breaker_note_failure(breaker_key)
        raise EIAError("EIA JSON root must be an object")
    _breaker_note_success(breaker_key)
    return payload


def fetch_rwtc_daily(
    api_key: str,
    *,
    session: _HttpSession | None = None,
    length: int = 120,
) -> list[DailyBar]:
    """Fetch EIA RWTC daily spot history (newest-first from API; returned ascending)."""
    length = max(1, int(length))
    params: dict[str, Any] = {
        "frequency": EIA_RWTC_FREQUENCY,
        "data[0]": "value",
        "facets[series][]": EIA_RWTC_SERIES_ID,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": length,
    }
    payload = _get_json(
        EIA_RWTC_ROUTE,
        params=params,
        api_key=api_key,
        session=session,
        breaker_key="eia_rwtc",
    )
    bars = parse_rwtc_daily(payload)
    if not bars:
        raise EIAError("EIA RWTC returned no parseable observations")
    return bars


def fetch_inventory_wcestus1(
    api_key: str,
    *,
    session: _HttpSession | None = None,
    length: int = 40,
) -> list[InventoryPoint]:
    """Fetch EIA WCESTUS1 weekly commercial crude stocks ex-SPR (ascending)."""
    length = max(1, int(length))
    params: dict[str, Any] = {
        "frequency": EIA_WTI_INVENTORY_FREQUENCY,
        "data[0]": "value",
        "facets[series][]": EIA_WTI_INVENTORY_SERIES_ID,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": length,
    }
    payload = _get_json(
        EIA_WTI_INVENTORY_ROUTE,
        params=params,
        api_key=api_key,
        session=session,
        breaker_key="eia_wcestus1",
    )
    points = parse_inventory_wcestus1(payload)
    if not points:
        raise EIAError("EIA WCESTUS1 returned no parseable observations")
    return points
