"""Explicit SQLite retention policy registry (V2.1.1 Phase 3A — audit only)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PolicyKind = Literal["APPROVED", "PROTECTED"]

# Defaults align with existing prune helpers / prior retention plan.
RETENTION_HEALTH_DAYS = 14
RETENTION_ANALYSIS_DAYS = 90
RETENTION_REPORT_DAYS = 30
RETENTION_TRADE_EVENTS_DAYS = 90
RETENTION_RECOMMENDATIONS_DAYS = 90
RETENTION_AUDIT_DAYS = 180
RETENTION_UNAUTHORIZED_DAYS = 90
RETENTION_BASELINE_RUNS_DAYS = 90
RETENTION_DELIVERIES_DAYS = 60
RETENTION_OPPORTUNITY_METRICS_DAYS = 30
RETENTION_OPPORTUNITY_SIGNALS_DAYS = 180
RETENTION_OPPORTUNITY_DELIVERIES_DAYS = 60
RETENTION_OPPORTUNITY_EDITS_DAYS = 30
DEFAULT_FUTURE_BATCH_SIZE = 1000


@dataclass(frozen=True)
class RetentionPolicy:
    """Declarative retention rule. Phase 3A evaluates dry-run only — never deletes."""

    dataset: str
    table: str
    kind: PolicyKind
    timestamp_column: str | None
    retention_days: int | None
    batch_size: int
    # Extra SQL predicate (no leading AND). Empty string if none.
    terminal_predicate: str
    # Rows matching this are excluded from candidates even if aged.
    protected_exclusion_predicate: str
    reason: str


def build_retention_policies() -> tuple[RetentionPolicy, ...]:
    """Source-of-truth registry derived from verified CREATE TABLE schemas."""
    return (
        # --- APPROVED technical history (future prune candidates only) ---
        RetentionPolicy(
            dataset="health_check_history",
            table="health_checks",
            kind="APPROVED",
            timestamp_column="checked_at",
            retention_days=RETENTION_HEALTH_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="Append-only probe history; existing prune_health_history keep_days=14",
        ),
        RetentionPolicy(
            dataset="health_alert_history",
            table="health_alerts",
            kind="APPROVED",
            timestamp_column="sent_at",
            retention_days=RETENTION_HEALTH_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="Append-only health alert dedup ledger; pruned with health history",
        ),
        RetentionPolicy(
            dataset="job_execution_history",
            table="job_executions",
            kind="APPROVED",
            timestamp_column="started_at",
            retention_days=RETENTION_HEALTH_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="status IN ('ok', 'error')",
            protected_exclusion_predicate="status = 'running'",
            reason="Finished scheduler job rows only; exclude in-flight running jobs",
        ),
        RetentionPolicy(
            dataset="completed_baseline_runs",
            table="baseline_runs",
            kind="APPROVED",
            timestamp_column="finished_at",
            retention_days=RETENTION_BASELINE_RUNS_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="finished_at IS NOT NULL AND status IN ('COMPLETE', 'FAILED')",
            protected_exclusion_predicate="finished_at IS NULL",
            reason="Historical baseline attempts after completion/failure",
        ),
        RetentionPolicy(
            dataset="terminal_alert_deliveries",
            table="copy_alert_deliveries",
            kind="APPROVED",
            timestamp_column="updated_at",
            retention_days=RETENTION_DELIVERIES_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="status = 'sent'",
            protected_exclusion_predicate="status IN ('claimed', 'failed')",
            reason="Terminal sent deliveries only; claimed/failed retained for retry safety",
        ),
        RetentionPolicy(
            dataset="analysis_record_history",
            table="analysis_records",
            kind="APPROVED",
            timestamp_column="created_at",
            retention_days=RETENTION_ANALYSIS_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="Append-only Weather analysis history",
        ),
        RetentionPolicy(
            dataset="report_send_history",
            table="report_history",
            kind="APPROVED",
            timestamp_column="sent_at",
            retention_days=RETENTION_REPORT_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="Scheduled report dedup ledger by scan window",
        ),
        RetentionPolicy(
            dataset="copy_trade_event_history",
            table="copy_trade_events",
            kind="APPROVED",
            timestamp_column="created_at",
            retention_days=RETENTION_TRADE_EVENTS_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="Non-authoritative trade event history (dedup_key ledger, not event_uid)",
        ),
        RetentionPolicy(
            dataset="copy_recommendation_history",
            table="copy_recommendations",
            kind="APPROVED",
            timestamp_column="created_at",
            retention_days=RETENTION_RECOMMENDATIONS_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="Append-only paper recommendation history",
        ),
        RetentionPolicy(
            dataset="copy_audit_history",
            table="copy_audit_log",
            kind="APPROVED",
            timestamp_column="created_at",
            retention_days=RETENTION_AUDIT_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="Append-only audit trail with long retention",
        ),
        RetentionPolicy(
            dataset="unauthorized_access_history",
            table="unauthorized_access_log",
            kind="APPROVED",
            timestamp_column="created_at",
            retention_days=RETENTION_UNAUTHORIZED_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="Hashed admin-gate denial history",
        ),
        RetentionPolicy(
            dataset="expired_job_locks",
            table="copy_job_locks",
            kind="APPROVED",
            timestamp_column="expires_at",
            retention_days=0,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="expires_at < ?",
            protected_exclusion_predicate="expires_at >= ?",
            reason="Expired maintenance/poll locks only (TTL past)",
        ),
        RetentionPolicy(
            dataset="opportunity_metric_history",
            table="opportunity_metric_buckets",
            kind="APPROVED",
            timestamp_column="bucket_at",
            retention_days=RETENTION_OPPORTUNITY_METRICS_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="Derived scanner metric buckets; no raw API payloads",
        ),
        RetentionPolicy(
            dataset="opportunity_execution_metric_history",
            table="opportunity_execution_metrics",
            kind="APPROVED",
            timestamp_column="observed_at",
            retention_days=RETENTION_OPPORTUNITY_METRICS_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="Derived allocation-sweep metrics at approved presets",
        ),
        RetentionPolicy(
            dataset="opportunity_signal_history",
            table="opportunity_signals",
            kind="APPROVED",
            timestamp_column="last_seen_at",
            retention_days=RETENTION_OPPORTUNITY_SIGNALS_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="status = 'expired'",
            protected_exclusion_predicate="status = 'active'",
            reason="Expired opportunity episodes only; active signals stay protected",
        ),
        RetentionPolicy(
            dataset="opportunity_terminal_delivery_history",
            table="opportunity_deliveries",
            kind="APPROVED",
            timestamp_column="updated_at",
            retention_days=RETENTION_OPPORTUNITY_DELIVERIES_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="status IN ('sent', 'dead_letter', 'suppressed')",
            protected_exclusion_predicate="status IN ('pending', 'claimed', 'failed')",
            reason="Terminal opportunity deliveries only; retry and claim state protected",
        ),
        RetentionPolicy(
            dataset="opportunity_settings_audit_history",
            table="opportunity_settings_audit",
            kind="APPROVED",
            timestamp_column="created_at",
            retention_days=RETENTION_AUDIT_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="",
            protected_exclusion_predicate="",
            reason="Sanitized scanner settings audit history",
        ),
        RetentionPolicy(
            dataset="opportunity_completed_setting_edits",
            table="opportunity_setting_edits",
            kind="APPROVED",
            timestamp_column="expires_at",
            retention_days=RETENTION_OPPORTUNITY_EDITS_DAYS,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="status IN ('confirmed', 'cancelled', 'expired')",
            protected_exclusion_predicate="status = 'pending'",
            reason="Completed or expired edit-token records; pending previews protected",
        ),
        # --- PROTECTED (never automatic candidates in V2.1.1) ---
        RetentionPolicy(
            dataset="processed_copy_events_dedup",
            table="processed_copy_events",
            kind="PROTECTED",
            timestamp_column="created_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Authoritative Copy dedup / anti-replay; Phase 3A metrics only",
        ),
        RetentionPolicy(
            dataset="wallet_monitor_state",
            table="wallet_monitor_state",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Current baselines and subscription freshness state",
        ),
        RetentionPolicy(
            dataset="wallet_event_watermarks",
            table="wallet_event_watermarks",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Current alert watermarks",
        ),
        RetentionPolicy(
            dataset="copy_watchlist",
            table="copy_watchlist",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Active Watchlist subscriptions",
        ),
        RetentionPolicy(
            dataset="users",
            table="users",
            kind="PROTECTED",
            timestamp_column="last_seen_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="User accounts",
        ),
        RetentionPolicy(
            dataset="user_filters",
            table="user_filters",
            kind="PROTECTED",
            timestamp_column=None,
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="User filter settings",
        ),
        RetentionPolicy(
            dataset="copy_user_settings",
            table="copy_user_settings",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Copy profile settings",
        ),
        RetentionPolicy(
            dataset="open_paper_positions",
            table="copy_paper_positions",
            kind="PROTECTED",
            timestamp_column="opened_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="status = 'open' OR closed_at IS NULL",
            reason="Open/active paper positions must not be auto-pruned",
        ),
        RetentionPolicy(
            dataset="active_health_incidents",
            table="health_incidents",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="state NOT IN ('resolved')",
            reason="Open/active incidents retained for continuity",
        ),
        RetentionPolicy(
            dataset="component_states",
            table="component_states",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Current Health component state",
        ),
        RetentionPolicy(
            dataset="copy_poll_state",
            table="copy_poll_state",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Current poll/discovery operational state",
        ),
        RetentionPolicy(
            dataset="provider_metrics",
            table="provider_metrics",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Current provider counters",
        ),
        RetentionPolicy(
            dataset="position_snapshots",
            table="copy_position_snapshots",
            kind="PROTECTED",
            timestamp_column="snapshot_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Current position book required for Copy polling diffs",
        ),
        RetentionPolicy(
            dataset="scan_snapshots",
            table="scan_snapshots",
            kind="PROTECTED",
            timestamp_column="completed_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Current Telegram scan cache (bounded current-state)",
        ),
        RetentionPolicy(
            dataset="opportunity_discovery_sessions",
            table="opportunity_discovery_sessions",
            kind="APPROVED",
            timestamp_column="expires_at",
            retention_days=1,
            batch_size=DEFAULT_FUTURE_BATCH_SIZE,
            terminal_predicate="state IN ('PERMANENT_FAILURE', 'EXPIRED', 'COMPLETE')",
            protected_exclusion_predicate="state IN ('NOT_STARTED', 'IN_PROGRESS', 'RETRYABLE_FAILURE')",
            reason="Abandoned/expired resumable discovery sessions; active rebuilds protected",
        ),
        RetentionPolicy(
            dataset="opportunity_catalog_versions",
            table="opportunity_catalog_versions",
            kind="PROTECTED",
            timestamp_column="published_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Versioned trusted COMPLETE catalogs; bounded in-code retention",
        ),
        RetentionPolicy(
            dataset="opportunity_catalog_markets",
            table="opportunity_catalog_markets",
            kind="PROTECTED",
            timestamp_column="metadata_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Normalized markets belonging to trusted catalog versions",
        ),
        RetentionPolicy(
            dataset="opportunity_market_state",
            table="opportunity_market_state",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Current per-asset baseline and scanner state",
        ),
        RetentionPolicy(
            dataset="opportunity_subscriptions",
            table="opportunity_subscriptions",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Current per-user scanner settings and delivery opt-in",
        ),
        RetentionPolicy(
            dataset="opportunity_scanner_state",
            table="opportunity_scanner_state",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="Current scanner baseline and completeness state",
        ),
        # --- Market Opportunity Intelligence (V4.1) — protected; prune disabled ---
        RetentionPolicy(
            dataset="moi_opportunities",
            table="moi_opportunities",
            kind="PROTECTED",
            timestamp_column="updated_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="V4.1 paper opportunity rows; prune remains disabled/dry-run",
        ),
        RetentionPolicy(
            dataset="moi_evidence",
            table="moi_evidence",
            kind="PROTECTED",
            timestamp_column="fetched_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="V4.1 evidence/provenance rows; prune remains disabled/dry-run",
        ),
        RetentionPolicy(
            dataset="moi_outcomes",
            table="moi_outcomes",
            kind="PROTECTED",
            timestamp_column="measured_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="V4.1 paper outcome stubs; prune remains disabled/dry-run",
        ),
        RetentionPolicy(
            dataset="moi_runtime_status",
            table="moi_runtime_status",
            kind="PROTECTED",
            timestamp_column="checked_at",
            retention_days=None,
            batch_size=0,
            terminal_predicate="",
            protected_exclusion_predicate="1=1",
            reason="V4.1 MOI runtime status singleton",
        ),
    )


def validate_policy_registry(policies: tuple[RetentionPolicy, ...] | None = None) -> list[str]:
    """Return validation errors for the registry (empty list = OK)."""
    policies = policies or build_retention_policies()
    errors: list[str] = []
    seen: set[str] = set()
    for policy in policies:
        if not policy.dataset or not policy.table:
            errors.append(f"invalid policy identity: {policy.dataset!r}/{policy.table!r}")
        if policy.dataset in seen:
            errors.append(f"duplicate dataset: {policy.dataset}")
        seen.add(policy.dataset)
        if policy.kind == "APPROVED":
            if not policy.timestamp_column:
                errors.append(f"{policy.dataset}: APPROVED requires timestamp_column")
            if policy.retention_days is None:
                errors.append(f"{policy.dataset}: APPROVED requires retention_days")
            if policy.batch_size <= 0:
                errors.append(f"{policy.dataset}: APPROVED requires positive batch_size")
        if policy.kind == "PROTECTED" and policy.batch_size != 0:
            errors.append(f"{policy.dataset}: PROTECTED batch_size must be 0")
    return errors
