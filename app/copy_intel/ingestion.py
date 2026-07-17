"""Position snapshot diffing and trade event ingestion."""

from __future__ import annotations

import logging
from typing import Any

from app.config.settings import Settings
from app.copy_intel.data_client import CopyDataClient, build_dedup_key
from app.copy_intel.event_identity import build_event_uid, normalize_size
from app.copy_intel.models import PositionSnapshot, TradeEvent, TradeEventType
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
    events: list[TradeEvent] = []
    all_ids = set(previous) | set(current)
    for condition_id in all_ids:
        old = previous.get(condition_id)
        new = current.get(condition_id)
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
            condition_id=condition_id,
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
    """Build a TradeEvent from Data API /trades row when a native ID exists."""
    tx_hash = raw.get("transactionHash") or raw.get("transaction_hash") or raw.get("txHash")
    trade_id = raw.get("id") or raw.get("tradeId")
    if not tx_hash and not trade_id:
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
        tx_hash=str(tx_hash).lower() if tx_hash else None,
        ts=ts,
        title=raw.get("title"),
        slug=raw.get("slug"),
        dedup_key="",
        trade_id=str(trade_id) if trade_id else None,
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
        trade_id=event.trade_id,
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
        trade_id = match.get("id") or match.get("tradeId")
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
            tx_hash=str(tx).lower() if tx else None,
            ts=ts,
            title=event.title,
            slug=event.slug,
            dedup_key=event.dedup_key,
            observed_at=event.observed_at,
            event_uid=None,
            prev_size=event.prev_size,
            new_size=event.new_size,
            trade_id=str(trade_id) if trade_id else None,
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
                trade_id=updated.trade_id,
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


def ingest_wallet(settings: Settings, wallet: str, client: CopyDataClient | None = None) -> list[TradeEvent]:
    client = client or CopyDataClient(settings)
    raw_positions = client.fetch_positions(wallet)
    current = {
        p.condition_id: p
        for p in (parse_position(item, wallet) for item in raw_positions)
        if p.condition_id and p.size > _SIZE_EPS
    }
    previous = load_snapshots_as_models(settings, wallet)
    events = diff_positions(wallet, previous, current)
    try:
        trades = client.fetch_trades(wallet, limit=50)
    except Exception:
        logger.exception("Trade fetch failed for %s; continuing with position-diff only", wallet[:10])
        trades = []
    events = enrich_events_with_trade_ids(events, trades)
    # Exact current book across polls/restarts — unchanged positions yield no events.
    replace_wallet_snapshots(settings, wallet, current)
    return persist_trade_events(settings, events)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
