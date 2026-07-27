# Market Opportunity Scanner (V2.2.3)

Technical contract for the read-only / paper-only Market Opportunity Scanner on
branch `feature/v2.2.3-market-opportunity-scanner`.

**Approved status (Checkpoint 8 release preparation):**

- **Implementation and Checkpoint 1–7 local commits complete.**
- **Automated verification (2026-07-27): full suite 1213 passed, 2 skipped, 1 warning; temp-DB migration PASS.**
- **Production migration: not performed.**
- **Live Telegram verification: pending.**
- **Genuine automatic opportunity delivery: not proven.**
- **User-delivery live proof: not completed.**

This is **not** production GO, **not** live-verified, and **not released**.

## Product purpose

Discover active Polymarket markets from **public** Gamma / CLOB data, score
executable momentum under fail-closed rules, surface Top Opportunities in
Telegram, and (when explicitly enabled) deliver same-wave admin/user paper
alerts. The module never places trades, never signs orders, and never uses
authenticated trading endpoints.

## User-visible value

| Surface | Behavior |
|---------|----------|
| Opportunities menu | Entry point for Top Opportunities, settings, live mode, scanner status |
| Top Opportunities | Deterministic ranked Market Opportunity cards from stored active signals |
| Market Opportunity cards | Score, direction, mode, reason codes, market links; no fabricated fair value |
| Editable settings | Preview → confirm/cancel; revision-protected; Reset to Balanced |
| Live / in-play | Normal live mode vs STRICT live (harder gates + second book confirm) |
| Automatic delivery | Architecture for same-wave PENDING deliveries; defaults keep auto/user delivery off |

## Safety boundary

- Public Data API / public CLOB book and price-history reads only.
- Read-only and paper-only.
- No wallet connection, no order placement, no authenticated CLOB trading.
- Runtime defaults (must stay false until explicit controlled enablement):
  - `OPPORTUNITY_SCANNER_ENABLED=false`
  - `OPPORTUNITY_AUTO_ALERTS_ENABLED=false`
  - `OPPORTUNITY_USER_DELIVERY_ENABLED=false`
- Health and Telegram surfaces must not expose raw URLs with secrets, asset
  dumps, absolute private paths, tokens, or API payloads.

## Complete discovery and fail-closed guarantees

- Gamma discovery must complete within the per-cycle Gamma request budget
  (maximum **10** actual HTTP attempts). Incomplete discovery fails closed.
- CLOB work shares one per-cycle budget of maximum **50** actual HTTP attempts
  covering `/prices-history`, `/book`, STRICT confirmation books, and retries.
- Cache-only paths do not consume the HTTP budget.
- Exhausted budget → affected assets fail closed with bounded diagnostics
  (`CLOB_BUDGET_EXHAUSTED` / `GAMMA_BUDGET_EXHAUSTED`); no silent overspend.
- Invalid books, stale quotes, malformed rows, and incomplete history fail
  closed rather than inventing midpoints or scores.

## Two-cycle baseline

- Per-asset baseline requires **two consecutive complete cycles** before
  signals may cross into delivery-eligible episodes (`BASELINE_VERSION = 1`).
- Markets already eligible when baseline becomes ready are suppressed for the
  initial post-baseline crossing (no fabricated “new” opportunity from history).
- Absence from complete discovery beyond the configured reset window resets
  baseline state with a diagnostic code.

## Canonical score version 1

- `SCORE_VERSION = 1` / `OPPORTUNITY_SCORE_VERSION = 1`.
- Score inputs are liquidity, volume, spread, slippage, and movement features
  under Normal or STRICT policy gates.
- Score v1 does **not** claim fair-value prediction, arbitrage, or guaranteed
  edge. Later Decimal-logarithmic scoring or an explicit reproducibility policy
  remains backlog.

## STRICT live confirmation

- STRICT mode uses harder liquidity/volume/spread/slippage/move thresholds.
- A second confirmed `/book` read (after a bounded delay) is required for
  STRICT live scoring; both reads consume the shared CLOB budget.
- Confirmation failure fails closed for that asset.

## Same-wave admin / user delivery

