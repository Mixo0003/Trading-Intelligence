"""Canonical asset-keyed position books for V2.2.2 Checkpoint 4.

Complete validated books, fail-closed mutation rules, and consistency
second-read control. Does not perform trade correlation or delivery.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from app.copy_intel.api_result import ApiFailure, ApiSuccess
from app.copy_intel.data_client import CopyDataClient, normalize_wallet
from app.copy_intel.event_identity import normalize_size
from app.copy_intel.models import PositionSnapshot

logger = logging.getLogger(__name__)

_SIZE_EPS = 1e-6
DEFAULT_POSITION_PAGE_SIZE = 100
MAX_POSITION_PAGES = 50


@dataclass(frozen=True)
class PositionBookSuccess:
    """A complete, validated position book keyed by asset."""

    positions: dict[str, PositionSnapshot]
    raw_row_count: int
    was_multipage: bool
    request_count: int

    @property
    def ok(self) -> bool:
        return True

    @property
    def empty(self) -> bool:
        return len(self.positions) == 0


@dataclass(frozen=True)
class PositionBookFailure:
    """Fail-closed book outcome; callers must not mutate snapshots."""

    code: str
    message: str
    request_count: int = 1

    @property
    def ok(self) -> bool:
        return False


PositionBookResult = PositionBookSuccess | PositionBookFailure


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_position_row(
    raw: Any, wallet: str
) -> tuple[PositionSnapshot | None, str | None]:
    """Validate one raw position row for the asset-keyed book.

    Returns ``(snapshot, None)`` on success or ``(None, reason_code)`` on failure.
    """
    if not isinstance(raw, dict):
        return None, "position_row_not_object"
    try:
        cleaned_wallet = normalize_wallet(wallet)
    except ValueError:
        return None, "invalid_wallet"

    asset_raw = raw.get("asset")
    if asset_raw is None or str(asset_raw).strip() == "":
        return None, "missing_asset"
    asset = str(asset_raw).strip()

    condition_id = raw.get("conditionId")
    if condition_id is None or str(condition_id).strip() == "":
        return None, "missing_condition_id"
    condition_id = str(condition_id).strip()

    size_raw = raw.get("size")
    if size_raw is None:
        return None, "missing_size"
    try:
        size_val = float(size_raw)
    except (TypeError, ValueError):
        return None, "invalid_size"
    if not math.isfinite(size_val) or size_val < 0:
        return None, "non_finite_or_negative_size"

    outcome = raw.get("outcome")
    if outcome is not None:
        outcome = str(outcome).strip() or None

    # Internal consistency: outcome string must not contradict empty/whitespace-only asset
    # or condition (already checked). Reject obviously broken type mixes.
    if not asset or not condition_id:
        return None, "inconsistent_asset_condition_outcome"

    size = normalize_size(size_val)
    return (
        PositionSnapshot(
            wallet=cleaned_wallet,
            condition_id=condition_id,
            asset=asset,
            outcome=outcome,
            size=size,
            avg_price=_float_or_none(raw.get("avgPrice")),
            cur_price=_float_or_none(raw.get("curPrice")),
            title=raw.get("title"),
            slug=raw.get("slug"),
            event_slug=raw.get("eventSlug"),
        ),
        None,
    )


def build_canonical_position_book(
    rows: list[Any], wallet: str
) -> PositionBookResult:
    """Build an asset-keyed map; duplicate assets or bad rows fail the whole book."""
    positions: dict[str, PositionSnapshot] = {}
    for raw in rows:
        snap, reason = validate_position_row(raw, wallet)
        if reason is not None or snap is None:
            return PositionBookFailure(
                code="malformed_row",
                message=reason or "malformed_row",
            )
        assert snap.asset is not None
        if snap.asset in positions:
            return PositionBookFailure(
                code="duplicate_asset",
                message=f"duplicate asset in position book: {snap.asset}",
            )
        if snap.size > _SIZE_EPS:
            positions[snap.asset] = snap
        # Zero-size rows are omitted from the open book (not an error).
    return PositionBookSuccess(
        positions=positions,
        raw_row_count=len(rows),
        was_multipage=False,
        request_count=1,
    )


def _normalize_fetch_result(result: object) -> ApiSuccess[list] | ApiFailure:
    if isinstance(result, ApiFailure):
        return result
    if isinstance(result, ApiSuccess):
        data = result.data
        if not isinstance(data, list):
            return ApiFailure(
                code="wrong_type",
                status_code=None,
                retryable=False,
                request_count=result.request_count,
                message="expected list data",
            )
        return ApiSuccess(data=data, request_count=result.request_count)
    if isinstance(result, list):
        # MagicMock / legacy tests: plain list is a successful page.
        return ApiSuccess(data=result, request_count=1)
    return ApiFailure(
        code="wrong_type",
        status_code=None,
        retryable=False,
        request_count=1,
        message="unexpected positions fetch type",
    )


@dataclass(frozen=True)
class _FetchedPages:
    rows: list[dict[str, Any]]
    request_count: int
    page_count: int
    hit_full_page: bool


def fetch_complete_position_rows(
    client: CopyDataClient,
    wallet: str,
    *,
    page_size: int = DEFAULT_POSITION_PAGE_SIZE,
    max_pages: int = MAX_POSITION_PAGES,
) -> _FetchedPages | ApiFailure:
    """Fetch all position pages. Distinguishes success empty from failure."""
    page_size = max(1, int(page_size))
    all_rows: list[dict[str, Any]] = []
    request_count = 0
    offset = 0
    pages = 0
    hit_full_page = False
    while pages < max_pages:
        raw = client.fetch_positions(wallet, limit=page_size, offset=offset)
        page = _normalize_fetch_result(raw)
        request_count += max(1, int(getattr(page, "request_count", 1) or 1))
        if isinstance(page, ApiFailure):
            return ApiFailure(
                code=page.code,
                status_code=page.status_code,
                retryable=page.retryable,
                request_count=request_count,
                message=page.message,
            )
        chunk = page.data
        all_rows.extend(chunk)
        pages += 1
        if len(chunk) == page_size:
            hit_full_page = True
        if len(chunk) < page_size:
            return _FetchedPages(
                rows=all_rows,
                request_count=request_count,
                page_count=pages,
                hit_full_page=hit_full_page,
            )
        offset += page_size
    return ApiFailure(
        code="incomplete_book",
        status_code=None,
        retryable=True,
        request_count=request_count,
        message="position book exceeded max pages",
    )


def fetch_validated_position_book(
    client: CopyDataClient,
    wallet: str,
    *,
    page_size: int = DEFAULT_POSITION_PAGE_SIZE,
) -> PositionBookResult:
    """Fetch a complete book and validate into a canonical asset map."""
    fetched = fetch_complete_position_rows(client, wallet, page_size=page_size)
    if isinstance(fetched, ApiFailure):
        return PositionBookFailure(
            code=fetched.code,
            message=fetched.message,
            request_count=fetched.request_count,
        )
    built = build_canonical_position_book(fetched.rows, wallet)
    if isinstance(built, PositionBookFailure):
        return PositionBookFailure(
            code=built.code,
            message=built.message,
            request_count=fetched.request_count,
        )
    # Multi-page or a full final page (possible truncation) triggers second-read.
    was_multipage = fetched.page_count > 1 or fetched.hit_full_page
    return PositionBookSuccess(
        positions=built.positions,
        raw_row_count=len(fetched.rows),
        was_multipage=was_multipage,
        request_count=fetched.request_count,
    )


def book_fingerprint(
    positions: dict[str, PositionSnapshot],
) -> dict[str, tuple[str, float, str]]:
    """Sanitized comparable fingerprint of a canonical book."""
    return {
        asset: (
            snap.condition_id,
            normalize_size(snap.size),
            (snap.outcome or ""),
        )
        for asset, snap in sorted(positions.items())
    }


def books_equal(
    left: dict[str, PositionSnapshot],
    right: dict[str, PositionSnapshot],
) -> bool:
    return book_fingerprint(left) == book_fingerprint(right)


def needs_consistency_second_read(
    previous: dict[str, PositionSnapshot],
    current: dict[str, PositionSnapshot],
    *,
    was_multipage: bool,
) -> bool:
    """Approved consistency triggers for a second complete read."""
    if was_multipage:
        return True
    if previous and not current:
        return True
    if any(asset not in current for asset in previous):
        return True
    return False


def sanitized_book_diagnostic(
    *,
    code: str,
    wallet: str,
    detail: str | None = None,
) -> dict[str, str]:
    """Diagnostic payload safe for logs / processed markers (no secrets/paths)."""
    payload = {
        "code": code,
        "wallet_prefix": wallet[:10].lower() if wallet else "",
    }
    if detail:
        payload["detail"] = detail[:200]
    return payload


def emit_book_diagnostic(diagnostic: dict[str, str]) -> None:
    logger.warning(
        "position_book_diagnostic code=%s wallet_prefix=%s detail=%s",
        diagnostic.get("code"),
        diagnostic.get("wallet_prefix"),
        diagnostic.get("detail", ""),
    )
