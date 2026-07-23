# Copy event freshness (V2.1 + V2.2.2)

Prevents historical trader activity from being alerted as new when a wallet is added or monitoring is re-enabled. V2.2.2 tightens deliverable New/Increase alerts to require authoritative fill correlation.

## Pipeline

Alerts still come from the position-diff poll path (`ingest_wallet` → correlation → recommendation → paper commit for COPY → delivery). Freshness gates run **before** deliverable recommendations.

## Subscription lifecycle

On **add** (new or previously disabled) or **re-enable**:

1. Write `wallet_monitor_state` with `baseline_status=PENDING`.
2. Store `subscription_started_at` (UTC).
3. Clear alert watermarks for a new cycle.

On each poll for an enabled wallet:

1. If baseline is not `COMPLETE`, run baseline only (no alerts).
2. Baseline (V3 / `COPY_BASELINE_VERSION = 3`) fetches a validated asset-keyed position book, commits `copy_position_snapshots_v2`, and seeds processed UIDs for the current book (`is_baseline=1`).
3. Baseline failure → `FAILED` and retry next poll; **no alerts until COMPLETE**.
4. After COMPLETE, ingest diffs, correlate trades for open/increase, and pass events through `filter_alertable_events`.

## Re-enable semantics (safest)

Disabling stops polling and retains rows. **Re-enabling starts a new baseline** with a new `subscription_started_at`. Activity that occurred while disabled is treated as historical at re-enable time and is never alerted.

## Alertable event rules

An event is alertable only if all apply:

- Baseline is `COMPLETE` at version 3 (legacy COMPLETE without version is rebuilt)
- Stable `event_uid` not already in `processed_copy_events`
- For deliverable **open/increase**: authoritative fill correlation with documented tx hashes and fill timestamps (`ff1:` SHA-256 UIDs)
- Unsigned position-diff open/increase/reduce/close without native trade identity are **quarantined** (not delivered)
- If `event.ts` / fill ts is set: respects subscription start lag and watermark rules
- Age ≤ configured max (global / per-user `max_event_age_seconds`; see `docs/COPY_SETTINGS.md`)
- Market not marked resolved (when provided)
- Recommendation drift/liquidity/order-book rules still apply afterward

Position-diff events without authoritative fills must not become FOLLOW/WATCH cards in V2.2.2.

## Storage

- `wallet_monitor_state`
- `wallet_event_watermarks`
- `processed_copy_events`
- `baseline_runs`
- `copy_position_snapshots` (legacy, retained)
- `copy_position_snapshots_v2` (production asset-keyed book)

Historical/non-alertable events may be stored; they must never be presented as new copy opportunities.

## Config

```
COPY_MAX_EVENT_AGE_SECONDS=600
```

Clock/API lag tolerance is fixed at 120 seconds in `app/copy_intel/freshness.py`.

## See also

- `docs/COPY_ALERTS.md` — full V2.2.2 alert contract
- `docs/COPY_SETTINGS.md` — per-user age and alert-mode filters
