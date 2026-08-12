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
  - **Executable market momentum and quality** (not fair value / not arbitrage).
  - **Implementation (Checkpoints 1–7) complete on `feature/v2.2.3-market-opportunity-scanner`.**
  - **Trusted catalog persistence + stale-while-refresh (v3) implemented in working tree; production migration not performed.**
  - **Checkpoint 8: documentation + full suite + release preparation.**
  - Full suite (2026-07-27): **1213 passed, 2 skipped, 1 warning**; temp-DB migration PASS
  - **Production migration: not performed.**
  - **Live Telegram verification: pending.**
  - **Not production GO and not released.**
  - Public-data-only discovery / history / books; complete-discovery fail-closed
  - Two-cycle baseline; canonical score v1; Normal + STRICT live
  - Top Opportunities + deterministic cards; editable settings
  - Same-wave admin/user delivery architecture; delivery worker
  - Scheduler with timeout containment; shared CLOB budget 50; Gamma budget 10; health
  - Additive migrations `opportunity_intel_v1` + `v2_resumable_discovery` + `v3_persisted_catalog`
  - See `docs/MARKET_OPPORTUNITY_SCANNER.md`
- [ ] **Next major intelligence direction — Arbitrage Intelligence** (backlog only; not V2.2.3)
  - same-market YES/NO inconsistencies
  - mutually exclusive outcome inconsistencies
  - cross-market relationship inconsistencies
  - fee-, spread-, depth-, and execution-aware profitability
  - no “guaranteed” label without fully executable proof
- [ ] **Later — Fair Value / Forecast Intelligence** (backlog only)
- [ ] **V2.2.4** Trader Comparison
- [ ] **V2.2.5** Paper Copy Portfolio
- [ ] **V2.2.6** Full Polymarket Discovery and Market Normalization
- [ ] **V2.2.7** Paper Arbitrage Intelligence
- [ ] **V2.2.8** Notifications, Digest Mode and Final UX

## Later (not started) / V2.2.3 mandatory backlog

- [ ] Large-wallet position-window support (deferred from V2.2.2)
- [ ] Accounting-snapshot source research
- [ ] Thresholded position universe and versioning
- [ ] `UNIVERSE_ENTRY` / threshold-crossing semantics
- [ ] Advanced Trader Scoring
- [ ] Optional AI Incident Explainer (sanitized, non-controlling)
- [ ] Reduce / Close copy-alert intent (deferred from V2.2.2)
- [ ] Multi-user / non-admin Copy subscriptions (deferred)
- [ ] WebSocket streaming, reconnect, replay, and gap recovery
- [ ] Faster / sub-minute live scanning
- [ ] True short-window volume spikes
- [ ] Trade-count reconstruction
- [ ] Digest mode
- [ ] Useful / Not useful feedback
- [ ] Saved watchlists
- [ ] Free/Pro entitlements
- [ ] Payments
- [ ] External forecasts
- [ ] Fair-value models
- [ ] AI/news explanations
- [ ] Arbitrage Intelligence
- [ ] Cross-market opportunities
- [ ] Outcome-performance calibration
- [ ] Scoring calibration from live data
- [ ] Fully Decimal logarithmic scoring or explicit score-v1 reproducibility policy
- [ ] Live execution / real trading (explicitly out of scope until approved)

## V4 — Market Opportunity Intelligence + Market Intelligence

Version naming: **V4.1**, **V4.2**, **V4.3**… (no letter suffixes).

MOI (`app/market_opportunity/`, `moi:*`) remains a sibling of Polymarket Opportunity Scanner (`app/opportunity_intel` / `opp:`). V4.3 also delivered the guest-facing **Market Intelligence** hub (internal `crypto:*` package) for spot cross-exchange paper arbitrage.

