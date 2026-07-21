"""Explicit-path file cleanup inventory and bounded deletion (V2.1.1 Phase 2)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.config.constants import STORAGE_CLEANUP_LOCK_JOB
from app.config.settings import Settings
from app.health import storage as health_storage
from app.health.sanitize import logical_path_label
from app.storage.lock import (
    acquire_cleanup_lock,
    fresh_exclusive_lock_job,
    release_cleanup_lock,
)
from app.storage.paths import (
    is_protected_file,
    is_within_approved_root,
    path_escapes_via_link,
    reject_reason_for_configured_root,
    resolve_approved_root,
)

logger = logging.getLogger(__name__)

_LAST_REPORT = None
_CONFIRMATIONS: dict[str, "CleanupConfirmation"] = {}


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
    failure_code: str = ""
    detail: str = ""
    confirmation_token_fingerprint: str | None = None
    started_at: str = ""
    finished_at: str = ""

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
            "cleanup_failure_code": self.failure_code or None,
            "cleanup_detail": self.detail or None,
        }


@dataclass(frozen=True)
class CleanupConfirmation:
    token: str
    operator_id: str
    candidate_fingerprint: str
    candidate_count: int
    reclaimable_bytes: int
    max_files: int
    max_bytes: int
    expires_at: float


def get_last_cleanup_report():
    return _LAST_REPORT


def _set_last_report(report: CleanupReport) -> CleanupReport:
    global _LAST_REPORT
    _LAST_REPORT = report
    return report


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _candidate_fingerprint(candidates: list[CleanupCandidate]) -> str:
    payload = [
        {
            "root": c.root_label,
            "name": c.name,
            "size": c.size_bytes,
            "policy": c.policy,
            "age": c.age_seconds,
        }
        for c in sorted(candidates, key=lambda x: (x.root_label, x.name, x.size_bytes))
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _record_cleanup_audit(settings: Settings, report: CleanupReport) -> None:
    try:
        summary = json.dumps(
            {
                "status": report.status,
                "dry_run": report.dry_run,
                "candidates": report.candidate_count,
                "deleted": report.deleted_count,
                "skipped_protected": report.skipped_protected,
                "errors": report.errors,
                "failure_code": report.failure_code or None,
            },
            sort_keys=True,
        )
        if any(x in summary.lower() for x in ("token", "wallet", ":\\", "/users/", "/home/")):
            summary = '{"status":"sanitized"}'
        started = report.started_at or report.checked_at or _now_iso()
        ok = report.status in {"dry_run", "disabled", "completed", "inventory"}
        health_storage.insert_job_execution(
            settings,
            job_name=STORAGE_CLEANUP_LOCK_JOB,
            status="ok" if ok else "error",
            started_at=started,
            expected_interval_seconds=None,
        )
        health_storage.finish_latest_job_execution(
            settings,
            job_name=STORAGE_CLEANUP_LOCK_JOB,
            status="ok" if ok else "error",
            error=summary,
        )
    except Exception:
        logger.debug("Cleanup audit record failed", exc_info=True)


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


def create_cleanup_confirmation(
    settings: Settings,
    *,
    operator_id: str,
) -> tuple[CleanupConfirmation | None, CleanupReport]:
    """Dry-run preview that binds a short-lived confirmation token. Never deletes."""
    candidates, report = inventory_cleanup_candidates(settings)
    report.dry_run = True
    report.cleanup_enabled = bool(settings.storage_cleanup_enabled)
    report.started_at = _now_iso()
    report.finished_at = report.started_at
    report.status = "dry_run" if settings.storage_cleanup_enabled else "disabled"
    report.detail = (
        "confirmation preview ready — destructive cleanup requires matching token"
        if settings.storage_cleanup_enabled
        else "file cleanup disabled by configuration"
    )

    if not settings.storage_cleanup_enabled:
        report.failure_code = "cleanup_disabled"
        _record_cleanup_audit(settings, report)
        return None, _set_last_report(report)

    conflict = fresh_exclusive_lock_job(settings)
    if conflict is not None:
        report.status = "lock_busy"
        report.failure_code = "maintenance_busy"
        report.detail = f"maintenance busy: {conflict}"
        _record_cleanup_audit(settings, report)
        return None, _set_last_report(report)

    token = uuid.uuid4().hex
    fingerprint = _candidate_fingerprint(candidates)
    confirmation = CleanupConfirmation(
        token=token,
        operator_id=str(operator_id),
        candidate_fingerprint=fingerprint,
        candidate_count=len(candidates),
        reclaimable_bytes=report.reclaimable_bytes,
        max_files=settings.storage_cleanup_max_files_per_run,
        max_bytes=settings.storage_cleanup_max_bytes_per_run,
        expires_at=time.time() + settings.storage_cleanup_confirm_ttl_seconds,
    )
    _CONFIRMATIONS[token] = confirmation
    report.confirmation_token_fingerprint = _token_fingerprint(token)
    _record_cleanup_audit(settings, report)
    return confirmation, _set_last_report(report)


def execute_confirmed_cleanup(
    settings: Settings,
    *,
    token: str,
    operator_id: str,
) -> CleanupReport:
    """Execute destructive cleanup only when confirmation binding still matches."""
    confirmation = _CONFIRMATIONS.get(token)
    if confirmation is None:
        report = CleanupReport(
            dry_run=False,
            cleanup_enabled=bool(settings.storage_cleanup_enabled),
            status="refused",
            failure_code="invalid_confirmation",
            detail="confirmation token unknown or already consumed",
            started_at=_now_iso(),
            finished_at=_now_iso(),
            checked_at=_now_iso(),
        )
        _record_cleanup_audit(settings, report)
        return _set_last_report(report)

    if time.time() > confirmation.expires_at:
        _CONFIRMATIONS.pop(token, None)
        report = CleanupReport(
            dry_run=False,
            cleanup_enabled=bool(settings.storage_cleanup_enabled),
            status="refused",
            failure_code="confirmation_expired",
            detail="confirmation token expired",
            confirmation_token_fingerprint=_token_fingerprint(token),
            started_at=_now_iso(),
            finished_at=_now_iso(),
            checked_at=_now_iso(),
        )
        _record_cleanup_audit(settings, report)
        return _set_last_report(report)

    if confirmation.operator_id != str(operator_id):
        report = CleanupReport(
            dry_run=False,
            cleanup_enabled=bool(settings.storage_cleanup_enabled),
            status="refused",
            failure_code="confirmation_operator_mismatch",
            detail="confirmation bound to a different operator",
            confirmation_token_fingerprint=_token_fingerprint(token),
            started_at=_now_iso(),
            finished_at=_now_iso(),
            checked_at=_now_iso(),
        )
        _record_cleanup_audit(settings, report)
        return _set_last_report(report)

    candidates, inventory = inventory_cleanup_candidates(settings)
    fingerprint = _candidate_fingerprint(candidates)
    if (
        fingerprint != confirmation.candidate_fingerprint
        or len(candidates) != confirmation.candidate_count
        or inventory.reclaimable_bytes != confirmation.reclaimable_bytes
        or settings.storage_cleanup_max_files_per_run != confirmation.max_files
        or settings.storage_cleanup_max_bytes_per_run != confirmation.max_bytes
    ):
        _CONFIRMATIONS.pop(token, None)
        report = CleanupReport(
            dry_run=False,
            cleanup_enabled=bool(settings.storage_cleanup_enabled),
            status="refused",
            failure_code="stale_preview",
            detail="candidates or limits changed — confirmation invalidated",
            confirmation_token_fingerprint=_token_fingerprint(token),
            candidate_count=len(candidates),
            reclaimable_bytes=inventory.reclaimable_bytes,
            started_at=_now_iso(),
            finished_at=_now_iso(),
            checked_at=_now_iso(),
        )
        _record_cleanup_audit(settings, report)
        return _set_last_report(report)

    if not settings.storage_cleanup_enabled:
        _CONFIRMATIONS.pop(token, None)
        report = CleanupReport(
            dry_run=True,
            cleanup_enabled=False,
            status="disabled",
            failure_code="cleanup_disabled",
            detail="file cleanup disabled by configuration",
            confirmation_token_fingerprint=_token_fingerprint(token),
            started_at=_now_iso(),
            finished_at=_now_iso(),
            checked_at=_now_iso(),
        )
        _record_cleanup_audit(settings, report)
        return _set_last_report(report)

    if settings.storage_cleanup_dry_run:
        _CONFIRMATIONS.pop(token, None)
        report = CleanupReport(
            dry_run=True,
            cleanup_enabled=True,
            status="refused",
            failure_code="dry_run_enforced",
            detail="destructive cleanup refused while STORAGE_CLEANUP_DRY_RUN=true",
            confirmation_token_fingerprint=_token_fingerprint(token),
            candidate_count=confirmation.candidate_count,
            reclaimable_bytes=confirmation.reclaimable_bytes,
            started_at=_now_iso(),
            finished_at=_now_iso(),
            checked_at=_now_iso(),
        )
        _record_cleanup_audit(settings, report)
        return _set_last_report(report)

    conflict = fresh_exclusive_lock_job(settings)
    if conflict is not None:
        report = CleanupReport(
            dry_run=False,
            cleanup_enabled=True,
            status="lock_busy",
            failure_code="maintenance_busy",
            detail=f"maintenance busy: {conflict}",
            confirmation_token_fingerprint=_token_fingerprint(token),
            started_at=_now_iso(),
            finished_at=_now_iso(),
            checked_at=_now_iso(),
        )
        _record_cleanup_audit(settings, report)
        return _set_last_report(report)

    _CONFIRMATIONS.pop(token, None)
    report = run_storage_cleanup(settings, force_dry_run=False)
    report.confirmation_token_fingerprint = _token_fingerprint(token)
    return report


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

    started = _now_iso()
    candidates, report = inventory_cleanup_candidates(settings)
    report.dry_run = dry_run
    report.cleanup_enabled = bool(settings.storage_cleanup_enabled)
    report.started_at = started
    report.status = "disabled" if not settings.storage_cleanup_enabled else (
        "dry_run" if dry_run else "running"
    )

    if not settings.storage_cleanup_enabled or dry_run:
        report.status = "dry_run" if settings.storage_cleanup_enabled else "disabled"
        report.deleted_count = 0
        report.deleted_bytes = 0
        report.finished_at = _now_iso()
        _record_cleanup_audit(settings, report)
        _set_last_report(report)
        return report

    conflict = fresh_exclusive_lock_job(settings, exclude_job=STORAGE_CLEANUP_LOCK_JOB)
    if conflict is not None:
        report.status = "lock_busy"
        report.failure_code = "maintenance_busy"
        report.detail = f"maintenance busy: {conflict}"
        report.finished_at = _now_iso()
        _record_cleanup_audit(settings, report)
        _set_last_report(report)
        return report

    owner = acquire_cleanup_lock(settings)
    if owner is None:
        report.status = "lock_busy"
        report.failure_code = "lock_busy"
        report.finished_at = _now_iso()
        _record_cleanup_audit(settings, report)
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
    report.status = "completed" if errors == 0 else "failed"
    if errors:
        report.failure_code = "cleanup_errors"
    report.checked_at = _now_iso()
    report.finished_at = report.checked_at
    _record_cleanup_audit(settings, report)
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
