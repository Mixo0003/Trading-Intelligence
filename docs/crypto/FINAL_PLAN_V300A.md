# Final Plan — V3.0.0a Crypto Intelligence

Branch: `feature/v3.0.0-crypto-spread-monitor`

## Deliverable

Read-only, paper-only Crypto Intelligence slice:

- Package `app/crypto/` with public REST exchange adapters (no CCXT).
- Telegram `app/telegram/crypto/` for `crypto:exchanges` and `crypto:spread`.
- Settings: `CRYPTO_INTEL_ENABLED` (default false), `CRYPTO_INTEL_READ_ONLY` (default true).
- Spot-only allowlisted symbols (BTC/USDT, ETH/USDT).
- Fail-closed freshness, fees, and book validation.
- Market Overview + Arbitrage remain placeholders.
- Market Research unchanged.

## Out of scope

- Live trading, keys, withdrawals, transfers, CCXT, DB migrations, arb scanner, market overview, Research product.

## Verification

- Synthetic fixture unit tests (no live exchange calls in CI).
- V2.2.4 product menu regression.
- Live Telegram proof only when explicitly requested (single existing poller).
