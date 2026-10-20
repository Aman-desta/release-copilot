#!/usr/bin/env bash
# Runs once, as the superuser, when the data volume is first initialised.
#
# - Enables pgvector in the app database (extensions need superuser; the app role never gets it).
# - Creates the least-privilege runtime role: LOGIN only, no SUPERUSER / CREATEDB / CREATEROLE.
# - Gives that role its own schema. Since Postgres 15, PUBLIC cannot create objects in schema "public",
#   so the app (Alembic, the LangGraph checkpointer and store) creates its tables in this schema.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_user="$APP_DB_USER" -v app_password="$APP_DB_PASSWORD" -v app_schema="$APP_DB_SCHEMA" \
  -v db_name="$POSTGRES_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;

CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db_name" TO :"app_user";

CREATE SCHEMA :"app_schema" AUTHORIZATION :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";  -- pgvector's types and operators live in public

ALTER ROLE :"app_user" IN DATABASE :"db_name" SET search_path = :"app_schema", public;
SQL
