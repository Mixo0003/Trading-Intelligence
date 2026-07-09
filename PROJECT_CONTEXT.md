# Project Context — Trading Intelligence

## Overview

**Trading Intelligence (TI)** is a private Python project for analyzing **Polymarket** and other **prediction markets**. It scans markets, evaluates mispricing against verified external data, and produces actionable signals with explicit risk guidance.

The system is designed for **resolution-first**, **EV-driven** analysis — not narrative trading or guaranteed outcomes.

---

## Current MVP Scope (V1)

The active MVP focuses on **Polymarket weather markets**:

1. **Scan** Polymarket for weather-related events and markets.
2. **Read** market title, description, rules, and resolution source from Polymarket metadata.
3. **Extract** resolution URL and/or weather station code (e.g., KORD, KJFK, Wunderground link) from rules text.
4. **Fetch forecast** from the appropriate provider (NWS/weather.gov for US cross-check; resolution-named source is primary).
5. **Compare** model probability vs current market price (implied probability).
6. **Return** a signal (**BET** / tier recommendation) or **SKIP** when rules or data are insufficient.

### Delivery surface

- **Telegram bot** — menus, market discovery, `/analyze` on Polymarket URLs
- **SQLite** — users, filters, and analysis history
- **Signal engine** (`app/signals/`) — makes final decisions; AI explains results

### Not in V1

- Auto-trading
- Kalshi, crypto exchanges, wallet tracking
- Sports markets (rules documented in `prompts/sports.md` for future use)

---

## Repository Layout

```
app/
  main.py              — application entry
  polymarket/          — API client, market parser, models
  weather/             — resolution parsing, station resolver, forecast
  signals/             — scanner, engine, probability, comparator, validator
  telegram/            — bot UI, notifications, commands
  database/            — SQLite models and storage
  config/              — settings and constants
  scheduler/           — scan loop and runner
  filters/             — user filter service

prompts/               — AI system and domain prompts
docs/                  — extended documentation
PROJECT_CONTEXT.md     — this file
cursor_rules.md        — Cursor agent rules
ARCHITECTURE.md        — module architecture
PROJECT_RULES.md       — development governance
ROADMAP.md             — version roadmap
```

Entry points:

- `app/main.py` — modular application
- `ti_polymarket_weather_bot_v1.py` — V1 bot entry (legacy/monolith path)

---

## Core Principles

| Principle | Description |
|-----------|-------------|
| **Resolution-first** | Parse market rules before any forecast or probability work |
| **No invented data** | Prices, forecasts, and rules must come from verified sources |
| **Engine decides** | Signal engine produces the outcome; Claude/AI explains it |
| **SKIP when unclear** | Missing resolution source or station → do not force a bet |
| **No profit guarantees** | All outputs include uncertainty and risk |

---

## Data Flow (Weather MVP)

```
Polymarket URL/event
    → polymarket/client + parser (title, description, rules, prices)
    → weather/resolution + station_resolver (station code, Wunderground URL)
    → weather/forecast (NWS or rule-specified source)
    → signals/probability + comparator (model vs implied prob, EV, edge)
    → signals/engine + validator (BET / SKIP / tier)
    → telegram/notifier (user delivery)
```

---

## Environment

Required configuration (see `.env.example`):

- `TELEGRAM_TOKEN`
- `ANTHROPIC_API_KEY`
- `ADMIN_IDS`

Dependencies: `requirements_v1.txt`

---

## AI and Prompts

Prompt stack (read in order for market analysis):

1. `prompts/system_prompt.md` — **AI constitution** (truth-first, no hallucination, no profit guarantees)
2. `prompts/thinking_rules.md` — internal analysis discipline and skip conditions
3. `prompts/strategy.md` — end-to-end market analysis workflow
4. `prompts/risk_policy.md` — bankroll and EV thresholds
5. `prompts/response_format.md` — standard analysis output template
6. Domain prompts — `polymarket.md`, `weather.md`, `sports.md`

Development and Cursor:

- `prompts/coding_rules.md` — code change discipline
- `cursor_rules.md` — short Cursor agent rules

---

## Related Documents

- [README_V1.md](README_V1.md) — setup and Telegram test steps
- [ARCHITECTURE.md](ARCHITECTURE.md) — module map
- [PROJECT_RULES.md](PROJECT_RULES.md) — team rules and V1 scope lock
- [ROADMAP.md](ROADMAP.md) — delivery checklist
- [CHANGELOG.md](CHANGELOG.md) — version history
