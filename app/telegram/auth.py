"""Telegram authorization helpers (ADMIN_IDS defense in depth)."""

from __future__ import annotations

import logging

from telegram import Update

from app.config.settings import Settings
from app.health.sanitize import hash_user_id
from app.health.storage import log_unauthorized_access

logger = logging.getLogger(__name__)

ACCESS_DENIED = "Access denied."


def is_admin(settings: Settings, user_id: int | None) -> bool:
    if user_id is None:
        return False
    return user_id in settings.admin_ids


def record_unauthorized(settings: Settings, user_id: int | None, action: str) -> None:
    uid_hash = hash_user_id(user_id or 0)
    try:
        log_unauthorized_access(settings, uid_hash, action)
    except Exception:
        logger.debug("Could not persist unauthorized access log", exc_info=True)
    logger.warning("Unauthorized access action=%s user_hash=%s", action, uid_hash)


async def require_admin(
    update: Update,
    settings: Settings,
    *,
    action: str,
) -> bool:
    """
    Return True if caller is admin. Otherwise reply Access denied and log.
    Never trusts hidden buttons alone.
    """
    user = update.effective_user
    user_id = user.id if user else None
    if is_admin(settings, user_id):
        return True
    record_unauthorized(settings, user_id, action)
    message = update.effective_message
    if message is not None:
        await message.reply_text(ACCESS_DENIED)
    return False
