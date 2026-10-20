# Release Copilot

Multi-agent LangGraph release automation. A supervisor and worker agents pull Jira tickets, GitHub diffs and
alerts, SonarQube coverage and Azure App Service deployment state; then generate the release document, open a
ServiceNow change request, route approval by risk, and plan the rollback. Runs are traced and scored by an
LLM judge.

This is a learning project: the roadmap is in [PLAN.md](PLAN.md) and the work is in [STORIES.md](STORIES.md).

## Repositories

| Repo | What it is |
|---|---|
| `release-copilot` (this one) | Agents, tools, integrations, persistence, guardrails, evals |
| [`release-copilot-mocks`](../release-copilot-mocks) | Mock Jira, GitHub, Azure, SonarQube, ServiceNow and Teams/Slack APIs with scenarios and fault injection |

The mocks copy the real APIs, so pointing the clients at real services only changes base URLs and credentials.

## Setup

Clone both repos side by side, then:

```bash
uv sync                  # also installs ../release-copilot-mocks (editable, dev only)
cp .env.example .env     # local development values
uv run pytest            # tests run the mocks in-process; database tests skip without a database
```

## Local infrastructure

`compose.yaml` runs Postgres 17 with pgvector and the mock services (built from `../release-copilot-mocks`).

```bash
make up          # start Postgres + mocks, wait until healthy
make migrate     # alembic upgrade head, as the app role
make psql        # psql as the app role (psql-admin for the superuser)
make down        # stop; data stays in the pgdata volume
```

| Service | Address | Notes |
|---|---|---|
| Postgres | `localhost:${RELEASE_COPILOT_DB_PORT:-5432}` | database `release_copilot` |
| Mocks | `http://localhost:${RELEASE_COPILOT_MOCKS_PORT:-9100}` | API docs at `/docs` |

Database layout:
- On first start, `docker/postgres/initdb.d/01-app-role.sh` enables `vector` and creates the least-privilege role
  `copilot_app` (no superuser, createdb or createrole) with its own schema `copilot`.
- The app, Alembic and the LangGraph checkpointer and store all create their tables there; `public` stays closed.
- Init scripts run only on an empty volume: after changing them, run `make db-destroy` (**deletes all data**), then `make up`.

To run the database tests, set `RELEASE_COPILOT_TEST_DATABASE_URL` (see `.env.example`) and run `uv run pytest tests/test_database.py`.

To run only the mocks, without Docker:

```bash
uv run --directory ../release-copilot-mocks python -m release_copilot_mocks --scenario clean
```
