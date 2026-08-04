"""Pure bounded classifier for future Telegram send outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from app.opportunity_intel.models import OpportunityDiagnosticCode

MAX_TELEGRAM_RETRY_AFTER_SECONDS = 3600

_RETRYABLE_NAMES = frozenset(
    {
        "TimedOut",
        "NetworkError",
        "RetryAfter",
        "TelegramError",
    }
)
_PERMANENT_NAMES = frozenset(
    {
        "Forbidden",
        "BadRequest",
        "ChatMigrated",
        "InvalidToken",
        "Unauthorized",
    }
)


@dataclass(frozen=True)
class TelegramSendClassification:
    code: OpportunityDiagnosticCode
    retryable: bool
    retry_after_seconds: float | None = None


def classify_telegram_send_outcome(
    exc: BaseException,
) -> TelegramSendClassification:
    """Map a Telegram/network exception to a sanitized bounded category.

    Never returns or stores raw exception messages.
    """
    name = type(exc).__name__
    message = str(exc).lower()
    retry_after = _retry_after_seconds(exc)

    if name == "RetryAfter" or retry_after is not None:
        return TelegramSendClassification(
            code=OpportunityDiagnosticCode.TELEGRAM_RATE_LIMITED,
            retryable=True,
            retry_after_seconds=retry_after,
        )
    if name in {"TimedOut"} or "timeout" in name.lower() or "timed out" in message:
        return TelegramSendClassification(
            code=OpportunityDiagnosticCode.TELEGRAM_TIMEOUT,
            retryable=True,
        )
    if name in {"NetworkError"} or "network" in name.lower():
        return TelegramSendClassification(
            code=OpportunityDiagnosticCode.TELEGRAM_NETWORK_ERROR,
            retryable=True,
        )
    if "429" in message or "too many requests" in message:
        return TelegramSendClassification(
            code=OpportunityDiagnosticCode.TELEGRAM_RATE_LIMITED,
            retryable=True,
            retry_after_seconds=retry_after,
        )
    if any(token in message for token in ("500", "502", "503", "504", "internal server")):
        return TelegramSendClassification(
            code=OpportunityDiagnosticCode.TELEGRAM_SERVER_ERROR,
            retryable=True,
        )
    if name in {"Forbidden"} or "forbidden" in message or "bot was blocked" in message:
        if "blocked" in message:
            return TelegramSendClassification(
                code=OpportunityDiagnosticCode.TELEGRAM_BLOCKED,
                retryable=False,
            )
        return TelegramSendClassification(
            code=OpportunityDiagnosticCode.TELEGRAM_FORBIDDEN,
            retryable=False,
        )
    if (
        name in {"BadRequest", "ChatMigrated"}
        or "chat not found" in message
        or "chat_id is empty" in message
        or "invalid chat" in message
    ):
        return TelegramSendClassification(
            code=OpportunityDiagnosticCode.TELEGRAM_CHAT_INVALID,
            retryable=False,
        )
    if "malformed" in message or "invalid user" in message:
        return TelegramSendClassification(
            code=OpportunityDiagnosticCode.TELEGRAM_RECIPIENT_MALFORMED,
            retryable=False,
        )
    if (
        "payload" in message
        or "render" in message
        or "immutable" in message
        or "contract" in message
    ):
        return TelegramSendClassification(
            code=OpportunityDiagnosticCode.PAYLOAD_CONTRACT_VIOLATION,
            retryable=False,
        )
    if name in _PERMANENT_NAMES:
        return TelegramSendClassification(
            code=OpportunityDiagnosticCode.TELEGRAM_FORBIDDEN,
            retryable=False,
        )
    if name in _RETRYABLE_NAMES:
        return TelegramSendClassification(
            code=OpportunityDiagnosticCode.TELEGRAM_NETWORK_ERROR,
            retryable=True,
        )
    return TelegramSendClassification(
        code=OpportunityDiagnosticCode.DELIVERY_FAILURE,
        retryable=False,
    )


def _retry_after_seconds(exc: BaseException) -> float | None:
    raw = getattr(exc, "retry_after", None)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not (value > 0):
        return None
    return min(value, float(MAX_TELEGRAM_RETRY_AFTER_SECONDS))
