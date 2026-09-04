# Changelog

## V2.4.4 — Live Edge Opportunities

**Branch:** `feature/v2.4.4-live-edge-opportunities`

### Added / changed

- Optional per-user **Live Edge Opportunities** toggle in Top Opportunities Filters
  (default **OFF**). Existing Opportunity Scanner scoring, shortlist, and
  Top Opportunities path are unchanged when the toggle is OFF.
- Isolated `app/opportunity_intel/live_edge/` branch: independent Gamma discovery
  (not sports-only, no 100-outcome shortlist, no 6-hour remaining-time gate),
  category adapters, event matching, conservative edge/EV, Ukrainian cards.
- Production live-state providers (fail closed when empty/stale): Leaguepedia
  cargoquery (lol.fandom.com) for named LoL series state; BO3.gg widget-matches
  for CS2; OpenDota live (named Dota only); TheSportsDB livescore; MLB Stats API;
  OpenLigaDB; Binance public spot; NWS METAR via Weather Intelligence.
  Ordered per-sport fallback: LoL Leaguepedia → LoL Esports (optional key) →
  PandaScore (optional ``LIVE_ESPORTS_PROVIDER_API_KEY``; free REST = named
  running series, not in-map frames) → liquipedia.net; CS2 BO3 → PandaScore →
  liquipedia.net; Dota OpenDota → PandaScore → liquipedia.net.
  Later providers may replace a same-event snapshot only when they add
  verified in-map/round metrics and do not disagree; conflicts stay
  ``SOURCE_CONFLICT`` (no BUY).
  Liquipedia.net cargoquery remains disabled (verified). Riot ``getLive`` remains
  HTTP 403 without a working key. PandaScore without a token is AUTH_REQUIRED.
- Evidence tiers HIGH/MEDIUM/LOW; BUY requires HIGH. Remaining-map combinatorics
  instead of a hardcoded 2–0 = 95% table. CLOB `/fee-rate` used when it returns
  a verified `base_fee`; otherwise fee is labeled unavailable.
- Signal types: live map/game winner, live match, near-resolution, general,
  WAIT candidates with trigger monitoring.
- Additive `live_edge_*` SQLite tables. Analysis/alerts only — no real trading.
- Optional INSTANT WATCH Telegram notices (cap 3 per user / 30 minutes, fingerprint
  dedupe). BUY/WAIT still take priority. CLOB quotes retry modest future-timestamp
  skew; empty books stay unquoted.
- Scan-lock renew during long cycles; batch signal persist; SQLite busy timeout
  with locked-write retry. Delivery does not invent BUY.
- Short Ukrainian Telegram cards for WATCH / WAIT / BUY / TOO LATE and Momentum /
  Top Opportunities. Original Polymarket titles are preserved. Validated
  Polymarket links use ``🔗 Відкрити на Polymarket``. Technical provider/evidence
  copy stays in logs and storage, not on normal cards.

## V4.10 — Scoring Policy & Ranking Enablement

**Branch:** `feature/v4.10-scoring-policy-ranking-enablement`

### Added / changed

- Scoring policy contracts (`scoring_policy.py`) for explicit weights,
  thresholds, ranking keys, confidence labels, and score versions.
- Policy validation for completeness, approval, and provenance alignment;
  deterministic ranking only under a complete approved ``ScoringPolicy``.
- Explainable weighted contributions (presence × policy weight); confidence
  labels are diagnostic only (not probabilities).
- No baked-in default policy; missing/contradictory/incomplete policies fail
  closed. Historical outcomes remain immutable.
- V4.4–V4.9 modules, Filters/History isolation, and scanner/delivery defaults
  unchanged.

## V4.9 — Verified News Ingest & Source Registry (merged)

