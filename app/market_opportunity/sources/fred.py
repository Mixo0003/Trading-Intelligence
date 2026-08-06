"""Optional FRED adapter for V4.2 WTI same-period price cross-check.

Series: DCOILWTICO (Crude Oil Prices: West Texas Intermediate — Cushing, OK).
Missing/stale/different-period FRED must not suppress publish (pipeline rule);
this module only parses and looks up same-period observations.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import requests

from app.http.retry import request_with_retry

logger = logging.getLogger(__name__)

FRED_DCOILWTICO_SERIES_ID = "DCOILWTICO"
FRED_OBSERVATIONS_ROUTE = "https://api.stlouisfed.org/fred/series/observations"
FRED_TOS_NOTICE = (
    "This product uses the FRED® API but is not endorsed or certified by "
    "the Federal Reserve Bank of St. Louis."
)

_DEFAULT_TIMEOUT = 20
_MAX_ATTEMPTS = 2
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN_SECONDS = 10 * 60

_breaker_lock = threading.Lock()
_breaker_failures = 0
_breaker_disabled_until = 0.0


class FredError(ValueError):
    """FRED adapter failure."""


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
class FredObservation:
    period: date
    value: Decimal


def _parse_period(raw: Any) -> date:
    text = str(raw).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise FredError(f"invalid FRED date: {raw!r}") from exc


def _parse_value(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text == ".":
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise FredError(f"invalid FRED value: {raw!r}") from exc
    if not value.is_finite():
        raise FredError(f"non-finite FRED value: {raw!r}")
    return value


def parse_dcoilwtico_observations(payload: dict[str, Any]) -> list[FredObservation]:
    """Parse FRED series/observations JSON for DCOILWTICO (ascending, skips missing)."""
    if not isinstance(payload, dict):
        raise FredError("FRED payload must be a dict")
    rows = payload.get("observations")
    if not isinstance(rows, list):
        raise FredError("FRED observations must be a list")
    out: list[FredObservation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            period = _parse_period(row.get("date"))
            value = _parse_value(row.get("value"))
        except FredError:
            continue
        if value is None:
            continue
        out.append(FredObservation(period=period, value=value))
    out.sort(key=lambda o: o.period)
    dedup: dict[date, FredObservation] = {o.period: o for o in out}
    return [dedup[k] for k in sorted(dedup)]


def lookup_same_period(
    observations: list[FredObservation],
    period: date,
) -> FredObservation | None:
    """Return the observation for the exact calendar period, else None."""
    if not isinstance(period, date) or isinstance(period, datetime):
        raise FredError("period must be a date")
    for obs in observations:
        if obs.period == period:
            return obs
    return None


def _breaker_tripped() -> bool:
    with _breaker_lock:
        return time.time() < _breaker_disabled_until


def _breaker_note_failure() -> None:
    global _breaker_failures, _breaker_disabled_until
    with _breaker_lock:
        _breaker_failures += 1
        if _breaker_failures >= _BREAKER_THRESHOLD:
            _breaker_disabled_until = time.time() + _BREAKER_COOLDOWN_SECONDS
            _breaker_failures = 0
            logger.warning(
                "FRED circuit open (%ss cooldown)", _BREAKER_COOLDOWN_SECONDS
            )


def _breaker_note_success() -> None:
    global _breaker_failures, _breaker_disabled_until
    with _breaker_lock:
        _breaker_failures = 0
        _breaker_disabled_until = 0.0


def reset_circuit_breakers() -> None:
    """Test helper: clear FRED circuit state."""
    global _breaker_failures, _breaker_disabled_until
    with _breaker_lock:
        _breaker_failures = 0
        _breaker_disabled_until = 0.0


def fetch_dcoilwtico(
    api_key: str,
    *,
    session: _HttpSession | None = None,
    limit: int = 120,
) -> list[FredObservation]:
    """Optional fetch of FRED DCOILWTICO observations (ascending)."""
    if not api_key or not str(api_key).strip():
        raise FredError("FRED_API_KEY is required")
    if _breaker_tripped():
        raise FredError("FRED circuit open")

    params: dict[str, Any] = {
        "series_id": FRED_DCOILWTICO_SERIES_ID,
        "api_key": str(api_key).strip(),
        "file_type": "json",
        "sort_order": "desc",
        "limit": max(1, int(limit)),
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "TradingIntelligence/4.2-MOI",
    }
    try:
        if session is not None:
            response = session.get(
                FRED_OBSERVATIONS_ROUTE,
                params=params,
                timeout=_DEFAULT_TIMEOUT,
                headers=headers,
            )
        else:
            response = request_with_retry(
                "GET",
                FRED_OBSERVATIONS_ROUTE,
                params=params,
                timeout=_DEFAULT_TIMEOUT,
                max_attempts=_MAX_ATTEMPTS,
                headers=headers,
            )
    except (requests.RequestException, OSError) as exc:
        _breaker_note_failure()
        raise FredError(f"FRED HTTP failure: {type(exc).__name__}") from exc

    status = getattr(response, "status_code", None)
    if status != 200:
        _breaker_note_failure()
        raise FredError(f"FRED HTTP status {status}")
    try:
        payload = response.json()
    except ValueError as exc:
        _breaker_note_failure()
        raise FredError("FRED response is not JSON") from exc
    if not isinstance(payload, dict):
        _breaker_note_failure()
        raise FredError("FRED JSON root must be an object")
    _breaker_note_success()
    observations = parse_dcoilwtico_observations(payload)
    if not observations:
        raise FredError("FRED DCOILWTICO returned no parseable observations")
    return observations
