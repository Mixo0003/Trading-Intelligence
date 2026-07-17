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

## V2.1 — Health Monitor + copy freshness (current)

- [x] Per-wallet baseline / watermarks / processed event IDs
- [x] No alerts until baseline completes; re-enable = new baseline
- [x] `COPY_MAX_EVENT_AGE_SECONDS` freshness gate
- [x] Deterministic Health Monitor (probes, thresholds, incidents)
- [x] Admin-only System Health Telegram UX (`ADMIN_IDS`)
- [x] Deduplicated health alerts
- [x] Operational docs (`AGENTS.md`, health + freshness docs)

## Later (not started)

- [ ] Optional AI Incident Explainer (sanitized, non-controlling)
- [ ] Richer activity-API event identity for copy alerts (without changing scoring decisions)
- [ ] Live execution (explicitly out of scope until approved)

## Core rules

- Weather analysis always reads market description / resolution source first.
- Copy Intelligence remains read-only and paper-only unless explicitly redesigned.
- Health status never depends on AI.
