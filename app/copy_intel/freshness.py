"""Copy-event freshness: baseline, watermarks, and alertable-event gating."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from app.config.settings import Settings
from app.copy_intel.api_result import unwrap_data_list
from app.copy_intel.data_client import CopyDataClient, is_trade_activity
from app.copy_intel.event_identity import build_event_uid, normalize_size
from app.copy_intel.ingestion import _snapshots_to_v2_payload
from app.copy_intel.models import PositionSnapshot, TradeEvent, TradeEventType
from app.copy_intel.position_book import (
    PositionBookFailure,
    fetch_validated_position_book,
)
from app.database import copy_storage

logger = logging.getLogger(__name__)

# Explicit clock / API-index lag tolerance (seconds).
CLOCK_LAG_TOLERANCE_SECONDS = 120

# Bump when baseline seeding / UID format changes. Legacy COMPLETE rows without
# this version are treated as outdated and rebuilt before any live alerts.
COPY_BASELINE_VERSION = 3

_SIZE_EPS = 1e-6

# Re-export for existing imports/tests.
__all__ = [
    "CLOCK_LAG_TOLERANCE_SECONDS",
    "COPY_BASELINE_VERSION",
    "annotate_events",
    "baseline_is_complete",
    "begin_subscription",
    "build_event_uid",
    "ensure_baseline_current",
    "ensure_monitor_state",
    "filter_alertable_events",
    "invalidate_baseline_for_rebuild",
    "rebuild_all_copy_baselines",
    "run_baseline",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_ts(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def begin_subscription(settings: Settings, wallet: str) -> None:
    """Start a new subscription cycle (add or re-enable). No alerts until baseline completes."""
    now = _now_iso()
    copy_storage.upsert_wallet_monitor_state(
        settings,
        wallet,
        subscription_started_at=now,
        baseline_started_at=None,
        baseline_status="PENDING",
        baseline_error=None,
        newest_event_ts=None,
        newest_event_uid=None,
        monitoring_enabled=True,
        baseline_version=None,
    )
    copy_storage.upsert_wallet_event_watermark(
        settings,
        wallet,
        newest_event_ts=None,
        newest_event_uid=None,
    )
    logger.info("Copy monitor subscription started for %s (baseline PENDING)", wallet)


def ensure_monitor_state(settings: Settings, wallet: str) -> copy_storage.WalletMonitorState:
    state = copy_storage.get_wallet_monitor_state(settings, wallet)
    if state is None:
        begin_subscription(settings, wallet)
        state = copy_storage.get_wallet_monitor_state(settings, wallet)
        assert state is not None
    return state


def _baseline_version_ok(state: copy_storage.WalletMonitorState) -> bool:
    return (
        state.baseline_status == "COMPLETE"
        and state.baseline_version is not None
        and int(state.baseline_version) >= COPY_BASELINE_VERSION
    )


def baseline_is_complete(settings: Settings, wallet: str) -> bool:
    state = copy_storage.get_wallet_monitor_state(settings, wallet)
    return state is not None and _baseline_version_ok(state)


def ensure_baseline_current(
    settings: Settings,
    wallet: str,
) -> copy_storage.WalletMonitorState:
    """
    Mark outdated or incomplete baselines PENDING so the next run rebuilds them.
    Legacy COMPLETE rows (missing/old baseline_version) never enter live delivery.
    """
    state = ensure_monitor_state(settings, wallet)
    if state.baseline_status in {"PENDING", "FAILED", "RUNNING", "MIGRATING"}:
        return state
    if _baseline_version_ok(state):
        return state

    copy_storage.update_wallet_monitor_fields(
        settings,
        wallet,
        baseline_status="MIGRATING",
        baseline_version=None,
        baseline_error="baseline_version_upgrade",
        baseline_started_at=None,
        newest_event_ts=None,
        newest_event_uid=None,
    )
    copy_storage.upsert_wallet_event_watermark(
        settings,
        wallet,
        newest_event_ts=None,
        newest_event_uid=None,
    )
    logger.info(
        "Copy baseline marked MIGRATING for %s (legacy/incomplete version)",
        wallet[:10],
    )
    state = copy_storage.get_wallet_monitor_state(settings, wallet)
    assert state is not None
    return state


def run_baseline(settings: Settings, wallet: str, client: CopyDataClient | None = None) -> str:
    """
    Initialize a full baseline for a wallet. Sends zero alerts.

    Fetches positions, trades, activity, and closed positions; persists snapshots;
    seeds processed UIDs that match live event identity; then marks COMPLETE.
    Partial failure leaves PENDING/FAILED and never enables alerts.
    """
    client = client or CopyDataClient(settings)
    state = ensure_baseline_current(settings, wallet)
    if _baseline_version_ok(state):
        return "COMPLETE"

    now_iso = _now_iso()
    observed_at = int(time.time())
    copy_storage.update_wallet_monitor_fields(
        settings,
        wallet,
        baseline_started_at=state.baseline_started_at or now_iso,
        baseline_status="RUNNING",
        baseline_error=None,
        baseline_version=None,
    )
    run_id = copy_storage.start_baseline_run(settings, wallet)
    try:
        # Complete validated asset book first — fail closed before any COMPLETE write.
        book = fetch_validated_position_book(client, wallet)
        if isinstance(book, PositionBookFailure):
            raise RuntimeError(f"baseline positions book failed: {book.code}")

        # Historical trade/activity/closed seeds still use compatibility unwrap.
        raw_trades = _as_list(client.fetch_trades(wallet, limit=200))
        raw_activity = _as_list(client.fetch_activity(wallet, limit=200))
        raw_closed = _as_list(client.fetch_closed_positions(wallet, limit=200))

        current = book.positions

        # V3 baseline writes asset-keyed v2 snapshots only; legacy table untouched.
        copy_storage.commit_complete_position_book_v2(
            settings,
            wallet,
            _snapshots_to_v2_payload(current),
            stamp_complete=True,
        )

        state = copy_storage.get_wallet_monitor_state(settings, wallet)
        assert state is not None
        sub_start = _parse_iso_ts(state.subscription_started_at)
        # Floor allows small API/index lag; seeded UIDs suppress true historical IDs.
        newest_ts = sub_start - CLOCK_LAG_TOLERANCE_SECONDS
        newest_uid: str | None = None

        # Seed live-compatible UIDs for every current position (0 → size open).
        for snap in sorted(
            current.values(), key=lambda s: (s.asset or "", s.condition_id)
        ):
            uid = _baseline_open_uid(wallet, snap)
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=uid,
                wallet=wallet,
                event_ts=sub_start,
                observed_at=observed_at,
                is_baseline=True,
                alertable=False,
                reject_reason="baseline",
            )
            newest_uid = uid

        # Seed native trade / activity IDs so historical txs cannot alert.
        for raw in raw_trades:
            uid, event_ts = _seed_native_trade_uid(
                settings, wallet, raw, observed_at=observed_at
            )
            if uid and event_ts is not None and event_ts > newest_ts:
                newest_ts = event_ts
                newest_uid = uid

        for raw in raw_activity:
            if not is_trade_activity(raw.get("type")):
                continue
            uid, event_ts = _seed_native_trade_uid(
                settings, wallet, raw, observed_at=observed_at
            )
            if uid and event_ts is not None and event_ts > newest_ts:
                newest_ts = event_ts
                newest_uid = uid

        # Seed closed-position close transitions.
        for raw in raw_closed:
            uid = _baseline_closed_uid(wallet, raw)
            if not uid:
                continue
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=uid,
                wallet=wallet,
                event_ts=sub_start,
                observed_at=observed_at,
                is_baseline=True,
                alertable=False,
                reject_reason="baseline",
            )
            newest_uid = newest_uid or uid

        # Watermark at least at subscription start (not poll observed time).
        copy_storage.upsert_wallet_event_watermark(
            settings,
            wallet,
            newest_event_ts=newest_ts,
            newest_event_uid=newest_uid,
        )
        # COMPLETE is last — alerts stay disabled until this commit succeeds.
        copy_storage.update_wallet_monitor_fields(
            settings,
            wallet,
            baseline_status="COMPLETE",
            baseline_error=None,
            baseline_version=COPY_BASELINE_VERSION,
            newest_event_ts=newest_ts,
            newest_event_uid=newest_uid,
        )
        copy_storage.finish_baseline_run(
            settings,
            run_id,
            status="COMPLETE",
            positions_count=len(current),
        )
        logger.info(
            "Baseline complete for %s (%s positions, trades/activity seeded); alerts enabled",
            wallet,
            len(current),
        )
        return "COMPLETE"
    except Exception as exc:
        err = str(exc)[:500]
        copy_storage.update_wallet_monitor_fields(
            settings,
            wallet,
            baseline_status="FAILED",
            baseline_error=err,
            baseline_version=None,
        )
        copy_storage.finish_baseline_run(
            settings,
            run_id,
            status="FAILED",
            error=err,
        )
        logger.exception("Baseline failed for %s", wallet)
        return "FAILED"


def _baseline_open_uid(wallet: str, snap: PositionSnapshot) -> str:
    """UID matching a live open transition 0 → size for this position."""
    size = normalize_size(snap.size)
    event = TradeEvent(
        wallet=wallet.lower(),
        event_type="open",
        condition_id=snap.condition_id,
        asset=snap.asset,
        outcome=snap.outcome,
        side="BUY",
        price=snap.avg_price,
        size=size,
        tx_hash=None,
        ts=None,
        title=snap.title,
        slug=snap.slug,
        dedup_key="baseline",
        prev_size=0.0,
        new_size=size,
    )
    return build_event_uid(event)


def _baseline_closed_uid(wallet: str, raw: dict[str, Any]) -> str | None:
    condition_id = raw.get("conditionId") or ""
    if not condition_id:
        return None
    size = normalize_size(float(raw.get("size") or raw.get("totalBought") or 0))
    if size <= _SIZE_EPS:
        size = 1.0
    event = TradeEvent(
        wallet=wallet.lower(),
        event_type="close",
        condition_id=condition_id,
        asset=raw.get("asset"),
        outcome=raw.get("outcome"),
        side="SELL",
        price=_float_or_none(raw.get("avgPrice") or raw.get("curPrice")),
        size=size,
        tx_hash=None,
        ts=None,
        title=raw.get("title"),
        slug=raw.get("slug"),
        dedup_key="baseline-close",
        prev_size=size,
        new_size=0.0,
    )
    return build_event_uid(event)


def _seed_native_trade_uid(
    settings: Settings,
    wallet: str,
    raw: dict[str, Any],
    *,
    observed_at: int,
) -> tuple[str | None, int | None]:
    # Documented native identity only — ignore undocumented id / tradeId.
    tx_hash = raw.get("transactionHash") or raw.get("transaction_hash") or raw.get("txHash")
    if not tx_hash:
        return None, None
    side = (raw.get("side") or "").upper()
    condition = (raw.get("conditionId") or "").lower()
    event_ts = int(raw["timestamp"]) if raw.get("timestamp") is not None else None
    if side == "BUY":
        actions: tuple[TradeEventType, ...] = ("open", "increase")
    elif side == "SELL":
        actions = ("reduce", "close")
    else:
        actions = ("open", "increase", "reduce", "close")

    last_uid: str | None = None
    for action in actions:
        stub = TradeEvent(
            wallet=wallet.lower(),
            event_type=action,
            condition_id=condition,
            asset=raw.get("asset"),
            outcome=raw.get("outcome"),
            side=side or None,
            price=_float_or_none(raw.get("price")),
            size=normalize_size(float(raw.get("size") or 0)),
            tx_hash=str(tx_hash).lower(),
            ts=event_ts,
            title=raw.get("title"),
            slug=raw.get("slug"),
            dedup_key="baseline-native",
            trade_id=None,
        )
        uid = build_event_uid(stub)
        copy_storage.mark_processed_copy_event(
            settings,
            event_uid=uid,
            wallet=wallet,
            event_ts=event_ts,
            observed_at=observed_at,
            is_baseline=True,
            alertable=False,
            reject_reason="baseline",
        )
        last_uid = uid
    return last_uid, event_ts


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list:
    # Accept ApiResult (Checkpoint 2) or plain lists (mocks / legacy).
    return unwrap_data_list(value)


def annotate_events(
    events: list[TradeEvent],
    *,
    observed_at: int | None = None,
) -> list[TradeEvent]:
    observed = observed_at if observed_at is not None else int(time.time())
    annotated: list[TradeEvent] = []
    for event in events:
        uid = event.event_uid or build_event_uid(event)
        annotated.append(
            replace(
                event,
                observed_at=event.observed_at if event.observed_at is not None else observed,
                event_uid=uid,
            )
        )
    return annotated


def filter_alertable_events(
    settings: Settings,
    wallet: str,
    events: list[TradeEvent],
    *,
    now: int | None = None,
    market_resolved: dict[str, bool] | None = None,
    max_age_seconds: int | None = None,
) -> list[TradeEvent]:
    """
    Return only genuinely new, actionable events after a COMPLETE baseline.
    Never treats observed_at / poll time as proof of newness.

    max_age_seconds: optional override (V2.2.1 per-user ceiling). When omitted,
    uses global settings.copy_max_event_age_seconds.
    """
    if not baseline_is_complete(settings, wallet):
        return []

    state = copy_storage.get_wallet_monitor_state(settings, wallet)
    if state is None:
        return []

    now_ts = now if now is not None else int(time.time())
    sub_start = _parse_iso_ts(state.subscription_started_at)
    watermark_ts = state.newest_event_ts
    lag = CLOCK_LAG_TOLERANCE_SECONDS
    max_age = (
        int(max_age_seconds)
        if max_age_seconds is not None
        else settings.copy_max_event_age_seconds
    )
    resolved_map = market_resolved or {}
    source_cutoff = sub_start - lag

    alertable: list[TradeEvent] = []
    newest_ts = watermark_ts
    newest_uid = state.newest_event_uid

    for event in annotate_events(events, observed_at=now_ts):
        uid = event.event_uid or build_event_uid(event)
        observed_at = event.observed_at if event.observed_at is not None else now_ts
        event_ts = int(event.ts) if event.ts is not None else None

        if copy_storage.is_processed_copy_event(settings, uid):
            continue

        if event_ts is not None and event_ts < source_cutoff:
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=uid,
                wallet=wallet,
                event_ts=event_ts,
                observed_at=observed_at,
                alertable=False,
                reject_reason="before_subscription",
            )
            continue

        if (
            watermark_ts is not None
            and event_ts is not None
            and event_ts < watermark_ts
        ):
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=uid,
                wallet=wallet,
                event_ts=event_ts,
                observed_at=observed_at,
                alertable=False,
                reject_reason="before_watermark",
            )
            continue

        if event_ts is not None:
            age = now_ts - event_ts
            if age > max_age:
                copy_storage.mark_processed_copy_event(
                    settings,
                    event_uid=uid,
                    wallet=wallet,
                    event_ts=event_ts,
                    observed_at=observed_at,
                    alertable=False,
                    reject_reason="stale_age",
                )
                continue

        # Position-diff size churn has no source ts and no native trade id.
        # Without authoritative trade correlation, unsigned
        # open/increase/reduce/close must not become deliverable alerts.
        # Checkpoint 5 attaches documented fill timestamps / tx hashes.
        has_native_id = bool(event.tx_hash)
        if (
            event_ts is None
            and not has_native_id
            and event.event_type in {"open", "increase", "reduce", "close"}
        ):
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=uid,
                wallet=wallet,
                event_ts=observed_at,
                observed_at=observed_at,
                alertable=False,
                reject_reason="unsigned_size_churn",
            )
            continue

        condition_id = event.condition_id or ""
        if condition_id and resolved_map.get(condition_id):
            copy_storage.mark_processed_copy_event(
                settings,
                event_uid=uid,
                wallet=wallet,
                event_ts=event_ts,
                observed_at=observed_at,
                alertable=False,
                reject_reason="market_resolved",
            )
            continue

        inserted = copy_storage.mark_processed_copy_event(
            settings,
            event_uid=uid,
            wallet=wallet,
            event_ts=event_ts if event_ts is not None else observed_at,
            observed_at=observed_at,
            alertable=True,
            reject_reason=None,
        )
        if not inserted:
            continue

        # Only advance watermark with real source timestamps (never poll clock).
        if event_ts is not None:
            if newest_ts is None or event_ts >= newest_ts:
                newest_ts = event_ts
                newest_uid = uid
        alertable.append(event)

    if newest_ts != watermark_ts or newest_uid != state.newest_event_uid:
        copy_storage.upsert_wallet_event_watermark(
            settings,
            wallet,
            newest_event_ts=newest_ts,
            newest_event_uid=newest_uid,
        )
        copy_storage.update_wallet_monitor_fields(
            settings,
            wallet,
            newest_event_ts=newest_ts,
            newest_event_uid=newest_uid,
        )

    return alertable


def invalidate_baseline_for_rebuild(
    settings: Settings,
    wallet: str,
    *,
    reason: str = "baseline_rebuild",
) -> None:
    """Force a subscription into MIGRATING without removing watchlist/analytics data."""
    ensure_monitor_state(settings, wallet)
    copy_storage.update_wallet_monitor_fields(
        settings,
        wallet,
        baseline_status="MIGRATING",
        baseline_version=None,
        baseline_error=reason[:200],
        baseline_started_at=None,
        newest_event_ts=None,
        newest_event_uid=None,
    )
    copy_storage.upsert_wallet_event_watermark(
        settings,
        wallet,
        newest_event_ts=None,
        newest_event_uid=None,
    )


def rebuild_all_copy_baselines(
    settings: Settings,
    client: CopyDataClient | None = None,
) -> dict[str, int]:
    """
    Admin rebuild for all enabled watchlist subscriptions.
    Sends zero alerts; preserves watchlist, paper, analytics, and processed history.
    """
    client = client or CopyDataClient(settings)
    wallets = copy_storage.list_enabled_wallets(settings)
    succeeded = 0
    failed = 0
    for wallet in wallets:
        invalidate_baseline_for_rebuild(settings, wallet, reason="admin_rebuild")
        status = run_baseline(settings, wallet, client=client)
        if status == "COMPLETE":
            succeeded += 1
        else:
            failed += 1
    return {
        "total": len(wallets),
        "succeeded": succeeded,
        "failed": failed,
    }
