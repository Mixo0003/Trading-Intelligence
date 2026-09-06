# V2.2.9 — Polymarket Trade Candidate: Final Plan

## Scope

Build a deterministic read-only decision layer over the existing V2.2.3
Polymarket scanner. Preserve complete Gamma discovery, baselines, signal history,
delivery authorization, and all no-trading boundaries.

## Data flow

1. Complete active Gamma catalog and category-balanced bounded shortlist.
2. Exact market resolution description and resolution source.
3. Fresh official Gamma event state, including sports live/score fields.
4. Fresh public CLOB price history and two-sided order book.
5. Exact paper-allocation ask sweep and bid-side exit-capacity sweep.
6. Per-market Gamma fee schedule and documented taker-fee formula.
7. Exact-market evidence policy: one official or two independent trusted sources;
   social evidence is warning-only; conflicts force WAIT.
8. Deterministic future-catalyst and stage classification.
9. Ukrainian `POLYMARKET TRADE CANDIDATE` Telegram card.

## Decision contract

Stages: PRE_MOVE_SETUP, ENTRY_TRIGGER, MOMENTUM_CONTINUATION,
EXECUTED_IMPULSE, EXIT_TAKE_PROFIT, WAIT, SKIP.

Actions: ENTER, WAIT, EXIT, SKIP. ENTER never places an order.

ENTER requires verified rules, a fresh executable entry, sufficient exit shares,
confirmed forward catalyst, acceptable entry price, defined target and
invalidation, and positive net return above the policy floor after fees and
slippage.

## Verification

- Pure unit tests for evidence, live score mapping, fee math, entry/exit depth,
  stages, and formatting.
- Focused Opportunity Scanner and Telegram regressions.
- Full repository suite.
- Bounded live proof using public Gamma/CLOB only; no persistence and no order.
- One-logical-instance Telegram verification only after explicit restart approval.
- Diff review, secret scan, explicit-path staging, commit, and push.
