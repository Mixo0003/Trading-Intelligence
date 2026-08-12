# Agent working rules (Trading Intelligence)

## Branch and scope

- One task per branch (or one clearly bounded task per session).
- Do not mix Weather Intelligence, Copy Intelligence, and Health Monitor changes unless the task explicitly requires a shared hook.
- Prefer minimal verified patches over broad refactors.
- Use separate small version branches for V2.2.1, V2.2.2, V2.2.3, and later versioned work — never combine multiple V2.2.x scopes on one branch.
- V2.2.x work uses numbered versions (V2.2.1, V2.2.2, V2.2.3…), not “Phase”.

## Permanent workflow

1. Plan / Research
2. Grill Me
3. Final Plan
4. Build
5. Debug
6. Focused tests
7. Full suite
8. Live Telegram verification
9. Release prep
10. Commit and push
11. PR and merge

Do not skip stages. Limited Grill Me applies per feature from **V2.2.2 onward** (do not claim V2.2.1 completed a formal Grill Me without repository evidence). Implement only on small versioned branches.

## Tool decisions

- Cursor remains the primary development environment.
- ChatGPT Project holds strategy and master context.
- Codex Remote will first be tested read-only.
- Strix is deferred to an isolated VM or Docker environment.
- Graphify will be tested separately and must not enter bot runtime.

## Tests

- Run focused tests for the changed module.
- After cross-cutting changes, also run regressions for Weather, Copy Intelligence, Telegram UX, and scheduler jobs.
- Unit tests alone are not enough for product paths; use a bounded read-only live proof when behavior is user-visible.
- Remove temporary verification scripts before final status / documentation / PR preparation.

## Secrets and environment

- Never modify or expose the real `.env`.
- Never commit credentials, tokens, local databases, WAL/SHM files, backups, logs, caches, screenshots, or other runtime artifacts.
- Health and status UIs must sanitize tokens, API keys, wallet secrets, and unsafe paths.
- Never show absolute private filesystem paths in Telegram.

## Telegram process safety

- Exactly one logical Telegram bot instance during live verification.
- Parent/child Python processes may represent one logical bot; that is normal.
- Never start a second Telegram polling process against the same bot token.
- Do not start, stop, or restart the bot unless the user explicitly asks.
- JobQueue registrations must be idempotent (`*_REGISTERED_KEY` guards).
- Long Telegram operations must acknowledge promptly and continue work in the background (avoid blocking the update handler).

## Git

- No automatic commit or push during implementation or audits.
- No commit or push before manual verification unless the user explicitly requests it.
- No commit or push without an explicit release prompt.
- Review `git diff` and scan for secrets before any commit.

## Trading safety

- No real trading unless explicitly approved.
- Copy Intelligence remains read-only and paper-only unless explicitly redesigned.
- V2.2.2 Copy alerts use public Data API / public CLOB book reads only — no authenticated order placement.

## Storage safety (V2.1.1)

- Real cleanup and retention pruning remain disabled and dry-run by default.
- Destructive cleanup or pruning requires explicit admin confirmation and all service-layer safety gates (locks, age/budget limits, verified-backup gate for prune).
- No automatic VACUUM or `journal_mode` changes.
- Backup creation is explicit and admin-confirmed; never invent or commit backup artifacts.

## Module boundaries

| Module | May do | Must not do |
|--------|--------|-------------|
| Weather Intelligence | Forecasts, scans, signal formatting | Place trades, own Copy alerts |
| Copy Intelligence | Read-only discovery, watchlist, paper alerts | Sign trades, disable read-only, alert baseline history |
| Market Intelligence (V4.3–V4.7; internal `crypto:*`) | Public market-data reads; spot cross-exchange **paper** arb; catalog-driven spot universe; V4.5–V4.6 foundation contracts; V4.7 news/fundamental overlays on existing opportunities only (fail-closed); per-user Filters/History | Trading keys, orders, withdrawals, transfers, wallet access; enable delivery/scanner without approval; invent fee/funding/directional/news formulas, TTLs, TP/SL, ranking, or confidence; create signals from news alone; claim complete futures/funding/directional/news engines |
| Crypto V3 legacy leaves | Overview / spread / exchanges (incl. Bybit health) for stale clients | Replace V4.3 Market Intelligence hub; put Bybit in V4.3 scanner universe |
| Market Opportunity Intelligence (V4.x MOI) | Paper-only opportunity model, WTI detectors when enabled, admin `moi:*` shell | Auto-alerts by default; invent prices/confidence %; present MOI as the main guest product after V4.3 |
| Health Monitor | Deterministic probes, admin diagnostics, incidents | Depend on AI for status, edit `.env`, restart processes, trade |
| Storage | Metrics, explicit-path cleanup, online backup, gated prune | Bypass dry-run/disabled defaults, commit runtime data, expose absolute paths |

## Version naming (V4)

- Use **V4.1**, **V4.2**, **V4.3**… — never letter suffixes (no V4.0.0a).
- Market Opportunity Intelligence is a sibling of Polymarket Opportunity Scanner (`opp:`), not an extension of `app/opportunity_intel`.

## Copy freshness

- New/re-enabled wallets require a successful baseline before any COPY/WATCH/SKIP/Telegram alerts.
- Re-enable always starts a new baseline (safest).
- Deduplicate by stable event UID; do not rely on timestamps alone.
- V2.2.2 deliverable New/Increase alerts require authoritative fill correlation timestamps; unsigned position churn is quarantined.

## Health

- System Health UI and `/health` are admin-only (`ADMIN_IDS`).
- Every storage callback re-checks admin authorization.
- Core health status is deterministic; AI explainer (if ever enabled) is optional and read-only on sanitized data.
