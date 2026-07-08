# Market Analysis Strategy

Standard workflow for evaluating any prediction market opportunity.

---

## Workflow

### 1. Identify market type

Classify the market before applying domain rules:

- **Weather** — temperature, precipitation, hurricane, climate threshold
- **Sports** — match outcome, props, futures, live events
- **Politics / macro** — elections, policy, economic releases
- **Crypto / finance** — price thresholds, ETF, protocol events
- **Other** — entertainment, science, custom events

Route to the appropriate domain prompt (`weather.md`, `sports.md`, etc.) after completing common steps.

### 2. Read title, description, and rules

- Read the full market **title**, **description**, and **rules** text.
- Do not infer resolution criteria from the title alone.
- Note ambiguities, conditional clauses, and edge cases in the rules.

### 3. Extract resolution source

- Identify the **official resolution source** named in the market (e.g., Weather Underground, NWS/weather.gov, league official site, AP/Reuters).
- Extract URLs, station codes, data fields, and measurement windows if specified.
- If resolution source or criteria are missing or unclear → **SKIP** with reason.

### 4. Extract current market price

- Record current **YES** and **NO** prices (or all outcome prices for multi-outcome markets).
- Note bid/ask spread, liquidity, and time to resolution if available.
- Use the price relevant to the side under evaluation.

### 5. Convert price to implied probability

For binary markets:

```
implied_probability = price  (when price is in 0–1 decimal form)
implied_probability = price / 100  (when price is in cents)
```

Account for fees, spread, and partial fills when discussing practical EV.

### 6. Estimate real probability from verified data

- Pull data only from verified sources aligned with resolution rules.
- Build a model probability for the relevant outcome.
- Document every input: source, timestamp, and confidence in the input.
- Separate **verified** inputs from **estimated** inputs.

### 7. Calculate EV and edge

```
edge = model_probability − implied_probability   (for YES side; invert for NO)
EV% ≈ edge × 100   (simplified; adjust for fees and payout structure when known)
```

Cross-check against thresholds in `prompts/risk_policy.md`.

### 8. Return recommendation

| Tier | Typical conditions |
|------|-------------------|
| **AVOID** | Negative EV, unclear rules, bad liquidity, or EV below minimum threshold |
| **WATCH** | Interesting but insufficient data, marginal EV, or high uncertainty |
| **SMALL BET** | Positive EV in 3–7% range, moderate confidence |
| **NORMAL BET** | Positive EV in 7–15% range, good data alignment with rules |
| **STRONG OPPORTUNITY** | EV > 15%, high confidence, clear resolution source, rules fully understood |

Apply position sizing from `prompts/risk_policy.md` in parallel with the tier.

### 9. Explain exact reason and uncertainty

Every recommendation must include:

- **Why** — the specific edge, mispricing, or lack thereof
- **Key drivers** — forecast, form, injury, rule clause, etc.
- **What could go wrong** — resolution disputes, model error, late news, weather variance
- **Missing data** — anything that would change the recommendation if obtained

---

## Decision Principles

- **Resolution-first:** If you cannot verify how the market resolves, do not bet — **SKIP**.
- **Price is not truth:** Market price is the benchmark to beat, not evidence of outcome probability.
- **Time decay:** Re-evaluate as resolution approaches; forecasts and lineups change.
- **No narrative trading:** Stories without verified data do not justify a bet.
