"""Read-only SQLite retention audit / dry-run preview (V2.1.1 Phase 3A)."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config.settings import Settings
from app.storage.retention_policies import (
    RetentionPolicy,
    build_retention_policies,
    validate_policy_registry,
)

logger = logging.getLogger(__name__)

PolicyStatus = str  # APPROVED | PROTECTED | UNSUPPORTED


@dataclass(frozen=True)
class PolicyAuditResult:
    dataset: str
    table: str
    status: PolicyStatus
    total_rows: int | None
    candidate_rows: int | None
    protected_or_excluded_rows: int | None
    oldest_candidate_age_seconds: float | None
    retention_cutoff_iso: str | None
    future_batch_limit: int
    reason: str
    scope_note: str = "row counts only — not reclaimable disk bytes"


@dataclass
class RetentionAuditReport:
    dry_run: bool
    status: str
    checked_at: str
    policies: list[PolicyAuditResult] = field(default_factory=list)
    registry_errors: list[str] = field(default_factory=list)
    mutation_proof: dict = field(default_factory=dict)

    def as_sanitized_dict(self) -> dict:
        return {
            "retention_dry_run": True,
            "retention_status": self.status,
            "retention_checked_at": self.checked_at,
            "retention_policy_count": len(self.policies),
            "retention_approved_candidates": sum(
                (p.candidate_rows or 0) for p in self.policies if p.status == "APPROVED"
            ),
            "retention_protected_datasets": sum(1 for p in self.policies if p.status == "PROTECTED"),
            "retention_unsupported_datasets": sum(
                1 for p in self.policies if p.status == "UNSUPPORTED"
            ),
            "retention_registry_ok": not self.registry_errors,
            "retention_mutation_proof": dict(self.mutation_proof),
            "retention_banner": "DRY RUN — no database rows were deleted.",
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_readonly(settings: Settings) -> sqlite3.Connection | None:
    db_path = Path(settings.database_path)
    if str(db_path) == ":memory:":
        return None
    try:
        if not db_path.exists():
            return None
        uri = db_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            pass
        return connection
    except sqlite3.Error as exc:
        logger.debug("Retention audit readonly open failed: %s", exc)
        return None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(r[1]) for r in rows}


def _safe_count(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> int | None:
    try:
        row = connection.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        return None


def _cutoff_iso(days: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, days))
    return cutoff.isoformat()


def _snapshot_counts(connection: sqlite3.Connection, tables: list[str]) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for table in tables:
        if not _table_exists(connection, table):
            out[table] = None
            continue
        out[table] = _safe_count(connection, f'SELECT COUNT(*) FROM "{table}"')
    return out


def _evaluate_policy(
    connection: sqlite3.Connection,
    policy: RetentionPolicy,
    *,
    now_iso: str,
) -> PolicyAuditResult:
    if not _table_exists(connection, policy.table):
        return PolicyAuditResult(
            dataset=policy.dataset,
            table=policy.table,
            status="UNSUPPORTED",
            total_rows=None,
            candidate_rows=None,
            protected_or_excluded_rows=None,
            oldest_candidate_age_seconds=None,
            retention_cutoff_iso=None,
            future_batch_limit=policy.batch_size,
            reason=f"table missing: {policy.table}",
        )

    cols = _columns(connection, policy.table)
    if policy.timestamp_column and policy.timestamp_column not in cols:
        total = _safe_count(connection, f'SELECT COUNT(*) FROM "{policy.table}"')
        return PolicyAuditResult(
            dataset=policy.dataset,
            table=policy.table,
            status="UNSUPPORTED",
            total_rows=total,
            candidate_rows=None,
            protected_or_excluded_rows=None,
            oldest_candidate_age_seconds=None,
            retention_cutoff_iso=None,
            future_batch_limit=policy.batch_size,
            reason=f"timestamp column missing: {policy.timestamp_column}",
        )

    total = _safe_count(connection, f'SELECT COUNT(*) FROM "{policy.table}"')

    if policy.kind == "PROTECTED":
        return PolicyAuditResult(
            dataset=policy.dataset,
            table=policy.table,
            status="PROTECTED",
            total_rows=total,
            candidate_rows=0,
            protected_or_excluded_rows=total,
            oldest_candidate_age_seconds=None,
            retention_cutoff_iso=None,
            future_batch_limit=0,
            reason=policy.reason,
        )

    # APPROVED path — require reliable timestamp.
    assert policy.timestamp_column is not None
    assert policy.retention_days is not None

    # Expired locks use expires_at compared to now rather than retention_days age.
    if policy.table == "copy_job_locks" and policy.timestamp_column == "expires_at":
        cutoff = now_iso
        candidate_sql = (
            f'SELECT COUNT(*) FROM "{policy.table}" WHERE {policy.timestamp_column} < ?'
        )
        protected_sql = (
            f'SELECT COUNT(*) FROM "{policy.table}" WHERE {policy.timestamp_column} >= ?'
        )
        candidates = _safe_count(connection, candidate_sql, (cutoff,))
        protected = _safe_count(connection, protected_sql, (cutoff,))
        oldest = None
        if candidates:
            row = connection.execute(
                f'SELECT MIN("{policy.timestamp_column}") FROM "{policy.table}" '
                f'WHERE "{policy.timestamp_column}" < ?',
                (cutoff,),
            ).fetchone()
            oldest = _age_seconds(row[0] if row else None)
        return PolicyAuditResult(
            dataset=policy.dataset,
            table=policy.table,
            status="APPROVED",
            total_rows=total,
            candidate_rows=candidates,
            protected_or_excluded_rows=protected,
            oldest_candidate_age_seconds=oldest,
            retention_cutoff_iso=cutoff,
            future_batch_limit=policy.batch_size,
            reason=policy.reason,
        )

    cutoff = _cutoff_iso(policy.retention_days)
    where_parts = [f'"{policy.timestamp_column}" IS NOT NULL', f'"{policy.timestamp_column}" < ?']
    params: list = [cutoff]
    if policy.terminal_predicate:
        where_parts.append(f"({policy.terminal_predicate})")
    if policy.protected_exclusion_predicate:
        where_parts.append(f"NOT ({policy.protected_exclusion_predicate})")
    where_sql = " AND ".join(where_parts)

    candidates = _safe_count(
        connection,
        f'SELECT COUNT(*) FROM "{policy.table}" WHERE {where_sql}',
        tuple(params),
    )

    protected = None
    if total is not None and candidates is not None:
        protected = max(0, total - candidates)

    oldest = None
    try:
        row = connection.execute(
            f'SELECT MIN("{policy.timestamp_column}") FROM "{policy.table}" WHERE {where_sql}',
            tuple(params),
        ).fetchone()
        oldest = _age_seconds(row[0] if row else None)
    except sqlite3.Error:
        oldest = None

    return PolicyAuditResult(
        dataset=policy.dataset,
        table=policy.table,
        status="APPROVED",
        total_rows=total,
        candidate_rows=candidates,
        protected_or_excluded_rows=protected,
        oldest_candidate_age_seconds=oldest,
        retention_cutoff_iso=cutoff,
        future_batch_limit=policy.batch_size,
        reason=policy.reason,
    )


def _age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, datetime.now(timezone.utc).timestamp() - dt.timestamp())
    except ValueError:
        return None


def run_retention_audit(settings: Settings) -> RetentionAuditReport:
    """
    Read-only retention dry-run.
    SELECT / PRAGMA table_info / query_only only — never mutates rows.
    """
    checked_at = _now_iso()
    policies = build_retention_policies()
    registry_errors = validate_policy_registry(policies)
    report = RetentionAuditReport(
        dry_run=True,
        status="ok",
        checked_at=checked_at,
        registry_errors=registry_errors,
    )
    if registry_errors:
        report.status = "registry_invalid"

    connection = _open_readonly(settings)
    if connection is None:
        report.status = "unavailable"
        report.mutation_proof = {"note": "database_unavailable_for_readonly_audit"}
        return report

    tables = sorted({p.table for p in policies})
    try:
        before = _snapshot_counts(connection, tables)
        db_path = Path(settings.database_path)
        size_before = db_path.stat().st_size if db_path.exists() else None
        mtime_before = db_path.stat().st_mtime if db_path.exists() else None

        results: list[PolicyAuditResult] = []
        for policy in policies:
            try:
                results.append(_evaluate_policy(connection, policy, now_iso=checked_at))
            except sqlite3.Error as exc:
                results.append(
                    PolicyAuditResult(
                        dataset=policy.dataset,
                        table=policy.table,
                        status="UNSUPPORTED",
                        total_rows=None,
                        candidate_rows=None,
                        protected_or_excluded_rows=None,
                        oldest_candidate_age_seconds=None,
                        retention_cutoff_iso=None,
                        future_batch_limit=policy.batch_size,
                        reason=f"policy evaluation error: {type(exc).__name__}",
                    )
                )

        after = _snapshot_counts(connection, tables)
        size_after = db_path.stat().st_size if db_path.exists() else None
        mtime_after = db_path.stat().st_mtime if db_path.exists() else None

        report.policies = results
        report.mutation_proof = {
            "row_counts_unchanged": before == after,
            "counts_before": before,
            "counts_after": after,
            "db_size_before": size_before,
            "db_size_after": size_after,
            "db_size_unchanged": size_before == size_after,
            "no_retention_transaction": True,
            "readonly_uri": True,
            "query_only": True,
        }
        if before != after:
            report.status = "mutation_detected"
        return report
    finally:
        connection.close()


def retention_metrics_for_health(settings: Settings) -> dict:
    """Compact sanitized metrics for Health infrastructure probe."""
    report = run_retention_audit(settings)
    return report.as_sanitized_dict()
