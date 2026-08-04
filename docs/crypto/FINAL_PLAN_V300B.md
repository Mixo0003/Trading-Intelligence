# Final Plan — V3.0.0b Market Overview

Branch: `feature/v3.0.0b-crypto-market-overview`
Base: `origin/main` @ merge PR #13 (V3.0.0a)

## Deliverable

- `app/crypto/overview.py` orchestrator over existing public adapters
- Wire `crypto:overview` live (Refresh + Back)
- Spot-only fixed watchlist; midpoint from bid/ask
- Keep `crypto:arb` stub; no new dependencies; no DB migration

## Out of scope

Arbitrage, 24h stats, new exchanges, CCXT, trading keys, Market Research.
