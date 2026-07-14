# EV Engine — Probability, Edge, EV, Kelly, and Recommendations

Pure mathematical engine for prediction-market analysis. This module is **isolated** from weather, Polymarket API, AI prompts, and trading execution.

Implementation: `app/ev/`

---

## Design Principles

1. **Pure functions** — no I/O, no side effects, no external APIs.
2. **Typed inputs and outputs** — validate at boundaries; raise explicit exceptions on invalid input.
3. **Risk policy as constants** — thresholds and bankroll caps live in `app/ev/policy.py`, aligned with `prompts/risk_policy.md`.
4. **Engine decides math only** — recommendation tiers are deterministic from EV, edge, confidence, and policy caps.

---

## Module Layout

```
app/ev/
  __init__.py       — public API exports
  exceptions.py     — validation errors
  types.py          — enums and result dataclasses
  policy.py         — EV thresholds and bankroll caps
  calculations.py   — probability, edge, EV, Kelly
  tier.py           — recommendation tier logic
```

---

## Formulas

### 1. Prediction market price → implied probability

For a YES share priced in decimal form (0–1):

```
implied_probability = price
```

Function: `prediction_market_price_to_probability(price)`

Validation: `0 <= price <= 1`

---

### 2. Decimal odds → implied probability

```
implied_probability = 1 / decimal_odds
```

Function: `decimal_odds_to_probability(decimal_odds)`

Validation: `decimal_odds > 1`

---

### 3. Edge

```
edge = model_probability - market_probability
```

Function: `calculate_edge(model_probability, market_probability)`

Both probabilities must be in `[0, 1]`.

Positive edge means the model estimates a higher win probability than the market implies.

---

### 4. Binary expected value (YES share)

Buying one YES share at `market_price` (cost per share):

| Outcome | Payout | Profit/Loss |
|---------|--------|-------------|
| Correct | $1 | `1 - market_price` |
| Incorrect | $0 | `-market_price` |

```
EV per share = model_probability * (1 - market_price)
             - (1 - model_probability) * market_price
```

Algebraically equivalent to:

```
EV per share = model_probability - market_price = edge
```

Function: `calculate_binary_ev(model_probability, market_price)`

Validation: `0 <= market_price <= 1`, `0 <= model_probability <= 1`

**EV percentage (for tier thresholds):** `ev_pct = ev * 100`  
Thresholds in `policy.py` use the decimal EV value (e.g. `0.03` = 3%).

---

### 5. Fractional Kelly

Full Kelly fraction for a binary YES contract at price `c` and model probability `p`:

```
full_kelly = max(0, (p - c) / (1 - c))
```

Fractional Kelly:

```
position_fraction = full_kelly * fraction
```

Then cap through risk policy:

```
position_fraction = min(position_fraction, max_bankroll_fraction)
```

Function: `calculate_fractional_kelly(model_probability, market_price, fraction, *, max_bankroll_fraction=None)`

Validation:

- `0 <= fraction <= 1`
- Never returns a negative position fraction
- When `market_price == 1`, full Kelly is undefined; returns `0.0`
- When `max_bankroll_fraction` is omitted, uses policy default (5%)

---

## Risk Policy Constants (`app/ev/policy.py`)

Aligned with `prompts/risk_policy.md`:

| Constant | Value | Meaning |
|----------|-------|---------|
| `EV_THRESHOLD_WATCH` | 0.03 | Below 3% EV → WATCH (maps prompt "AVOID") |
| `EV_THRESHOLD_SMALL_BET` | 0.03 | 3% minimum for SMALL BET |
| `EV_THRESHOLD_NORMAL_BET` | 0.07 | 7% minimum for NORMAL BET |
| `EV_THRESHOLD_STRONG_OPPORTUNITY` | 0.15 | 15% minimum for STRONG OPPORTUNITY |
| `MAX_BANKROLL_DEFAULT` | 0.05 | 5% max per market |
| `MAX_BANKROLL_LOW_CONFIDENCE` | 0.01 | 1% max when confidence is LOW |
| `MAX_BANKROLL_WEATHER` | 0.02 | 2% max for weather markets |

---

## Recommendation Tiers

| Tier | Typical conditions |
|------|-------------------|
| **SKIP** | Invalid inputs or insufficient data to evaluate |
| **WATCH** | EV below 3%, negative EV, or downgraded from a bet tier due to low confidence |
| **SMALL BET** | EV 3–7%, adequate confidence |
| **NORMAL BET** | EV 7–15%, medium or high confidence |
| **STRONG OPPORTUNITY** | EV > 15%, high confidence |

Prompt "AVOID" (EV < 3%) maps to **WATCH** — no separate AVOID tier in code.

### Confidence adjustments

- **LOW** confidence: downgrade one tier (STRONG → NORMAL → SMALL → WATCH); Kelly cap at 1%.
- **MEDIUM** / **HIGH**: no tier downgrade from confidence alone.

### Tier determination flow

1. Validate inputs → invalid → **SKIP**
2. Compute implied probability, edge, EV
3. Base tier from EV thresholds
4. Apply confidence downgrade if LOW
5. Compute fractional Kelly with category-appropriate bankroll cap

Function: `determine_recommendation_tier(model_probability, market_price, confidence, *, market_category="default", kelly_fraction=0.25)`

---

## Break-Even Examples

| model_probability | market_price | edge | EV | full_kelly |
|-------------------|--------------|------|-----|------------|
| 0.50 | 0.50 | 0.00 | 0.00 | 0.00 |
| 0.60 | 0.50 | 0.10 | 0.10 | 0.20 |
| 0.40 | 0.50 | -0.10 | -0.10 | 0.00 |

---

## Exceptions

| Exception | Raised when |
|-----------|-------------|
| `InvalidProbabilityError` | Probability not in `[0, 1]` |
| `InvalidMarketPriceError` | Market price not in `[0, 1]` |
| `InvalidDecimalOddsError` | Decimal odds `<= 1` |
| `InvalidKellyFractionError` | Kelly fraction not in `[0, 1]` |

---

## Integration (future phase)

`app/signals/generator.py` and `app/signals/comparator.py` will call this engine in a separate pass. Until then, the weather signal path continues using its existing inline edge logic.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

Test modules:

- `tests/test_ev_calculations.py` — probability, edge, EV, Kelly
- `tests/test_ev_tier.py` — recommendation tiers and policy caps
