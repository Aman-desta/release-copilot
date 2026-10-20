"""F2 acceptance against a real Postgres. Skipped unless RELEASE_COPILOT_TEST_DATABASE_URL is set (see .env.example)."""

import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

URL = os.environ.get("RELEASE_COPILOT_TEST_DATABASE_URL")
SCHEMA = os.environ.get("RELEASE_COPILOT_DB_SCHEMA", "copilot")
pytestmark = pytest.mark.skipif(not URL, reason="RELEASE_COPILOT_TEST_DATABASE_URL not set")


@pytest.fixture
def conn():
    with psycopg.connect(URL.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as c:
        yield c


def test_connects_as_least_privilege_role(conn):
    user, superuser, createdb, createrole = conn.execute(
        "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    assert user != "postgres"
    assert (superuser, createdb, createrole) == (False, False, False)


def test_pgvector_is_installed_and_usable(conn):
    assert conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
    conn.execute("CREATE TEMP TABLE v (embedding vector(3))")
    conn.execute("INSERT INTO v VALUES ('[1,0,0]'), ('[0,1,0]')")
    nearest = conn.execute("SELECT embedding::text FROM v ORDER BY embedding <=> '[0.9,0.1,0]' LIMIT 1").fetchone()[0]
    assert nearest == "[1,0,0]"


def test_app_tables_default_to_app_schema_and_public_is_closed(conn):
    assert conn.execute("SELECT current_schema()").fetchone()[0] == SCHEMA
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("CREATE TABLE public.should_fail (id int)")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def test_alembic_upgrade_head(conn):
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.attributes.update(database_url=URL, db_schema=SCHEMA)
    command.upgrade(cfg, "head")
    assert conn.execute(f'SELECT version_num FROM "{SCHEMA}".alembic_version').fetchone()[0] == "0001"
