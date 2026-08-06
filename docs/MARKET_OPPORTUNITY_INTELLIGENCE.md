# Market Opportunity Intelligence (V4.x)

Paper-only market opportunity product inside the **existing** Trading Intelligence Telegram bot.

**Not** a new bot, token, poller, repository, or deployment.

## V4.1 — Market Opportunity Foundation

**Status:** implementation + live Telegram verification complete on
`feature/v4.1-market-opportunity-foundation` (commit/PR pending operator prompt).

| Item | Value |
|------|--------|
| Package | `app/market_opportunity/` |
| Telegram hub | Market Opportunities |
| Callback namespace | `moi:*` |
| Access | Admin-only (`ADMIN_IDS`) |
| Schema marker | `market_opportunity_v1` |
| Live detectors | **None** |
| Auto alerts | **Disabled** (must stay false in V4.1) |

### Safety contract

- Every opportunity is paper-only.
- No real orders, trading API keys, wallet, balances, transfers, withdrawals.
- Missing, stale, contradictory, or unverifiable data fails closed.
- Confidence is **LOW / MEDIUM / HIGH** only (data completeness / history depth). Not an invented win-rate percent until V4.6 calibration.
- AI may explain evidence later; must never invent prices, levels, sources, timestamps, or confidence. **V4.1 has no AI path.**
- Never promise guaranteed profit.
- Exactly one logical Telegram poller (reuse existing bot).

### Distinct from Polymarket Opportunity Scanner

| | Polymarket Scanner (V2.2.3) | Market Opportunity Intelligence (V4.x) |
|--|----------------------------|----------------------------------------|
| Package | `app/opportunity_intel/` | `app/market_opportunity/` |
| Callbacks | `opp:` / `oset:` | `moi:` |
| Menu label | Opportunity Scanner | Market Opportunities |
| Domain | Polymarket executable momentum | Cross-asset paper ideas |

### Flags (safe defaults)

```
MARKET_OPPORTUNITY_ENABLED=false
MARKET_OPPORTUNITY_AUTO_ALERTS_ENABLED=false
MARKET_OPPORTUNITY_READ_ONLY=true
```

### Migration

Additive `initialize_market_opportunity_schema(settings)`.

Tables: `moi_schema_migrations`, `moi_opportunities`, `moi_evidence`, `moi_outcomes`, `moi_runtime_status`.

Production apply requires verified backup + integrity `ok` + operator confirmation. V4.1 production migration was applied under that gate; further applies are idempotent.

### Known pre-existing Copy health limitation (non-blocking)

System Health may report Overall DEGRADED because `copy_intel` is DOWN while `copy_readonly` is HEALTHY. Cause: repeated `incomplete_book` baselines for large enabled wallets. Copy jobs continue; those wallets remain fail-closed. Predates V4.1; not changed by this module.

### Roadmap

- **V4.2** — Gold + WTI live paper detectors
- **V4.3** — Silver + Natural Gas
- **V4.4** — Crypto & cross-market (V3 unfinished universe/L2/funding remain backlog)
- **V4.5** — Scoring & Top Opportunities
- **V4.6** — Paper outcomes & confidence calibration

### Crypto V3 pause

V3.0.0a/b/c remain intact. Further V3 expansion is **paused**, not abandoned. See `ROADMAP.md` backlog.
