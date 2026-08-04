"""Quote freshness checks — fail closed on missing/stale timestamps."""

from __future__ import annotations

import time

from app.crypto.api_result import ApiFailure, ApiSuccess


def now_ms() -> int:
    return int(time.time() * 1000)


def age_seconds(timestamp_ms: int | None, *, now: int | None = None) -> float | None:
    if timestamp_ms is None:
        return None
    if timestamp_ms <= 0:
        return None
    current = now if now is not None else now_ms()
    return max(0.0, (current - timestamp_ms) / 1000.0)


def require_fresh_timestamp(
    timestamp_ms: int | None,
    *,
    max_age_seconds: float,
    now: int | None = None,
    request_count: int = 1,
) -> ApiSuccess[tuple[int, float]] | ApiFailure:
    if timestamp_ms is None or timestamp_ms <= 0:
        return ApiFailure(
            code="missing_timestamp",
            status_code=None,
            retryable=False,
            request_count=request_count,
            message="Quote timestamp missing",
        )
    age = age_seconds(timestamp_ms, now=now)
    if age is None:
        return ApiFailure(
            code="missing_timestamp",
            status_code=None,
            retryable=False,
            request_count=request_count,
            message="Quote timestamp invalid",
        )
    if age > max_age_seconds:
        return ApiFailure(
            code="stale_quote",
            status_code=None,
            retryable=True,
            request_count=request_count,
            message=f"Quote age {age:.1f}s exceeds max {max_age_seconds:.1f}s",
        )
    return ApiSuccess(data=(timestamp_ms, age), request_count=request_count)
