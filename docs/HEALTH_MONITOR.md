# Health Monitor (V2.1)

Deterministic operational health for Telegram, scheduler, weather providers, Copy Intelligence, and infrastructure.

## Access

- Visible only to Telegram users listed in `ADMIN_IDS`.
- Main menu shows **System Health** only for admins.
- Every health callback, `/health`, and self-test re-checks admin authorization.
- Unauthorized attempts receive `Access denied.` and a sanitized log entry (`user_id_hash` only).

## Status values

`HEALTHY` | `DEGRADED` | `DOWN` | `DISABLED`

## Thresholds (defaults)

| Setting | Default | Meaning |
|---------|---------|---------|
| `HEALTH_FAILURE_THRESHOLD` | 3 | Consecutive failures → DEGRADED |
| `HEALTH_RECOVERY_THRESHOLD` | 2 | Consecutive successes after failure → HEALTHY |
| `HEALTH_JOB_STALE_MULTIPLIER` | 2 | Job overdue if silent longer than interval × multiplier |
| `HEALTH_CHECK_INTERVAL_SECONDS` | 300 | `health_monitor_tick` period |

DOWN is used for hard faults (SQLite unavailable, Telegram getUpdates conflict / duplicate process, copy read-only violation) or failures ≥ 2× failure threshold.

## Overall aggregation

1. Critical DOWN (`telegram`, `sqlite`, `copy_readonly`) → overall DOWN.
2. Else any non-disabled DOWN/DEGRADED → overall DEGRADED.
3. Else HEALTHY.
4. DISABLED components are excluded.

## Components

- **Telegram** — polling, last update, conflicts, startup timeout.
- **Scheduler** — job executions, stale/failed/overdue jobs.
- **Weather** — provider metrics, valid provider count vs `FORECAST_MIN_PROVIDERS`.
- **Copy Intelligence** — poll freshness, baseline failures, stale rejects, API errors.
- **copy_readonly** — asserts `COPY_INTEL_READ_ONLY=true` when copy is enabled.
- **SQLite / infrastructure** — schema, disk free, sanitized config summary.

## Alerts

Admin-only, deduplicated by `(component, incident_type, fingerprint)`:

- open → updated → acknowledged → suppressed → resolved
- Notify on open, severity escalate to DOWN, and recovery
- No spam while an incident remains open

## AI boundary

- Core status never depends on AI.
- `HEALTH_AI_EXPLAINER_ENABLED` defaults false.
- AI must never restart processes, edit `.env`, run git, expose secrets, change thresholds, or trade.

## Telegram UX

Edit-in-place English menu under **System Health**: Summary, Weather Providers, Copy Intelligence, Jobs & Scheduler, Database & Cache, **Storage**, Retention Dry-Run, Backup Status, Recent Incidents, Run Self-Test, Health Settings, Main Menu.

### Storage menu (admin-only, V2.1.1)

Compact Storage section under System Health. Every entry re-checks `ADMIN_IDS`. Telegram shows logical path labels only (`live_database`, `logs`, `cache`, `backup`, `proof_artifacts`) — never absolute private paths, secrets, wallets, Telegram IDs, raw SQL, or message contents.

| Action | Kind | Behavior |
|--------|------|----------|
| **Metrics** | Preview-only | Sanitized disk, DB/WAL sizes, storage level, config flags. Read-only. |
| **Backup Status** | Preview-only | Last backup label/status/age and integrity-gate readiness. No files created. |
| **Create Backup** | Explicit confirmation | Online SQLite backup + integrity check. Requires admin confirm token; refuses if backup dir unset or free space too low. |
| **File Cleanup Preview** | Preview-only | Inventory of eligible files under configured roots; dry-run style report. Deletes nothing. |
| **Retention Preview** | Preview-only | Read-only SQLite retention audit / candidate counts. Deletes no rows. |
| **Run Safe Cleanup** | Explicit confirmation | Bounded explicit-path cleanup. Blocked unless enabled and not dry-run; still requires admin confirm + locks/budgets. |
| **Run Retention Batch** | Explicit confirmation | Bounded prune. Requires verified-backup gate, enabled + non-dry-run settings, admin confirm, locks/row budgets. |
| **Maintenance History** | Preview-only | Sanitized recent cleanup / backup / prune outcomes. |
| **Refresh** | Preview-only | Re-renders Storage summary from current metrics. |

#### Defaults and gates

- `STORAGE_CLEANUP_ENABLED=false`, `STORAGE_CLEANUP_DRY_RUN=true` by default.
- `STORAGE_RETENTION_PRUNE_ENABLED=false`, `STORAGE_RETENTION_PRUNE_DRY_RUN=true` by default.
- `STORAGE_BACKUP_ENABLED=false` by default; backup dir must be an absolute configured path.
- Retention prune requires a recent **verified** backup (age gate); otherwise gate status is BLOCKED.
- No automatic VACUUM and no automatic `journal_mode` changes.
- Confirmation tokens are bound to admin identity, database identity, and preview fingerprints. Handlers never bypass service-layer gates.

### Storage alerts

Admin-only, watermarked transitions (restart-safe):

- storage HEALTHY → DEGRADED / DEGRADED → CRITICAL / recovery → HEALTHY
- backup VERIFIED → FAILED; verified backup too old
- retention gate READY → BLOCKED
- cleanup/pruning failure

Component recovery and overall recovery are deduplicated for the same underlying transition.
