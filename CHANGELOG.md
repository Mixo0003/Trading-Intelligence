# Changelog

## V2.1.1 — Storage retention (2026-07-21)

Branch: `feature/v2.1.1-storage-retention` (completed — pending PR merge).

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
- Full suite: 652 passed, 2 skipped.
- Strix: evaluated but unavailable locally; no scan executed; optional audit deferred (not a V2.1.1 PR blocker).

## 2026-07-02

- Created clean GitHub repository.
- Connected private GitHub repo to VPS.
- Confirmed VPS runs Ubuntu 24.04 LTS.
- Created modular project folders.
- Defined V1.0 scope: Polymarket weather markets only.
- Confirmed resolution-first strategy.
