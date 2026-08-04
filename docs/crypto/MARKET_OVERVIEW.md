# Market Overview (V3.0.0b)

Read-only Telegram leaf for the configured spot watchlist (`CRYPTO_SYMBOLS`).

## Displayed fields (successful rows)

- Canonical symbol, exchange, market type `spot`
- Best bid, best ask, midpoint `(bid + ask) / 2`
- Quote timestamp (venue or receive-time per V3.0.0a adapter rules)
- Age seconds and freshness `fresh`
- Exchange health must be `ok`

## Fail-closed

Reject a row when stale, missing timestamp (where required), malformed, unsupported symbol/market type, degraded health, missing/invalid bid-ask, or crossed book. Never invent values. 24h statistics are omitted in V3.0.0b.

## Callbacks

- Entry / refresh: `crypto:overview`
- Back: `crypto:menu`
