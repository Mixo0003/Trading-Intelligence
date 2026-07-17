# Copy event freshness (V2.1)

Prevents historical trader activity from being alerted as new when a wallet is added or monitoring is re-enabled.

## Pipeline

Alerts still come from the position-diff poll path (`ingest_wallet` → recommendation). Freshness gates run **before** `build_recommendation`.

## Subscription lifecycle

On **add** (new or previously disabled) or **re-enable**:

1. Write `wallet_monitor_state` with `baseline_status=PENDING`.
2. Store `subscription_started_at` (UTC).
3. Clear alert watermarks for a new cycle.

On each poll for an enabled wallet:

1. If baseline is not `COMPLETE`, run baseline only (no alerts).
2. Baseline fetches positions, replaces snapshots, marks current book as seen (`processed_copy_events` with `is_baseline=1`).
3. Baseline failure → `FAILED` and retry next poll; **no alerts until COMPLETE**.
4. After COMPLETE, ingest diffs and pass events through `filter_alertable_events`.

## Re-enable semantics (safest)

Disabling stops polling and retains rows. **Re-enabling starts a new baseline** with a new `subscription_started_at`. Activity that occurred while disabled is treated as historical at re-enable time and is never alerted.

## Alertable event rules

An event is alertable only if all apply:

- Baseline is `COMPLETE`
- Stable `event_uid` not already in `processed_copy_events`
- If `event.ts` is set: `ts >= subscription_started_at - lag` and `ts >= watermark` (equal timestamps allowed when UID is new)
- Age ≤ `COPY_MAX_EVENT_AGE_SECONDS` (default 600)
- Market not marked resolved (when provided)
- Existing recommendation drift/liquidity rules still apply afterward

Position-diff events without native `ts`/`tx_hash` use `observed_at` (poll UTC) and composite UIDs (`pd:...` or `tx:...`).

## Storage

- `wallet_monitor_state`
- `wallet_event_watermarks`
- `processed_copy_events`
- `baseline_runs`

Historical/non-alertable events may be stored; they must never be presented as new copy opportunities.

## Config

```
COPY_MAX_EVENT_AGE_SECONDS=600
```

Clock/API lag tolerance is fixed at 120 seconds in `app/copy_intel/freshness.py`.
