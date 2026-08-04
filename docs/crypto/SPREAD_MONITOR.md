# Spread Monitor (V3.0.0a)

## Inputs

For each configured exchange and allowlisted spot symbol:

- Best bid / best ask from public book ticker (or top of book)
- Exchange timestamp (or receive time when venue omits timestamp — still age-checked)
- Fee schedule presence (display may note fees missing for arb later; spread % is top-of-book)

## Outputs

- Symbol (canonical `BASE/QUOTE`)
- Exchange id
- Best bid, best ask
- Spread percentage: `(ask - bid) / mid * 100` when mid > 0
- Timestamp and freshness status (`fresh` / `stale` / `rejected`)

## Fail-closed

Reject a venue row when:

- Timestamp missing or quote older than `CRYPTO_MAX_QUOTE_AGE_SECONDS`
- Bid/ask missing, non-positive, or ask < bid
- Unsupported market type (non-spot)
- Symbol cannot be normalized to the allowlist
- Exchange health is degraded / unsupported
