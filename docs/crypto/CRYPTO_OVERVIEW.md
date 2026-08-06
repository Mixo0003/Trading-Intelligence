# Crypto Intelligence Overview

Read-only public market-data product under the V2.2.4 Product Menu.

## Live leaves

| Telegram button | Callback | Version | Behavior |
|-----------------|----------|---------|----------|
| Market Overview | `crypto:overview` | V3.0.0b | Spot watchlist bid/ask/mid/age per healthy venue |
| Arbitrage Opportunities | `crypto:arb` | V3.0.0c | Best positive **gross** cross-venue gap per symbol |
| Exchanges & Health | `crypto:exchanges` | V3.0.0a | Per-exchange health from public endpoints |
| Spread Monitor | `crypto:spread` | V3.0.0a | Best bid/ask, spread %, freshness per venue |

## Still placeholders

- Entire Market Research hub (`research:*`)

## Development status

Further V3 Crypto Intelligence expansion is **paused** (not abandoned). V3.0.0a/b/c leaves remain intact. Unfinished V3 work (global universe, user exchange selection, L2 executable arb, listings, funding/basis/paper execution, 24h stats, net arb) is recorded on the V4 roadmap backlog. New priority: Market Opportunity Intelligence (`docs/MARKET_OPPORTUNITY_INTELLIGENCE.md`).

## Safety

- Public REST only; no trading API keys.
- `CRYPTO_INTEL_ENABLED` defaults false; `CRYPTO_INTEL_READ_ONLY` defaults true.
- No order placement, withdrawals, transfers, or wallet access.
- Stale/missing data fails closed (no invented quotes).
- Arbitrage is gross-only and never presented as guaranteed profit.
- 24h statistics omitted until authoritative adapter fields exist.
