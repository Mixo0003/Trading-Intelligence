"""Maintenance locks for storage cleanup, backup, and retention prune."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.config.constants import (
    STORAGE_BACKUP_LOCK_JOB,
    STORAGE_CLEANUP_LOCK_JOB,
    STORAGE_RETENTION_PRUNE_LOCK_JOB,
)
from app.config.settings import Settings
from app.database import copy_storage
from app.database.storage import get_connection

_EXCLUSIVE_JOBS = (
    STORAGE_BACKUP_LOCK_JOB,
    STORAGE_CLEANUP_LOCK_JOB,
    STORAGE_RETENTION_PRUNE_LOCK_JOB,
)


def _lock_is_fresh(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    now = datetime.now(timezone.utc)
    try:
        exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return exp > now


def fresh_exclusive_lock_job(
    settings: Settings,
    *,
    exclude_job: str | None = None,
) -> str | None:
    """
    Return the job name of a fresh exclusive maintenance lock, if any.
    Used to refuse conflicting backup/cleanup/prune operations safely.
    """
    with get_connection(settings) as connection:
        for job_name in _EXCLUSIVE_JOBS:
            if exclude_job and job_name == exclude_job:
                continue
            row = connection.execute(
                "SELECT expires_at FROM copy_job_locks WHERE job_name = ?",
                (job_name,),
            ).fetchone()
            if row is not None and _lock_is_fresh(row["expires_at"]):
                return job_name
    return None


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


def acquire_backup_lock(settings: Settings, *, ttl_seconds: int | None = None) -> str | None:
    """
    Acquire exclusive online-backup lock.
    Returns owner token on success, None if another backup holds a fresh lock.
    Stale locks (past expires_at) are stolen by try_acquire_job_lock.
    """
    owner = f"storage-backup-{uuid.uuid4().hex[:12]}"
    ttl = int(
        ttl_seconds if ttl_seconds is not None else settings.storage_backup_lock_ttl_seconds
    )
    ok = copy_storage.try_acquire_job_lock(
        settings,
        job_name=STORAGE_BACKUP_LOCK_JOB,
        owner=owner,
        ttl_seconds=max(1, ttl),
    )
    return owner if ok else None


def release_backup_lock(settings: Settings, owner: str) -> None:
    copy_storage.release_job_lock(
        settings,
        job_name=STORAGE_BACKUP_LOCK_JOB,
        owner=owner,
    )


def acquire_prune_lock(settings: Settings, *, ttl_seconds: int | None = None) -> str | None:
    """
    Acquire exclusive retention-prune lock.
    Returns owner token on success, None if another prune holds a fresh lock.
    Stale locks (past expires_at) are stolen by try_acquire_job_lock.
    """
    owner = f"storage-prune-{uuid.uuid4().hex[:12]}"
    ttl = int(
        ttl_seconds
        if ttl_seconds is not None
        else settings.storage_retention_prune_lock_ttl_seconds
    )
    ok = copy_storage.try_acquire_job_lock(
        settings,
        job_name=STORAGE_RETENTION_PRUNE_LOCK_JOB,
        owner=owner,
        ttl_seconds=max(1, ttl),
    )
    return owner if ok else None


def release_prune_lock(settings: Settings, owner: str) -> None:
    copy_storage.release_job_lock(
        settings,
        job_name=STORAGE_RETENTION_PRUNE_LOCK_JOB,
        owner=owner,
    )
