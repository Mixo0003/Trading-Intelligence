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

## V2.1.1 — Storage retention (completed — pending merge)

Branch: `feature/v2.1.1-storage-retention`

- [x] Storage metrics and disk / growth thresholds
- [x] Explicit-path file cleanup (disabled + dry-run by default)
- [x] SQLite retention audit and dry-run preview
- [x] Verified online SQLite backup and integrity gate
- [x] Verified-backup-gated bounded retention pruning (disabled + dry-run by default)
- [x] Admin Storage Telegram UX, alerts, recovery deduplication, maintenance history
- [x] Live Telegram verification; Weather / Copy / scheduler regressions; full suite 652 passed, 2 skipped
- [ ] PR review and merge to main

Defaults remain non-destructive: no automatic VACUUM or `journal_mode` changes; backup creation is explicit and admin-confirmed.

## Pre-V2.2 requirements

Before starting V2.2.x implementation:

- [ ] Official Polymarket API research
- [ ] Editable-settings specification
- [ ] Paper-copy architecture
- [ ] Security threat model
- [ ] Limited Grill Me review for Copy Settings

## V2.2.x — Copy Intelligence expansion (not started)

Use a separate small version branch per item. Do not start until V2.1.1 is merged and pre-V2.2 requirements above are satisfied.

- [ ] **V2.2.1** Editable Copy Settings
- [ ] **V2.2.2** Improved Copy Alerts and Position Intent
- [ ] **V2.2.3** Advanced Trader Scoring
- [ ] **V2.2.4** Trader Comparison
- [ ] **V2.2.5** Paper Copy Portfolio
- [ ] **V2.2.6** Full Polymarket Discovery and Market Normalization
- [ ] **V2.2.7** Paper Arbitrage Intelligence
- [ ] **V2.2.8** Notifications, Digest Mode and Final UX

## Later (not started)

- [ ] Optional AI Incident Explainer (sanitized, non-controlling)
- [ ] Richer activity-API event identity for copy alerts (without changing scoring decisions)
- [ ] Live execution (explicitly out of scope until approved)

## Core rules

- Weather analysis always reads market description / resolution source first.
- Copy Intelligence remains read-only and paper-only unless explicitly redesigned.
- Health status never depends on AI.
- Storage cleanup / pruning remain disabled and dry-run by default until explicitly enabled with all safety gates.
