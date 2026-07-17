# Agent working rules (Trading Intelligence)

## Branch and scope

- One task per branch (or one clearly bounded task per session).
- Do not mix Weather Intelligence, Copy Intelligence, and Health Monitor changes unless the task explicitly requires a shared hook.
- Prefer minimal verified patches over broad refactors.

## Tests

- Run focused tests for the changed module.
- After cross-cutting changes, also run regressions for Weather, Copy Intelligence, Telegram UX, and scheduler jobs.
- Unit tests alone are not enough for product paths; use a bounded read-only live proof when behavior is user-visible.

## Secrets and environment

- Never modify or expose the real `.env`.
- Never commit credentials, tokens, local databases, logs, caches, or screenshots.
- Health and status UIs must sanitize tokens, API keys, wallet secrets, and unsafe paths.

## Telegram process safety

- Never start a second Telegram polling process against the same bot token.
- Do not start, stop, or restart the bot unless the user explicitly asks.
- JobQueue registrations must be idempotent (`*_REGISTERED_KEY` guards).

## Git

- No commit or push before manual verification unless the user explicitly requests it.
- Review `git diff` and scan for secrets before any commit.

## Module boundaries

| Module | May do | Must not do |
|--------|--------|-------------|
| Weather Intelligence | Forecasts, scans, signal formatting | Place trades, own Copy alerts |
| Copy Intelligence | Read-only discovery, watchlist, paper alerts | Sign trades, disable read-only, alert baseline history |
| Health Monitor | Deterministic probes, admin diagnostics, incidents | Depend on AI for status, edit `.env`, restart processes, trade |

## Copy freshness

- New/re-enabled wallets require a successful baseline before any COPY/WATCH/SKIP/Telegram alerts.
- Re-enable always starts a new baseline (safest).
- Deduplicate by stable event UID; do not rely on timestamps alone.

## Health

- System Health UI and `/health` are admin-only (`ADMIN_IDS`).
- Core health status is deterministic; AI explainer (if ever enabled) is optional and read-only on sanitized data.
