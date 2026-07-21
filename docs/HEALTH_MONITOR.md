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

### Storage (admin-only, V2.1.1 Phase 5)

Compact Storage section with:

- Storage Metrics, Backup Status, Create Backup
- File Cleanup Preview, Retention Preview
- Run Safe File Cleanup, Run Retention Batch
- Maintenance History, Refresh, Back to System Health

Safe actions require explicit confirmation tokens bound to admin identity, database identity, and preview fingerprints. Defaults keep cleanup/pruning disabled and dry-run enabled. Destructive handlers never bypass service-layer gates. Paths, secrets, wallets, Telegram IDs, raw SQL, and message contents are never shown.

### Storage alerts

Admin-only, watermarked transitions (restart-safe):

- storage HEALTHY → DEGRADED / DEGRADED → CRITICAL / recovery → HEALTHY
- backup VERIFIED → FAILED; verified backup too old
- retention gate READY → BLOCKED
- cleanup/pruning failure

Component recovery and overall recovery are deduplicated for the same underlying transition.
