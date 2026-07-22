"""Short-lived confirmation tokens for Copy Settings edit/reset (V2.2.1)."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from app.config.constants import COPY_SETTINGS_CONFIRM_TTL_SECONDS
from app.copy_intel.settings_profiles import (
    apply_validated_payload,
    defaults_for_profile,
    get_user_settings,
    settings_fingerprint,
    settings_to_payload,
)

ConfirmKind = Literal["edit", "reset"]

_CONFIRMATIONS: dict[str, "SettingsConfirmation"] = {}


@dataclass(frozen=True)
class SettingsConfirmation:
    token: str
    operator_id: str
    kind: ConfirmKind
    settings_fingerprint: str
    proposed_payload: dict[str, Any]
    changed_keys: tuple[str, ...]
    expires_at: float
    preview_lines: tuple[str, ...]


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def clear_confirmations_for_tests() -> None:
    _CONFIRMATIONS.clear()


def drop_operator_confirmations(operator_id: str | int) -> None:
    oid = str(operator_id)
    stale = [tok for tok, conf in _CONFIRMATIONS.items() if conf.operator_id == oid]
    for tok in stale:
        _CONFIRMATIONS.pop(tok, None)


def create_edit_confirmation(
    settings,
    *,
    operator_id: int,
    proposed_payload: dict[str, Any],
    fingerprint: str,
    changed_keys: list[str],
    preview_lines: list[str],
) -> SettingsConfirmation:
    drop_operator_confirmations(operator_id)
    token = uuid.uuid4().hex
    confirmation = SettingsConfirmation(
        token=token,
        operator_id=str(operator_id),
        kind="edit",
        settings_fingerprint=fingerprint,
        proposed_payload=dict(proposed_payload),
        changed_keys=tuple(changed_keys),
        expires_at=time.time() + COPY_SETTINGS_CONFIRM_TTL_SECONDS,
        preview_lines=tuple(preview_lines),
    )
    _CONFIRMATIONS[token] = confirmation
    return confirmation


def create_reset_confirmation(
    settings,
    *,
    operator_id: int,
) -> tuple[SettingsConfirmation, list[str]]:
    user = get_user_settings(settings, operator_id)
    old_payload = settings_to_payload(user)
    balanced = defaults_for_profile(
        "Balanced",
        global_max_event_age_seconds=int(settings.copy_max_event_age_seconds),
    )
    proposed = {"profile": "Balanced", **balanced}
    lines = [
        "Reset Copy Settings to Balanced?",
        f"Current profile: {user.profile}",
        "New profile: Balanced",
        f"Min resolved markets: {old_payload['min_resolved_markets']} → {proposed['min_resolved_markets']}",
        f"Min active days: {old_payload['min_active_days']} → {proposed['min_active_days']}",
        f"Max drawdown: {old_payload['max_drawdown_pct']:.0%} → {proposed['max_drawdown_pct']:.0%}",
        f"Poll interval: {old_payload['poll_interval_seconds']}s → {proposed['poll_interval_seconds']}s",
        f"Max event age: {old_payload['max_event_age_seconds']}s → {proposed['max_event_age_seconds']}s",
        f"Alert mode: {old_payload['alert_decisions']} → {proposed['alert_decisions']}",
        "This replaces all Copy Settings for you. Confirm to save.",
    ]
    drop_operator_confirmations(operator_id)
    token = uuid.uuid4().hex
    confirmation = SettingsConfirmation(
        token=token,
        operator_id=str(operator_id),
        kind="reset",
        settings_fingerprint=settings_fingerprint(old_payload),
        proposed_payload=proposed,
        changed_keys=tuple(k for k in proposed if k != "profile"),
        expires_at=time.time() + COPY_SETTINGS_CONFIRM_TTL_SECONDS,
        preview_lines=tuple(lines),
    )
    _CONFIRMATIONS[token] = confirmation
    return confirmation, lines


def execute_confirmed_settings(
    settings,
    *,
    token: str,
    operator_id: int,
) -> tuple[str, object | None]:
    """
    Returns (status, user_or_none).
    status: ok | invalid_confirmation | confirmation_expired | operator_mismatch | stale_preview | invalid_settings
    """
    confirmation = _CONFIRMATIONS.get(token)
    if confirmation is None:
        return "invalid_confirmation", None

    if time.time() > confirmation.expires_at:
        _CONFIRMATIONS.pop(token, None)
        return "confirmation_expired", None

    if confirmation.operator_id != str(operator_id):
        return "operator_mismatch", None

    try:
        user = apply_validated_payload(
            settings,
            operator_id,
            confirmation.proposed_payload,
            expected_fingerprint=confirmation.settings_fingerprint,
            changed_keys=list(confirmation.changed_keys),
            action=(
                "copy_settings_reset"
                if confirmation.kind == "reset"
                else "copy_settings_save"
            ),
        )
    except ValueError as exc:
        _CONFIRMATIONS.pop(token, None)
        msg = str(exc)
        if msg == "stale_preview":
            return "stale_preview", None
        return "invalid_settings", None

    _CONFIRMATIONS.pop(token, None)
    return "ok", user


def peek_confirmation(token: str) -> SettingsConfirmation | None:
    return _CONFIRMATIONS.get(token)


def token_fingerprint(token: str) -> str:
    return _token_fingerprint(token)
