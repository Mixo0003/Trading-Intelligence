# Trading-Intelligence Project Status

## Product purpose

Trading-Intelligence is an analysis-only Telegram bot for discovering and analyzing Polymarket weather markets, plus read-only / paper-only Copy Intelligence and admin Health / Storage tooling.

It must:

- use the exact Polymarket event and real listed outcomes;
- identify the exact resolution source and station;
- forecast the correct daily high or daily low for the station’s local date;
- map the forecast to real Polymarket buckets and live prices;
- return honest statuses such as VALIDATED, WATCH, FORECAST ONLY, or SKIP;
- never place trades;
- never connect to a wallet;
- never fabricate a signal, price, station, forecast, probability, or outcome.

## 11 non-negotiable development rules

1. **Evidence before modification** — Reproduce the exact failure and collect runtime evidence before changing code.
2. **One problem per development step** — Each task addresses one bounded failure or one bounded feature.
3. **Minimal verified changes** — Change only the smallest required set of files.
4. **Real product output is the acceptance test** — Unit tests alone are insufficient; the real Telegram path must show the intended result.
5. **No fabricated data or signals** — Never invent market data, weather data, probabilities, edges, or actionable opportunities.
6. **Polymarket resolution rules are authoritative** — The event rules, resolution source, station, local date, statistic, unit, and rounding define the market.
7. **Exact market outcomes only** — Every displayed bucket must exist in the live event, and its price must belong to that exact outcome.
8. **Protect secrets and repository safety** — Never commit the real `.env`, credentials, local databases, logs, caches, screenshots, or temporary diagnostic output.
9. **Review before commit** — Review intended diffs, run required tests, scan for secrets, and check `git diff` before commit or push.
10. **Stabilize before expanding** — Do not add arbitrage, copy trading, exchanges, coins, resale scanners, wallet integration, or automatic execution until the weather MVP is accepted.
11. **Document every accepted stage** — After each accepted stage, update `PROJECT_STATUS.md` with verified behavior, evidence, limitations, and the next bounded task.

## Permanent workflow

Every feature follows this sequence:

1. Plan / Research
2. Grill Me
3. Final Plan
4. Build
5. Debug
6. Focused tests
7. Full suite
8. Live Telegram verification
9. Release prep
10. Commit and push
11. PR and merge

Do not skip stages. Do not commit or push without an explicit release prompt.

**Grill Me policy:** the permanent formal Grill Me requirement applies from **V2.2.2 onward**. Do not claim retroactively that V2.2.1 completed a formal Grill Me unless repository evidence proves it.

## Tool decisions

| Tool | Role |
|------|------|
| Cursor | Primary development environment |
| ChatGPT Project | Strategy and master context |
| Codex Remote | First tested read-only only |
| Strix | Deferred to an isolated VM or Docker environment |
| Graphify | Tested separately; must not enter bot runtime |

## Safety rules (always on)

- Exactly one Telegram poller (one logical bot instance) during live verification.
- Do not start, stop, or restart the bot unless the user explicitly asks.
- Never commit the real `.env`.
- Never commit runtime, database, WAL/SHM, backup, log, or cache artifacts.
- No real trading unless explicitly approved.
- No commit or push without an explicit release prompt.

## Mandatory working method

For every development step:

1. Reproduce one failure.
2. Show exact evidence.
3. Apply one minimal patch.
4. Run targeted tests.
5. Run one bounded live read-only proof.
6. Stop for human review.
7. Commit only after explicit approval.

Do not perform broad refactors without a proven failing acceptance test.

## Current weather pipeline

