# Crypto Intelligence Overview

Read-only public market-data product under the V2.2.4 Product Menu.

## Live leaves

| Telegram button | Callback | Version | Behavior |
|-----------------|----------|---------|----------|
| Market Overview | `crypto:overview` | V3.0.0b | Spot watchlist bid/ask/mid/age per healthy venue |
| Exchanges & Health | `crypto:exchanges` | V3.0.0a | Per-exchange health from public endpoints |
| Spread Monitor | `crypto:spread` | V3.0.0a | Best bid/ask, spread %, freshness per venue |

## Still placeholders

- Arbitrage Opportunities (`crypto:arb`) — V3.0.0c
- Entire Market Research hub (`research:*`)

## Safety

- Public REST only; no trading API keys.
- `CRYPTO_INTEL_ENABLED` defaults false; `CRYPTO_INTEL_READ_ONLY` defaults true.
- No order placement, withdrawals, transfers, or wallet access.
- Stale/missing data fails closed (no invented quotes).
- 24h statistics omitted until authoritative adapter fields exist.
