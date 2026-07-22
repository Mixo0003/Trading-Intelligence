# Copy Settings (V2.2.1)

Per-user editable Copy Intelligence settings via Telegram.

## Defaults (Balanced)

| Setting | Default | Unit | Min | Max |
|---------|---------|------|-----|-----|
| min_resolved_markets | 20 | count | 3 | 500 |
| min_active_days | 30 | days | 1 | 3650 |
| max_drawdown_pct | 0.40 | fraction | 0.05 | 1.00 |
| max_single_win_contribution | 0.40 | fraction | 0.05 | 1.00 |
| max_category_concentration | 0.45 | fraction | 0.10 | 1.00 |
| min_liquidity_usd | 500 | USD | 0 | 1_000_000 |
| poll_interval_seconds | 120 | seconds | 60 | 3600 |
| alert_decisions | FOLLOW_AND_WATCH | enum | — | — |
| max_event_age_seconds | from `COPY_MAX_EVENT_AGE_SECONDS` (env default 600) | seconds | 30 | 86400 |

Profiles Conservative / Balanced / Exploratory remain available as full replacements.

## Migration

- Storage: `copy_user_settings.settings_json` (no table rebuild).
- Missing `max_event_age_seconds` is filled from global `settings.copy_max_event_age_seconds` on load.
- Rollback: ignore the JSON key and fall back to global age / poll behavior.

## Runtime

- **Poll skip:** single global JobQueue job; per-wallet skip when `now - last_polled_at < owner.poll_interval_seconds` (`added_by`; Balanced if missing). Cannot beat the global job floor.
- **Max event age:** hard filter uses `max(admin ages)`; delivery soft-filters per subscriber; `FOLLOW_ONLY` suppresses WATCH for that subscriber.
- **Liquidity:** wallet-owner `min_liquidity_usd` overrides recommendation liquidity gate.

## UX

Copy Intelligence → Copy Settings → Edit Settings → choose field → preset or type value → preview → confirm/cancel. Reset to Balanced uses the same preview/confirm pattern with stale-token protection.
