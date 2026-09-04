"""Bounded public JSON fetches with cache, timeout, and retry. No secrets."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.http.retry import get_with_retry
from app.opportunity_intel.live_edge.health import record_http

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_CACHE_TTL_SECONDS = 12.0
LIVE_EDGE_USER_AGENT = (
    "TradingIntelligenceLiveEdge/2.4.4 (analysis-only; structured JSON)"
)


@dataclass(frozen=True)
class JsonFetchResult:
    payload: Any | None
    status_code: int | None
    error: str | None


@dataclass
class _CacheEntry:
    expires_at: float
    payload: Any
    status_code: int


_CACHE: dict[str, _CacheEntry] = {}
_LAST_HOST_AT: dict[str, float] = {}


def clear_live_edge_http_cache() -> None:
    _CACHE.clear()
    _LAST_HOST_AT.clear()


def fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
    min_interval_seconds: float = 0.0,
    cache_key: str | None = None,
    provider: str | None = None,
) -> Any | None:
    """Return parsed JSON or None on transport/HTTP/parse failure (fail closed)."""
    return fetch_json_result(
        url,
        headers=headers,
        timeout=timeout,
        cache_ttl=cache_ttl,
        min_interval_seconds=min_interval_seconds,
        cache_key=cache_key,
        provider=provider,
    ).payload


def fetch_json_result(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
    min_interval_seconds: float = 0.0,
    cache_key: str | None = None,
    provider: str | None = None,
) -> JsonFetchResult:
    key = cache_key or url
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached is not None and cached.expires_at > now:
        result = JsonFetchResult(cached.payload, cached.status_code, None)
        _record(provider, result)
        return result
    host = url.split("/", 3)[2] if "://" in url else ""
    if min_interval_seconds > 0 and host:
        last = _LAST_HOST_AT.get(host, 0.0)
        wait = min_interval_seconds - (now - last)
        if wait > 0:
            time.sleep(wait)
    merged = {
        "User-Agent": LIVE_EDGE_USER_AGENT,
        "Accept": "application/json",
    }
    if headers:
        merged.update(headers)
    try:
        response = get_with_retry(
            url, headers=merged, timeout=timeout, max_attempts=3
        )
    except Exception as exc:
        logger.warning("Live Edge fetch failed for %s", url.split("?", 1)[0])
        result = JsonFetchResult(None, None, f"{type(exc).__name__}: {exc}")
        _record(provider, result)
        return result
    _LAST_HOST_AT[host] = time.monotonic()
    if response.status_code == 429:
        logger.warning("Live Edge rate-limited for %s", url.split("?", 1)[0])
        result = JsonFetchResult(None, 429, response.text[:180] or "rate limited")
        _record(provider, result)
        return result
    if response.status_code >= 400:
        logger.info(
            "Live Edge HTTP %s for %s",
            response.status_code,
            url.split("?", 1)[0],
        )
        result = JsonFetchResult(
            None, response.status_code, response.text[:180] or f"HTTP {response.status_code}"
        )
        _record(provider, result)
        return result
    try:
        payload = response.json()
    except ValueError:
        logger.warning("Live Edge invalid JSON from %s", url.split("?", 1)[0])
        result = JsonFetchResult(None, response.status_code, "malformed JSON")
        _record(provider, result)
        return result
    _CACHE[key] = _CacheEntry(
        expires_at=time.monotonic() + max(0.0, cache_ttl),
        payload=payload,
        status_code=response.status_code,
    )
    result = JsonFetchResult(payload, response.status_code, None)
    _record(provider, result)
    return result


def _record(provider: str | None, result: JsonFetchResult) -> None:
    if not provider:
        return
    record_http(
        provider,
        status_code=result.status_code,
        error=result.error,
    )
