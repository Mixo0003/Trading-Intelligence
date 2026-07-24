# Changelog

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
