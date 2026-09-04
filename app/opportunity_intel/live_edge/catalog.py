"""Merge Live Edge cards into Top Opportunities when the per-user toggle is ON."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from app.config.settings import Settings
from app.database import live_edge_storage
from app.opportunity_intel.catalog import TopOpportunitiesResult
from app.opportunity_intel.settings import OpportunityScannerSettings

WATCH_CATALOG_MAX_AGE_SECONDS = 15 * 60


@dataclass(frozen=True)
class LiveEdgeCatalogCard:
    signal_uid: str
    question: str
    text: str
    decision: str
    source_label: str = "🔥 Live Edge"


def load_live_edge_cards(
    settings: Settings,
    *,
    telegram_id: int,
    user_settings: OpportunityScannerSettings,
    limit: int = 5,
) -> tuple[LiveEdgeCatalogCard, ...]:
    if not user_settings.live_edge_enabled:
        return ()
    clock = datetime.now(timezone.utc)
    cards: list[LiveEdgeCatalogCard] = []
    for row in live_edge_storage.list_active_signals(
        settings, telegram_id=telegram_id, limit=limit * 3
    ):
        if row.decision == "WATCH" and _stale_watch(row, clock):
            continue
        payload = row.card_json
        text = (
            f"{row.question}\n"
            f"🔥 Live Edge\n"
            f"Рішення: {row.decision}\n"
            f"{payload}"
        )
        cards.append(
            LiveEdgeCatalogCard(
                signal_uid=row.signal_uid,
                question=row.question,
                text=format_stored_card(row),
                decision=row.decision,
            )
        )
        if len(cards) >= limit:
            break
    return tuple(cards)


def format_stored_card(row) -> str:
    from app.opportunity_intel.live_edge.formatting import format_decision_telegram

    return format_decision_telegram(row)


def _expected_uid_prefix(telegram_id: int) -> str:
    return f"user:{telegram_id}"


def merge_top_opportunities(
    existing: TopOpportunitiesResult,
    live_edge_texts: tuple[str, ...],
) -> TopOpportunitiesResult:
    if not live_edge_texts:
        return existing
    extra = "\n\n".join(live_edge_texts)
    if existing.status == "ready":
        message = f"{existing.message}\n\n{extra}" if extra else existing.message
        return replace(existing, message=message)
    if existing.status in {"empty", "stale", "discovering", "baselining", "not_initialized", "disabled"}:
        return TopOpportunitiesResult(
            status="ready",
            entries=existing.entries,
            message=f"🔥 Live Edge\n\n{extra}",
        )
    return existing


def _stale_watch(row, clock: datetime) -> bool:
    try:
        stamp = datetime.fromisoformat(str(row.evaluated_at))
    except (TypeError, ValueError):
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = (clock - stamp.astimezone(timezone.utc)).total_seconds()
    return age > WATCH_CATALOG_MAX_AGE_SECONDS
