# V2.2.9 — Polymarket Trade Candidate: Grill Me

## Decisions challenged before build

1. **Can price movement alone produce ENTER?** No. Movement is market evidence,
   not a cause. ENTER additionally requires a supported forward catalyst.
2. **Can one news headline produce ENTER?** No. One official source or two
   independent trusted sources are required, and evidence must match the exact
   market, entities, date, and outcome. Social sources never confirm a catalyst.
3. **What happens when sources conflict?** WAIT, regardless of score.
4. **Can a live sports market enter without an official live state?** No. Missing,
   stale, ended, ambiguous, or outcome-unmapped live state is WAIT/SKIP.
5. **Can the engine call an old impulse an entry?** No. When the executable price
   is above the deterministic entry limit, the stage is EXECUTED_IMPULSE / WAIT.
6. **Are displayed prices executable?** Entry uses ask-side VWAP for the selected
   paper allocation. Exit capacity is checked against bid-side shares and its
   current impact is carried into the projected exit.
7. **Are fees assumed to be zero?** No. A fee-enabled market requires a complete
   official Gamma fee schedule. Net profit includes entry and exit taker fees.
8. **Is a target presented as fair value or guaranteed profit?** No. It is a
   deterministic paper exit target, bounded to the valid price range.
9. **Can the bot place trades?** No. V2.2.9 remains public-data, read-only, and
   paper-only. ENTER is an analytical state only.
10. **Can every active market receive expensive CLOB reads every cycle?** No. The
    complete catalog covers all active markets, while a category-balanced bounded
    shortlist protects provider limits and bot responsiveness.

## Approved fail-closed result

- Missing resolution rules/source: SKIP.
- Missing fresh history or two-sided book: SKIP.
- Insufficient entry or exit capacity: SKIP.
- Missing forward catalyst: WAIT.
- Entry price already exceeded: EXECUTED_IMPULSE / WAIT.
- Live state missing or contradictory: WAIT.
- Projected net return below policy floor: SKIP.
- Target reached: EXIT / TAKE_PROFIT.
