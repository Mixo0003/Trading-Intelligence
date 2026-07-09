# Thinking Rules — Internal Analysis Discipline

Internal reasoning standards for Trading Intelligence market analysis. Apply these **before** calculating EV or issuing a recommendation.

These rules govern **how to think**, not the final output format — see `response_format.md` for that.

---

## 1. Identify Market Type First

Before any domain logic, classify the market:

- Weather
- Sports (pre-match or live)
- Politics / macro
- Crypto / finance
- Other

Route to the correct domain prompt after common steps are complete:

- Weather → `weather.md`
- Sports → `sports.md`
- Polymarket mechanics → `polymarket.md`

---

## 2. Read Full Market Metadata

Always read, in full:

1. **Title**
2. **Description**
3. **Rules**
4. **Resolution source** (named provider, URL, station, or official reference)

**Never analyze from the title alone.** Titles summarize; rules define settlement.

---

## 3. Check Data Completeness

Before any calculation, verify required inputs exist:

| Input | Required for full analysis |
|-------|---------------------------|
| Resolution source / criteria | Yes — missing → **SKIP** |
| Current market price | Yes — missing → **SKIP** |
| Domain data (forecast, stats, etc.) | Yes for estimated probability — missing → **SKIP** or **WATCH** with reason |

If a required input is absent, **state exactly what is missing** — not a vague "insufficient data."

Example: *"Missing: station code in rules; cannot fetch resolution-aligned forecast."*

---

## 4. Separate Information Types

Maintain strict categories in reasoning and output:

| Type | Meaning |
|------|---------|
| **Facts** | Verified from market metadata or resolution-defined / official sources |
| **Assumptions** | Unverified context used to interpret facts (label explicitly) |
| **Model estimates** | Probabilities or projections computed from facts + stated assumptions |

Never present assumptions or model estimates as verified facts.

---

## 5. Calculate Only When Inputs Are Available

- **Implied probability** — only when current price is known
- **Estimated probability** — only when domain data aligns with resolution rules
- **Edge and EV** — only when both implied and estimated probability are grounded in available data

Do not interpolate missing prices, forecasts, or lineups to force a numeric recommendation.

---

## 6. Skip Conditions

Return **SKIP** when:

- Resolution source is **missing or ambiguous**
- Current **price is unavailable** or cannot be attributed to the evaluated outcome
- Required domain data cannot be obtained (e.g., station unresolvable, forecast unavailable)
- Rules contradict available data sources

SKIP is a valid, preferred outcome when integrity requirements are not met.

---

## 7. Source Priority

1. **Resolution-defined source** from market rules (highest priority)
2. **Official provider** named or implied by rules (NWS, league site, etc.)
3. **Cross-check sources** — supplementary only; never replace the resolution source
4. **Secondary / inferred sources** — label as assumption; reduce confidence

Prefer official or resolution-defined data over social media, rumors, or title inference.

---

## 8. Uncertainty

Always explain uncertainty clearly:

- What could change the outcome before resolution
- Where the model may be wrong
- What additional data would upgrade confidence or change the recommendation
- Time sensitivity (forecast updates, lineup confirmations, line movement)

Low confidence → cap position per `risk_policy.md` regardless of headline EV.

---

## 9. Handoff to Workflow and Output

After internal reasoning:

1. Continue through `strategy.md` for workflow steps and recommendation tiers
2. Apply `risk_policy.md` for sizing
3. Format final answer per `response_format.md`
