"""Explicit-path file cleanup inventory and bounded deletion (V2.1.1 Phase 2)."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.config.settings import Settings
from app.health.sanitize import logical_path_label
from app.storage.lock import acquire_cleanup_lock, release_cleanup_lock
from app.storage.paths import (
    is_protected_file,
    is_within_approved_root,
    path_escapes_via_link,
    reject_reason_for_configured_root,
    resolve_approved_root,
)

logger = logging.getLogger(__name__)

_LAST_REPORT = None


@dataclass(frozen=True)
class CleanupCandidate:
    root_label: str
    name: str
    size_bytes: int
    age_seconds: float
    policy: str


@dataclass
class CleanupReport:
    dry_run: bool
    cleanup_enabled: bool
    status: str
    candidate_count: int = 0
    reclaimable_bytes: int = 0
    oldest_age_seconds: float | None = None
    newest_age_seconds: float | None = None
    deleted_count: int = 0
    deleted_bytes: int = 0
    skipped_protected: int = 0
    skipped_locked: int = 0
    skipped_young: int = 0
    skipped_limits: int = 0
    skipped_invalid: int = 0
    errors: int = 0
    roots: dict[str, str] = field(default_factory=dict)
    checked_at: str = ""

    def as_sanitized_dict(self) -> dict:
        return {
            "cleanup_dry_run": self.dry_run,
            "cleanup_enabled": self.cleanup_enabled,
            "cleanup_status": self.status,
            "cleanup_candidate_count": self.candidate_count,
            "cleanup_reclaimable_bytes": self.reclaimable_bytes,
            "cleanup_oldest_age_seconds": self.oldest_age_seconds,
            "cleanup_newest_age_seconds": self.newest_age_seconds,
            "cleanup_deleted_count": self.deleted_count,
            "cleanup_deleted_bytes": self.deleted_bytes,
            "cleanup_skipped_protected": self.skipped_protected,
            "cleanup_skipped_locked": self.skipped_locked,
            "cleanup_skipped_young": self.skipped_young,
            "cleanup_skipped_limits": self.skipped_limits,
            "cleanup_skipped_invalid": self.skipped_invalid,
            "cleanup_errors": self.errors,
            "cleanup_roots": dict(self.roots),
            "cleanup_checked_at": self.checked_at,
        }


def get_last_cleanup_report():
    return _LAST_REPORT


def _set_last_report(report: CleanupReport) -> None:
    global _LAST_REPORT
    _LAST_REPORT = report


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _active_log_path(settings: Settings, logs_root: Path | None) -> Path | None:
    if logs_root is None:
        return None
    name = (settings.storage_active_log_name or "").strip()
    if not name or ".." in Path(name).parts or Path(name).is_absolute():
        return None
    return logs_root / name


def _configured_roots(settings: Settings) -> list[tuple[str, Path]]:
    mapping = (
        ("logs", settings.storage_logs_dir),
        ("cache", settings.storage_cache_dir),
        ("proof_artifacts", settings.storage_proof_dir),
    )
    roots: list[tuple[str, Path]] = []
    for label, configured in mapping:
        reason = reject_reason_for_configured_root(configured)
        if reason == "empty":
            continue
        if reason is not None:
            logger.info("Cleanup root %s rejected: %s", logical_path_label(label), reason)
            continue
        resolved = resolve_approved_root(configured)
        if resolved is None:
            continue
        roots.append((label, resolved))
    return roots


def _file_age_seconds(path: Path, now: float) -> float | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return max(0.0, now - mtime)


def _is_rotated_log(path: Path, active_name: str) -> bool:
    name = path.name
    if name == active_name:
        return False
    # RotatingFileHandler: name.1, name.2, ... or name.YYYYMMDD
    if name.startswith(active_name + "."):
        return True
    stem = Path(active_name).stem
    suffix = Path(active_name).suffix
    if suffix and name.startswith(stem) and name != active_name:
        # e.g. trading_intelligence.log.1
        return True
    return False


def _min_age_for_policy(settings: Settings, policy: str) -> int:
    if policy == "rotated_log":
        return max(
            settings.storage_cleanup_min_age_seconds,
            int(settings.storage_log_retention_days * 86400),
        )
    if policy == "expired_cache":
        return settings.storage_cache_min_age_seconds
    if policy == "expired_proof":
        return settings.storage_proof_min_age_seconds
    return settings.storage_cleanup_min_age_seconds


def _iter_files(root: Path):
    if not root.exists() or not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Do not descend into symlinked directories.
        safe_dirs = []
        for d in list(dirnames):
            child = Path(dirpath) / d
            if child.is_symlink() or path_escapes_via_link(child, root):
                continue
            if not is_within_approved_root(child, root):
                continue
            safe_dirs.append(d)
        dirnames[:] = safe_dirs
        for name in filenames:
            yield Path(dirpath) / name


def inventory_cleanup_candidates(settings: Settings) -> tuple[list[CleanupCandidate], CleanupReport]:
    """Build candidate list (read-only). Never deletes."""
    now = time.time()
    roots = _configured_roots(settings)
    root_labels = {label: logical_path_label(label) for label, _ in roots}
    candidates: list[CleanupCandidate] = []
    skipped_protected = 0
    skipped_young = 0
    skipped_invalid = 0

    logs_root = next((path for label, path in roots if label == "logs"), None)
    active_log = _active_log_path(settings, logs_root)
    active_name = settings.storage_active_log_name.strip() or "trading_intelligence.log"

    for label, root in roots:
        if not root.exists():
            continue
        for path in _iter_files(root):
            if path_escapes_via_link(path, root) or not is_within_approved_root(path, root):
                skipped_invalid += 1
                continue
            if is_protected_file(
                path,
                database_path=settings.database_path,
                active_log_path=active_log,
            ):
                skipped_protected += 1
                continue

            policy = None
            if label == "logs":
                if _is_rotated_log(path, active_name):
                    policy = "rotated_log"
            elif label == "cache":
                policy = "expired_cache"
            elif label == "proof_artifacts":
                policy = "expired_proof"
            if policy is None:
                skipped_invalid += 1
                continue

            age = _file_age_seconds(path, now)
            if age is None:
                skipped_invalid += 1
                continue
            min_age = _min_age_for_policy(settings, policy)
            if age < min_age:
                skipped_young += 1
                continue
            try:
                size = int(path.stat().st_size)
            except OSError:
                skipped_invalid += 1
                continue
            candidates.append(
                CleanupCandidate(
                    root_label=logical_path_label(label),
                    name=path.name,
                    size_bytes=size,
                    age_seconds=round(age, 1),
                    policy=policy,
                )
            )

    ages = [c.age_seconds for c in candidates]
    report = CleanupReport(
        dry_run=True,
        cleanup_enabled=bool(settings.storage_cleanup_enabled),
        status="inventory",
        candidate_count=len(candidates),
        reclaimable_bytes=sum(c.size_bytes for c in candidates),
        oldest_age_seconds=max(ages) if ages else None,
        newest_age_seconds=min(ages) if ages else None,
        skipped_protected=skipped_protected,
        skipped_young=skipped_young,
        skipped_invalid=skipped_invalid,
        roots=root_labels,
        checked_at=_now_iso(),
    )
    return candidates, report


def _resolve_candidate_path(
    settings: Settings,
    candidate: CleanupCandidate,
    roots: list[tuple[str, Path]],
) -> Path | None:
    for label, root in roots:
        if logical_path_label(label) != candidate.root_label:
            continue
        # Search by exact basename under root (no wildcard delete).
        matches = [p for p in _iter_files(root) if p.name == candidate.name]
        if len(matches) != 1:
            # Ambiguous or missing — skip.
            return None
        path = matches[0]
        if not is_within_approved_root(path, root):
            return None
        if path_escapes_via_link(path, root):
            return None
        logs_root = next((r for lbl, r in roots if lbl == "logs"), None)
        active_log = _active_log_path(settings, logs_root)
        if is_protected_file(
            path,
            database_path=settings.database_path,
            active_log_path=active_log,
        ):
            return None
        return path
    return None


def run_storage_cleanup(
    settings: Settings,
    *,
    force_dry_run: bool | None = None,
) -> CleanupReport:
    """
    Inventory and optionally delete within explicit roots.
    Default: cleanup disabled and dry-run — zero deletions.
    """
    dry_run = settings.storage_cleanup_dry_run if force_dry_run is None else force_dry_run
    if not settings.storage_cleanup_enabled:
        dry_run = True

    candidates, report = inventory_cleanup_candidates(settings)
    report.dry_run = dry_run
    report.cleanup_enabled = bool(settings.storage_cleanup_enabled)
    report.status = "disabled" if not settings.storage_cleanup_enabled else (
        "dry_run" if dry_run else "running"
    )

    if not settings.storage_cleanup_enabled or dry_run:
        report.status = "dry_run" if settings.storage_cleanup_enabled else "disabled"
        report.deleted_count = 0
        report.deleted_bytes = 0
        _set_last_report(report)
        return report

    owner = acquire_cleanup_lock(settings)
    if owner is None:
        report.status = "lock_busy"
        _set_last_report(report)
        return report

    deleted_count = 0
    deleted_bytes = 0
    skipped_limits = 0
    skipped_locked = 0
    errors = 0
    roots = _configured_roots(settings)

    try:
        for candidate in candidates:
            if deleted_count >= settings.storage_cleanup_max_files_per_run:
                skipped_limits += 1
                continue
            if deleted_bytes + candidate.size_bytes > settings.storage_cleanup_max_bytes_per_run:
                skipped_limits += 1
                continue
            path = _resolve_candidate_path(settings, candidate, roots)
            if path is None:
                report.skipped_invalid += 1
                continue
            try:
                path.unlink()
                deleted_count += 1
                deleted_bytes += candidate.size_bytes
            except PermissionError:
                skipped_locked += 1
            except OSError:
                errors += 1
                logger.debug("Cleanup delete failed for %s", candidate.name, exc_info=True)
    finally:
        release_cleanup_lock(settings, owner)

    report.deleted_count = deleted_count
    report.deleted_bytes = deleted_bytes
    report.skipped_limits = skipped_limits
    report.skipped_locked = skipped_locked
    report.errors = errors
    report.status = "completed"
    report.checked_at = _now_iso()
    _set_last_report(report)
    return report


def cleanup_metrics_for_health(settings: Settings) -> dict:
    """Read-only inventory + last cleanup status for Health probes."""
    _candidates, inventory = inventory_cleanup_candidates(settings)
    payload = inventory.as_sanitized_dict()
    last = get_last_cleanup_report()
    if last is not None:
        payload["cleanup_last_status"] = last.status
        payload["cleanup_last_deleted_count"] = last.deleted_count
        payload["cleanup_last_deleted_bytes"] = last.deleted_bytes
        payload["cleanup_last_errors"] = last.errors
        payload["cleanup_last_checked_at"] = last.checked_at
    else:
        payload["cleanup_last_status"] = "never"
        payload["cleanup_last_deleted_count"] = 0
        payload["cleanup_last_deleted_bytes"] = 0
        payload["cleanup_last_errors"] = 0
        payload["cleanup_last_checked_at"] = None
    return payload
