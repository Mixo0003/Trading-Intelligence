# Grill Me — V3.0.0b Market Overview (limited)

Scope: Market Overview leaf only. Arbitrage remains V3.0.0c.

## Challenges and locked answers

1. **Why a fixed CRYPTO_SYMBOLS watchlist?**
   Unlimited symbols explode public GET volume. **Lock:** configured allowlist only (default BTC/USDT, ETH/USDT).

2. **Why omit 24h stats?**
   Adapters lack authoritative parsed 24h fields; partial stats would invent. **Lock:** bid/ask/mid/age/health only.

3. **Why require exchange health OK before quoting?**
   Degraded venues produce misleading overview rows. **Lock:** fail closed per venue when health ≠ ok.

4. **Why reuse crypto:overview without a new refresh callback?**
   Preserves V2.2.4/V3.0.0a menu contract. **Lock:** Refresh reuses `crypto:overview`.

5. **Why keep Arb stubbed?**
   Separate fail-closed fee/slippage/transfer design. **Lock:** V3.0.0c only.
