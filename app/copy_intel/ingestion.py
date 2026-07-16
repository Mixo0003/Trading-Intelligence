"""Position snapshot diffing and trade event ingestion."""

from __future__ import annotations

import logging
from typing import Any

from app.config.settings import Settings
from app.copy_intel.data_client import CopyDataClient, build_dedup_key
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
        size=float(raw.get("size") or 0),
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
        old_size = old.size if old else 0.0
        new_size = new.size if new else 0.0
        if old_size <= _SIZE_EPS and new_size > _SIZE_EPS:
            event_type: TradeEventType = "open"
        elif old_size > _SIZE_EPS and new_size <= _SIZE_EPS:
            event_type = "close"
        elif new_size > old_size + _SIZE_EPS:
            event_type = "increase"
        elif new_size < old_size - _SIZE_EPS and new_size > _SIZE_EPS:
            event_type = "reduce"
        else:
            continue
        ref = new or old
        assert ref is not None
        events.append(
            TradeEvent(
                wallet=wallet.lower(),
                event_type=event_type,
                condition_id=condition_id,
                asset=ref.asset,
                outcome=ref.outcome,
                side="BUY" if event_type in {"open", "increase"} else "SELL",
                price=ref.avg_price,
                size=abs(new_size - old_size) if event_type != "close" else old_size,
                tx_hash=None,
                ts=None,
                title=ref.title,
                slug=ref.slug,
                dedup_key=build_dedup_key(
                    wallet,
                    tx_hash=None,
                    asset=ref.asset,
                    side=event_type,
                    size=abs(new_size - old_size),
                    ts=None,
                    event_type=event_type,
                ),
            )
        )
    return events


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


def load_snapshots_as_models(settings: Settings, wallet: str) -> dict[str, PositionSnapshot]:
    rows = copy_storage.load_position_snapshots(settings, wallet)
    result: dict[str, PositionSnapshot] = {}
    for condition_id, row in rows.items():
        result[condition_id] = PositionSnapshot(
            wallet=wallet.lower(),
            condition_id=condition_id,
            asset=row["asset"],
            outcome=row["outcome"],
            size=float(row["size"] or 0),
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
            },
        )
        if inserted:
            saved.append(event)
    return saved


def ingest_wallet(settings: Settings, wallet: str, client: CopyDataClient | None = None) -> list[TradeEvent]:
    client = client or CopyDataClient(settings)
    raw_positions = client.fetch_positions(wallet)
    current = {
        (p.condition_id): p
        for p in (parse_position(item, wallet) for item in raw_positions)
        if p.condition_id and p.size > _SIZE_EPS
    }
    previous = load_snapshots_as_models(settings, wallet)
    events = diff_positions(wallet, previous, current)
    persist_snapshots(settings, {**previous, **current})
    # Remove closed positions from snapshot table by zeroing — upsert handles updates
    for condition_id in set(previous) - set(current):
        old = previous[condition_id]
        copy_storage.upsert_position_snapshot(
            settings,
            wallet,
            condition_id,
            asset=old.asset,
            outcome=old.outcome,
            size=0.0,
            avg_price=old.avg_price,
            cur_price=old.cur_price,
            title=old.title,
            slug=old.slug,
            event_slug=old.event_slug,
        )
    return persist_trade_events(settings, events)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
