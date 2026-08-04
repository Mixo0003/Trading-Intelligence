# Grill Me — V3.0.0c Arbitrage (limited)

1. **Why gross only?**
   Net needs authoritative fees, slippage, and transfer costs. **Lock:** gross display + disclaimer; no net estimate.

2. **Why best-per-symbol only?**
   Telegram length and call budget. **Lock:** one positive gross winner per watchlist symbol.

3. **Why require two healthy venues?**
   Single-exchange "arb" is not cross-exchange. **Lock:** `insufficient_exchanges` fail closed.

4. **Why reuse CRYPTO_SYMBOLS?**
   Same bounded watchlist as Overview/Spread. **Lock:** no unlimited symbols.