| Stage | Real path and symbol | Status | Known limitation |
|---|---|---|---|
| Telegram button | `app/telegram/buttons.py` — `regional_action_keyboard`, `ScanRegion.scan_button` | VERIFIED | Only weather-region flow is in current scope. |
| Regional scan handler | `app/telegram/handlers.py` — `button_message_handler`, `scan_command`, `_run_scan` | VERIFIED | Latest live proof used the same scan service plus Telegram formatter; handler path is covered by a targeted test. |
| Parent-event discovery | `app/polymarket/markets.py` — `get_weather_markets`, `_collect_markets_from_events`, `_limit_parent_events` | PARTIALLY VERIFIED | Parent limiting is verified for USA; live Europe and Asia acceptance remain. |
| Full parent context and rules | `app/polymarket/market_text.py` — `build_market_context_text` | VERIFIED | Depends on complete Polymarket parent metadata. |
| Resolution-source parsing | `app/weather/resolution.py` — `parse_resolution_details`; `app/weather/station_resolver.py` — `extract_resolution_url_from_text` | VERIFIED | Verified for the live Austin event; source page availability is external. |
| Station resolution | `app/weather/station_resolver.py` — `resolve_verified_station_from_text` | VERIFIED | AviationWeather and resolution-source availability can fail transiently. |
| Region classification | `app/region/classifier.py` — `classify_market_region`; `app/signals/scan_service.py` — `_classify_parent_event` | VERIFIED | Live acceptance currently proves USA only. |
| Child outcome expansion | `app/polymarket/event_expansion.py` — `expand_group_children` | VERIFIED | Verified with all 11 live Austin child markets. |
| Exact outcome and live-price mapping | `app/polymarket/sibling_outcomes.py` — `parse_sibling_outcomes`; `app/polymarket/pricing.py` — `extract_yes_price`; `app/signals/event_analysis.py` — `_validate_child_market_mapping` | VERIFIED | Prices are live and may change after a scan. |
| Station-specific weather forecast | `app/weather/daily_forecast.py` — `fetch_daily_forecast_stats`; `app/weather/forecast_ensemble.py` — `fetch_forecast_ensemble` | VERIFIED | Austin proof returned Open-Meteo GFS; provider availability varies. |
| Daily high/low selection | `app/weather/daily_forecast.py` — `fetch_daily_forecast_stats`; `app/polymarket/event_type.py` — `detect_event_temperature_type` | PARTIALLY VERIFIED | Live daily high is verified; a live daily low still needs acceptance proof. |
| Bucket probability | `app/signals/bucket_probability.py` — `compute_sibling_probabilities` | VERIFIED | Calibration quality is not yet measured historically. |
| Status and ranking | `app/signals/forecast_display.py` — `compute_display_status`; `app/signals/ranking.py` — `sort_analyses` | VERIFIED | Live proof produced WATCH, not a validated opportunity. |
| Telegram card formatting | `app/telegram/formatting.py` — `format_market_result_card`, `format_scan_report` | VERIFIED | Direct event links are stored but not yet shown in the concise card. |
| Cache and storage | `app/telegram/scan_cache.py` — `save_scan_snapshot`, `load_latest_scan_snapshot`; `app/telegram/formatting.py` — `persist_scan_results`; `app/database/storage.py` — `save_analysis_record` | PARTIALLY VERIFIED | Automated tests pass; the latest bounded live script did not test restart persistence. |

## Current verified state

