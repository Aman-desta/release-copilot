.PHONY: up down ps logs migrate revision psql psql-admin test db-destroy obs-up obs-down obs-logs review

OBS = docker compose -f compose.observability.yaml

up:            ## start Postgres + mocks and wait until healthy
	docker compose up -d --build
	@# Wait until every container in the project is healthy. (Older Compose versions ignore the service
	@# argument to "ps -q", so check all containers instead of one per service.) Gives up after 120s.
	@for i in $$(seq 120); do \
	  states="$$(docker inspect -f '{{.State.Health.Status}}' $$(docker compose ps -q) | sort -u)"; \
	  [ "$$states" = healthy ] && break; \
	  [ $$i = 120 ] && { echo "containers not healthy: $$states"; docker compose ps; exit 1; }; \
	  sleep 1; \
	done
	@docker compose ps

down:          ## stop containers (data volume is kept)
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f --tail=100

migrate:       ## apply migrations as the app role
	uv run alembic upgrade head

revision:      ## make revision m="add runs table"
	uv run alembic revision -m "$(m)"

psql:          ## shell as the app role
	docker compose exec postgres sh -c 'PGPASSWORD="$$APP_DB_PASSWORD" psql -h localhost -U "$$APP_DB_USER" -d "$$POSTGRES_DB"'

psql-admin:    ## shell as the superuser (extensions, roles)
	docker compose exec postgres psql -U postgres -d $${RELEASE_COPILOT_DB_NAME:-release_copilot}

test:
	uv run pytest

db-destroy:    ## DESTRUCTIVE: delete containers AND the data volume (init scripts re-run on next up)
	docker compose down -v

obs-up:        ## start Phoenix (:6006) and Langfuse (:3000), wait until healthy (the worker has no health check)
	$(OBS) up -d
	@for i in $$(seq 180); do \
	  states="$$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $$($(OBS) ps -q) | sort -u | tr '\n' ' ')"; \
	  case "$$states" in *starting*|*unhealthy*|*created*) ;; *) break;; esac; \
	  [ $$i = 180 ] && { echo "not healthy: $$states"; $(OBS) ps; exit 1; }; \
	  sleep 1; \
	done
	@$(OBS) ps

obs-down:      ## stop the tracing backends (data volumes are kept)
	$(OBS) down

obs-logs:
	$(OBS) logs -f --tail=100

review:        ## make review s=clean: one traced review (needs make up, make obs-up and a real LLM in .env)
	uv run python -m release_copilot.review --scenario $(or $(s),clean)
