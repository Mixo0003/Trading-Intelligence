# Health Monitor (V4.2.1)

Deterministic operational health for **all** Trading Intelligence product areas:
Core Runtime, Storage, Polymarket, Weather, Copy, Crypto, Market Opportunities,
Delivery, and External Sources.

## Access (admin-only)

- Visible only to Telegram users listed in `ADMIN_IDS` (authorization uses
  **Telegram `user_id`**, never `chat_id` alone).
- Main menu shows **System Health** only for admins (UX only — not the security boundary).
- Every health command, callback, detail screen, and storage action re-checks admin authorization.
- Unauthorized `/health` → chat reply `Access denied.`
- Unauthorized health callback → **callback toast only** `Access denied.` (no health body, no chat pollution).
- Missing/invalid `ADMIN_IDS` fails closed: no button, deny all health access, **zero** automatic alerts.
- Unauthorized attempts log a sanitized `user_id_hash` only.

## Global registry and diagnostics

Every emitted component is registered in `app/health/registry.py` with:

- stable internal `component_id` (logs/DB only)
- Ukrainian user-facing label
- product area, criticality, impacts, fail-safe, admin action
- alert / recovery policy and normalized reason codes
- UI group

Telegram/UI **never** shows raw component IDs. Unregistered IDs render as
«Невідомий компонент».

Hard-failure admin alerts use structured Ukrainian sections:
`Що сталося`, `Вплив`, `Безпека`, `Підтверджено`, `Початок`, `Дія`.

## Status values

`HEALTHY` | `DEGRADED` | `DOWN` | `DISABLED` | `WARMING_UP` | `UNKNOWN`

| Status | Meaning | Telegram |
|--------|---------|----------|
| HEALTHY | Normal | Recovery only if a DOWN was previously notified |
| DEGRADED | Reduced but usable | **UI only** (no Telegram by default) |
| DOWN | Materially unavailable / unsafe | After threshold (or immediate hard safety) |
| DISABLED | Intentionally off | Never (not a failure) |
| WARMING_UP | Fair first-check pending | Never |
| UNKNOWN | Insufficient evidence | Never until a fair check |

## Criticality

- **hard_critical** — poller conflict, SQLite integrity/write unsafe, read-only violations
- **feature_critical** — primary discovery/weather/EIA/zero executable books/no usable venues
- **optional** — FRED, partial history, single optional provider

## Thresholds (defaults)

| Setting | Default | Meaning |
|---------|---------|---------|
| `HEALTH_FAILURE_THRESHOLD` | 3 | Consecutive failures → confirmed DOWN alert (feature-critical) |
| `HEALTH_RECOVERY_THRESHOLD` | 2 | Consecutive successes after failure → HEALTHY |
| `HEALTH_JOB_STALE_MULTIPLIER` | 2 | Job overdue if silent longer than interval × multiplier |
| `HEALTH_CHECK_INTERVAL_SECONDS` | 300 | `health_monitor_tick` period |

Immediate hard-down is reserved for verified safety invariants already supported
in code (e.g. read-only violation, poller conflict, SQLite hard fault).

## Overall aggregation

1. Critical DOWN (`telegram`, `sqlite`, `copy_readonly`) → overall DOWN.
2. Else any non-disabled DOWN/DEGRADED → overall DEGRADED (UI truthful).
3. Soft-only overall DEGRADED → **no** overall Telegram.
4. Prefer specific capability alerts over overall spam.
5. `WARMING_UP` / `UNKNOWN` / `DISABLED` do not drive false overall failures alone.

## opportunity_books

- Status from **own** book metrics only (no inherit from delivery/history/stale).
- Reduced but usable coverage → DEGRADED, admin UI only.
- Zero valid executable books after attempts → DOWN; Telegram after failure threshold.
- Incomplete/unverified books remain fail-closed for publication.

## MOI (Market Opportunity Intelligence)

- Optional FRED missing → DEGRADED, no Telegram.
- Warmup → no Telegram.
- Confirmed EIA RWTC failure → one alert after threshold; one recovery.

## Limitation: process death

A stopped bot process **cannot** report its own death. In-process poller
conflict/runtime diagnostics are in scope. External systemd/VPS watchdog
monitoring is **not** implemented by the bot and must be provided separately.

## Probe classification limitations (fail closed)

- **Weather:** no global module DISABLED flag and no labeled primary provider in
  metrics; `forecast_min_providers` is the primary capability floor. Optional
  provider HTTP errors while the floor is met → DEGRADED UI only.
- **Copy:** `baselines_incomplete` is the available aggregate proxy for
  fail-closed / incomplete_book wallets (no per-wallet IDs in Telegram).
- **Scheduler:** only `copy_intel_poll` and `health_monitor_tick` are currently
  interval-probed; copy poll is treated as feature-critical, health tick delay
  as soft DEGRADED. Opportunity scanner job liveness is covered via
  `opportunity_scheduler` metrics, not this probe.
- **Crypto:** exchange venue health is mapped when read-only is intact; read-only
  violations are owned by `crypto_readonly`.
- **storage_transitions:** internal watermark row in `component_states` only — not
  a System Health probe component and never rendered in Telegram.

## Telegram UX

Admin System Health groups:

- Core Runtime, Storage, Polymarket, Weather, Copy Intelligence,
  Crypto Intelligence, Market Opportunities, Delivery, External Sources

Storage destructive actions remain confirm-gated, dry-run by default, and
never expose absolute private paths.

## AI boundary

- Core status never depends on AI.
- `HEALTH_AI_EXPLAINER_ENABLED` defaults false.
- AI must never restart processes, edit `.env`, run git, expose secrets, change thresholds, or trade.
