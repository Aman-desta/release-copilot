# AGENTS.md: Release Copilot

Instructions for AI coding agents working in this repository.

- Learning project; the phases are in `PLAN.md`. Work only on the phase you are asked to.
- `src/release_copilot/mocks/`: mock enterprise services (Phase 0). Routers mirror the real vendor paths and
  JSON shapes; keep them that way, so real clients only need a different base URL. Scenario data lives in
  `mocks/scenarios/` (`base.py` world + `variants.py` edits + `expected` labels).
- Agents must never call `/_admin` as a tool; it is for tests and evals only.
- `uv sync` then `uv run pytest`; tests must pass and must not call real LLMs or external services.
- Never run destructive git commands (reset --hard, push --force, branch -D) without asking.
