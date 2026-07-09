# Response Format — Market Analysis Output

Standard output template for Trading Intelligence market analysis. Use this structure unless the user requests a different format.

Keep sections **concise but complete**. Use **SKIP** early and populate **Missing Data** when analysis cannot proceed fully.

---

## Template

```
## Market
[Title, URL, market type, resolution date if known]

## Resolution Rules
[Key settlement criteria, resolution source, station/URL/metric, ambiguities]

## Data Used
[Verified facts only — market metadata, forecasts, stats, with timestamps]

## Market Price / Implied Probability
[YES/NO or outcome prices → implied probability for evaluated side]

## Estimated Probability
[Model or verified-data probability; note if Assumption or Model estimate]

## Edge
[Estimated prob − implied prob for evaluated side]

## EV
[Expected value % on stake; note fees/spread if relevant]

## Risk
[Resolution, model, liquidity, timing risks — brief bullets]

## Suggested Position Size
[% of bankroll per risk_policy.md and confidence level]

## Recommendation
[SKIP | AVOID | WATCH | SMALL BET | NORMAL BET | STRONG OPPORTUNITY]

## Confidence
[Low | Medium | High]

## Missing Data
[Exact list of unavailable inputs, or "None" if complete]

## Sources
[Links, station codes, APIs, providers, observation times]
```

---

## Section Rules

### Market
One-line summary plus URL. State market type (weather, sports, etc.).

### Resolution Rules
Quote or paraphrase settlement criteria from rules text. Flag ambiguity. If rules were not read → **SKIP**.

### Data Used
**Verified facts only.** Do not mix assumptions here — put those in Estimated Probability with labels.

### Market Price / Implied Probability
Show raw price and converted implied probability. If price unknown → **SKIP**, list in Missing Data.

### Estimated Probability
Show methodology in one line if non-obvious. Label **Verified**, **Assumption**, or **Model estimate** components.

### Edge / EV
Omit or mark N/A if either probability input is missing. Do not fabricate values.

### Risk
Cover at minimum: resolution interpretation, data staleness, liquidity, and domain-specific variance.

### Suggested Position Size
Must align with `risk_policy.md` caps and **Confidence** level.

### Recommendation

| Tier | When to use |
|------|-------------|
| **SKIP** | Missing resolution source, price, or critical domain data |
| **AVOID** | Negative EV, EV below threshold, or unfavorable risk/reward |
| **WATCH** | Marginal EV, incomplete secondary data, or high uncertainty |
| **SMALL BET** | EV 3–7%, moderate confidence |
| **NORMAL BET** | EV 7–15%, good rule/data alignment |
| **STRONG OPPORTUNITY** | EV > 15%, high confidence, clear resolution |

### Confidence

| Level | Typical basis |
|-------|---------------|
| **Low** | Missing secondary inputs, stale data, ambiguous rules, live market |
| **Medium** | Core inputs verified, minor unknowns |
| **High** | Full rule clarity, fresh resolution-aligned data, stable market |

### Missing Data
List **exact** missing fields. Example: `"Current NO price; hourly forecast for 2026-07-10 at KORD"`. Write **None** only when analysis is fully sourced.

### Sources
Every external data point must trace to a source entry here.

---

## Short Analysis Variant

For quick replies, minimum required fields:

**Market · Recommendation · Confidence · Implied vs Estimated prob · Edge/EV · Missing Data · Sources**

Expand to full template when the user asks for detail or when recommendation is not SKIP.

---

## Related Prompts

- Constitution: `system_prompt.md`
- Reasoning discipline: `thinking_rules.md`
- Workflow: `strategy.md`
- Sizing: `risk_policy.md`
