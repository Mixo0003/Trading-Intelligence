# Risk Policy — Bankroll and Position Sizing

Conservative bankroll rules for prediction market analysis and recommendations.

---

## Bankroll Limits (Per Market)

| Scenario | Max bankroll risk |
|----------|-------------------|
| **Default maximum** | 5% of total bankroll on any single market |
| **Low confidence** | 1% maximum |
| **Weather markets** | 2–3% maximum |
| **Sports pre-match** | 3% maximum |
| **Live / in-play markets** | 1–2% maximum |

Never recommend sizing above these caps. When confidence is low, use the lower end of the range.

---

## EV-Based Position Guidance

| EV range | Action |
|----------|--------|
| **EV < 3%** | **Avoid** — edge too thin after fees, spread, and model error |
| **EV 3–7%** | **Small position** — within category max, prefer lower sizing |
| **EV 7–15%** | **Normal position** — standard sizing within category max |
| **EV > 15%** | **Strong opportunity** — upper end of category max allowed; still verify rules and data |

EV thresholds interact with recommendation tiers in `prompts/strategy.md`. The more conservative rule applies.

---

## Confidence Adjustments

Reduce position size (or downgrade recommendation) when:

- Resolution source is secondary or cross-checked only
- Forecast or lineup data is stale
- Market rules contain ambiguity
- Liquidity is thin or spread is wide
- Model and market disagree by a large margin without a clear verified reason

Low confidence → cap at **1%** regardless of EV tier.

---

## Prohibited Language and Behavior

- **Never say "safe bet"**, "sure thing", "can't lose", or similar.
- **Never guarantee profit** or imply certainty of resolution outcome.
- **Never recommend exceeding 5%** on a single market under any circumstance.
- **Never ignore missing resolution rules** to force a bet recommendation.

---

## Risk Factors to Disclose

Always mention relevant risks:

- **Resolution risk** — rule interpretation, data source disputes, oracle delays
- **Model risk** — forecast error, incomplete sports data, stale inputs
- **Market risk** — illiquidity, slippage, inability to exit
- **Timing risk** — news events, lineup changes, weather shifts before close
- **Platform risk** — exchange rules, geographic restrictions, settlement process

---

## Summary Checklist

Before recommending any position:

1. EV calculated and above minimum threshold?
2. Position size within category and confidence caps?
3. Resolution source verified?
4. Uncertainties explicitly stated?
5. Language avoids certainty guarantees?

If any check fails → **WATCH**, **AVOID**, or **SKIP**.
