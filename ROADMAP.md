# Trading Intelligence Roadmap

## V1.1 — Weather Intelligence (stable on main)

- [x] Regional weather scans (USA / Europe / Asia)
- [x] Multi-provider forecast ensemble
- [x] Exact Polymarket outcome mapping
- [x] English inline Telegram navigation
- [x] Analysis-only / no trade execution

## V2 — Trader / Copy Intelligence (stable on main)

- [x] Read-only Polymarket public data client
- [x] Watchlist + position-diff ingestion
- [x] Paper-only COPY / WATCH / SKIP recommendations
- [x] Trader discovery scores
- [x] Static read-only guard tests

## V2.1 — Health Monitor + copy freshness (stable on main)

- [x] Per-wallet baseline / watermarks / processed event IDs
- [x] No alerts until baseline completes; re-enable = new baseline
- [x] `COPY_MAX_EVENT_AGE_SECONDS` freshness gate
- [x] Deterministic Health Monitor (probes, thresholds, incidents)
- [x] Admin-only System Health Telegram UX (`ADMIN_IDS`)
- [x] Deduplicated health alerts
- [x] Operational docs (`AGENTS.md`, health + freshness docs)

## V2.1.1 — Storage retention (merged to main)

- [x] Storage metrics and disk / growth thresholds
- [x] Explicit-path file cleanup (disabled + dry-run by default)
- [x] SQLite retention audit and dry-run preview
- [x] Verified online SQLite backup and integrity gate
- [x] Verified-backup-gated bounded retention pruning (disabled + dry-run by default)
- [x] Admin Storage Telegram UX, alerts, recovery deduplication, maintenance history
- [x] Live Telegram verification; Weather / Copy / scheduler regressions
- [x] Full suite at V2.1.1 completion: **652 passed, 2 skipped** (historical; superseded by V2.2.1 baseline)
- [x] PR review and merge to main (PR #8)

Defaults remain non-destructive: no automatic VACUUM or `journal_mode` changes; backup creation is explicit and admin-confirmed.

## Pre-V2.2 / ongoing requirements

These remain for V2.2.x expansion (use numbered versions, not “Phase”):

- [x] Limited Grill Me per feature — **permanent formal requirement from V2.2.2 onward** (not claimed retroactively for V2.2.1)
- [ ] Official Polymarket API research (ongoing as needed)
- [ ] Broader paper-copy portfolio architecture (beyond V2.2.2 paper-commit path)
- [ ] Security threat model (ongoing)
- [x] Editable-settings specification (delivered as V2.2.1)
- [x] Implementation in small versioned branches (permanent rule)

## V2.2.x — Copy Intelligence expansion

V2.2.x work uses numbered versions such as **V2.2.1**, **V2.2.2**, and **V2.2.3** — not “Phase”. Use a separate small version branch per item.

- [x] **V2.2.1** Editable Copy Settings — completed, merged to main, live-verified (PR #9, merge `cf6d2f5`)
  - nine editable per-user settings
  - validation bounds
  - preview / cancel / confirm
  - Reset to Balanced
  - stale-token and concurrent-edit protection
  - per-user poll interval behavior
  - per-user maximum event age
  - Verification: focused 18 passed; Copy Intelligence 144 passed; Telegram 67 passed; Storage/health 126 passed, 2 skipped; full suite **671 passed, 2 skipped**; live Telegram **GO**
- [ ] **V2.2.2** Trustworthy New/Increase Copy Alerts
  - **Implementation and automated verification complete.**
  - **Live infrastructure verified.**
  - **Genuine supported-wallet event proof pending.**
  - **Large-wallet pagination support deferred.**
  - Not full Live Telegram GO and not released
  - New Position and Increase only (Reduce/Close deferred)
  - fail-closed APIs, asset-keyed snapshots, bounded trade correlation, SHA-256 fill UIDs (`ff1:`)
  - validated order book; allocation-based slippage; distinct VWAP/mid/ask/execution prices
  - owner-admin delivery; paper commit before COPY alert; execution VWAP entry (no 0.5 fallback)
  - plaintext FOLLOW/WATCH cards; validated Polymarket Market/Trader links; no inline buttons
  - Automated verification (2026-07-23): Copy 422 passed; Telegram 114 passed; health/storage/scheduler/safety 136 passed, 2 skipped; full suite **949 passed, 2 skipped**
  - Live infrastructure: verified production backup; additive migration; production DB integrity `ok`; one logical poller; `wallet_2` V3-complete; zero historical alert, paper, or delivery side effects
  - `wallet_1` and `wallet_3`: complete public positions book cannot be proven inside the legal pagination window; snapshots preserved; alert paths fail-closed
  - See `docs/COPY_ALERTS.md`
- [ ] **V2.2.3** Market Opportunity Scanner
  - discover active Polymarket markets
  - filter by liquidity, volume, and freshness
  - detect meaningful price movement and spread
  - inspect executable order-book depth
  - rank opportunities
  - send useful Telegram alerts or digests
  - store signal history
  - later measure post-signal outcomes
  - remain read-only and paper-only
- [ ] **V2.2.4** Trader Comparison
- [ ] **V2.2.5** Paper Copy Portfolio
- [ ] **V2.2.6** Full Polymarket Discovery and Market Normalization
- [ ] **V2.2.7** Paper Arbitrage Intelligence
- [ ] **V2.2.8** Notifications, Digest Mode and Final UX

## Later (not started)

- [ ] Large-wallet position-window support (deferred from V2.2.2)
- [ ] Accounting-snapshot source research
- [ ] Thresholded position universe and versioning
- [ ] `UNIVERSE_ENTRY` / threshold-crossing semantics
- [ ] Advanced Trader Scoring
- [ ] Optional AI Incident Explainer (sanitized, non-controlling)
- [ ] Reduce / Close copy-alert intent (deferred from V2.2.2)
- [ ] Multi-user / non-admin Copy subscriptions (deferred)
- [ ] Live execution (explicitly out of scope until approved)

## Product development gate

Before starting a major version or feature, ask:

1. Does it provide visible new user value?
2. Does it move the product toward paid subscriptions or revenue?
3. Does it create something meaningful to announce or demonstrate to the user community?

If all three answers are no, normally move the work to backlog unless it addresses critical safety, data loss, security, or materially incorrect financial output.

## Permanent workflow

Plan / Research → Grill Me → Final Plan → Build → Debug → Focused tests → Full suite → Live Telegram verification → Release prep → Commit and push → PR and merge.

Formal Grill Me applies from V2.2.2 onward.

## Tool decisions

- Cursor remains the primary development environment.
- ChatGPT Project holds strategy and master context.
- Codex Remote will first be tested read-only.
- Strix is deferred to an isolated VM or Docker environment.
- Graphify will be tested separately and must not enter bot runtime.

## Core rules

- Weather analysis always reads market description / resolution source first.
- Copy Intelligence remains read-only and paper-only unless explicitly redesigned.
- Health status never depends on AI.
- Storage cleanup / pruning remain disabled and dry-run by default until explicitly enabled with all safety gates.
- Exactly one Telegram poller; no automatic bot restart; no `.env` commits; no runtime/database/backup files in Git.
- No real trading unless explicitly approved.
- No commit or push without an explicit release prompt.