**Commit:** `7c99d26` · **Main merge:** `8908a3f` (PR #26)

### Added / changed

- Authoritative source registry (`source_registry.py`) with only in-repo verified
  EIA/FRED routes (RWTC, WCESTUS1, DCOILWTICO).
- Verified ingest module (`verified_news_ingest.py`) using existing MOI parsers
  and ``OfficialFreshnessPolicy`` wrapping MOI ``assert_fresh`` (no new TTLs).
- Provenance, dedupe, asset mapping, and overlays
  (confirm/reject/downgrade/suppress) on existing opportunities only.
- Unsupported sources/event classes fail closed; news alone never creates a
  signal; no News button; scanner/delivery defaults unchanged.
- V4.4–V4.8 foundation modules left intact.

## V4.8 — Unified Scoring & Calibration Foundation (merged)

**Commit:** `a573659` · **Main merge:** `95a1bd6` (PR #25)

### Added / changed

- Unified scoring foundation (`app/crypto/opportunity/unified_scoring.py`) with
  strategy kinds for spot arb, basis, funding, directional, and news overlays.
- Explainable score component labels and strategy evidence normalization refs;
  fail closed without approved weight / threshold / ranking policies.
- Paper outcome lifecycle contracts (succeeded / failed / expired / unobservable)
  with immutability; calibration records require real outcomes (no fabrication).
- Status cards state score is not a probability and not guaranteed profit.
- V4.4–V4.7 modules, Filters/History isolation, and scanner/delivery defaults
  unchanged.

## V4.7 — News & Fundamental Intelligence Foundation (merged)

**Commit:** `c989e3a` · **Main merge:** `639f071` (PR #24)

### Added / changed

- News/fundamental foundation module (`app/crypto/opportunity/news_fundamentals.py`)
  with event-class labels, full provenance evidence contracts, and overlay actions
  (confirm / reject / downgrade / suppress) on **existing** opportunities only.
- Hard rule: news alone never creates a trading signal; no separate News button.
- Deduplication, unverified/irrelevant/contradictory rejection, ambiguous asset
  label rejection (Gold/Oil/etc.), V4 venue/product gates.
- Background integration entrypoint fails closed without approved source registry
  / freshness policy. No invented ranking, confidence, TTLs, or profitability.
- V4.3–V4.6 spot arb, directional foundation, Filters/History isolation, and
  scanner/delivery defaults unchanged.

## V4.6 — Directional Trading Intelligence Foundation (merged)

**Commit:** `5cd2dc1` · **Main merge:** `378c7b6` (PR #23)

### Added / changed

- Directional foundation module (`app/crypto/opportunity/directional.py`) with
  LONG/SHORT sides and strategy labels: trend, breakout, momentum, mean
  reversion, order-book imbalance.
- Evidence presence for reference price, order-book depth, volume, and
  volatility; fail closed when volume/volatility sources are missing (no
  verified crypto volume/volatility adapters in-repo).
- Fail-closed evaluators when signal formula, risk/TP-SL policy, or freshness
  policy is missing; futures products remain unsupported without verified adapters.
- Honest foundation cards may show side, strategy, symbol, venue, reference
  price, signal age, and evidence — paper-only / not guaranteed.
- V4.3/V4.4 spot paper arbitrage, Filters, History isolation, and scanner/
  delivery defaults unchanged. Top Opportunities remain unified (no new
  per-strategy Telegram buttons).

## V4.5 — Futures / Basis / Funding Foundation (merged)

**Commit:** `640f515` · **Main merge:** `e99ae03` (PR #22)

### Added / changed

- Capability-aware registry helpers for spot / perpetual / dated_future on Binance,
  BingX, MEXC, OKX, KuCoin; spot remains supported; perpetual and dated futures
  remain **unsupported** (no new futures HTTP catalog adapters).
- Optional instrument constraint fields on `CanonicalInstrument` (settlement,
  tick_size, quantity_step, contract_code) for in-memory normalization only.
- Normalized market snapshot contracts with explicit capability presence,
  provenance, and fail-closed validation (bid/ask, mark/index, funding fields).
- Basis and funding observation contracts plus fail-closed evaluators when fee
  policy, funding formula, or freshness policy is missing.
- V4.4 spot catalog and paper arbitrage path unchanged; scanner/delivery defaults
  remain off; Bybit remains legacy Crypto V3 only.
- No real trading, LONG/SHORT, TP/SL, news signals, guaranteed profit, or invented
  APR/ROI/liquidation formulas.

## V4.4 — Catalog-Driven Spot Arbitrage (merged)

**Commit:** `a4bbc46` · **Main merge:** `09ca984` (PR #21)

### Added / changed

- Catalog-driven spot universe for Market Intelligence venues; complete-miss only
  on COMPLETE discovery; partial discovery never delists.
- Executable paper arb cards with BUY/SELL venue, fees, slippage, net, ROI.
- V4.3 UX Filters / History / isolation preserved.

## V4.3 — Market Intelligence UX (merged)

**Commit:** `7da710e` · **Main merge:** `677b761` (PR #20)

### Added / changed

- Guest/admin main menu: **Polymarket · Market Intelligence · Settings**
  (+ admin **System Health**). Market Research and Market Opportunities removed
  from visible main keyboards (handlers retained for compatibility).
- Market Intelligence hub: **New Opportunities · Filters · Opportunity History · Back**.
- New Opportunities = **spot cross-exchange paper arbitrage** (read-only / paper-only)
  on Binance, BingX, MEXC, OKX, KuCoin; Bybit remains legacy Crypto V3 only.
- Per-user Filters with presets and custom numeric input; Preview → Confirm → Cancel;
  `telegram_id` isolation; stale-revision / token fail-closed; delivery never enabled
  from this UI.
- Opportunity History lists delivery / last-sent rows for the current `telegram_id` only.
- Defaults remain off: `CRYPTO_OPPORTUNITY_SCANNER_ENABLED`,
  `CRYPTO_OPPORTUNITY_DELIVERY_ENABLED`.
- No real trading; no LONG/SHORT, TP/SL, futures, funding, or news signals in V4.3.
- MOI/WTI (`moi:*`) remain compatible, not the
  main guest product. Polymarket, Weather, Copy Intelligence, Crypto V3 leaves preserved.

### Verification

- Focused crypto/MI tests **24 passed**; full suite **1535 passed, 2 skipped, 1 warning**
  (poller stopped for isolation).
- Live Telegram verification: owner-confirmed PASS for menus, Filters (including custom
  numeric), History, admin Health, defaults off.

## V4.2.1 — Global Admin Health and Diagnostics (merged)

**Branch:** `feature/v4.2.1-global-admin-health` (merged to main before V4.3)

### Added / changed

- Global admin-only health framework: registry, severity model
  (`WARMING_UP` / `UNKNOWN`), Ukrainian diagnostic builder, alert policy.
- System Health UI grouped by product area; raw component IDs never shown in Telegram.
- Soft DEGRADED (including `opportunity_books` coverage) is UI-only; confirmed
  hard/feature-critical DOWN alerts after `HEALTH_FAILURE_THRESHOLD`.
- Non-admin health callbacks deny via toast only; missing `ADMIN_IDS` sends zero alerts.
- Documented limitation: external process-death monitoring requires a VPS/systemd
  watchdog (not implemented in-bot).

## V4.2 — WTI Opportunity Intelligence (merged)

**Branch:** `feature/v4.2-wti-opportunity-intelligence` (merged to main before V4.3)

### Added / changed

- WTI-first paper detectors (`wti_trend`, `wti_breakout`, `wti_inventory`) on EIA
  `RWTC` + `WCESTUS1` (U.S. commercial crude stocks excluding SPR).
- `RWTC` treated as official **daily** EIA print (not live futures); as-of date required
  on evidence / price cards.
- Optional FRED `DCOILWTICO` same-period cross-check; missing FRED caps confidence
  at MEDIUM and may leave System Health **DEGRADED** (does not suppress publish;
  does **not** send Telegram health alerts by itself).
- Stage A/B pipeline, deterministic geometry, checked-in blackout calendar,
  paper outcome measurement, storage upsert/list APIs on `market_opportunity_v1`.
- Admin Telegram WTI list + Gold deferred stub; no auto-alerts; no Gold detectors.
- MOI JobQueue runtime status **merges** detail slices (outcomes cannot wipe EIA).
- Health Telegram policy: warmup and optional-FRED DEGRADED suppressed; confirmed EIA
  failure alerts once after threshold; one EIA recovery notice; no `updated`-spam.
- Defaults remain: module disabled, read-only, auto-alerts false.
- UA_INVEST noted as later product-reference only — **not** integrated.

## V4.1 — Market Opportunity Foundation (release prep)

**Branch:** `feature/v4.1-market-opportunity-foundation`

### Added / changed

- New sibling module `app/market_opportunity/` (not an extension of `app/opportunity_intel`).
- Paper-only Opportunity models, lifecycle guards, LOW/MEDIUM/HIGH confidence bands.
- Empty detector registry (no Gold/WTI/Silver/NatGas detectors).
- Additive schema marker `market_opportunity_v1` (`moi_*` tables).
- Admin-only Telegram hub **Market Opportunities** (`moi:*`).
- Polymarket menu label rename: Market Opportunities → Opportunity Scanner (label only).
- Flags default off: `MARKET_OPPORTUNITY_ENABLED`, `MARKET_OPPORTUNITY_AUTO_ALERTS_ENABLED`; `MARKET_OPPORTUNITY_READ_ONLY=true`.
- Crypto V3.0.0a/b/c behavior unchanged; further V3 work paused to backlog.
- Weather eligibility date parse: strip day ordinals only so month names like **August** are not corrupted; accepts `August 7, 2026` and `August 07, 2026`.
- See `docs/MARKET_OPPORTUNITY_INTELLIGENCE.md`.

### Verification

- Focused V4.1 tests green; full suite **1421 passed, 2 skipped, 0 failed**.
- Verified production backup + `PRAGMA quick_check=ok`; additive `market_opportunity_v1` applied under operator gate.
- Single-poller restart from this branch; manual Live Telegram verification **PASS**.
- MOI remains DISABLED / read-only; no live detectors; no auto-alerts.

### Known pre-existing limitation (non-blocking)

- System Health may show Overall **DEGRADED** because `copy_intel` is **DOWN** while `copy_readonly` stays **HEALTHY**.
- Cause: repeated `incomplete_book` baseline failures for large enabled wallets (public pagination window).
- Copy jobs continue; affected wallets remain fail-closed (no alerts). Predates V4.1; Copy behavior unchanged in this release.

## V2.2.3 — Market Opportunity Scanner (2026-07-27)

**Approved status:**

- **Implementation (Checkpoints 1–7) complete and committed locally on `feature/v2.2.3-market-opportunity-scanner`.**
- **Checkpoint 8 documentation + full suite + release preparation.**
- **Production migration: not performed.**
- **Live Telegram verification: pending.**
- **Genuine automatic opportunity delivery: not proven.**

This is **not** production GO, **not** live-verified, and **not released**.

### Added / changed

- Additive SQLite foundations (`opportunity_intel_v1`) for market state, metrics, signals, subscriptions, settings edits/audit, deliveries, gates, scanner/runtime state.
- Public Gamma discovery with complete-discovery fail-closed and separate Gamma HTTP budget (max 10 attempts/cycle).
- Public CLOB price-history and order-book clients with shared per-cycle CLOB HTTP budget (max 50 attempts including retries and STRICT confirmation books).
- Two-cycle baseline, episode hysteresis, canonical score version 1, Normal and STRICT live policy gates.
- Top Opportunities catalog and deterministic Market Opportunity Telegram cards.
- Editable per-user opportunity settings with preview/confirm, revision protection, and audit.
- Same-wave admin/user delivery selection, delivery worker (PENDING / RETRY_WAIT / CLAIMED), immutable delivery payloads.
- Runtime scheduler with idempotent JobQueue registration, DB lock renew, cooperative deadline token, late-worker selection suppression, and health probes.
- Retention policy registration for opportunity datasets (prune remains disabled / dry-run by default).
- Config flags default **off**: `OPPORTUNITY_SCANNER_ENABLED`, `OPPORTUNITY_AUTO_ALERTS_ENABLED`, `OPPORTUNITY_USER_DELIVERY_ENABLED`.

### Docs

- `docs/MARKET_OPPORTUNITY_SCANNER.md` (new)
- Updates: `PROJECT_STATUS.md`, `ROADMAP.md`, `CHANGELOG.md`

### Deferred / mandatory backlog

- WebSocket streaming, reconnect, replay, and gap recovery
- Faster / sub-minute live scanning
- True short-window volume spikes; trade-count reconstruction
- Digest mode; Useful / Not useful feedback; saved watchlists
- Free/Pro entitlements; payments
- External forecasts; fair-value models; AI/news explanations
- Arbitrage Intelligence; cross-market opportunities
- Outcome-performance calibration; scoring calibration from live data
- Fully Decimal logarithmic scoring or explicit score-v1 reproducibility policy
- Large-wallet Copy pagination; Reduce/Close Copy alerts
- Real trading only after separate explicit permission

### Automated verification (2026-07-27, Checkpoint 8)

- Temp-DB migration proof: empty, pre-existing, idempotent, rollback, retention registry, integrity `ok` — PASS (`opportunity_intel_v1`).
- Focused foundation/retention tests: **37 passed**.
- Full suite `pytest -q`: **1213 passed, 2 skipped, 1 warning** in 977.58s.
- Warning: Python 3.15 date-parse DeprecationWarning in `app/polymarket/discovery.py` (pre-existing weather path; not a V2.2.3 failure).
- Live Telegram verification: **pending**.

### Controlled live / production gates (not started)

- Verified production backup and integrity check
- Additive production migration of `opportunity_intel_v1` through the approved entry point
- Idempotent re-run; integrity `ok`; no VACUUM / journal_mode changes
- Exactly one logical Telegram poller
- Controlled enablement of scanner / alerts only under explicit operator instruction
- Genuine automatic opportunity delivery and user-delivery live proof

### Safety

- Read-only / paper-only / public-data-only
- No second Telegram poller launched by opportunity scheduling
- No `.env`, production DB, or runtime artifacts modified during Checkpoint 8

## V2.2.2 — Trustworthy New/Increase Copy Alerts (2026-07-23)

**Approved status:**

- **Implementation and automated verification complete.**
- **Live infrastructure verified.**
- **Genuine supported-wallet event proof pending.**
- **Large-wallet pagination support deferred.**

This is **not** full Live Telegram GO, **not** genuine FOLLOW-event verification, and **not released**.

### Added / changed

- Fail-closed Data API / CLOB client (`ApiResult`), row validation, trade-row sanitization (documented tx-hash identity only).
- Asset-keyed position snapshots (`copy_position_snapshots_v2`) with additive, idempotent migration; legacy table retained.
- V3 baseline (`COPY_BASELINE_VERSION = 3`) seeds current book without historical alerts.
- Bounded authoritative BUY-fill correlation with authoritative fill timestamps; SHA-256 correlated UIDs (`ff1:<hex>`).
- Asset identity validation; duplicate-event, duplicate-paper, and exactly-once delivery protections.
- Validated single-snapshot order book; Trader VWAP / midpoint / best ask / execution VWAP kept distinct.
- Allocation-based order-book impact, slippage, and liquidity; configured paper-bankroll share only.
- Recommendation decisions COPY / WATCH / SKIP for **open** and **increase** only (Reduce/Close deferred).
- Atomic paper commit before COPY delivery; entry price = execution VWAP; **no 0.5 fallback**; `source_event_uid` exactly-once.
- Owner-admin delivery routing (`added_by`) with cross-user/admin isolation; claim / retry / dead-letter; SKIP never delivered.
- Deterministic plaintext Telegram FOLLOW/WATCH cards; validated `https://polymarket.com` Market/Trader links; no inline buttons.

### Docs

- `docs/COPY_ALERTS.md` (new)
- Updates: `docs/COPY_EVENT_FRESHNESS.md`, `docs/COPY_SETTINGS.md`, `PROJECT_STATUS.md`, `ROADMAP.md`, `AGENTS.md`

### Deferred

- Large-wallet position-window support
- Accounting-snapshot source research
- Thresholded position universe and `UNIVERSE_ENTRY` semantics
- Reduce / Close alerts
- Multi-user / non-admin subscriptions
- Digest mode / mutating alert buttons
- Real trading / authenticated CLOB order placement

### Automated verification (2026-07-23)

- Copy Intelligence: 422 passed.
- Telegram regressions: 114 passed.
- Health/storage/scheduler/safety: 136 passed, 2 skipped.
- Full suite: **949 passed, 2 skipped**.
- Checkpoint 11 also fixed `:memory:` schema init (migration on same connection) after full-suite exposure.

### Controlled live infrastructure verification

- Created and integrity-checked a verified production backup.
- Applied the additive production migration through the approved entry point and verified idempotency.
- Production DB integrity remained `ok`.
- Used exactly one logical Telegram poller.
- `wallet_2` completed V3 baseline.
- Baseline initialization created zero historical Copy recommendations, paper positions, or delivery claims.
- `wallet_1` and `wallet_3` remained fail-closed because a complete public positions book could not be proven inside the legal offset-pagination window.
- Stopped the bot through a controlled graceful shutdown after verification.
- A genuine supported-wallet New Position / Increase FOLLOW event was not observed; that proof remains pending.

### Next candidate

- **V2.2.3 — Market Opportunity Scanner:** active-market discovery; liquidity, volume, and freshness filters; price-movement and spread detection; executable order-book depth; ranked opportunities; Telegram alerts or digests; signal history; later post-signal outcome measurement; read-only and paper-only.

## V2.2.1 — Editable Copy Settings (2026-07-22)

**Status:** completed, merged to main (PR #9, merge `cf6d2f5`), live-verified.

### Added

- Per-user editable Copy Settings (nine fields) with centralized defaults and bounds.
- Telegram Edit Settings flow: choose field → enter/select → validate → preview → confirm/cancel.
- Reset to Balanced with preview/confirm and stale-token protection.
- Concurrent-edit protection on settings confirmation tokens.
- `max_event_age_seconds` on `CopyUserSettings` (migration default from global env).
- Per-user poll skip (global JobQueue floor) and per-subscriber max-age / alert-mode filtering.
- Audit payloads for settings save/reset (scalars only; no secrets).

### Docs

- `docs/COPY_SETTINGS.md`

### Verification

- Focused V2.2.1: 18 passed.
- Copy Intelligence: 144 passed.
- Telegram regressions: 67 passed.
- Storage/health: 126 passed, 2 skipped.
- Full suite: **671 passed, 2 skipped** (stable pre-V2.2.2 baseline).
- Live Telegram verification: GO.
- Note: formal Grill Me was not claimed for V2.2.1; permanent Grill Me policy applies from V2.2.2 onward.

## V2.1.1 — Storage retention (2026-07-21)

**Status:** completed, merged to main (PR #8).

### Added

- Storage metrics and disk / DB growth / WAL thresholds in infrastructure health.
- Explicit-path file cleanup inventory with rotation-aware log policy (disabled + dry-run by default).
- Read-only SQLite retention audit and dry-run preview.
- Online SQLite backup with integrity verification gate (absolute backup dir required; creation is admin-confirmed).
- Verified-backup-gated bounded retention pruning (disabled + dry-run by default; no automatic VACUUM or `journal_mode` changes).
- Admin Storage Telegram UX: Metrics, Backup Status, Create Backup, File Cleanup Preview, Retention Preview, Run Safe Cleanup, Run Retention Batch, Maintenance History, Refresh.
- Storage transition alerts with restart-safe watermarks and recovery deduplication vs infrastructure/overall recovery.
- Sanitized maintenance history for admin review.

### Safety

- Admin-only authorization on every storage callback (`ADMIN_IDS`).
- No absolute private paths, secrets, wallets, or raw SQL in Telegram.
- Destructive actions require explicit confirmation tokens bound to admin identity and preview fingerprints.

### Verification

- Live Telegram verification passed.
- Weather, Copy Intelligence, and scheduler regressions passed.
- Full suite at V2.1.1 completion: **652 passed, 2 skipped** (historical; later superseded by V2.2.1 **671 passed, 2 skipped**).
- Strix: evaluated but unavailable locally; no scan executed; optional audit deferred (not a V2.1.1 PR blocker).

## 2026-07-02

- Created clean GitHub repository.
- Connected private GitHub repo to VPS.
- Confirmed VPS runs Ubuntu 24.04 LTS.
- Created modular project folders.
- Defined V1.0 scope: Polymarket weather markets only.
- Confirmed resolution-first strategy.