- [x] **V4.1** Market Opportunity Foundation — paper-only models, lifecycle, empty detector registry, additive `market_opportunity_v1`, admin-only Telegram shell, flags default off; **no live detectors / no auto alerts**
- [x] **V4.2** WTI-first paper opportunity intelligence (EIA RWTC + WCESTUS1; Gold deferred until licensed feed) — merged
- [x] **V4.2.1** Global admin health / diagnostics — merged
- [x] **V4.3** Market Intelligence UX — merged (`7da710e`, main merge `677b761` / PR #20)
  - Main menu: Polymarket · Market Intelligence · Settings (+ admin System Health)
  - Hub: New Opportunities · Filters · Opportunity History · Back
  - New Opportunities = **spot cross-exchange paper arbitrage** only (read-only / paper-only)
  - V4.3 venues: Binance, BingX, MEXC, OKX, KuCoin (Bybit = legacy Crypto V3 only)
  - Per-user Filters: presets + custom numeric input; Preview → Confirm → Cancel; `telegram_id` isolation; stale-revision protection
  - Opportunity History scoped by `telegram_id`
  - `CRYPTO_OPPORTUNITY_SCANNER_ENABLED` and `CRYPTO_OPPORTUNITY_DELIVERY_ENABLED` default **false**
  - No real trading; no LONG/SHORT, TP/SL, futures, funding, or news signals in V4.3
  - MOI/WTI handlers remain importable; not presented as the main guest product
  - Legacy Polymarket, Weather, Copy Intelligence, and Crypto V3 leaves preserved for compatibility
- [x] **V4.4** Catalog-driven cross-exchange spot arbitrage — merged (`a4bbc46`, main merge `09ca984` / PR #21)
  - Authoritative spot catalog discovery on Binance, BingX, MEXC, OKX, KuCoin
  - Complete-miss deactivation only after COMPLETE discovery; partial never delists
  - Executable paper arb unchanged in spirit (books, fees, slippage); Bybit excluded
- [x] **V4.5** Futures / basis / funding **foundation** (contracts + capability registry)
  - Product families: spot, perpetual, dated_future (registry-aware)
  - Perpetual / dated futures remain **unsupported** (no verified futures HTTP adapters)
  - Normalized snapshot + basis/funding observation contracts; fail closed without approved fee/formula/TTL policies
  - Does **not** implement complete futures arb, funding-arb engine, LONG/SHORT, TP/SL, or news
- [x] **V4.6** Directional trading intelligence **foundation** (LONG/SHORT contracts)
  - Strategy labels: trend, breakout, momentum, mean reversion, order-book imbalance
  - Volume/volatility evidence presence with fail-closed missing-source behavior
  - Fail closed without approved signal formula, risk/TP-SL policy, or freshness policy
  - Does **not** invent thresholds, ranking, confidence, or complete directional execution
  - Spot paper arb / New Opportunities path unchanged; Top Opportunities remain unified
- [x] **V4.7** News & fundamental intelligence **foundation**
  - Evidence overlays on **existing** opportunities only (confirm / reject / downgrade / suppress)
  - News alone never creates a trading signal; no separate News button
  - Provenance required: URL, publisher/source type, timestamps, asset, dedupe key, verification
  - Fail closed without approved source registry, freshness, or ranking policies
  - Ambiguous labels (Gold/Oil/etc.) rejected; Bybit excluded from V4 venues
- [x] **V4.8** Unified scoring & calibration **foundation**
  - Universal score contract across spot arb, basis/funding, directional, news overlays
  - Explainable component labels; strategy evidence normalization refs
  - Outcome lifecycle (succeeded / failed / expired / unobservable) with immutability
  - Fail closed without approved weight / threshold / ranking policies
  - Does **not** invent numeric weights, probabilities, ROI, or rewrite history
- [x] **V4.9** Verified news ingest & authoritative source registry
  - Registry limited to in-repo verified EIA/FRED routes only (no invented endpoints)
  - Offline-capable ingest via existing MOI parsers; MOI ``assert_fresh`` freshness policy
  - Provenance, dedupe, asset mapping; overlays on existing opportunities only
  - News alone never creates a signal; unsupported sources/event classes fail closed
- [ ] Later — Additional official source adapters beyond EIA/FRED (deferred)
- [ ] Later — Approved scoring weights / thresholds / ranking policies (deferred)
- [ ] Later — Advanced arb hardening / cross-market expansion beyond V4.4–V4.9 foundation (deferred)
- [ ] Later — Silver / Natural Gas commodity expansion (was formerly labeled V4.3; deferred)
- [ ] Later — Opportunity scoring calibration & paper outcome tracking (former MOI slots; deferred)

### Known non-blocking health note (pre-existing)

System Health may show Overall DEGRADED / `copy_intel` DOWN from repeated large-wallet `incomplete_book` baselines. `copy_readonly` remains HEALTHY; Copy jobs keep running; incomplete-baseline wallets stay fail-closed. Not caused by V4.3; large-wallet pagination remains backlog.

### Crypto V3 — paused (not abandoned)

V3.0.0a/b/c leaves remain intact for stale Telegram clients (`crypto:overview`, `crypto:spread`, `crypto:exchanges`, gross V3.0.0c path where applicable). Further V3 hub expansion is paused. Backlog:

- Global market universe beyond the V4.3 five-venue paper scanner
- Bybit remains in legacy Crypto V3 adapters only (excluded from V4.3 scanner universe)
- New-listing detection
- Funding, basis, and paper execution beyond spot paper arb
- Market Research hub (`research:*` still placeholder)
- 24h statistics

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