- Delivery waves select recipients from subscriptions and gates in one wave.
- Admin delivery gate and user delivery gate are separate; user delivery remains
  off by default (`OPPORTUNITY_USER_DELIVERY_ENABLED` and DB gate).
- Delivery statuses: PENDING → CLAIMED → SENT / RETRY_WAIT / DEAD / expired.
- Payload rows are immutable after insert (SQLite trigger).
- Automatic alerts require `OPPORTUNITY_AUTO_ALERTS_ENABLED` plus successful
  cycle finalization that is allowed to run selection.

## Runtime scheduler, deadline containment, budgets, health

| Concern | Behavior |
|---------|----------|
| Registration | `schedule_opportunity_jobs` is idempotent (`OPPORTUNITY_SCAN_REGISTERED_KEY`); disabled scanner registers nothing |
| Overlap | DB job lock + renew while worker active; in-process in-flight futures |
| Deadline | Configured deadline limits how long the scheduler awaits; timeout does **not** hard-cancel the thread |
| Late worker | In-flight guard and lock remain until the executor future completes; late completion suppresses wave selection and Telegram sends |
| Cooperative abort | Deadline token checked before/after request batches and before opening the SQLite commit |
| Budgets | Shared CLOB cap 50; separate Gamma cap 10; sanitized metrics only |
| Health | Deterministic opportunity probes mapped into System Health; admin-only |
| Shutdown | Cancel awaiting asyncio tasks; expire deadline token on non-cancellable executor work |

Chosen late-worker approach: **cooperative deadline + suppress downstream
selection/sends** after terminal timeout. Do not pretend the underlying thread
was cancelled.

## Migration and retention

- Additive migration marker: `opportunity_intel_v1` (`SCHEMA_VERSION = 1`).
- Creates opportunity tables, indexes, delivery-payload immutability trigger,
  gates, and runtime status foundations without rewriting legacy tables.
- Idempotent; injected failure rolls back new objects (verified on temp DBs).
- Retention policies register opportunity metric/signal/delivery/edit datasets;
  real prune remains disabled / dry-run by default.
- No automatic VACUUM or `journal_mode` changes from this module.

## Telegram callback namespace

Opportunity callbacks are routed through `handle_opportunity_callback` and use
the Opportunities menu / settings keyboards. They must not collide with Copy,
Health, or Weather callback namespaces. Registration does not start a second
Telegram poller; scheduling attaches JobQueue work to the existing application.

## What this version does not include

- WebSocket streaming, reconnect, replay, or gap recovery
- Sub-minute guaranteed live scanning
- True short-window volume-spike reconstruction or trade-count reconstruction
- Digest mode; Useful / Not useful feedback; saved watchlists
- Free/Pro entitlements or payments
- External forecasts, fair-value models, AI/news explanations
- Arbitrage Intelligence or cross-market opportunities
- Outcome-performance / scoring calibration from live data
- Real trading

## Live verification status

**Pending.** Controlled production migration and genuine live Telegram proof
have not been executed as part of Checkpoint 8.

Automated evidence recorded here (2026-07-27):

- Temp-DB migration proof PASS (`opportunity_intel_v1`).
- Full suite: **1213 passed, 2 skipped, 1 warning** in 977.58s.

Do not claim production GO, genuine automatic delivery, or user-delivery live
proof until those gates pass under an explicit operator prompt.

## Related modules

| Area | Path |
|------|------|
| Models / settings | `app/opportunity_intel/models.py`, `settings.py`, `versions.py` |
| Discovery / history / books | `discovery.py`, `history.py`, `book.py` |
| Scoring / episodes / scanner | `scoring.py`, `episodes.py`, `scanner.py`, `observation.py` |
| Runtime / deadline / budget | `runtime.py`, `deadline.py`, `request_budget.py`, `scheduler.py` |
| Delivery | `delivery_worker.py`, storage delivery APIs |
| Health / status | `health.py`, `status_view.py` |
| Storage | `app/database/opportunity_storage.py` |
| Telegram UX | `app/telegram/opportunity_intel/*`, inline menus |
| Retention | `app/storage/retention_policies.py` |
| Config flags | `app/config/settings.py` (`OPPORTUNITY_*`) |