- Product baseline on **main**: Weather Intelligence V1.1 + Trader Intelligence V2 (read-only / paper-only) + Health Monitor V2.1 + Storage Retention **V2.1.1** + Editable Copy Settings **V2.2.1**.
- **V2.1.1 storage retention — merged to main** (PR #8; merge commit `80bc4b5`, 2026-07 timeframe on main history).
  - Live Telegram verification for V2.1.1: passed (historical).
  - Full-suite baseline recorded at V2.1.1 completion: **652 passed, 2 skipped** (superseded by later V2.2.1 baseline below).
- **V2.2.1 Editable Copy Settings — completed, merged to main, and live-verified** (PR #9 from `feature/v2.2.1-copy-settings`; merge commit `cf6d2f5`).
  - Delivered: nine editable per-user settings; validation bounds; preview / cancel / confirm; Reset to Balanced; stale-token and concurrent-edit protection; per-user poll interval; per-user maximum event age.
  - Verification (PR #9 / V2.2.1 release evidence):
    - focused V2.2.1: **18 passed**;
    - Copy Intelligence: **144 passed**;
    - Telegram regressions: **67 passed**;
    - Storage/health: **126 passed, 2 skipped**;
    - full suite: **671 passed, 2 skipped**;
    - live Telegram verification: **GO**.
  - Formal Grill Me was **not** claimed for V2.2.1 (policy formalized from V2.2.2 onward).
- Safety defaults unchanged: real cleanup and pruning remain disabled / dry-run; no automatic VACUUM or `journal_mode` changes; backup creation is explicit and admin-confirmed.
- Copy freshness: baseline/watermarks/`processed_copy_events`; re-enable starts a new baseline; no alerts until baseline `COMPLETE`.
- Health: deterministic probes, admin-only System Health UX (`ADMIN_IDS`), deduplicated incidents; AI explainer off by default.
- The real `.env` was not modified as part of V2.1 / V2.1.1 / V2.2.1 / V2.2.2 source work.

### V2.2.2 — Trustworthy New/Increase Copy Alerts (implementation complete; live verification pending)

- Branch: `feature/v2.2.2-trustworthy-copy-alerts` (uncommitted implementation as of documentation update **2026-07-23**).
- Status: **implementation and automated verification complete; live Telegram verification still pending**. **Not released.** Do not mark complete until live verification GO.
- Automated verification (2026-07-23, Checkpoint 11):
  - `pytest -q tests/copy_intel` → **422 passed**;
  - Telegram regressions (commands/inline/scan/workflow/reply-keyboard + Copy formatting/delivery/watchlist) → **114 passed**;
  - health/storage/scheduler/readonly/weather-isolation → **136 passed, 2 skipped**;
  - full suite `pytest -q` → **949 passed, 2 skipped**;
  - Correction during Checkpoint 11: `:memory:` DB init must run V2.2.2 migration on the same connection as base schema (fixed and re-verified).
- Supported event types: **New Position (`open`) and Increase only**. Reduce and Close remain deferred.
- Authoritative trade timestamp required; missing/ambiguous correlation is quarantined and not alerted.
- Distinct price concepts: Trader VWAP, Market midpoint, Executable best ask, Paper execution VWAP.
- Slippage is allocation-based (execution VWAP vs midpoint from one validated book snapshot).
- Paper bankroll share means configured paper-bankroll share only (not a trader-bankroll estimate).
- Admin-only **owner-scoped** delivery (`added_by`); multi-user / non-admin subscriptions deferred.
- Paper entry uses execution VWAP; **no 0.5 fallback**.
- Telegram: plaintext FOLLOW/WATCH cards; validated `https://polymarket.com` Market/Trader links; **no inline buttons**.
- Paper-only / read-only boundary preserved; real trading not enabled; no authenticated CLOB trading calls.
- See `docs/COPY_ALERTS.md`, `docs/COPY_EVENT_FRESHNESS.md`, `ROADMAP.md`, `CHANGELOG.md`.

### Version naming (V2.2.x)

V2.2.x work uses numbered versions such as **V2.2.1**, **V2.2.2**, and **V2.2.3** — not “Phase”. Use a separate small version branch per version; never combine multiple V2.2.x scopes on one branch.

### Pre-V2.2 / ongoing requirements (still open for later expansion)

- official Polymarket API research (ongoing as needed per version);
- broader paper-copy portfolio architecture (beyond V2.2.2 paper commit path);
- security threat model (ongoing);
- limited Grill Me per feature (**required from V2.2.2 onward**).

### Strix (optional external audit)

- Strix was evaluated but unavailable locally; no Strix scan was executed and no findings were produced.
- Audit deferred to a disposable VM or isolated Docker host.
- Only a sanitized source-only worktree may be scanned; exclude `.env`, databases, backups, logs, caches, and runtime data.
- Strix is **not a blocker** for merged V2.1.1 / V2.2.1 work — it remains an optional external audit.

## V2.1 / V2.1.1 / V2.2.1 / V2.2.2 modules

| Area | Path |
|------|------|
| Freshness | `app/copy_intel/freshness.py` |
| Copy settings | `app/copy_intel/settings_*.py`, `app/telegram/copy_intel/*`, `docs/COPY_SETTINGS.md` |
| Trustworthy alerts (V2.2.2) | `app/copy_intel/{api_result,data_client,position_book,trade_correlation,order_book,recommendation,paper_commit,delivery}.py`, `app/telegram/copy_intel/formatting.py`, `docs/COPY_ALERTS.md` |
| Health core | `app/health/*` |
| Storage metrics / cleanup / backup / prune | `app/storage/*`, `app/health/storage_metrics.py`, `app/health/storage_alerts.py` |
| Admin auth | `app/telegram/auth.py` |
| Health + Storage UX | `app/telegram/health/*` |
| Docs | `AGENTS.md`, `docs/HEALTH_MONITOR.md`, `docs/COPY_EVENT_FRESHNESS.md`, `docs/COPY_SETTINGS.md`, `docs/COPY_ALERTS.md`, `ROADMAP.md`, `CHANGELOG.md` |

## Latest live weather proof

- Event: `Highest temperature in Austin on July 15?`
- Slug: `highest-temperature-in-austin-on-july-15-2026`
- Polymarket URL: `https://polymarket.com/event/highest-temperature-in-austin-on-july-15-2026`
- Region/city: USA / Austin
- Resolution station: `KAUS`
- Station coordinates: `30.1831, -97.68063`
- Resolution source: `https://www.wunderground.com/history/daily/us/tx/austin/KAUS`
- Target date/timezone: `2026-07-15` / `America/Chicago`
- Statistic/unit: daily high / Fahrenheit
- Real selected outcome: `82-83°F`, child market `2916028`
- Live YES price at proof time: `18%`
- Forecast: `82.6°F`
- Expected range: `78–87°F`
- Model probability: `20.3665%`
- Final status: `WATCH`
- Reason: Meets watch criteria but not full validated betting thresholds.
- Mapping checks: selected label existed in all 11 live children; selected market ID, label, and price matched the analysis.
- Remaining failure for this event: none. The card is correct; the broader MVP still lacks bounded Europe, Asia, and daily-low acceptance proofs.

## Weather MVP completion criteria

The weather MVP is complete only when:

1. A genuine active USA event produces a concrete Telegram card.
2. A genuine active Europe event produces a card when an active usable event exists.
3. A genuine active Asia event produces a card when an active usable event exists.
4. The exact Polymarket resolution station is used.
5. The correct local date and daily high/low statistic are used.
6. The selected outcome exists in the event.
7. The live price belongs to that exact outcome.
8. Sibling outcome probabilities are coherent.
9. WATCH and FORECAST ONLY remain visible when no VALIDATED signal exists.
10. Telegram does not return only counters when usable cards exist.
11. Targeted tests and bounded live read-only proofs pass.
12. No secrets or runtime artifacts are committed.
13. Scheduled reports run without duplicate bot instances.
14. The system works after a fresh clone and documented setup.

Major new product verticals remain blocked until these criteria are met.

## Development roadmap

### Phase 0 — Finish one event end to end

Acceptance card must contain station, resolution source, forecast, expected range, exact real outcome, exact live price, model probability, status, and one-line reason.

Current result: one active USA event satisfies this acceptance card.

### Phase 1 — Stabilize weather MVP

- verify at least three USA events;
- verify Europe;
- verify Asia;
- verify daily-high and daily-low markets;
- fix regional counters;
- ensure usable cards appear instead of counters only.

### Phase 2 — Forecast quality

- save forecast snapshots;
- compare forecasts with resolved outcomes;
- calculate Brier score;
- measure bucket hit rate;
- measure error by station and lead time;
- validate provider performance;
- prohibit uncalibrated confidence claims.

### Phase 3 — Reliability and operations

- optimize caching;
- reduce scan latency;
- add bounded retries and rate limits;
- verify scheduler reliability;
- document VPS deployment;
- [x] Health Monitor + admin diagnostics (V2.1);
- [x] Copy event freshness / baseline protection (V2.1);
- [x] Storage metrics, cleanup, backup, retention pruning, admin Storage UX (V2.1.1 — merged);
- [x] Editable Copy Settings (V2.2.1 — merged, live-verified);
- [ ] Trustworthy New/Increase Copy Alerts (V2.2.2 — implementation + automated tests complete; live Telegram verification pending).

### Phase 4 — Telegram UX

- fast regional discovery;
- selected-event deep analysis;
- concise result cards;
- direct Polymarket links;
- clearer WATCH and SKIP reasons;
- historical results.

### Phase 5 — Future read-only modules

Only after weather MVP acceptance, evaluate separate modules for arbitrage detection, exchange comparison, crypto market scanning, coin monitoring, public-wallet and trader analytics, copy-trader research, and resale opportunity scanning.

Each future module must have a separate collector, normalizer, signal engine, cache, tests, and Telegram view.

### Phase 6 — Financial validation

Before execution features: paper tracking, historical simulation, fees, slippage, liquidity, execution delay, risk limits, false-positive analysis, and legal and platform constraints.

### Phase 7 — Execution

Wallet connection, order placement, automatic trading, and automatic copying remain prohibited until separately approved after security and risk review.

## Do not build now

- automatic trading;
- wallet integration;
- leverage;
- automatic copy trading;
- broad exchange integrations;
- unrelated market categories;
- LLM-generated probability guesses;
- decorative dashboards;
- large refactors;
- unsupported weather providers.

## Highest-priority next task

Complete **separately approved live Telegram verification** for V2.2.2 on `feature/v2.2.2-trustworthy-copy-alerts` (see live-verification checklist in Checkpoint 11 report / `docs/COPY_ALERTS.md`). Do not commit, push, or open a PR until live verification is GO and an explicit release prompt is given. After V2.2.2 release, continue with V2.2.3+ on separate version branches per `ROADMAP.md`.

## Fresh Cursor context startup prompt

```text
Read PROJECT_STATUS.md, README.md, the latest five Git commits, and the current git status.

Do not modify code yet.

Verify the documented repository state against the current code. Report only discrepancies.

Then continue only the single “Highest-priority next task” from PROJECT_STATUS.md.

Follow all 11 non-negotiable development rules:

- one bounded task;
- one minimal patch;
- targeted tests;
- one bounded live read-only proof;
- stop for review;
- no commit or push unless explicitly requested.
```
