"""Typed API boundary results for Crypto Intelligence HTTP clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ApiSuccess(Generic[T]):
    data: T
    request_count: int = 1

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class ApiFailure:
    code: str
    status_code: int | None
    retryable: bool
    request_count: int
    message: str

    @property
    def ok(self) -> bool:
        return False


ApiResult = ApiSuccess[T] | ApiFailure
