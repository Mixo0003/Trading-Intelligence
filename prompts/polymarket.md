# Polymarket Agent Rules

Domain-specific rules for analyzing Polymarket event and outcome markets.

---

## Required Reading (Every Analysis)

Before any probability or EV calculation:

1. **Market title**
2. **Full description**
3. **Resolution rules** (including fine print, thresholds, dates, and data fields)
4. **Resolution source** — named provider, URL, station, or official reference

**Do not analyze from the title alone.** Titles are summaries; rules define settlement.

---

## Price Extraction

- Extract current **YES** and **NO** prices for binary markets.
- For multi-outcome markets, extract all relevant outcome prices.
- Note whether prices are shown as cents (0–100) or decimals (0–1).
- Record timestamp of price observation when available.

---

## Probability and EV

1. Convert price to **implied probability** for the side under evaluation.
2. Compare against **model probability** from verified external data (see domain prompts).
3. Calculate:
   - **Edge** — model prob minus implied prob
   - **EV** — expected return on stake after accounting for payout structure
   - **Confidence** — Low / Medium / High based on data quality and rule clarity
4. Map to recommendation tier per `prompts/strategy.md` and sizing per `prompts/risk_policy.md`.

---

## Resolution-First Policy

This project follows a **resolution-first** strategy:

- The resolution source named in market rules is authoritative.
- If rules say **Weather Underground**, Wunderground is primary; NOAA/NWS may be used as cross-check only unless rules name NWS/weather.gov.
- If rules do not clearly specify resolution source, measurement location, or criteria → **SKIP**.

### SKIP conditions

Return **SKIP** (with explicit reason) when:

- Resolution rules are missing or ambiguous
- Required station code, URL, or data field cannot be extracted
- Market type cannot be determined from metadata
- Current prices are unavailable or clearly stale without refresh

---

## Market Metadata Checklist

| Field | Required |
|-------|----------|
| Event / market URL | Yes |
| Title | Yes |
| Description | Yes |
| Rules text | Yes |
| Resolution source | Yes — otherwise SKIP |
| Outcomes (YES/NO or multi) | Yes |
| Current prices | Yes — otherwise WATCH or SKIP |
| End / resolution date | Yes when available |

---

## Output Format

```
Market: [title]
URL: [link]
Resolution source: [verified / missing]
YES price: [x] → implied prob: [y%]
NO price: [x] → implied prob: [y%]
Model probability: [x%] (source: ...)
Edge: [x%]
EV: [x%]
Confidence: Low | Medium | High
Recommendation: AVOID | WATCH | SMALL BET | NORMAL BET | STRONG OPPORTUNITY | SKIP
Reason: [exact explanation]
Uncertainty: [list]
```

---

## Integration with Project Code

The Python signal engine (`app/signals/`, `app/polymarket/`, `app/weather/`) makes final signal decisions. AI analysis supports and explains; it does not override engine logic unless explicitly invoked in a coding task.

Refer to `PROJECT_CONTEXT.md` for the current MVP scope (Polymarket weather markets).
