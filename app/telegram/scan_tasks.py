"""Background scan task registry keyed by scan scope."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

_running: dict[str, asyncio.Task] = {}
_lock = asyncio.Lock()


def running_scopes() -> set[str]:
    return {scope for scope, task in _running.items() if not task.done()}


def is_scope_running(scope: str) -> bool:
    task = _running.get(scope)
    return task is not None and not task.done()


async def try_start_scoped_task(
    scope: str,
    factory: Callable[[], Awaitable[Any]],
) -> tuple[bool, asyncio.Task | None]:
    """Start at most one task per scope. Returns (started, task)."""
    async with _lock:
        existing = _running.get(scope)
        if existing is not None and not existing.done():
            return False, existing

        async def _runner() -> Any:
            try:
                return await factory()
            except asyncio.CancelledError:
                logger.info("Scan task cancelled for scope=%s", scope)
                raise
            except Exception:
                logger.exception("Scan task failed for scope=%s", scope)
                raise
            finally:
                async with _lock:
                    current = _running.get(scope)
                    if current is not None and current.done():
                        _running.pop(scope, None)

        task = asyncio.create_task(_runner(), name=f"weather-scan-{scope}")
        _running[scope] = task

        def _cleanup(done: asyncio.Task) -> None:
            try:
                exc = done.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    logger.error(
                        "Scan task ended with error for scope=%s: %s",
                        scope,
                        type(exc).__name__,
                    )
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        task.add_done_callback(_cleanup)
        return True, task


def clear_registry_for_tests() -> None:
    """Test helper: drop finished or leftover registry entries."""
    _running.clear()
