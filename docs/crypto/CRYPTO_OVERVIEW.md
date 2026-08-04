# Crypto Intelligence Overview (V3.0.0a)

Read-only public market-data product under the V2.2.4 Product Menu.

## Live in V3.0.0a

| Telegram button | Callback | Behavior |
|-----------------|----------|----------|
| Exchanges & Health | `crypto:exchanges` | Per-exchange health from public endpoints |
| Spread Monitor | `crypto:spread` | Best bid/ask, spread %, freshness per venue |

## Still placeholders

- Market Overview (`crypto:overview`)
- Arbitrage Opportunities (`crypto:arb`)
- Entire Market Research hub (`research:*`)

## Safety

- Public REST only; no trading API keys.
- `CRYPTO_INTEL_READ_ONLY=true` required when enabled.
- No order placement, withdrawals, transfers, or wallet access.
- Stale/missing data fails closed (no invented quotes).
