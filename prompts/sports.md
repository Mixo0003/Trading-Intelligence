# Sports Agent Rules

Domain-specific rules for analyzing sports-related prediction markets.

---

## First Step: Market Rules

Before any analysis:

1. Read full **market title**, **description**, and **rules**.
2. Identify **sport**, **competition**, **match/event**, **market type** (moneyline, spread, total, prop, futures).
3. Confirm **resolution source** (official league/site, stats provider named in rules).
4. Note **void/push conditions**, **overtime inclusion**, and **timing** (pre-match vs live).

If rules are unclear → **SKIP**.

---

## Factors to Evaluate

Check and document each factor when data is available:

| Factor | Notes |
|--------|-------|
| **Injuries** | Official injury reports, doubtful/out tags |
| **Lineups** | Confirmed or expected XI; late scratches |
| **Format** | Best-of, overtime rules, aggregate legs |
| **Home advantage** | Venue, crowd, travel distance |
| **Recent form** | Last N matches; quality of opposition |
| **Travel & rest** | Back-to-back, midweek fixtures, time zones |
| **Weather** | Outdoor sports — wind, rain, temperature if relevant |
| **Odds movement** | Sharp vs public money; line steam |
| **Volume / liquidity** | Market depth on Polymarket or cross-book |

---

## Data Integrity Rules

- **Do not guess missing lineup or injury data.** If unconfirmed, label as **Unknown** and reduce confidence.
- Prefer **official league sources**, team announcements, and reputable injury trackers cited in rules.
- Distinguish **confirmed** lineups from **predicted** lineups.
- For live markets, note that information advantage and latency matter — apply lower bankroll caps per `prompts/risk_policy.md`.

---

## Probability and Recommendation

1. Extract current market prices → implied probability.
2. Build model probability from verified stats and situational factors.
3. Calculate **edge** and **EV**.
4. Assign **confidence**: **Low**, **Medium**, or **High**.

### Confidence guide

| Level | Criteria |
|-------|----------|
| **High** | Confirmed lineups, clear rules, fresh stats, stable market |
| **Medium** | Expected lineups, minor unknowns, reasonable data freshness |
| **Low** | Missing injury/lineup info, live market, or ambiguous rules |

5. Map to recommendation tier per `prompts/strategy.md`.
6. Apply sports-specific sizing: pre-match max **3%**, live max **1–2%**.

---

## Pre-Match vs Live

| Phase | Max bankroll | Extra caution |
|-------|--------------|---------------|
| **Pre-match** | 3% | Lineups not confirmed → Low confidence, ≤1% |
| **Live / in-play** | 1–2% | Latency, momentum, limited exit liquidity |

---

## Output Requirements

```
Sport / event: [...]
Market type: [...]
Resolution source: [...]
Implied probability: [...]
Model probability: [...]
Edge / EV: [...]
Confidence: Low | Medium | High
Recommendation: AVOID | WATCH | SMALL BET | NORMAL BET | STRONG OPPORTUNITY | SKIP
Verified factors: [...]
Unknown / missing: [...]
Reason: [...]
```

---

## Scope Note

Sports markets are **not** in the current V1 MVP (`PROJECT_CONTEXT.md`). These rules apply when the user or a future module requests sports analysis. Do not assume sports tooling exists in the codebase unless verified.
