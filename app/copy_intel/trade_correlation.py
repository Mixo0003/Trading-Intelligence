"""Authoritative trade correlation for New Position / Increase (V2.2.2 Checkpoint 5).

Pure correlation logic: no order-book, recommendation, delivery, or paper writes.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

from app.copy_intel.api_result import ApiFailure, ApiSuccess
from app.copy_intel.data_client import CopyDataClient
from app.copy_intel.event_identity import normalize_size
from app.copy_intel.models import TradeEvent

logger = logging.getLogger(__name__)

# Approved overlap behind last complete positions stamp (seconds).
TRADE_CORRELATION_OVERLAP_SECONDS = 120

# Exact subset search is O(2^n). Reject before search above this candidate count.
# Internal constant only — not a user setting. Do not silently truncate.
MAX_CORRELATION_CANDIDATE_FILLS = 16

_VALID_PRICE_MIN = 0.0
_VALID_PRICE_MAX = 1.0
_UID_VERSION_PREFIX = "ff1"
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


@dataclass(frozen=True)
class TradeFill:
    tx_hash: str
    asset: str
    condition_id: str
    side: str
    size: float
    price: float
    timestamp: int


@dataclass(frozen=True)
class CorrelationAccept:
    event: TradeEvent
    fills: tuple[TradeFill, ...]


@dataclass(frozen=True)
class CorrelationQuarantine:
    code: str
    transition: TradeEvent
    diagnostic: dict[str, Any]


CorrelationOutcome = CorrelationAccept | CorrelationQuarantine


def _len_prefixed(value: str) -> str:
    """Frame a string so embedded separators/delimiters cannot collide."""
    encoded = value.encode("utf-8")
    return f"{len(encoded)}:{value}"


def fill_fingerprint(fill: TradeFill) -> str:
    """Documented-field fingerprint; multiplicity is preserved by callers."""
    return _FIELD_SEP.join(
        [
            _len_prefixed(fill.tx_hash.lower()),
            _len_prefixed(fill.asset),
            _len_prefixed(fill.side.upper()),
            _len_prefixed(f"{normalize_size(fill.size):.6f}"),
            _len_prefixed(f"{normalize_size(fill.price):.6f}"),
            _len_prefixed(str(int(fill.timestamp))),
        ]
    )


def build_correlated_canonical_payload(
    *,
    wallet: str,
    asset: str,
    event_type: str,
    prev_size: float,
    new_size: float,
    fills: Sequence[TradeFill],
) -> str:
    """Build the canonical pre-hash payload (not stored as the event UID)."""
    fps = sorted(fill_fingerprint(f) for f in fills)
    return _RECORD_SEP.join(
        [
            _len_prefixed(wallet.lower()),
            _len_prefixed(asset),
            _len_prefixed(event_type),
            _len_prefixed(f"{normalize_size(prev_size):.6f}"),
            _len_prefixed(f"{normalize_size(new_size):.6f}"),
            _len_prefixed(str(len(fps))),
            *[_len_prefixed(fp) for fp in fps],
        ]
    )


def build_correlated_event_uid(
    *,
    wallet: str,
    asset: str,
    event_type: str,
    prev_size: float,
    new_size: float,
    fills: Sequence[TradeFill],
) -> str:
    """Deterministic SHA-256 UID: ``ff1:<64 lowercase hex chars>``."""
    payload = build_correlated_canonical_payload(
        wallet=wallet,
        asset=asset,
        event_type=event_type,
        prev_size=prev_size,
        new_size=new_size,
        fills=fills,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_UID_VERSION_PREFIX}:{digest}"


def compute_trader_vwap(fills: Sequence[TradeFill]) -> float:
    total_size = sum(normalize_size(f.size) for f in fills)
    if total_size <= 0:
        raise ValueError("cannot compute VWAP with zero size")
    weighted = sum(normalize_size(f.price) * normalize_size(f.size) for f in fills)
    return weighted / total_size


def parse_candidate_fill(raw: Any, *, wallet: str) -> TradeFill | None:
    """Parse one trade row into a fill; reject undocumented / invalid fields."""
    if not isinstance(raw, dict):
        return None
    tx = raw.get("transactionHash") or raw.get("transaction_hash") or raw.get("txHash")
    if not tx:
        return None
    # Explicitly ignore undocumented id / tradeId.
    asset = raw.get("asset")
    if asset is None or str(asset).strip() == "":
        return None
    condition = raw.get("conditionId")
    if condition is None or str(condition).strip() == "":
        return None
    side = (raw.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        return None
    ts_raw = raw.get("timestamp")
    if ts_raw is None:
        return None
    try:
        timestamp = int(ts_raw)
    except (TypeError, ValueError):
        return None
    try:
        size_val = float(raw.get("size"))
        price_val = float(raw.get("price"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(size_val) or size_val <= 0:
        return None
    if not math.isfinite(price_val):
        return None
    if price_val < _VALID_PRICE_MIN or price_val > _VALID_PRICE_MAX:
        return None
    return TradeFill(
        tx_hash=str(tx).lower(),
        asset=str(asset).strip(),
        condition_id=str(condition).strip(),
        side=side,
        size=normalize_size(size_val),
        price=normalize_size(price_val),
        timestamp=timestamp,
    )


def filter_fills_for_transition(
    fills: Sequence[TradeFill],
    transition: TradeEvent,
    *,
    lower_ts: int,
    upper_ts: int,
    trade_watermark_ts: int | None,
) -> tuple[list[TradeFill], list[TradeFill]]:
    """Return (buy_candidates, sell_same_asset) inside the correlation window."""
    asset = (transition.asset or "").strip()
    condition = (transition.condition_id or "").strip()
    buys: list[TradeFill] = []
    sells: list[TradeFill] = []
    for fill in fills:
        if fill.asset != asset:
            continue
        if condition and fill.condition_id != condition:
            continue
        if fill.timestamp < lower_ts or fill.timestamp > upper_ts:
            continue
        if trade_watermark_ts is not None and fill.timestamp <= trade_watermark_ts:
            continue
        if fill.side == "SELL":
            sells.append(fill)
        elif fill.side == "BUY":
            buys.append(fill)
    return buys, sells


def _exact_sum_subsets(
    fills: Sequence[TradeFill], target: float
) -> list[tuple[TradeFill, ...]]:
    """All non-empty subsets whose sizes sum to target (6-decimal exact).

    Caller must enforce ``MAX_CORRELATION_CANDIDATE_FILLS`` before invoking.
    """
    target_n = normalize_size(target)
    n = len(fills)
    if n == 0:
        return []
    if n > MAX_CORRELATION_CANDIDATE_FILLS:
        raise RuntimeError("subset search invoked above complexity bound")
    results: list[tuple[TradeFill, ...]] = []
    for mask in range(1, 1 << n):
        subset = [fills[i] for i in range(n) if mask & (1 << i)]
        if normalize_size(sum(f.size for f in subset)) == target_n:
            results.append(tuple(subset))
    return results


def correlate_transition(
    transition: TradeEvent,
    fills: Sequence[TradeFill],
    *,
    lower_ts: int,
    upper_ts: int,
    trade_watermark_ts: int | None,
    detected_at: int,
) -> CorrelationOutcome:
    """Correlate one New/Increase transition against a shared trade book."""
    if transition.event_type not in {"open", "increase"}:
        return CorrelationQuarantine(
            code="unsupported_event_type",
            transition=transition,
            diagnostic={"code": "unsupported_event_type"},
        )
    prev = normalize_size(transition.prev_size)
    new = normalize_size(transition.new_size)
    delta = normalize_size(new - prev)
    if delta <= 0:
        return CorrelationQuarantine(
            code="non_positive_delta",
            transition=transition,
            diagnostic={"code": "non_positive_delta", "delta": delta},
        )
    if not transition.asset:
        return CorrelationQuarantine(
            code="missing_asset",
            transition=transition,
            diagnostic={"code": "missing_asset"},
        )

    buys, sells = filter_fills_for_transition(
        fills,
        transition,
        lower_ts=lower_ts,
        upper_ts=upper_ts,
        trade_watermark_ts=trade_watermark_ts,
    )
    if sells:
        return CorrelationQuarantine(
            code="interleaved_sell",
            transition=transition,
            diagnostic={
                "code": "interleaved_sell",
                "sell_count": len(sells),
                "asset": transition.asset,
            },
        )

    if len(buys) > MAX_CORRELATION_CANDIDATE_FILLS:
        return CorrelationQuarantine(
            code="correlation_complexity_exceeded",
            transition=transition,
            diagnostic={
                "code": "correlation_complexity_exceeded",
                "candidate_count": len(buys),
                "limit": MAX_CORRELATION_CANDIDATE_FILLS,
            },
        )

    subsets = _exact_sum_subsets(buys, delta)
    if len(subsets) == 0:
        buy_sum = normalize_size(sum(f.size for f in buys))
        code = "partial_coverage" if 0 < buy_sum < delta else "trade_size_mismatch"
        return CorrelationQuarantine(
            code=code,
            transition=transition,
            diagnostic={
                "code": code,
                "delta": delta,
                "buy_sum": buy_sum,
                "buy_count": len(buys),
            },
        )
    if len(subsets) > 1:
        return CorrelationQuarantine(
            code="ambiguous_trade_match",
            transition=transition,
            diagnostic={
                "code": "ambiguous_trade_match",
                "subset_count": len(subsets),
                "delta": delta,
            },
        )

    matched = subsets[0]
    vwap = compute_trader_vwap(matched)
    first_ts = min(f.timestamp for f in matched)
    last_ts = max(f.timestamp for f in matched)
    matched_size = normalize_size(sum(f.size for f in matched))
    unique_txs = sorted({f.tx_hash for f in matched})
    uid = build_correlated_event_uid(
        wallet=transition.wallet,
        asset=transition.asset,
        event_type=transition.event_type,
        prev_size=prev,
        new_size=new,
        fills=matched,
    )
    accepted = TradeEvent(
        wallet=transition.wallet,
        event_type=transition.event_type,
        condition_id=transition.condition_id,
        asset=transition.asset,
        outcome=transition.outcome,
        side="BUY",
        price=vwap,
        size=delta,
        tx_hash=unique_txs[0] if len(unique_txs) == 1 else None,
        ts=last_ts,
        title=transition.title,
        slug=transition.slug,
        dedup_key=transition.dedup_key,
        observed_at=transition.observed_at,
        event_uid=uid,
        prev_size=prev,
        new_size=new,
        trade_id=None,
        trader_vwap=vwap,
        first_fill_ts=first_ts,
        last_fill_ts=last_ts,
        matched_fill_count=len(matched),
        matched_fill_size=matched_size,
        detected_at=detected_at,
    )
    return CorrelationAccept(event=accepted, fills=matched)


def correlate_transitions(
    transitions: Sequence[TradeEvent],
    raw_trades: Sequence[Any],
    *,
    wallet: str,
    lower_ts: int,
    upper_ts: int,
    trade_watermark_ts: int | None,
    detected_at: int,
) -> tuple[list[TradeEvent], list[CorrelationQuarantine]]:
    """Correlate open/increase transitions against one shared trade fetch."""
    fills: list[TradeFill] = []
    for raw in raw_trades:
        fill = parse_candidate_fill(raw, wallet=wallet)
        if fill is not None:
            fills.append(fill)

    accepted: list[TradeEvent] = []
    quarantined: list[CorrelationQuarantine] = []
    # Consume matched fills so the same fill cannot accept two transitions.
    remaining = list(fills)
    for transition in transitions:
        outcome = correlate_transition(
            transition,
            remaining,
            lower_ts=lower_ts,
            upper_ts=upper_ts,
            trade_watermark_ts=trade_watermark_ts,
            detected_at=detected_at,
        )
        if isinstance(outcome, CorrelationQuarantine):
            quarantined.append(outcome)
            continue
        accepted.append(outcome.event)
        consumed = Counter(fill_fingerprint(f) for f in outcome.fills)
        new_remaining: list[TradeFill] = []
        for fill in remaining:
            fp = fill_fingerprint(fill)
            if consumed[fp] > 0:
                consumed[fp] -= 1
            else:
                new_remaining.append(fill)
        remaining = new_remaining
    return accepted, quarantined


def fetch_complete_trades(
    client: CopyDataClient,
    wallet: str,
    *,
    start_ts: int,
    end_ts: int,
    page_size: int = 100,
    max_pages: int = 50,
) -> ApiSuccess[list[dict[str, Any]]] | ApiFailure:
    """Paginated trades fetch with takerOnly=false and fixed time bounds."""
    page_size = max(1, int(page_size))
    all_rows: list[dict[str, Any]] = []
    request_count = 0
    offset = 0
    for _ in range(max_pages):
        try:
            result = client.fetch_trades(
                wallet,
                limit=page_size,
                offset=offset,
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed boundary
            return ApiFailure(
                code="request_error",
                status_code=None,
                retryable=True,
                request_count=max(1, request_count),
                message=str(exc)[:200],
            )
        request_count += max(1, int(getattr(result, "request_count", 1) or 1))
        if isinstance(result, ApiFailure):
            return ApiFailure(
                code=result.code,
                status_code=result.status_code,
                retryable=result.retryable,
                request_count=request_count,
                message=result.message,
            )
        if isinstance(result, ApiSuccess):
            chunk = result.data if isinstance(result.data, list) else []
        elif isinstance(result, list):
            chunk = result
        else:
            return ApiFailure(
                code="wrong_type",
                status_code=None,
                retryable=False,
                request_count=request_count,
                message="unexpected trades fetch type",
            )
        all_rows.extend(chunk)
        if len(chunk) < page_size:
            return ApiSuccess(data=all_rows, request_count=request_count)
        offset += page_size
    return ApiFailure(
        code="incomplete_trades",
        status_code=None,
        retryable=True,
        request_count=request_count,
        message="trades book exceeded max pages",
    )


def sanitized_correlation_diagnostic(
    quarantine: CorrelationQuarantine,
) -> dict[str, Any]:
    """Sanitized diagnostic for persistence/logs (no secrets or absolute paths)."""
    t = quarantine.transition
    return {
        "code": quarantine.code,
        "wallet_prefix": (t.wallet or "")[:10].lower(),
        "asset": t.asset,
        "event_type": t.event_type,
        "prev_size": t.prev_size,
        "new_size": t.new_size,
        **{k: v for k, v in quarantine.diagnostic.items() if k != "code"},
    }
