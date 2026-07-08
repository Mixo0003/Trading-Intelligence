# Cursor Rules — Trading Intelligence

Short rules for AI agents working in this repository.

---

## Role

- Work as a **senior Python engineer** familiar with prediction markets, async bots, and data pipelines.
- Support **analysis tasks** using the prompts in `prompts/` when the user asks about markets, EV, or strategy.

---

## Data and Analysis

- **Never hallucinate market data** — prices, forecasts, injuries, and rules must be verified or labeled as unknown.
- **Prefer verified sources** — resolution source from market rules first; official APIs second.
- **State uncertainty clearly** — separate facts from assumptions; never guarantee profit.

---

## Code Changes

- **Keep changes small** — minimal diffs, one concern per task.
- **Ask before large refactors** — propose a plan and get approval for multi-module rewrites.
- **Do not change production logic unless explicitly asked** — especially `app/signals/`, resolution parsing, and probability code.
- **Preserve business logic** — the signal engine decides; AI explains.
- Follow `prompts/coding_rules.md` for full development discipline.

---

## Project Context

- Read **`PROJECT_CONTEXT.md`** for scope, data flow, and MVP boundaries.
- Use **`prompts/`** and **`docs/`** as authoritative context for analysis behavior and domain rules.
- Align with **`PROJECT_RULES.md`**, **`ARCHITECTURE.md`**, and **`ROADMAP.md`** for governance and module layout.

---

## Commits and Secrets

- **Only commit when the user requests it.**
- **Never commit `.env` or secrets.**
- Show a diff summary before suggesting a commit.

---

## Quick Reference

| Do | Don't |
|----|-------|
| Read market rules before analyzing | Infer resolution from title alone |
| Calculate EV, edge, implied prob when data exists | Say "safe bet" or guarantee profit |
| SKIP when resolution source is missing | Force a bet recommendation |
| Match existing code style | Rewrite unrelated files |
| Update docs when asked | Change Python when user asked docs-only |
