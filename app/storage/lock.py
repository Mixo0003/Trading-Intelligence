"""Maintenance lock for storage file cleanup (separate from copy/weather jobs)."""

from __future__ import annotations

import uuid

from app.config.constants import STORAGE_CLEANUP_LOCK_JOB
from app.config.settings import Settings
from app.database import copy_storage


def acquire_cleanup_lock(settings: Settings, *, ttl_seconds: int | None = None) -> str | None:
    """
    Acquire exclusive storage cleanup lock.
    Returns owner token on success, None if another cleanup holds a fresh lock.
    Stale locks (past expires_at) are stolen by try_acquire_job_lock.
    """
    owner = f"storage-cleanup-{uuid.uuid4().hex[:12]}"
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.storage_cleanup_lock_ttl_seconds)
    ok = copy_storage.try_acquire_job_lock(
        settings,
        job_name=STORAGE_CLEANUP_LOCK_JOB,
        owner=owner,
        ttl_seconds=max(1, ttl),
    )
    return owner if ok else None


def release_cleanup_lock(settings: Settings, owner: str) -> None:
    copy_storage.release_job_lock(
        settings,
        job_name=STORAGE_CLEANUP_LOCK_JOB,
        owner=owner,
    )
