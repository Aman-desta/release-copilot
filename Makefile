.PHONY: up down ps logs migrate revision psql psql-admin test db-destroy

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
