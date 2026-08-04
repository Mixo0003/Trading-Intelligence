# Arbitrage Opportunities (V3.0.0c)

Read-only Telegram leaf that surfaces the **best positive gross** cross-exchange top-of-book price gap per configured spot symbol.

## Scope

- Watchlist: `CRYPTO_SYMBOLS` only (default BTC/USDT, ETH/USDT)
- Market type: **spot** only
- Venues: existing six public adapters
- Buy side: best **ask**; sell side: best **bid**
- At most one opportunity row per symbol (highest gross %)

## Displayed fields

- Symbol, buy exchange + ask, sell exchange + bid
- Absolute gross spread and gross spread %
- Buy/sell exchange timestamps, receive timestamp, age, health

## Semantics

Every result is labeled:

> Gross price difference before fees, slippage, transfer costs, latency, and execution risk. Not guaranteed profit and not an executable arbitrage instruction.

Net profit is **not** estimated in V3.0.0c (fees/slippage/transfer not applied).

## Fail-closed

Reject when bid/ask missing or non-positive, crossed book, stale/missing timestamps, degraded health, uncertain symbol/market type, malformed data, fewer than two valid exchanges, or no positive gross gap.

## Callbacks

- Entry / refresh: `crypto:arb`
- Back: `crypto:menu`
