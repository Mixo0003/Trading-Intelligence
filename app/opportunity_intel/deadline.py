"""Cooperative cycle deadline token (no hard thread cancellation)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class CycleDeadlineToken:
    """Marks when the scheduler has stop-waiting on a cycle.

    Threads are not hard-cancelled. Callers must check ``expired`` at safe
    boundaries (before/after request batches, before SQLite commit, before
    delivery selection).
    """

    deadline_monotonic: float
    _expired: bool = False
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def from_timeout_seconds(cls, seconds: float) -> "CycleDeadlineToken":
        timeout = max(0.0, float(seconds))
        return cls(deadline_monotonic=time.monotonic() + timeout)

    def expire(self) -> None:
        with self._lock:
            self._expired = True

    @property
    def expired(self) -> bool:
        with self._lock:
            if self._expired:
                return True
            if time.monotonic() >= self.deadline_monotonic:
                self._expired = True
                return True
            return False

    def raise_if_expired(self) -> None:
        if self.expired:
            raise CycleDeadlineExpired()


class CycleDeadlineExpired(RuntimeError):
    """Cooperative stop signal — cycle must not open new work."""
