"""Per-cycle HTTP request budgets for Market Opportunity Scanner."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from app.opportunity_intel.versions import (
    LOCAL_CLOB_REQUEST_CAP_PER_CYCLE,
    LOCAL_GAMMA_REQUEST_CAP_PER_CYCLE,
)


class RequestBudgetExhausted(RuntimeError):
    """Raised when a cycle request budget cannot reserve another attempt."""

    def __init__(self, *, kind: str) -> None:
        self.kind = kind
        super().__init__(f"{kind} request budget exhausted")


@dataclass(frozen=True)
class RequestBudgetSnapshot:
    clob_attempts_used: int
    clob_cap: int
    gamma_attempts_used: int
    gamma_cap: int
    clob_exhausted_count: int
    gamma_exhausted_count: int


class CycleRequestBudget:
    """Thread-safe per-cycle counters for actual CLOB/Gamma HTTP attempts.

    Reservation occurs before each network attempt. Retries consume additional
    units. Cache-only paths must not call ``try_consume_*``.
    """

    def __init__(
        self,
        *,
        clob_cap: int = LOCAL_CLOB_REQUEST_CAP_PER_CYCLE,
        gamma_cap: int = LOCAL_GAMMA_REQUEST_CAP_PER_CYCLE,
    ) -> None:
        if clob_cap < 1 or gamma_cap < 1:
            raise ValueError("request caps must be positive")
        self.clob_cap = int(clob_cap)
        self.gamma_cap = int(gamma_cap)
        self._lock = threading.Lock()
        self._clob_used = 0
        self._gamma_used = 0
        self._clob_exhausted = 0
        self._gamma_exhausted = 0

    def try_consume_clob(self, units: int = 1) -> bool:
        need = max(1, int(units))
        with self._lock:
            if self._clob_used + need > self.clob_cap:
                self._clob_exhausted += 1
                return False
            self._clob_used += need
            return True

    def try_consume_gamma(self, units: int = 1) -> bool:
        need = max(1, int(units))
        with self._lock:
            if self._gamma_used + need > self.gamma_cap:
                self._gamma_exhausted += 1
                return False
            self._gamma_used += need
            return True

    def snapshot(self) -> RequestBudgetSnapshot:
        with self._lock:
            return RequestBudgetSnapshot(
                clob_attempts_used=self._clob_used,
                clob_cap=self.clob_cap,
                gamma_attempts_used=self._gamma_used,
                gamma_cap=self.gamma_cap,
                clob_exhausted_count=self._clob_exhausted,
                gamma_exhausted_count=self._gamma_exhausted,
            )

    def sanitized_metrics(self) -> dict[str, int]:
        snap = self.snapshot()
        return {
            "clob_attempts_used": snap.clob_attempts_used,
            "clob_cap": snap.clob_cap,
            "gamma_attempts_used": snap.gamma_attempts_used,
            "gamma_cap": snap.gamma_cap,
            "budget_exhausted_assets": snap.clob_exhausted_count,
            "gamma_budget_exhausted": snap.gamma_exhausted_count,
        }
