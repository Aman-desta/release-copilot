"""Alembic environment: app-owned tables live in the app schema; the URL comes from settings.

LangGraph's checkpointer and store tables are created by their own ``.setup()`` (story P1), not by Alembic,
so autogenerate ignores them.
"""

from alembic import context
from sqlalchemy import create_engine, pool, text

from release_copilot.config import get_settings

config = context.config
target_metadata = None  # set to the app's SQLAlchemy MetaData once tables exist (story P1)

LIBRARY_TABLES = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations", "store", "store_vectors", "store_migrations", "vector_migrations"}


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    return not (type_ == "table" and name in LIBRARY_TABLES)


def _url_and_schema() -> tuple[str, str]:
    # A URL passed programmatically (tests) wins over settings.
    url = config.attributes.get("database_url") or get_settings().database_url.get_secret_value()
    return url, config.attributes.get("db_schema") or get_settings().db_schema


def run_migrations_offline() -> None:
    url, schema = _url_and_schema()
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, version_table_schema=schema, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url, schema = _url_and_schema()
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text(f'SET search_path TO "{schema}", public'))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=schema,
            include_schemas=False,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
