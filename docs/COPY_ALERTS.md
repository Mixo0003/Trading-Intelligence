# Copy Alerts — Trustworthy New/Increase (V2.2.2)

Contract for deliverable Copy Intelligence alerts on branch `feature/v2.2.2-trustworthy-copy-alerts`.

**Status (2026-07-23):** implementation and automated verification complete (full suite **949 passed, 2 skipped**); **live Telegram verification still pending**; **not released**.

## Supported events

| Internal `event_type` | User-facing label | In V2.2.2 scope |
|-----------------------|-------------------|-----------------|
| `open` | New Position | Yes |
| `increase` | Increase | Yes |
| `reduce` | — | Deferred |
| `close` | — | Deferred |

Reduce and Close must not produce FOLLOW/WATCH delivery in V2.2.2.

## Authoritative correlation

Deliverable New/Increase alerts require bounded trade correlation against documented `/trades` fills:

- documented transaction-hash identity only (never undocumented `id` / `tradeId`);
- authoritative `first_fill_ts` / `last_fill_ts` from fills;
- correlated event UID: SHA-256 digest prefixed `ff1:` (`build_correlated_event_uid`);
- poll clock / `detected_at` must never be shown as trade time.

Missing, incomplete, or ambiguous correlation → **quarantine** (no alert).

## Quarantine and diagnostics

Fail-closed categories (sanitized diagnostics only; no raw API bodies, stack traces, or absolute paths):

- incomplete / inconsistent position book reads;
- incomplete trade window / correlation failure;
- missing authoritative timestamps;
- malformed API rows / wrong types / timeouts;
- invalid market slug or wallet for required links;
- alert formatting failure.

Quarantined events are marked processed where appropriate so they do not reappear as fresh opportunities without a new correlated signal.

## Event age

- Signal age on the card is derived from authoritative **`last_fill_ts`**, not `detected_at`.
- Hard / soft max-age gates still respect per-user `max_event_age_seconds` (see `docs/COPY_SETTINGS.md`).
- Baseline V3 must complete before any live alerts (`COPY_BASELINE_VERSION = 3`).

## Price labels (must stay distinct)

| Label | Meaning |
|-------|---------|
| Trader VWAP | Fill-weighted trader price from correlated trades |
| Market midpoint | Midpoint from the **same** validated CLOB book snapshot |
| Executable best ask | Top-of-book ask from that snapshot |
| Paper execution VWAP | Allocation walk of the ask ladder (paper sizing) |

Never substitute position `avgPrice` for Trader VWAP, midpoint for execution price, or best ask for execution VWAP.

## Allocation and bankroll share

- **Paper allocation** — proposed USD notional for the paper path.
- **Paper bankroll share** — `proposed_allocation / configured_paper_bankroll` only.
- Never display a trader-bankroll estimate on the alert card.

## Order-book freshness and slippage

- One validated `/book` snapshot per recommendation; no midpoint refetch; no refetch-on-stale for metrics.
- **Slippage** = `(execution_vwap - midpoint) / midpoint` (allocation-based). Never heuristic bid–ask spread as slippage.
- **Visible ask depth** — USD depth used for liquidity / sizing checks from that snapshot.

## Decision matrix

| Decision | Meaning | Paper row | Telegram |
|----------|---------|-----------|----------|
| COPY | Meets FOLLOW thresholds | Required before delivery | Shown as **FOLLOW** |
| WATCH | Soft fail (e.g. slippage/score/metrics) | None | Shown as **WATCH**; must state no paper position opened |
| SKIP | Hard reject / out of scope | None | Never rendered or delivered |

## Delivery filters

- `FOLLOW_ONLY` — deliver FOLLOW (COPY) only.
- `FOLLOW_AND_WATCH` — deliver FOLLOW and eligible WATCH.
- Owner-admin routing only: recipient is watchlist `added_by`, must remain in `ADMIN_IDS`. No fan-out to all admins. Cross-admin isolation required.
- Multi-user / non-admin subscriptions: **deferred**.

## Paper commit ordering

For COPY:

1. Build recommendation with execution VWAP.
2. Atomic paper commit (`source_event_uid` exactly-once).
3. Format plaintext card with validated links.
4. Claim delivery row → send → mark sent (or retry / dead-letter).

Entry price is **execution VWAP only**. Missing/invalid VWAP fails closed. **No 0.5 fallback.**

WATCH never opens paper.

## Telegram card

- Plaintext only (no Markdown/HTML parse mode).
- No inline buttons, no callback data, no mutating actions.
- One alert per message (≤ 4096 characters).
- Validated links only:
  - Market: `https://polymarket.com/event/{slug}` (reject empty, `/ ? #`, traversal, controls, absolute URLs)
  - Trader: `https://polymarket.com/profile/{0x+40hex}`
- Invalid required link → fail closed before claim/send.

## Retry / dead-letter

- Claim → send; failures enter retry_wait (~60s / ~300s) then dead_letter after exhaustion.
- Retries reuse stored deterministic `payload_text`.
- Ownership rechecked before send; revoked/unbound recipients are suppressed.

## Storage / migration notes

- Additive `copy_position_snapshots_v2` (asset-keyed); legacy `copy_position_snapshots` retained.
- Migration is transactional and idempotent; rollback tests cover failpoints.
- Production migration must not run until a verified backup gate in a separately approved live session.
- Destructive cleanup / prune is **not** part of V2.2.2.

## Deferred scope

- Reduce / Close alerts
- Digest mode
- Inline Open Market / Open Trader / paper / mute / pause buttons
- Non-admin subscriptions
- Authenticated CLOB / real trading

## Rollback / live-verification prerequisites

Before live verification:

1. Verified production DB backup + integrity check.
2. Confirm single Telegram poller (no second process).
3. Controlled restart only if separately authorized.
4. Successful additive migration after backup gate.
5. V3 baseline-only cycle with **zero historical alerts**.
6. Observe a genuine New Position or Increase (cannot be fabricated).
7. Verify fills, timestamps, VWAP, single book snapshot metrics, owner-only delivery, exactly one paper row for COPY, no duplicate on next poll, WATCH creates no paper row, sanitized logs/request counters.

Do not mark V2.2.2 released until live Telegram verification is GO.
