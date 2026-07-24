"""Position snapshot diffing and trade event ingestion."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from app.config.settings import Settings
from app.copy_intel.api_result import ApiFailure
from app.copy_intel.data_client import CopyDataClient, build_dedup_key
from app.copy_intel.event_identity import build_event_uid, normalize_size
from app.copy_intel.models import PositionSnapshot, TradeEvent, TradeEventType
from app.copy_intel.trade_correlation import (
    TRADE_CORRELATION_OVERLAP_SECONDS,
    correlate_transitions,
    fetch_complete_trades,
    sanitized_correlation_diagnostic,
)
from app.database import copy_storage

logger = logging.getLogger(__name__)

_SIZE_EPS = 1e-6


def parse_position(raw: dict[str, Any], wallet: str) -> PositionSnapshot:
    return PositionSnapshot(
        wallet=wallet.lower(),
        condition_id=raw.get("conditionId") or "",
        asset=raw.get("asset"),
        outcome=raw.get("outcome"),
        size=normalize_size(float(raw.get("size") or 0)),
        avg_price=_float_or_none(raw.get("avgPrice")),
        cur_price=_float_or_none(raw.get("curPrice")),
        title=raw.get("title"),
        slug=raw.get("slug"),
        event_slug=raw.get("eventSlug"),
    )


def diff_positions(
    wallet: str,
    previous: dict[str, PositionSnapshot],
    current: dict[str, PositionSnapshot],
) -> list[TradeEvent]:
    """Diff two position maps. Keys are opaque identity (asset for V2 path)."""
    events: list[TradeEvent] = []
    all_ids = set(previous) | set(current)
    for key in all_ids:
        old = previous.get(key)
        new = current.get(key)
        old_size = normalize_size(old.size if old else 0.0)
        new_size = normalize_size(new.size if new else 0.0)
        if old_size <= _SIZE_EPS and new_size > _SIZE_EPS:
            event_type: TradeEventType = "open"
        elif old_size > _SIZE_EPS and new_size <= _SIZE_EPS:
            event_type = "close"
        elif new_size > old_size + _SIZE_EPS:
            event_type = "increase"
        elif new_size < old_size - _SIZE_EPS and new_size > _SIZE_EPS:
            event_type = "reduce"
        else:
            # Unchanged (including both empty) — no event.
            continue
        ref = new or old
        assert ref is not None
        delta = abs(new_size - old_size) if event_type != "close" else old_size
        event = TradeEvent(
            wallet=wallet.lower(),
            event_type=event_type,
            condition_id=ref.condition_id,
            asset=ref.asset,
            outcome=ref.outcome,
            side="BUY" if event_type in {"open", "increase"} else "SELL",
            price=ref.avg_price,
            size=delta,
            tx_hash=None,
            ts=None,
            title=ref.title,
            slug=ref.slug,
            dedup_key="",
            prev_size=old_size,
            new_size=new_size,
        )
        uid = build_event_uid(event)
        dedup = build_dedup_key(
            wallet,
            tx_hash=None,
            asset=ref.asset,
            side=event_type,
            size=delta,
            ts=None,
            event_type=event_type,
        )
        # Include absolute transition in dedup so reopen noise cannot re-insert.
        dedup = f"{dedup}|{old_size:.6f}->{new_size:.6f}|{ref.outcome or ''}"
        events.append(
            TradeEvent(
                wallet=event.wallet,
                event_type=event.event_type,
                condition_id=event.condition_id,
                asset=event.asset,
                outcome=event.outcome,
                side=event.side,
                price=event.price,
                size=event.size,
                tx_hash=None,
                ts=None,
                title=event.title,
                slug=event.slug,
                dedup_key=dedup,
                event_uid=uid,
                prev_size=old_size,
                new_size=new_size,
            )
        )
    return events


def parse_trade_event(raw: dict[str, Any], wallet: str) -> TradeEvent | None:
    """Build a TradeEvent from Data API /trades row when a documented tx hash exists.

    Native identity uses only documented fields (transactionHash variants).
    Undocumented id / tradeId are ignored and never trusted.
    """
    tx_hash = raw.get("transactionHash") or raw.get("transaction_hash") or raw.get("txHash")
    if not tx_hash:
        return None
    side = (raw.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        return None
    size = normalize_size(float(raw.get("size") or 0))
    if size <= _SIZE_EPS:
        return None
    event_type: TradeEventType = "open" if side == "BUY" else "reduce"
    ts = int(raw["timestamp"]) if raw.get("timestamp") is not None else None
    condition_id = raw.get("conditionId") or ""
    event = TradeEvent(
        wallet=wallet.lower(),
        event_type=event_type,
        condition_id=condition_id,
        asset=raw.get("asset"),
        outcome=raw.get("outcome"),
        side=side,
        price=_float_or_none(raw.get("price")),
        size=size,
        tx_hash=str(tx_hash).lower(),
        ts=ts,
        title=raw.get("title"),
        slug=raw.get("slug"),
        dedup_key="",
        trade_id=None,
        prev_size=None,
        new_size=None,
    )
    uid = build_event_uid(event)
    dedup = build_dedup_key(
        wallet,
        tx_hash=event.tx_hash,
        asset=event.asset,
        side=side,
        size=size,
        ts=ts,
        event_type=event_type,
    )
    return TradeEvent(
        wallet=event.wallet,
        event_type=event.event_type,
        condition_id=event.condition_id,
        asset=event.asset,
        outcome=event.outcome,
        side=event.side,
        price=event.price,
        size=event.size,
        tx_hash=event.tx_hash,
        ts=event.ts,
        title=event.title,
        slug=event.slug,
        dedup_key=dedup,
        event_uid=uid,
        trade_id=None,
    )


def enrich_events_with_trade_ids(
    events: list[TradeEvent],
    trades: list[dict[str, Any]],
) -> list[TradeEvent]:
    """Attach transaction hashes to position-diff events when a trade matches."""
    if not events or not trades:
        return events
    by_condition: dict[str, list[dict[str, Any]]] = {}
    for raw in trades:
        cid = raw.get("conditionId") or ""
        if cid:
            by_condition.setdefault(cid, []).append(raw)

    enriched: list[TradeEvent] = []
    for event in events:
        if event.tx_hash or not event.condition_id:
            enriched.append(event)
            continue
        candidates = by_condition.get(event.condition_id, [])
        match = _best_trade_match(event, candidates)
        if match is None:
            enriched.append(event)
            continue
        tx = match.get("transactionHash") or match.get("transaction_hash") or match.get("txHash")
        # Undocumented id / tradeId are ignored; only documented tx hash is trusted.
        if not tx:
            enriched.append(event)
            continue
        ts = int(match["timestamp"]) if match.get("timestamp") is not None else event.ts
        updated = TradeEvent(
            wallet=event.wallet,
            event_type=event.event_type,
            condition_id=event.condition_id,
            asset=event.asset,
            outcome=event.outcome,
            side=event.side,
            price=event.price,
            size=event.size,
            tx_hash=str(tx).lower(),
            ts=ts,
            title=event.title,
            slug=event.slug,
            dedup_key=event.dedup_key,
            observed_at=event.observed_at,
            event_uid=None,
            prev_size=event.prev_size,
            new_size=event.new_size,
            trade_id=None,
        )
        uid = build_event_uid(updated)
        dedup = build_dedup_key(
            event.wallet,
            tx_hash=updated.tx_hash,
            asset=event.asset,
            side=event.side,
            size=event.size,
            ts=ts,
            event_type=event.event_type,
        )
        enriched.append(
            TradeEvent(
                wallet=updated.wallet,
                event_type=updated.event_type,
                condition_id=updated.condition_id,
                asset=updated.asset,
                outcome=updated.outcome,
                side=updated.side,
                price=updated.price,
                size=updated.size,
                tx_hash=updated.tx_hash,
                ts=updated.ts,
                title=updated.title,
                slug=updated.slug,
                dedup_key=dedup,
                event_uid=uid,
                prev_size=updated.prev_size,
                new_size=updated.new_size,
                trade_id=None,
            )
        )
    return enriched


def _best_trade_match(event: TradeEvent, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    want_side = "BUY" if event.event_type in {"open", "increase"} else "SELL"
    delta = normalize_size(event.size)
    best: dict[str, Any] | None = None
    best_dist = 1e18
    for raw in candidates:
        side = (raw.get("side") or "").upper()
        if side != want_side:
            continue
        size = normalize_size(float(raw.get("size") or 0))
        dist = abs(size - delta)
        if dist <= max(_SIZE_EPS, delta * 0.01) and dist < best_dist:
            best = raw
            best_dist = dist
    return best


def persist_snapshots(settings: Settings, positions: dict[str, PositionSnapshot]) -> None:
    for snap in positions.values():
        copy_storage.upsert_position_snapshot(
            settings,
            snap.wallet,
            snap.condition_id,
            asset=snap.asset,
            outcome=snap.outcome,
            size=snap.size,
            avg_price=snap.avg_price,
            cur_price=snap.cur_price,
            title=snap.title,
            slug=snap.slug,
            event_slug=snap.event_slug,
        )


def replace_wallet_snapshots(
    settings: Settings,
    wallet: str,
    positions: dict[str, PositionSnapshot],
) -> None:
    """Persist the exact current book; removes closed/missing markets."""
    copy_storage.clear_position_snapshots(settings, wallet)
    persist_snapshots(settings, positions)


def load_snapshots_as_models(settings: Settings, wallet: str) -> dict[str, PositionSnapshot]:
    """Load legacy condition-keyed snapshots (rollback / characterization only)."""
    rows = copy_storage.load_position_snapshots(settings, wallet)
    result: dict[str, PositionSnapshot] = {}
    for condition_id, row in rows.items():
        size = normalize_size(float(row["size"] or 0))
        if size <= _SIZE_EPS:
            continue
        result[condition_id] = PositionSnapshot(
            wallet=wallet.lower(),
            condition_id=condition_id,
            asset=row["asset"],
            outcome=row["outcome"],
            size=size,
            avg_price=row["avg_price"],
            cur_price=row["cur_price"],
            title=row["title"],
            slug=row["slug"],
            event_slug=row["event_slug"],
        )
    return result


def load_snapshots_v2_as_models(
    settings: Settings, wallet: str
) -> dict[str, PositionSnapshot]:
    """Load production V2 snapshots keyed by asset."""
    rows = copy_storage.load_position_snapshots_v2(settings, wallet)
    result: dict[str, PositionSnapshot] = {}
    for asset, row in rows.items():
        size = normalize_size(float(row["size"] or 0))
        if size <= _SIZE_EPS:
            continue
        result[str(asset)] = PositionSnapshot(
            wallet=wallet.lower(),
            condition_id=row["condition_id"],
            asset=str(asset),
            outcome=row["outcome"],
            size=size,
            avg_price=row["avg_price"],
            cur_price=row["cur_price"],
            title=row["title"],
            slug=row["slug"],
            event_slug=row["event_slug"],
        )
    return result


def _snapshots_to_v2_payload(
    positions: dict[str, PositionSnapshot],
) -> dict[str, dict]:
    payload: dict[str, dict] = {}
    for asset, snap in positions.items():
        key = snap.asset or asset
        payload[str(key)] = {
            "condition_id": snap.condition_id,
            "outcome": snap.outcome,
            "size": snap.size,
            "avg_price": snap.avg_price,
            "cur_price": snap.cur_price,
            "title": snap.title,
            "slug": snap.slug,
            "event_slug": snap.event_slug,
        }
    return payload


def persist_trade_events(settings: Settings, events: list[TradeEvent]) -> list[TradeEvent]:
    saved: list[TradeEvent] = []
    for event in events:
        inserted = copy_storage.insert_trade_event(
            settings,
            dedup_key=event.dedup_key,
            wallet=event.wallet,
            event_type=event.event_type,
            condition_id=event.condition_id,
            asset=event.asset,
            outcome=event.outcome,
            side=event.side,
            price=event.price,
            size=event.size,
            tx_hash=event.tx_hash,
            ts=event.ts,
            title=event.title,
            slug=event.slug,
            raw_json={
                "event_type": event.event_type,
                "condition_id": event.condition_id,
                "outcome": event.outcome,
                "event_uid": event.event_uid,
                "prev_size": event.prev_size,
                "new_size": event.new_size,
            },
        )
        if inserted:
            saved.append(event)
    return saved


def _resolve_consistent_book(
    wallet: str,
    client: CopyDataClient,
    previous: dict[str, PositionSnapshot],
):
    """Fetch a validated book and apply the approved second-read consistency control."""
    from app.copy_intel.position_book import (
        PositionBookFailure,
        books_equal,
        emit_book_diagnostic,
        fetch_validated_position_book,
        needs_consistency_second_read,
        sanitized_book_diagnostic,
    )

    first = fetch_validated_position_book(client, wallet)
    if isinstance(first, PositionBookFailure):
        diagnostic = sanitized_book_diagnostic(
            code=first.code, wallet=wallet, detail=first.message
        )
        emit_book_diagnostic(diagnostic)
        return None, diagnostic

    current = first.positions
    if needs_consistency_second_read(
        previous, current, was_multipage=first.was_multipage
    ):
        second = fetch_validated_position_book(client, wallet)
        if isinstance(second, PositionBookFailure):
            diagnostic = sanitized_book_diagnostic(
                code=second.code,
                wallet=wallet,
                detail=f"second_read_failed:{second.message}",
            )
            emit_book_diagnostic(diagnostic)
            return None, diagnostic
        if not books_equal(current, second.positions):
            diagnostic = sanitized_book_diagnostic(
                code="inconsistent_second_read",
                wallet=wallet,
                detail="complete position reads disagreed; retained prior snapshot",
            )
            emit_book_diagnostic(diagnostic)
            return None, diagnostic
        current = second.positions
    return current, None


def _parse_iso_to_epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp())
    except (TypeError, ValueError):
        return None


def ingest_wallet(
    settings: Settings, wallet: str, client: CopyDataClient | None = None
) -> list[TradeEvent]:
    """
    Asset-keyed complete-book ingestion with Checkpoint 5 trade correlation.

    - ApiFailure / malformed books do not mutate snapshots.
    - Trade ApiFailure retains snapshots and yields no accepted events.
    - Only correlated New/Increase events are returned/persisted.
    - Reduce/Close remain suppressed.
    """
    client = client or CopyDataClient(settings)
    previous = load_snapshots_v2_as_models(settings, wallet)
    current, _diagnostic = _resolve_consistent_book(wallet, client, previous)
    if current is None:
        return []

    all_events = diff_positions(wallet, previous, current)
    open_increase = [e for e in all_events if e.event_type in {"open", "increase"}]

    detected_at = int(time.time())
    state = copy_storage.get_wallet_monitor_state(settings, wallet)
    complete_epoch = (
        _parse_iso_to_epoch(state.last_complete_positions_at) if state else None
    )
    if complete_epoch is not None:
        lower_ts = complete_epoch - TRADE_CORRELATION_OVERLAP_SECONDS
    else:
        lower_ts = detected_at - max(
            TRADE_CORRELATION_OVERLAP_SECONDS, settings.copy_max_event_age_seconds
        )
    upper_ts = detected_at
    trade_watermark = state.last_correlated_trade_ts if state else None

    if open_increase:
        trades_result = fetch_complete_trades(
            client,
            wallet,
            start_ts=lower_ts,
            end_ts=upper_ts,
        )
        if isinstance(trades_result, ApiFailure):
            logger.warning(
                "trade correlation fetch failed for %s: code=%s",
                wallet[:10],
                trades_result.code,
            )
            # Retain prior snapshots; do not commit the new book or emit events.
            return []

        accepted, quarantines = correlate_transitions(
            open_increase,
            trades_result.data,
            wallet=wallet,
            lower_ts=lower_ts,
            upper_ts=upper_ts,
            trade_watermark_ts=trade_watermark,
            detected_at=detected_at,
        )
        complexity_hit = any(
            item.code == "correlation_complexity_exceeded" for item in quarantines
        )
        for item in quarantines:
            diagnostic = sanitized_correlation_diagnostic(item)
            copy_storage.record_trade_correlation_quarantine(
                settings,
                wallet=wallet,
                reason=item.code,
                asset=item.transition.asset,
                condition_id=item.transition.condition_id,
                event_type=item.transition.event_type,
                prev_size=item.transition.prev_size,
                new_size=item.transition.new_size,
                detected_at=detected_at,
                diagnostic=diagnostic,
            )
            # Complexity rejection must remain retryable: no processed mark, no commit.
            if item.code == "correlation_complexity_exceeded":
                continue
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=(
                    item.transition.event_uid
                    or build_event_uid(item.transition)
                ),
                wallet=wallet,
                event_ts=None,
                observed_at=detected_at,
                alertable=False,
                reject_reason=item.code,
                diagnostic_json=diagnostic,
            )
        if complexity_hit:
            # Retain prior snapshots; no accepted events on this path.
            return []
    else:
        accepted = []

    copy_storage.commit_complete_position_book_v2(
        settings,
        wallet,
        _snapshots_to_v2_payload(current),
        stamp_complete=True,
    )
    if accepted:
        max_fill_ts = max(
            int(e.last_fill_ts)
            for e in accepted
            if e.last_fill_ts is not None
        )
        copy_storage.update_wallet_monitor_fields(
            settings,
            wallet,
            last_correlated_trade_ts=max_fill_ts,
        )
    return persist_trade_events(settings, accepted)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
