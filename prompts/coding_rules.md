# Coding Rules for Cursor

Rules for AI-assisted development in the Trading Intelligence repository.

---

## Before Making Code Changes

1. **Explain the plan** — describe what will change, which files, and why.
2. **Show affected files before making changes** — list every file that will be created, modified, or deleted; wait for implicit or explicit approval on large scopes.
3. **Read surrounding code** — match existing patterns, naming, and module boundaries.
4. **Confirm scope** — only change what the task requires; do not refactor unrelated areas.

---

## Change Discipline

- **Make minimal safe changes.** Prefer the smallest diff that solves the problem.
- **Do not rewrite unrelated files.** Touch only modules directly involved in the task.
- **Do not create huge rewrites unless explicitly requested.** No full-module refactors, architecture overhauls, or mass reformatting without user approval.
- **Preserve existing business logic** unless the user explicitly requests a logic change.
- **One commit = one finished task** (per `PROJECT_RULES.md`).

---

## Documentation-Only Tasks

When the user asks for documentation, prompts, or config text only:

- **Do not touch Python files** — no changes under `app/`, `*.py`, or test modules unless explicitly requested.
- Limit edits to `prompts/`, `docs/`, `*.md`, and other non-code artifacts as scoped.
- Do not "helpfully" fix code while updating docs.

---

## Architecture Awareness

- **GitHub is the source of truth**; VPS runs deployed code only.
- **Signal Engine makes decisions**; Claude/AI explains results — do not invert this without explicit approval.
- **Resolution-first** for weather: always parse market rules before fetching forecasts.
- Follow module layout in `ARCHITECTURE.md`:
  - `app/polymarket/` — API and market parsing
  - `app/weather/` — station, forecast, resolution
  - `app/signals/` — engine, scoring, validation
  - `app/telegram/` — bot interface
  - `app/database/` — SQLite storage
  - `app/config/` — settings

---

## Testing and Verification

- **Add tests when changing logic** — especially signal, probability, and parsing code.
- **Run compile/check commands after edits** — e.g., syntax check, linter, or test suite if available.
- **Do not push broken code to main.**

---

## Secrets and Safety

- **Never commit secrets** — `.env`, API keys, tokens stay local/VPS only.
- Use `.env.example` for documented placeholders.
- Rotate any token that was ever exposed publicly.

---

## Documentation

- Update `CHANGELOG.md` after meaningful changes.
- Follow `ROADMAP.md` order; do not add new market types before Polymarket Weather V1 works.
- Use `prompts/` and `docs/` as project context for analysis behavior.

---

## Before Suggesting a Commit

- **Show git diff summary** — list changed files and brief description of each change.
- **Only commit when the user asks** — do not commit proactively.
- Ensure no secrets or accidental large generated files are staged.

---

## When Not to Change Code

- User asks for **documentation only** → create/update docs and prompts, not Python.
- User asks for **market analysis** → use prompts in `prompts/`, do not modify `app/`.
- User asks **questions about behavior** → read and explain; do not "fix" unless asked.

Refer also to `cursor_rules.md` in the repository root.
