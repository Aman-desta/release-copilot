"""Versioned storage for release documents (``release_documents``).

Every document a run produces is kept, valid or rejected: a rejected document is evidence of what the writer got
wrong, and the audit trail needs it. Versions count up per release (``payments-api@2.4.0``: 1, 2, 3...).

**Ensure state, not "do action".** Saving the same content for the same release again returns the existing version
instead of adding one: a retried node, or a re-run on unchanged evidence, doesn't create v2, v3... of the same
document. The content key covers the Markdown, the status and the evidence fingerprint, so the same words over
different evidence are a new version.

Two implementations with one contract (tests/test_documents.py runs both): ``InMemoryDocumentStore`` for tests and
local runs, ``PostgresDocumentStore`` for real ones. In Postgres, concurrent saves for one release are serialised
by a transaction-level advisory lock; the unique constraints are the backstop.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, LiteralString, Protocol

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel, ConfigDict

from release_copilot.documents.validate import ValidationIssue

DocumentStatus = Literal["valid", "rejected"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NewDocument(_Frozen):
    release: str  # the release's correlation id, e.g. payments-api@2.4.0
    run_id: str
    status: DocumentStatus
    markdown: str
    evidence_fingerprint: str  # EvidenceIndex.fingerprint() of the evidence it was written from
    issues: tuple[ValidationIssue, ...] = ()  # empty when valid

    @property
    def content_key(self) -> str:
        content = json.dumps([self.markdown, self.status, self.evidence_fingerprint], ensure_ascii=False)
        return hashlib.sha256(content.encode()).hexdigest()


class DocumentRecord(_Frozen):
    release: str
    version: int
    run_id: str  # the run that first produced this content
    status: DocumentStatus
    markdown: str
    content_key: str
    evidence_fingerprint: str
    issues: tuple[ValidationIssue, ...] = ()
    created_at: datetime


class DocumentStore(Protocol):
    async def save(self, doc: NewDocument) -> DocumentRecord:
        """Store ``doc`` as the release's next version, or return the version that already has this content."""
        ...

    async def versions(self, release: str) -> list[DocumentRecord]:
        """Every version of the release's document, oldest first."""
        ...

    async def latest(self, release: str, *, status: DocumentStatus | None = None) -> DocumentRecord | None: ...


class InMemoryDocumentStore:
    def __init__(self) -> None:
        self._records: dict[str, list[DocumentRecord]] = {}
        self._lock = asyncio.Lock()

    async def save(self, doc: NewDocument) -> DocumentRecord:
        async with self._lock:
            records = self._records.setdefault(doc.release, [])
            existing = next((r for r in records if r.content_key == doc.content_key), None)
            if existing is not None:
                return existing
            record = DocumentRecord(
                **doc.model_dump(exclude={"issues"}), issues=doc.issues, version=len(records) + 1,
                content_key=doc.content_key, created_at=datetime.now(UTC),
            )
            records.append(record)
            return record

    async def versions(self, release: str) -> list[DocumentRecord]:
        return list(self._records.get(release, ()))

    async def latest(self, release: str, *, status: DocumentStatus | None = None) -> DocumentRecord | None:
        return next((r for r in reversed(self._records.get(release, ())) if status in (None, r.status)), None)


_COLUMNS = "release, version, run_id, status, markdown, content_key, evidence_fingerprint, issues, created_at"


class PostgresDocumentStore:
    """``release_documents`` in the app schema (migration 0002). One short connection per call until P1 adds a pool."""

    def __init__(self, conninfo: str, schema: str) -> None:
        self._conninfo = conninfo
        self._table = sql.Identifier(schema, "release_documents")

    def _query(self, text: LiteralString) -> sql.Composed:
        return sql.SQL(text).format(table=self._table, columns=sql.SQL(_COLUMNS))

    async def _rows(self, conn: psycopg.AsyncConnection, query: LiteralString, params: tuple) -> list[DictRow]:
        """Only literal SQL reaches ``sql.SQL``: values always go as parameters, identifiers through ``sql.Identifier``."""
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(self._query(query), params)
            return await cur.fetchall()

    async def save(self, doc: NewDocument) -> DocumentRecord:
        async with await psycopg.AsyncConnection.connect(self._conninfo) as conn, conn.transaction():
            # Serialise saves for this release: "max(version) + 1" is only safe if nobody else is computing it.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"release_documents:{doc.release}",))
            rows = await self._rows(
                conn, "SELECT {columns} FROM {table} WHERE release = %s AND content_key = %s", (doc.release, doc.content_key)
            )
            if rows:
                return _record(rows[0])
            rows = await self._rows(
                conn,
                "INSERT INTO {table} (release, version, run_id, status, markdown, content_key, evidence_fingerprint, issues) "
                "SELECT %s, coalesce(max(version), 0) + 1, %s, %s, %s, %s, %s, %s::jsonb FROM {table} WHERE release = %s "
                "RETURNING {columns}",
                (
                    doc.release, doc.run_id, doc.status, doc.markdown, doc.content_key, doc.evidence_fingerprint,
                    json.dumps([i.model_dump(mode="json") for i in doc.issues]), doc.release,
                ),
            )
            return _record(rows[0])

    async def versions(self, release: str) -> list[DocumentRecord]:
        async with await psycopg.AsyncConnection.connect(self._conninfo) as conn:
            rows = await self._rows(conn, "SELECT {columns} FROM {table} WHERE release = %s ORDER BY version", (release,))
            return [_record(row) for row in rows]

    async def latest(self, release: str, *, status: DocumentStatus | None = None) -> DocumentRecord | None:
        async with await psycopg.AsyncConnection.connect(self._conninfo) as conn:
            rows = await self._rows(
                conn,
                "SELECT {columns} FROM {table} WHERE release = %s AND (%s::text IS NULL OR status = %s) ORDER BY version DESC LIMIT 1",
                (release, status, status),
            )
            return _record(rows[0]) if rows else None


def _record(row: DictRow) -> DocumentRecord:
    return DocumentRecord.model_validate({**row, "issues": tuple(row["issues"])})
