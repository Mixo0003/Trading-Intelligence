"""Owner-admin delivery routing, claim, batch send, and bounded retry (V2.2.2).

Checkpoint 9: ownership, claim/retry state machine.
Checkpoint 10: final plaintext card formatting + validated links before claim/send.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.config.settings import Settings
from app.copy_intel.recommendation import is_recommendation_deliverable
from app.copy_intel.settings_profiles import get_user_settings
from app.database import copy_storage
from app.telegram.auth import is_admin
from app.telegram.copy_intel.formatting import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    format_deliverable_alert_card,
)

logger = logging.getLogger(__name__)

MAX_SEND_ATTEMPTS = 3
# Delay after failed attempt N before the next claim is due.
RETRY_DELAY_SECONDS_AFTER_ATTEMPT = {
    1: 60,  # ~1 minute
    2: 300,  # ~5 minutes
    3: 900,  # ~15 minutes (before final attempt exhaustion → dead_letter)
}
MAX_TELEGRAM_RETRY_AFTER_SECONDS = 3600

CODE_RECIPIENT_UNBOUND = "recipient_unbound"
CODE_RECIPIENT_NOT_ADMIN = "recipient_not_admin"
CODE_RECIPIENT_REVOKED = "recipient_revoked"
CODE_RECIPIENT_NO_LONGER_OWNER = "recipient_no_longer_owner"
CODE_WATCHLIST_DISABLED = "watchlist_disabled"
CODE_WATCHLIST_MISSING = "watchlist_missing"
CODE_SKIP_NOT_DELIVERABLE = "skip_not_deliverable"
CODE_NOT_DELIVERABLE = "not_deliverable"
CODE_PAPER_REQUIRED = "paper_commit_required"
CODE_TELEGRAM_RATE_LIMITED = "telegram_rate_limited"
CODE_TELEGRAM_TIMEOUT = "telegram_timeout"
CODE_TELEGRAM_SEND_FAILED = "telegram_send_failed"
CODE_RETRY_EXHAUSTED = "retry_exhausted"
CODE_OWNERSHIP_CHANGED = "ownership_changed"
CODE_FORMAT_FAILED = "alert_format_failed"
CODE_INVALID_MARKET_LINK = "invalid_market_link"
CODE_INVALID_TRADER_LINK = "invalid_trader_link"
CODE_SKIP_NOT_RENDERABLE = "skip_not_renderable"

STATUS_CLAIMED = "claimed"
STATUS_SENT = "sent"
STATUS_RETRY_WAIT = "retry_wait"
STATUS_DEAD_LETTER = "dead_letter"
STATUS_SUPPRESSED = "suppressed"
# Legacy alias still accepted when reclaiming.
STATUS_FAILED_LEGACY = "failed"


@dataclass(frozen=True)
class RecipientResolution:
    ok: bool
    recipient_id: int | None
    code: str | None
    message: str


@dataclass(frozen=True)
class DeliveryBatch:
    text: str
    items: tuple[tuple[str, str], ...]  # (wallet, event_uid)


def utc_now(*, now: float | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(float(now), tz=timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def resolve_owner_recipient(settings: Settings, wallet: str) -> RecipientResolution:
    """Resolve the sole owner-admin recipient for a watchlist wallet.

    Never fans out to all ADMIN_IDS. Never silently reassigns unbound rows.
    """
    entry = copy_storage.get_watchlist_entry(settings, wallet)
    if entry is None:
        return RecipientResolution(
            ok=False,
            recipient_id=None,
            code=CODE_WATCHLIST_MISSING,
            message="watchlist row missing",
        )
    if not entry.enabled:
        return RecipientResolution(
            ok=False,
            recipient_id=None,
            code=CODE_WATCHLIST_DISABLED,
            message="watchlist row disabled",
        )
    if entry.added_by is None:
        return RecipientResolution(
            ok=False,
            recipient_id=None,
            code=CODE_RECIPIENT_UNBOUND,
            message="watchlist owner unbound",
        )
    owner_id = int(entry.added_by)
    if not is_admin(settings, owner_id):
        return RecipientResolution(
            ok=False,
            recipient_id=owner_id,
            code=CODE_RECIPIENT_NOT_ADMIN,
            message="owner is not an admin",
        )
    return RecipientResolution(
        ok=True, recipient_id=owner_id, code=None, message="ok"
    )


def recheck_recipient_authorization(
    settings: Settings,
    *,
    wallet: str,
    subscriber_id: int,
) -> RecipientResolution:
    """Recheck ownership + admin before every send or retry."""
    entry = copy_storage.get_watchlist_entry(settings, wallet)
    if entry is None:
        return RecipientResolution(
            False, None, CODE_WATCHLIST_MISSING, "watchlist row missing"
        )
    if not entry.enabled:
        return RecipientResolution(
            False, None, CODE_WATCHLIST_DISABLED, "watchlist row disabled"
        )
    if entry.added_by is None:
        return RecipientResolution(
            False, None, CODE_RECIPIENT_UNBOUND, "watchlist owner unbound"
        )
    if int(entry.added_by) != int(subscriber_id):
        return RecipientResolution(
            False,
            int(entry.added_by),
            CODE_RECIPIENT_NO_LONGER_OWNER,
            "subscriber no longer owns watchlist row",
        )
    if not is_admin(settings, subscriber_id):
        return RecipientResolution(
            False, int(subscriber_id), CODE_RECIPIENT_REVOKED, "subscriber not admin"
        )
    return RecipientResolution(True, int(subscriber_id), None, "ok")


def recommendation_may_claim(rec: Any) -> tuple[bool, str | None]:
    action = getattr(rec, "action", None) or "SKIP"
    if action == "SKIP":
        return False, CODE_SKIP_NOT_DELIVERABLE
    if action not in {"COPY", "WATCH"}:
        return False, CODE_NOT_DELIVERABLE
    return True, None


def owner_allows_recommendation(settings: Settings, owner_id: int, rec: Any) -> bool:
    user = get_user_settings(settings, owner_id)
    return is_recommendation_deliverable(
        user.alert_decisions, getattr(rec, "action", None) or "SKIP"
    )


def event_too_old_for_owner(
    settings: Settings,
    owner_id: int,
    event_ts: int | None,
    *,
    now: float | None = None,
) -> bool:
    if event_ts is None:
        return False
    user = get_user_settings(settings, owner_id)
    now_ts = int(time.time() if now is None else now)
    return (now_ts - int(event_ts)) > int(user.max_event_age_seconds)


def compute_next_attempt_at(
    attempt_count: int,
    *,
    now: float | None = None,
    retry_after_seconds: float | None = None,
) -> tuple[str | None, str | None]:
    """Return (next_attempt_at_iso, terminal_code).

    terminal_code is set when attempts are exhausted (dead_letter).
    Telegram RetryAfter, when present and within the safe bound, takes priority.
    """
    if attempt_count >= MAX_SEND_ATTEMPTS:
        return None, CODE_RETRY_EXHAUSTED
    policy = float(RETRY_DELAY_SECONDS_AFTER_ATTEMPT.get(attempt_count, 900))
    if retry_after_seconds is not None and float(retry_after_seconds) > 0:
        delay = max(
            1.0,
            min(float(retry_after_seconds), float(MAX_TELEGRAM_RETRY_AFTER_SECONDS)),
        )
    else:
        delay = policy
    nxt = utc_now(now=now) + timedelta(seconds=delay)
    return to_iso(nxt), None


def classify_telegram_error(exc: BaseException) -> tuple[str, float | None]:
    """Return (sanitized_error_code, retry_after_seconds_or_none). Never includes tokens/bodies."""
    name = type(exc).__name__
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is None and hasattr(exc, "retry_after"):
        retry_after = getattr(exc, "retry_after", None)
    # python-telegram-bot RetryAfter
    if name == "RetryAfter" or retry_after is not None:
        try:
            return CODE_TELEGRAM_RATE_LIMITED, float(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            return CODE_TELEGRAM_RATE_LIMITED, None
    if name in {"TimedOut", "NetworkError"} or "timeout" in str(name).lower():
        return CODE_TELEGRAM_TIMEOUT, None
    return CODE_TELEGRAM_SEND_FAILED, None


def cards_to_batches(
    cards_with_keys: list[tuple[str, str, str]],
) -> list[DeliveryBatch]:
    """One alert card per Telegram message (Checkpoint 10 clarity / claim correctness).

    Each batch maps exactly to one (wallet, event_uid) delivery row so partial-batch
    success/failure from Checkpoint 9 remains precise.
    """
    if not cards_with_keys:
        return []
    batches: list[DeliveryBatch] = []
    for wallet, event_uid, card in cards_with_keys:
        text = f"Copy Trading Alerts\n{card}".rstrip()
        if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            # Should not happen after format fail-closed; skip unsafe oversized payloads.
            continue
        batches.append(
            DeliveryBatch(text=text, items=((wallet, event_uid),))
        )
    return batches


def emit_delivery_diagnostic(
    settings: Settings,
    *,
    code: str,
    wallet: str,
    event_uid: str | None = None,
    subscriber_id: int | None = None,
) -> None:
    copy_storage.write_audit_log(
        settings,
        actor="copy_delivery",
        action="delivery_diagnostic",
        payload={
            "code": code,
            "wallet": wallet.lower()[:16],
            "event_uid": (event_uid or "")[:24] or None,
            "subscriber_id": subscriber_id,
        },
    )


def mark_send_failure(
    settings: Settings,
    *,
    subscriber_id: int,
    wallet: str,
    event_uid: str,
    attempt_count: int,
    error_code: str,
    now: float | None = None,
    retry_after_seconds: float | None = None,
) -> str:
    """Persist retry_wait or dead_letter after a failed send. Returns new status."""
    next_at, terminal = compute_next_attempt_at(
        attempt_count, now=now, retry_after_seconds=retry_after_seconds
    )
    if terminal is not None:
        copy_storage.mark_copy_alert_delivery(
            settings,
            subscriber_id=subscriber_id,
            wallet=wallet,
            event_uid=event_uid,
            status=STATUS_DEAD_LETTER,
            last_error_code=terminal,
            next_attempt_at=None,
        )
        return STATUS_DEAD_LETTER
    copy_storage.mark_copy_alert_delivery(
        settings,
        subscriber_id=subscriber_id,
        wallet=wallet,
        event_uid=event_uid,
        status=STATUS_RETRY_WAIT,
        last_error_code=error_code,
        next_attempt_at=next_at,
    )
    return STATUS_RETRY_WAIT


def suppress_delivery(
    settings: Settings,
    *,
    subscriber_id: int,
    wallet: str,
    event_uid: str,
    code: str,
) -> None:
    copy_storage.mark_copy_alert_delivery(
        settings,
        subscriber_id=subscriber_id,
        wallet=wallet,
        event_uid=event_uid,
        status=STATUS_SUPPRESSED,
        last_error_code=code,
        next_attempt_at=None,
    )


async def deliver_pending_alerts(
    context,
    settings: Settings,
    pending_recs: list[tuple[str, str, object, int | None]],
    *,
    now: float | None = None,
    send_message: Callable | None = None,
) -> None:
    """Owner-admin delivery with atomic claim and per-batch sent marking."""
    if not pending_recs or not settings.admin_ids:
        return
    send = send_message or context.bot.send_message

    # Group claimable cards by recipient.
    by_recipient: dict[int, list[tuple[str, str, str]]] = {}
    for event_uid, wallet, rec, event_ts in pending_recs:
        may, skip_code = recommendation_may_claim(rec)
        if not may:
            continue
        resolution = resolve_owner_recipient(settings, wallet)
        if not resolution.ok or resolution.recipient_id is None:
            emit_delivery_diagnostic(
                settings,
                code=resolution.code or CODE_RECIPIENT_UNBOUND,
                wallet=wallet,
                event_uid=event_uid,
            )
            continue
        recipient_id = resolution.recipient_id
        # Recheck immediately before claim.
        auth = recheck_recipient_authorization(
            settings, wallet=wallet, subscriber_id=recipient_id
        )
        if not auth.ok:
            emit_delivery_diagnostic(
                settings,
                code=auth.code or CODE_OWNERSHIP_CHANGED,
                wallet=wallet,
                event_uid=event_uid,
                subscriber_id=recipient_id,
            )
            continue
        if not owner_allows_recommendation(settings, recipient_id, rec):
            continue
        if event_too_old_for_owner(settings, recipient_id, event_ts, now=now):
            continue
        formatted = format_deliverable_alert_card(rec, now=now)  # type: ignore[arg-type]
        if not formatted.ok or not formatted.text:
            emit_delivery_diagnostic(
                settings,
                code=formatted.code or CODE_FORMAT_FAILED,
                wallet=wallet,
                event_uid=event_uid,
                subscriber_id=recipient_id,
            )
            continue
        card = formatted.text
        decision = getattr(rec, "action", None)
        if not copy_storage.claim_copy_alert_delivery(
            settings,
            subscriber_id=recipient_id,
            wallet=wallet,
            event_uid=event_uid,
            payload_text=card,
            decision=str(decision) if decision else None,
            now=now,
        ):
            continue
        by_recipient.setdefault(recipient_id, []).append((wallet, event_uid, card))

    for recipient_id, cards in by_recipient.items():
        await _send_claimed_batches(
            settings,
            recipient_id=recipient_id,
            cards_with_keys=cards,
            send=send,
            now=now,
        )


async def retry_due_alert_deliveries(
    context,
    settings: Settings,
    *,
    now: float | None = None,
    limit: int = 50,
    send_message: Callable | None = None,
) -> None:
    if not settings.admin_ids:
        return
    send = send_message or context.bot.send_message
    rows = copy_storage.list_due_copy_alert_retries(
        settings, now=now, limit=limit, max_attempts=MAX_SEND_ATTEMPTS
    )
    # Group by subscriber for batching of payloads that are still pending.
    by_recipient: dict[int, list[tuple[str, str, str]]] = {}
    for row in rows:
        subscriber_id = int(row["subscriber_id"])
        wallet = str(row["wallet"])
        event_uid = str(row["event_uid"])
        payload = row["payload_text"]
        if not payload:
            continue
        auth = recheck_recipient_authorization(
            settings, wallet=wallet, subscriber_id=subscriber_id
        )
        if not auth.ok:
            code = auth.code or CODE_OWNERSHIP_CHANGED
            if code in {CODE_RECIPIENT_NOT_ADMIN, CODE_RECIPIENT_REVOKED}:
                code = CODE_RECIPIENT_REVOKED
            elif code == CODE_RECIPIENT_NO_LONGER_OWNER:
                code = CODE_OWNERSHIP_CHANGED
            suppress_delivery(
                settings,
                subscriber_id=subscriber_id,
                wallet=wallet,
                event_uid=event_uid,
                code=code,
            )
            emit_delivery_diagnostic(
                settings,
                code=code,
                wallet=wallet,
                event_uid=event_uid,
                subscriber_id=subscriber_id,
            )
            continue
        if not copy_storage.claim_copy_alert_delivery(
            settings,
            subscriber_id=subscriber_id,
            wallet=wallet,
            event_uid=event_uid,
            payload_text=payload,
            now=now,
            allow_retry=True,
        ):
            continue
        by_recipient.setdefault(subscriber_id, []).append(
            (wallet, event_uid, str(payload))
        )

    for recipient_id, cards in by_recipient.items():
        await _send_claimed_batches(
            settings,
            recipient_id=recipient_id,
            cards_with_keys=cards,
            send=send,
            now=now,
        )


async def _send_claimed_batches(
    settings: Settings,
    *,
    recipient_id: int,
    cards_with_keys: list[tuple[str, str, str]],
    send: Callable,
    now: float | None,
) -> None:
    batches = cards_to_batches(cards_with_keys)
    send_failed = False
    fail_code = CODE_TELEGRAM_SEND_FAILED
    retry_after: float | None = None
    for batch in batches:
        if send_failed:
            for wallet, event_uid in batch.items:
                row = copy_storage.get_copy_alert_delivery(
                    settings,
                    subscriber_id=recipient_id,
                    wallet=wallet,
                    event_uid=event_uid,
                )
                attempts = int(row["attempt_count"] or 1) if row is not None else 1
                mark_send_failure(
                    settings,
                    subscriber_id=recipient_id,
                    wallet=wallet,
                    event_uid=event_uid,
                    attempt_count=attempts,
                    error_code=fail_code,
                    now=now,
                    retry_after_seconds=retry_after,
                )
            continue
        for wallet, _uid in batch.items:
            auth = recheck_recipient_authorization(
                settings, wallet=wallet, subscriber_id=recipient_id
            )
            if not auth.ok:
                for w, uid in batch.items:
                    suppress_delivery(
                        settings,
                        subscriber_id=recipient_id,
                        wallet=w,
                        event_uid=uid,
                        code=auth.code or CODE_OWNERSHIP_CHANGED,
                    )
                return
        try:
            await send(chat_id=recipient_id, text=batch.text)
        except Exception as exc:
            fail_code, retry_after = classify_telegram_error(exc)
            logger.warning(
                "Copy alert send failed recipient=%s code=%s",
                recipient_id,
                fail_code,
            )
            for wallet, event_uid in batch.items:
                row = copy_storage.get_copy_alert_delivery(
                    settings,
                    subscriber_id=recipient_id,
                    wallet=wallet,
                    event_uid=event_uid,
                )
                attempts = int(row["attempt_count"] or 1) if row is not None else 1
                mark_send_failure(
                    settings,
                    subscriber_id=recipient_id,
                    wallet=wallet,
                    event_uid=event_uid,
                    attempt_count=attempts,
                    error_code=fail_code,
                    now=now,
                    retry_after_seconds=retry_after,
                )
            send_failed = True
            continue
        for wallet, event_uid in batch.items:
            copy_storage.mark_copy_alert_delivery(
                settings,
                subscriber_id=recipient_id,
                wallet=wallet,
                event_uid=event_uid,
                status=STATUS_SENT,
                now=now,
            )
