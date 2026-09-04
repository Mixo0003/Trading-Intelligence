"""WAIT trigger monitoring. Fires a distinct Telegram alert when the condition hits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.config.settings import Settings
from app.copy_intel.order_book import ValidatedOrderBook
from app.database import live_edge_storage, opportunity_storage
from app.opportunity_intel.live_edge.decisions import wait_trigger_met
from app.opportunity_intel.live_edge.economics import (
    conservative_edge_pp,
    executable_quote_from_book,
)
from app.opportunity_intel.live_edge.models import LiveEdgeDecision, ProbabilityRange


@dataclass(frozen=True)
class WaitFire:
    wait_uid: str
    telegram_id: int
    signal_uid: str
    question: str
    trigger_description: str


def check_wait_triggers(
    settings: Settings,
    *,
    quotes: dict[str, ValidatedOrderBook],
    probabilities: dict[str, ProbabilityRange],
    current_maps: dict[str, int] | None = None,
    now: datetime | None = None,
) -> tuple[WaitFire, ...]:
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    fired: list[WaitFire] = []
    maps = current_maps or {}
    for row in live_edge_storage.list_open_waits(settings):
        telegram_id = int(row["telegram_id"])
        sub = opportunity_storage.load_subscription(settings, telegram_id)
        if sub is None or not sub.settings.live_edge_enabled:
            continue
        trigger = live_edge_storage.parse_wait_trigger(row["trigger_json"])
        asset_id = row["asset_id"]
        book = quotes.get(asset_id)
        quote = None
        edge = None
        if book is not None:
            quote = executable_quote_from_book(
                book,
                allocation_usd=sub.settings.paper_allocation_usd,
                side="YES",
                now=clock,
            )
            probability = probabilities.get(asset_id)
            if probability is not None and quote is not None:
                edge = conservative_edge_pp(probability, quote)
        if not wait_trigger_met(
            trigger,
            quote=quote,
            edge_pp=edge,
            current_map=maps.get(row["market_id"]),
        ):
            continue
        live_edge_storage.mark_wait_fired(settings, row["wait_uid"], fired_at=clock)
        signal = live_edge_storage.load_signal(settings, row["signal_uid"])
        fired.append(
            WaitFire(
                wait_uid=row["wait_uid"],
                telegram_id=telegram_id,
                signal_uid=row["signal_uid"],
                question=signal.question if signal is not None else row["market_id"],
                trigger_description=trigger.description_uk,
            )
        )
    return tuple(fired)
