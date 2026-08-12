# Market Intelligence / Crypto Overview

Guest-facing product surface for read-only public crypto market data and
**V4.3 spot cross-exchange paper arbitrage**. Internal package and callbacks
remain `app/crypto` / `crypto:*` (label rename only).

## Main menu (V4.3)

| Audience | Buttons |
|----------|---------|
| Guest | Polymarket · Market Intelligence · Settings |
| Admin | Polymarket · Market Intelligence · Settings · System Health |

Market Research and Market Opportunities are **not** on the main keyboard.
MOI/WTI (`moi:*`) handlers remain compatible for stale clients and admins who
deep-link; they are not the main guest product.

## Market Intelligence hub

| Telegram button | Callback | Behavior |
|-----------------|----------|----------|
| New Opportunities | `crypto:arb` | Spot cross-exchange **paper** arbitrage (see `docs/crypto/ARBITRAGE.md`) |
| Filters | `crypto:filters` | Per-user paper scanner filters (presets + custom numeric) |
| Opportunity History | `crypto:history` | Delivery / last-sent rows for **this** `telegram_id` only |
| Back | `nav:main` | Main menu |

### Filters (V4.3)

- Product type displayed as fixed **Spot** (not editable).
- Editable: selected exchanges (V4.3 set only), paper allocation, min net profit,
  min ROI, max slippage, min executable depth, max signal age, cooldown.
- Preset buttons retained; each numeric field also has **Enter custom value**.
- Draft bound to `telegram_id`; Preview shows old → new; Confirm persists;
  Cancel discards with no DB write.
- Confirm reloads persisted settings, checks revision, rejects stale tokens
  fail-closed; never enables delivery.
- Bybit cannot be selected.
- Not exposed: `delivery_enabled`, risk preference, evidence requirements,
  env vars, raw JSON, diagnostics.

### Defaults

```
CRYPTO_INTEL_ENABLED=false
CRYPTO_INTEL_READ_ONLY=true
CRYPTO_OPPORTUNITY_SCANNER_ENABLED=false
CRYPTO_OPPORTUNITY_DELIVERY_ENABLED=false
```

## V4.3 venues

Binance, BingX, MEXC, OKX, KuCoin.

**Bybit** remains in legacy Crypto V3 adapters only and is excluded from the
V4.3 scanner universe.

## Legacy Crypto V3 leaves (compatibility)

Still importable / callable for stale Telegram clients; **not** on the V4.3 hub:

| Button (legacy) | Callback | Notes |
|-----------------|----------|-------|
| Market Overview | `crypto:overview` | V3.0.0b |
| Spread Monitor | `crypto:spread` | V3.0.0a |
| Exchanges & Health | `crypto:exchanges` | V3.0.0a (may still show Bybit health) |

Market Research hub (`research:*`) remains a placeholder.

## Safety / non-claims (V4.3)

- Public REST only; no trading API keys; no orders, withdrawals, transfers, wallets.
- Read-only / paper-only analysis; not guaranteed profit.
- No LONG/SHORT, TP/SL, perpetuals, dated futures, funding, basis, stocks, ETFs,
  indices, forex, commodities, or news-based signals in V4.3.
- Advanced arb / perps / LONG-SHORT / news remain **V4.4–V4.7 deferred**.
