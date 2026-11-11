"""release_documents: every release document a run produced, versioned per release (story A4)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # env.py sets search_path to the app schema, so the table lands there.
    op.execute(
        """
        CREATE TABLE release_documents (
            id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            release              text        NOT NULL,
            version              integer     NOT NULL CHECK (version > 0),
            run_id               text        NOT NULL,
            status               text        NOT NULL CHECK (status IN ('valid', 'rejected')),
            markdown             text        NOT NULL,
            content_key          char(64)    NOT NULL,
            evidence_fingerprint char(64)    NOT NULL,
            issues               jsonb       NOT NULL DEFAULT '[]'::jsonb,
            created_at           timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT release_documents_version_uq UNIQUE (release, version),
            CONSTRAINT release_documents_content_uq UNIQUE (release, content_key)
        )
        """
    )
    op.execute("COMMENT ON TABLE release_documents IS 'A4: release documents, valid and rejected, versioned per release'")


def downgrade() -> None:
    op.execute("DROP TABLE release_documents")
