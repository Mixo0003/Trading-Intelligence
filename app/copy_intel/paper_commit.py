"""Atomic COPY paper commit (V2.2.2 Checkpoint 8).

Ordering (immutable before write):
  accepted event → validated quote → proposed allocation → sweep → final COPY
  → numeric validation → exposure recheck in DB txn → atomic envelope+paper → commit.

Entry price is exactly allocation execution VWAP. No Telegram delivery here.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any

from app.config.settings import Settings
from app.copy_intel.models import CopyRecommendation
from app.copy_intel.risk import assert_read_only
from app.database import copy_storage

logger = logging.getLogger(__name__)

# Prediction-market price bounds (same family as validated book levels).
_PRICE_MIN = 0.0
_PRICE_MAX = 1.0
_MATCH_EPS = 1e-9

CODE_COMMITTED = "copy_paper_committed"
CODE_IDEMPOTENT = "copy_paper_idempotent"
CODE_NOT_COPY = "not_copy_decision"
CODE_MISSING_EXECUTION_VWAP = "missing_execution_vwap"
CODE_INVALID_EXECUTION_VWAP = "invalid_execution_vwap"
CODE_INVALID_ALLOCATION = "invalid_allocation"
CODE_INVALID_FILLED_SHARES = "invalid_filled_shares"
CODE_INVALID_BANKROLL = "invalid_bankroll"
CODE_INVALID_BANKROLL_SHARE = "invalid_bankroll_share"
CODE_INVALID_ASSET = "invalid_asset"
CODE_INVALID_EVENT_UID = "invalid_event_uid"
CODE_INVALID_TIMESTAMP = "invalid_authoritative_timestamp"
CODE_INVALID_CONDITION = "invalid_condition_id"
CODE_EXPOSURE_ZERO = "exposure_capacity_zero_at_commit"
CODE_EXPOSURE_CHANGED = "exposure_changed_at_commit"
CODE_CONFLICT = "conflicting_duplicate_paper"
CODE_STORAGE_ERROR = "paper_storage_error"


@dataclass(frozen=True)
class PaperCommitResult:
    ok: bool
    inserted: bool
    code: str
    message: str
    paper_id: int | None = None
    delivery_eligible: bool = False


def commit_copy_paper_for_recommendation(
    settings: Settings,
    rec: CopyRecommendation,
    *,
    condition_id: str,
    asset: str,
) -> PaperCommitResult:
    """Persist COPY recommendation + paper atomically after an immutable COPY decision.

    Never opens paper for WATCH/SKIP. Entry price is ``rec.execution_vwap`` only.
    """
    assert_read_only(settings)

    if rec.action != "COPY":
        return PaperCommitResult(
            ok=False,
            inserted=False,
            code=CODE_NOT_COPY,
            message="paper write requires final COPY decision",
            delivery_eligible=False,
        )

    validation = _validate_copy_numerics(rec, condition_id=condition_id, asset=asset)
    if validation is not None:
        return validation

    event_uid = str(rec.event_uid)
    execution_vwap = float(rec.execution_vwap)  # type: ignore[arg-type]
    allocation = float(rec.proposed_allocation_usd or rec.max_alloc)
    filled_shares = float(rec.resulting_position_size)  # type: ignore[arg-type]
    authoritative_ts = int(rec.last_fill_ts or rec.first_fill_ts)  # type: ignore[arg-type]

    envelope = _envelope_payload(
        rec,
        asset=asset,
        condition_id=condition_id,
        authoritative_ts=authoritative_ts,
        filled_shares=filled_shares,
    )
    recommendation_row = {
        "wallet": rec.wallet,
        "market_slug": rec.market_slug,
        "market_title": rec.market_title,
        "outcome": rec.outcome,
        "action": rec.action,
        "trader_score": rec.trader_score,
        "max_alloc": allocation,
        "reason": rec.reason,
        "entry_price": execution_vwap,
        "current_price": rec.midpoint,
        "price_drift": rec.price_drift,
        "slippage_est": rec.slippage,
        "event_uid": event_uid,
        "envelope_json": json.dumps(envelope, separators=(",", ":"), sort_keys=True),
        "reason_codes_json": json.dumps(list(rec.reason_codes)),
    }
    paper_row = {
        "wallet": rec.wallet,
        "condition_id": condition_id,
        "market_slug": rec.market_slug,
        "outcome": rec.outcome,
        "notional": allocation,
        "entry_price": execution_vwap,
        "asset": str(asset).strip(),
        "source_event_uid": event_uid,
    }
    audit_payload = {
        "wallet": rec.wallet.lower(),
        "source_event_uid": event_uid,
        "asset": str(asset).strip(),
        "condition_id": condition_id,
        "outcome": rec.outcome,
        "event_type": rec.event_type,
        "authoritative_trade_ts": authoritative_ts,
        "detected_at": rec.detected_at,
        "trader_vwap": rec.trader_vwap,
        "midpoint": rec.midpoint,
        "best_ask": rec.best_ask,
        "execution_vwap": execution_vwap,
        "allocation": allocation,
        "filled_shares": filled_shares,
        "paper_bankroll_share": rec.paper_bankroll_share,
        "quote_at": rec.quote_at,
        "slippage": rec.slippage,
        "action": "COPY",
    }

    try:
        storage_result = copy_storage.commit_copy_recommendation_and_paper(
            settings,
            recommendation=recommendation_row,
            paper=paper_row,
            audit_payload=audit_payload,
        )
    except Exception:
        logger.exception("Atomic COPY paper commit failed")
        return PaperCommitResult(
            ok=False,
            inserted=False,
            code=CODE_STORAGE_ERROR,
            message="storage error during paper commit",
            delivery_eligible=False,
        )

    return PaperCommitResult(
        ok=storage_result.ok,
        inserted=storage_result.inserted,
        code=storage_result.code,
        message=storage_result.message,
        paper_id=storage_result.paper_id,
        delivery_eligible=bool(storage_result.ok),
    )


def prepare_pending_recommendation(
    settings: Settings,
    rec: CopyRecommendation,
    *,
    condition_id: str,
    asset: str,
) -> tuple[bool, PaperCommitResult | None]:
    """Scheduler gate: COPY requires successful paper commit; WATCH/SKIP skip paper write.

    Returns ``(include_in_pending, paper_result_or_none)``.
    Failed COPY paper write blocks later delivery eligibility for that event.
    """
    if rec.action == "COPY":
        result = commit_copy_paper_for_recommendation(
            settings, rec, condition_id=condition_id, asset=asset
        )
        return result.ok, result
    if rec.action == "WATCH":
        return True, None
    # SKIP / other: never enter pending delivery.
    return False, None


def _validate_copy_numerics(
    rec: CopyRecommendation,
    *,
    condition_id: str,
    asset: str,
) -> PaperCommitResult | None:
    if not rec.event_uid or not str(rec.event_uid).strip():
        return _reject(CODE_INVALID_EVENT_UID, "event uid required")
    if not condition_id or not str(condition_id).strip():
        return _reject(CODE_INVALID_CONDITION, "condition id required")
    if not asset or not str(asset).strip():
        return _reject(CODE_INVALID_ASSET, "asset required")

    if rec.execution_vwap is None:
        return _reject(CODE_MISSING_EXECUTION_VWAP, "execution vwap required")
    vwap = float(rec.execution_vwap)
    if not math.isfinite(vwap) or vwap <= _PRICE_MIN or vwap > _PRICE_MAX:
        return _reject(CODE_INVALID_EXECUTION_VWAP, "execution vwap out of range")

    allocation = rec.proposed_allocation_usd if rec.proposed_allocation_usd is not None else rec.max_alloc
    try:
        allocation_f = float(allocation)
    except (TypeError, ValueError):
        return _reject(CODE_INVALID_ALLOCATION, "allocation invalid")
    if not math.isfinite(allocation_f) or allocation_f <= 0:
        return _reject(CODE_INVALID_ALLOCATION, "allocation must be finite positive")

    if rec.resulting_position_size is None:
        return _reject(CODE_INVALID_FILLED_SHARES, "filled shares required")
    filled = float(rec.resulting_position_size)
    if not math.isfinite(filled) or filled <= 0:
        return _reject(CODE_INVALID_FILLED_SHARES, "filled shares must be finite positive")

    if rec.paper_bankroll_usd is None:
        return _reject(CODE_INVALID_BANKROLL, "paper bankroll required")
    bankroll = float(rec.paper_bankroll_usd)
    if not math.isfinite(bankroll) or bankroll <= 0:
        return _reject(CODE_INVALID_BANKROLL, "paper bankroll must be finite positive")

    if rec.paper_bankroll_share is None:
        return _reject(CODE_INVALID_BANKROLL_SHARE, "bankroll share required")
    share = float(rec.paper_bankroll_share)
    if not math.isfinite(share) or share <= 0:
        return _reject(CODE_INVALID_BANKROLL_SHARE, "bankroll share must be finite positive")

    ts = rec.last_fill_ts if rec.last_fill_ts is not None else rec.first_fill_ts
    if ts is None:
        return _reject(CODE_INVALID_TIMESTAMP, "authoritative trade timestamp required")
    try:
        ts_i = int(ts)
    except (TypeError, ValueError):
        return _reject(CODE_INVALID_TIMESTAMP, "authoritative trade timestamp invalid")
    if ts_i <= 0:
        return _reject(CODE_INVALID_TIMESTAMP, "authoritative trade timestamp invalid")

    return None


def _reject(code: str, message: str) -> PaperCommitResult:
    return PaperCommitResult(
        ok=False,
        inserted=False,
        code=code,
        message=message,
        delivery_eligible=False,
    )


def _envelope_payload(
    rec: CopyRecommendation,
    *,
    asset: str,
    condition_id: str,
    authoritative_ts: int,
    filled_shares: float,
) -> dict[str, Any]:
    return {
        "event_uid": rec.event_uid,
        "wallet": rec.wallet,
        "asset": str(asset).strip(),
        "condition_id": condition_id,
        "action": rec.action,
        "reason_codes": list(rec.reason_codes),
        "event_type": rec.event_type,
        "first_fill_ts": rec.first_fill_ts,
        "last_fill_ts": rec.last_fill_ts,
        "authoritative_trade_ts": authoritative_ts,
        "detected_at": rec.detected_at,
        "event_age_seconds": rec.event_age_seconds,
        "trader_vwap": rec.trader_vwap,
        "midpoint": rec.midpoint,
        "best_bid": rec.best_bid,
        "best_ask": rec.best_ask,
        "execution_vwap": rec.execution_vwap,
        "slippage": rec.slippage,
        "spread": rec.spread,
        "visible_ask_depth_usd": rec.visible_ask_depth_usd,
        "proposed_allocation_usd": rec.proposed_allocation_usd or rec.max_alloc,
        "paper_bankroll_usd": rec.paper_bankroll_usd,
        "paper_bankroll_share": rec.paper_bankroll_share,
        "resulting_position_size": filled_shares,
        "filled_shares": filled_shares,
        "size_delta": rec.size_delta,
        "matched_fill_count": rec.matched_fill_count,
        "quote_at": rec.quote_at,
        "quote_age_seconds": rec.quote_age_seconds,
        "trader_score": rec.trader_score,
        "price_drift": rec.price_drift,
        "entry_price": rec.execution_vwap,
    }


def values_match(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= _MATCH_EPS
