# Grill Me — V3.0.0a Crypto Intelligence (limited)

Scope under challenge: Exchanges & Health + Spread Monitor only.

## Challenges and locked answers

1. **Why not CCXT now?**
   Private trading surface + heavy transitive deps conflict with a minimal `requests`-only stack. **Lock:** no CCXT in V3.0.0a.

2. **Why spot-only?**
   Futures/perpetuals change fees, symbols, and liquidation semantics. **Lock:** unsupported market types fail closed.

3. **Why leave Overview and Arb as stubs?**
   Big-bang increases fail-closed and UX risk. **Lock:** wire `crypto:exchanges` and `crypto:spread` only.

4. **Why keep Crypto open (not admin-gated)?**
   Matches V2.2.4 shell and Weather/Opp public-analysis pattern. **Lock:** open auth; read-only enforced in settings/code.

5. **Why fail closed on missing fees/timestamps?**
   Indicative spreads without fees or freshness become misleading. **Lock:** reject; never invent numbers.

6. **Why not Market Research?**
   Separate product shell; out of Crypto V3.0.0. **Lock:** Research stubs unchanged.

7. **Transfer-required arb?**
   Out of this slice; when Arb lands later, transfer mode fails closed until costs exist. **Lock:** N/A for V3.0.0a.

8. **DB migration?**
   Not required for on-demand Telegram reads. **Lock:** no migration in V3.0.0a.
