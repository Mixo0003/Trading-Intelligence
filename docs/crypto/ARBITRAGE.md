# New Opportunities / Spot Paper Arbitrage

Primary V4.3 Market Intelligence leaf (`crypto:arb`, button **New Opportunities**).

## V4.3 — spot cross-exchange paper arbitrage

Read-only / paper-only. Surfaces executable **net** paper opportunities from
public L2 books when the scanner is enabled.

### Scope

- Market type: **spot** only
- Strategy: buy low / sell high across venues (paper allocation)
- V4.3 venues: **Binance, BingX, MEXC, OKX, KuCoin**
- **Bybit excluded** (legacy Crypto V3 only)
- Each card shows the **actual buy and sell venues** used for that opportunity
- Estimated fees, slippage, executable quantity, net profit, and ROI when the
  engine provides them
- Stale, rejected, incomplete, or non-executable rows are not shown

### Defaults

- `CRYPTO_OPPORTUNITY_SCANNER_ENABLED=false`
- `CRYPTO_OPPORTUNITY_DELIVERY_ENABLED=false`
- No automatic delivery from Filters or New Opportunities UI

### Non-claims

Not LONG/SHORT, TP/SL, news signals. Not real trading. Not guaranteed profit.

V4.5–V4.7 foundation contracts (basis/funding, directional, news overlays)
exist in-code for normalization and fail-closed validation only. They do
**not** change New Opportunities cards or enable futures/funding/directional/
news execution. News alone never creates a trading signal.

### Callbacks

- Entry / refresh: `crypto:arb`
- Hub back: `crypto:menu` (Market Intelligence)

## Legacy note — V3.0.0c gross gap

Older V3.0.0c documentation described a **gross** top-of-book gap across six
venues (including Bybit) without fee/slippage netting. That gross-only semantics
is superseded for the guest New Opportunities path by the V4.3 net paper scanner.
Legacy overview/spread/exchanges callbacks remain for compatibility.
