# Market Analysis Strategy

End-to-end workflow for evaluating prediction market opportunities in Trading Intelligence.

---

## Prompt Dependencies

This workflow sits in the prompt stack below `system_prompt.md`. Use these files together:

| File | Role in workflow |
|------|------------------|
| `thinking_rules.md` | Internal discipline — read first; skip conditions; facts vs assumptions |
| `strategy.md` | This file — step-by-step analysis workflow |
| `risk_policy.md` | Bankroll caps, EV thresholds, position sizing |
| `response_format.md` | Standard output template for final answer |
| `polymarket.md` | Polymarket metadata, prices, resolution-first rules |
| `weather.md` | Station, forecast, Wunderground/NWS rules |
| `sports.md` | Injuries, lineups, pre-match vs live rules |

**Order of application:** `thinking_rules.md` → this workflow → domain prompt → `risk_policy.md` → `response_format.md`

---

## Workflow

### 1. Identify market type

Classify the market before applying domain rules:

- **Weather** — temperature, precipitation, hurricane, climate threshold → `weather.md`
- **Sports** — match outcome, props, futures, live → `sports.md`
- **Politics / macro** — elections, policy, economic releases
- **Crypto / finance** — price thresholds, ETF, protocol events
- **Other** — entertainment, science, custom events

All Polymarket markets also follow `polymarket.md`.

### 2. Read title, description, and rules

- Read full **title**, **description**, and **rules** text.
- **Never analyze from the title alone** (see `thinking_rules.md`).
- Note ambiguities, conditional clauses, and edge cases.

### 3. Extract resolution source

- Identify the **official resolution source** from rules.
- Extract URLs, station codes, data fields, and measurement windows.
- Missing or unclear resolution source → **SKIP** (`thinking_rules.md`).

### 4. Extract current market price

- Record **YES** and **NO** prices (or all outcome prices).
- Note spread, liquidity, and time to resolution.
- Missing price → **SKIP**.

### 5. Convert price to implied probability

```
implied_probability = price          (0–1 decimal)
implied_probability = price / 100    (cents)
```

Account for fees and spread when discussing practical EV.

### 6. Estimate probability from verified data

- Pull data only from resolution-aligned sources (domain prompt).
- Build **estimated probability** for the evaluated outcome.
- Separate **verified facts**, **assumptions**, and **model estimates** (`thinking_rules.md`).

### 7. Calculate EV and edge

```
edge = estimated_probability − implied_probability   (evaluated side)
EV% ≈ edge × 100   (simplified; adjust for fees when known)
```

Gate against thresholds in `risk_policy.md`.

### 8. Return recommendation

| Tier | Typical conditions |
|------|-------------------|
| **SKIP** | Missing resolution source, price, or critical domain data |
| **AVOID** | Negative EV, EV below minimum, poor liquidity |
| **WATCH** | Marginal EV, insufficient secondary data, high uncertainty |
| **SMALL BET** | EV 3–7%, moderate confidence |
| **NORMAL BET** | EV 7–15%, strong rule/data alignment |
| **STRONG OPPORTUNITY** | EV > 15%, high confidence, clear resolution |

Apply position sizing from `risk_policy.md`.

### 9. Explain reason and uncertainty

Every recommendation must state:

- **Why** — specific edge, mispricing, or lack thereof
- **Key drivers** — forecast, form, rule clause, etc.
- **What could go wrong** — resolution disputes, model error, late news
- **Missing data** — exact fields that would change the call

Format output per `response_format.md`.

---

## Decision Principles

- **Resolution-first:** Cannot verify resolution → **SKIP**, not bet.
- **Price is not truth:** Market price is the benchmark to beat.
- **Calculate only with inputs:** No guessed prices or forecasts (`thinking_rules.md`).
- **Time decay:** Re-evaluate as resolution approaches.
- **No narrative trading:** Stories without verified data do not justify a bet.
