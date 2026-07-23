"""Typed API boundary results for Copy Intelligence HTTP clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ApiSuccess(Generic[T]):
    """Successful API response.

    Distinguishes successful empty payloads from successful non-empty payloads
    via ``empty`` / ``non_empty`` without conflating either with failure.
    """

    data: T
    request_count: int = 1

    @property
    def ok(self) -> bool:
        return True

    @property
    def empty(self) -> bool:
        data = self.data
        if data is None:
            return True
        if isinstance(data, (list, tuple, set, dict, str, bytes)):
            return len(data) == 0
        return False

    @property
    def non_empty(self) -> bool:
        return not self.empty


@dataclass(frozen=True)
class ApiFailure:
    """Fail-closed API outcome with sanitized internal diagnostics only."""

    code: str
    status_code: int | None
    retryable: bool
    request_count: int
    message: str

    @property
    def ok(self) -> bool:
        return False


ApiResult = ApiSuccess[T] | ApiFailure


def unwrap_data_list(value: object) -> list:
    """Compatibility adapter for callers that still expect a bare list.

    - ``ApiSuccess`` → underlying list (empty or not)
    - ``ApiFailure`` → ``[]`` (preserves pre-Checkpoint-3 external empty-book behavior)
    - plain ``list`` → returned as-is (MagicMock / legacy tests)
    - anything else → ``[]``
    """
    if isinstance(value, ApiSuccess):
        data = value.data
        return list(data) if isinstance(data, list) else []
    if isinstance(value, ApiFailure):
        return []
    if isinstance(value, list):
        return value
    return []
