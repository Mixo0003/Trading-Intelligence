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

## Safety / non-claims

- Public REST only; no trading API keys; no orders, withdrawals, transfers, wallets.
- Read-only / paper-only analysis; not guaranteed profit.
- Guest New Opportunities remain **spot paper arb** only (V4.3/V4.4).
- V4.5 adds **foundation contracts** for perpetual/dated futures, basis, and funding
  observations. Those product families stay **unsupported** in the venue registry
  until verified adapters and approved fee/formula policies exist.
- V4.6 adds **directional foundation contracts** (LONG/SHORT + strategy/evidence
  labels). Signal formulas, TP/SL, ranking, and confidence remain unsupported
  until approved policies exist.
- V4.7 adds **news/fundamental overlay contracts** on existing opportunities
  only. News alone never creates a trading signal; no separate News button.
  Source registry / freshness / ranking remain fail-closed until approved.
- V4.8 adds **unified scoring/calibration contracts** across strategies.
  Scores are not probabilities; weights/thresholds/ranking remain unsupported
  until approved policies exist. Outcomes are immutable once recorded.
- V4.9 adds an **authoritative source registry** and verified EIA/FRED ingest
  for news/fundamental overlays on existing opportunities only. Unsupported
  sources fail closed; news alone never creates a trading signal.
- V4.10 adds **scoring policy contracts** and deterministic ranking **only**
  when a complete approved ``ScoringPolicy`` is supplied. No baked-in default
  weights/thresholds; confidence labels are not probabilities.
