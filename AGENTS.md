# AGENTS.md: Release Copilot

Instructions for AI coding agents working in this repository.

- Multi-agent LangGraph release automation (the agents repo). The roadmap is in `PLAN.md`, the work is in
  `STORIES.md`; work only on the story you are assigned, and update only its status marker.
- The enterprise systems are mocked in the sibling repo `../release-copilot-mocks` (an editable **dev**
  dependency). Tests use them in-process through the `mocks_http` / `mocks_app` fixtures in `tests/conftest.py`;
  locally you can run them with `uv run --directory ../release-copilot-mocks python -m release_copilot_mocks`.
  Application code must never import `release_copilot_mocks`; it only knows base URLs from config.
- Agents must never call `/_admin` as a tool; it is for tests and evals only.
- `uv sync` then `uv run pytest`; tests must pass and must not call real LLMs or external services.
- Never run destructive git commands (reset --hard, push --force, branch -D) without asking.
