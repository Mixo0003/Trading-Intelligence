"""Bounded exponential-backoff retries for transient HTTP failures only."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_PERMANENT_STATUS = frozenset({400, 401, 403})
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BASE_DELAY = 0.25
_DEFAULT_MAX_DELAY = 4.0


def is_retryable_status(status_code: int | None) -> bool:
    if status_code is None:
        return False
    return status_code in _TRANSIENT_STATUS


def is_permanent_status(status_code: int | None) -> bool:
    if status_code is None:
        return False
    return status_code in _PERMANENT_STATUS


def _backoff_seconds(attempt: int, *, base: float, cap: float) -> float:
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    jitter = random.uniform(0.0, delay * 0.25)
    return delay + jitter


def request_with_retry(
    method: str,
    url: str,
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    base_delay: float = _DEFAULT_BASE_DELAY,
    max_delay: float = _DEFAULT_MAX_DELAY,
    retry_permanent: bool = False,
    before_attempt: Callable[[int], None] | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Issue an HTTP request, retrying only connection/timeout/429/5xx failures.

    Permanent client errors (400/401/403) are never retried unless
    ``retry_permanent`` is explicitly enabled (tests only).

    ``before_attempt(attempt_number)`` runs before each network call so callers
    can enforce per-attempt budgets. Every network attempt counts once.
    """
    attempts = max(1, int(max_attempts))
    last_exc: BaseException | None = None
    response: requests.Response | None = None
    attempt_count = 0

    for attempt in range(1, attempts + 1):
        if before_attempt is not None:
            before_attempt(attempt)
        attempt_count = attempt
        try:
            response = requests.request(method, url, **kwargs)
            status = response.status_code
            if is_permanent_status(status) and not retry_permanent:
                setattr(response, "request_count", attempt_count)
                return response
            if is_retryable_status(status) and attempt < attempts:
                delay = _backoff_seconds(attempt, base=base_delay, cap=max_delay)
                logger.warning(
                    "Transient HTTP %s for %s (attempt %s/%s); retrying in %.2fs",
                    status,
                    _safe_url(url),
                    attempt,
                    attempts,
                    delay,
                )
                time.sleep(delay)
                continue
            setattr(response, "request_count", attempt_count)
            return response
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
            delay = _backoff_seconds(attempt, base=base_delay, cap=max_delay)
            logger.warning(
                "HTTP timeout for %s (attempt %s/%s); retrying in %.2fs",
                _safe_url(url),
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
            delay = _backoff_seconds(attempt, base=base_delay, cap=max_delay)
            logger.warning(
                "HTTP connection error for %s (attempt %s/%s); retrying in %.2fs",
                _safe_url(url),
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)

    if response is not None:
        setattr(response, "request_count", attempt_count)
        return response
    assert last_exc is not None
    raise last_exc


def get_with_retry(url: str, **kwargs: Any) -> requests.Response:
    return request_with_retry("GET", url, **kwargs)


def _safe_url(url: str) -> str:
    """Strip query strings so credentials never appear in logs."""
    return url.split("?", 1)[0]


def call_with_retry(fn: Callable[[], requests.Response], **kwargs: Any) -> requests.Response:
    """Compatibility wrapper unused by production; kept for tests."""
    return fn()
