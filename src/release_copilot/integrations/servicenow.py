"""ServiceNow Table API client: change requests, approvals, CMDB lookups (story T4).

YOUR PART: ``Q`` and ``ServiceNowClient``. The contract is in tests/test_servicenow.py.

ServiceNow quirks this client hides from callers:
- Every value is a string; reference fields arrive as ``{"link": ..., "value": sys_id}`` (or a plain sys_id
  when ``sysparm_exclude_reference_link=true``): callers only ever see sys_ids.
- Queries are *encoded strings* (``field=value^field2!=x^ORDERBYfield``). Values are untrusted, so they must be
  escaped (``^`` -> ``^^``), or a value like ``x^ORactive=true`` rewrites the query: query injection.
- Business rules answer 403: those become ``PolicyViolationError``, not "permission denied".
- There is no idempotency key on create: ``correlation_id`` plus "query before create" is the pattern (A6).
"""

from collections.abc import AsyncIterator, Iterable, Sequence
import re
from typing import Any

from release_copilot.integrations.errors import ConflictError, IntegrationError, PermissionDeniedError, PolicyViolationError
from release_copilot.integrations.http import ServiceClient
from release_copilot.integrations.models import (
    Approval,
    ChangeRequest,
    ChangeRequestDraft,
    ConfigurationItem,
    SnowGroup,
    SnowUser,
)

TABLE = "/api/now/table/{table}"
RECORD = "/api/now/table/{table}/{sys_id}"
_FIELD = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*")


class Q:
    """Immutable builder for ServiceNow encoded queries. Every method returns a NEW ``Q``.

        Q().eq("cmdb_ci", ci).ne("state", "4").order_by_desc("sys_created_on")
        -> "cmdb_ci=<ci>^state!=4^ORDERBYDESCsys_created_on"

    Rules:
    - Field names must match ``^[a-z_][a-z0-9_]*(\\.[a-z_][a-z0-9_]*)*$`` (dot-walking allowed), else ValueError.
    - Values are converted with ``str()`` and every ``^`` is escaped as ``^^``.
    - ``in_(field, values)``: values are joined with ``,``. A comma can't be escaped there, so a value containing
      one raises ValueError; an empty ``values`` raises ValueError too (it would match everything or nothing).
    - ``str(q)`` / ``q.build()`` give the encoded string; an empty ``Q()`` is ``""``.
    """

    def __init__(self, parts: tuple[str, ...] = ()) -> None:
        self._parts = parts

    @staticmethod
    def _field(field: str) -> str:
        if not _FIELD.fullmatch(field):
            raise ValueError(f"Invalid ServiceNow field name: {field!r}")
        return field

    @staticmethod
    def _value(value: object) -> str:
        return str(value).replace("^", "^^")

    def _append(self, part: str) -> "Q":
        return Q((*self._parts, part))

    def eq(self, field: str, value: object) -> "Q":
        return self._append(f"{self._field(field)}={self._value(value)}")

    def ne(self, field: str, value: object) -> "Q":
        return self._append(f"{self._field(field)}!={self._value(value)}")

    def like(self, field: str, value: object) -> "Q":
        return self._append(f"{self._field(field)}LIKE{self._value(value)}")

    def starts_with(self, field: str, value: object) -> "Q":
        return self._append(f"{self._field(field)}STARTSWITH{self._value(value)}")

    def in_(self, field: str, values: Iterable[object]) -> "Q":
        checked_field = self._field(field)
        escaped: list[str] = []
        for value in values:
            text = str(value)
            if "," in text:
                raise ValueError("IN values cannot contain commas")
            escaped.append(self._value(text))
        if not escaped:
            raise ValueError("IN requires at least one value")
        return self._append(f"{checked_field}IN{','.join(escaped)}")

    def order_by(self, field: str) -> "Q":
        return self._append(f"ORDERBY{self._field(field)}")

    def order_by_desc(self, field: str) -> "Q":
        return self._append(f"ORDERBYDESC{self._field(field)}")

    @property
    def ordered(self) -> bool:
        # Field names are lowercase (validated), so only order_by/order_by_desc produce an uppercase ORDERBY part.
        return any(part.startswith("ORDERBY") for part in self._parts)

    def build(self) -> str:
        return "^".join(self._parts)

    def __str__(self) -> str:
        return self.build()


def _checked(query: Q | None) -> Q:
    if query is None:
        return Q()
    if not isinstance(query, Q):
        raise TypeError(f"queries must be built with Q (values are escaped there), not {type(query).__name__}")
    return query


class ServiceNowClient:
    def __init__(self, http: ServiceClient, *, page_size: int = 100) -> None:
        self._http = http
        self.page_size = page_size

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "ServiceNowClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------------------------ generic table

    async def records(self, table: str, query: Q | None = None, *, fields: Sequence[str] | None = None) -> AsyncIterator[dict[str, Any]]:
        """Every matching record, paging with ``sysparm_limit`` / ``sysparm_offset`` until all ``X-Total-Count``
        records are read. Reference fields are flattened to sys_ids. Always sends ``sysparm_display_value=false``.

        Only ``Q`` is accepted, never a raw string: that keeps every value escaped (no query injection).
        Offset paging needs a stable order, so ``ORDERBYsys_id`` is added when the query has none, and a record
        seen on an earlier page (the table changed while paging) is not yielded twice.
        """
        if self.page_size <= 0:
            raise ValueError("page_size must be greater than zero")
        query = _checked(query)
        if not query.ordered:
            query = query.order_by("sys_id")
        if fields and "sys_id" not in fields:
            fields = [*fields, "sys_id"]  # needed to de-duplicate
        seen: set[str] = set()
        offset = 0
        total: int | None = None
        while total is None or offset < total:
            response = await self._http.request(
                "GET",
                TABLE,
                path_params={"table": table},
                params={
                    "sysparm_query": str(query),
                    "sysparm_limit": self.page_size,
                    "sysparm_offset": offset,
                    "sysparm_display_value": "false",
                    **({"sysparm_fields": ",".join(fields)} if fields else {}),
                },
            )
            header = response.headers.get("X-Total-Count")
            try:
                page_total = int(header) if header is not None else -1
            except ValueError:
                page_total = -1
            if page_total < 0:
                raise IntegrationError(
                    "ServiceNow pagination response is missing a valid X-Total-Count header",
                    service="servicenow",
                    method="GET",
                    path=TABLE,
                )
            total = page_total
            body = response.json()
            batch = body.get("result") if isinstance(body, dict) else None
            if not isinstance(batch, list):
                raise IntegrationError(
                    "ServiceNow pagination response does not contain a result list",
                    service="servicenow",
                    method="GET",
                    path=TABLE,
                )
            if not batch and offset < total:
                raise IntegrationError(
                    f"ServiceNow pagination ended after {offset} of {total} records",
                    service="servicenow",
                    method="GET",
                    path=TABLE,
                )
            for record in batch:
                if offset >= total:
                    break
                if not isinstance(record, dict):
                    raise IntegrationError(
                        "ServiceNow pagination returned a non-object record",
                        service="servicenow",
                        method="GET",
                        path=TABLE,
                    )
                offset += 1
                flat = self._flatten_record(record)
                if flat.get("sys_id") in seen:
                    continue
                seen.add(flat.get("sys_id"))
                yield flat

    # ---------------------------------------------------------------------------------- change requests

    async def get_change(self, sys_id: str) -> ChangeRequest:
        raw = await self._http.get_json(
            RECORD,
            path_params={"table": "change_request", "sys_id": sys_id},
            params={"sysparm_display_value": "false"},
        )
        return self._change(raw["result"])

    async def find_changes(self, query: Q | None = None) -> list[ChangeRequest]:
        return [self._change(record) async for record in self.records("change_request", query)]

    async def find_change_by_correlation_id(self, correlation_id: str) -> ChangeRequest | None:
        """The one change for ``correlation_id`` (e.g. ``payments-api@2.4.0``), or None. More than one is a data
        problem: raise ``ConflictError`` rather than pick one."""
        matches: list[ChangeRequest] = []
        query = Q().eq("correlation_id", correlation_id)
        async for record in self.records("change_request", query):
            matches.append(self._change(record))
            if len(matches) > 1:
                raise ConflictError(
                    f"Multiple change requests found for correlation_id {correlation_id!r}",
                    service="servicenow",
                    method="GET",
                    path=TABLE,
                    status=409,
                )
        return matches[0] if matches else None

    async def create_change(self, draft: ChangeRequestDraft) -> ChangeRequest:
        """POST a new change (``draft.to_snow()``). Not retried: a lost response could mean it exists (A6 handles that)."""
        raw = await self._http.post_json(
            TABLE,
            path_params={"table": "change_request"},
            json=draft.to_snow(),
            idempotent=False,
        )
        return self._change(raw["result"])

    async def update_change(self, sys_id: str, **fields: str) -> ChangeRequest:
        """PATCH the given fields. Business-rule refusals (403) raise ``PolicyViolationError``."""
        try:
            raw = await self._http.request(
                "PATCH",
                RECORD,
                path_params={"table": "change_request", "sys_id": sys_id},
                json=fields,
            )
        except PermissionDeniedError as exc:
            if exc.status != 403:
                raise
            raise PolicyViolationError(
                exc.message,
                service=exc.service,
                method=exc.method,
                path=exc.path,
                status=exc.status,
                retry_after=exc.retry_after,
            ) from exc
        return self._change(raw.json()["result"])

    # ---------------------------------------------------------------------------------------- approvals

    async def request_approval(self, change_sys_id: str, approver_sys_id: str, *, group_sys_id: str | None = None) -> Approval:
        """Make sure ``approver_sys_id`` has an approval record for the change (state ``requested`` when created).

        Idempotent: if the approver already has a record (requested, approved or rejected), it is returned and
        nothing is created. A retry or an agent re-run must not ask the same person twice, or leave the change
        waiting on a duplicate that nobody will answer.
        """
        for existing in await self.approvals(change_sys_id):
            if existing.approver_sys_id == approver_sys_id:
                return existing
        payload = {"sysapproval": change_sys_id, "approver": approver_sys_id, "state": "requested"}
        if group_sys_id is not None:
            payload["group"] = group_sys_id
        raw = await self._http.post_json(
            TABLE,
            path_params={"table": "sysapproval_approver"},
            json=payload,
            idempotent=False,
        )
        return self._approval(raw["result"])

    async def approvals(self, change_sys_id: str) -> list[Approval]:
        query = Q().eq("sysapproval", change_sys_id)
        return [self._approval(record) async for record in self.records("sysapproval_approver", query)]

    # --------------------------------------------------------------------------------------------- CMDB

    async def get_ci(self, name: str) -> ConfigurationItem | None:
        """The ``cmdb_ci_service`` record for a service name (risk input: business criticality, owners)."""
        async for raw in self.records("cmdb_ci_service", Q().eq("name", name)):
            return ConfigurationItem(
                sys_id=raw["sys_id"],
                name=raw["name"],
                business_criticality=raw.get("business_criticality", ""),
                used_for=raw.get("used_for", ""),
                support_group_sys_id=raw.get("support_group") or None,
                owned_by_sys_id=raw.get("owned_by") or None,
                short_description=raw.get("short_description", ""),
            )
        return None

    async def get_group(self, name: str) -> SnowGroup | None:
        async for raw in self.records("sys_user_group", Q().eq("name", name)):
            return SnowGroup(
                sys_id=raw["sys_id"],
                name=raw["name"],
                manager_sys_id=raw.get("manager") or None,
                type=raw.get("type", ""),
            )
        return None

    async def get_user(self, sys_id: str) -> SnowUser | None:
        async for raw in self.records("sys_user", Q().eq("sys_id", sys_id)):
            return SnowUser(
                sys_id=raw["sys_id"],
                user_name=raw["user_name"],
                name=raw["name"],
                email=raw.get("email", ""),
                title=raw.get("title", ""),
            )
        return None

    @staticmethod
    def _change(raw: dict[str, Any]) -> ChangeRequest:
        raw = ServiceNowClient._flatten_record(raw)
        return ChangeRequest(
            sys_id=raw["sys_id"],
            number=raw["number"],
            short_description=raw["short_description"],
            description=raw.get("description", ""),
            type=raw["type"],
            state=raw["state"],
            risk=raw["risk"],
            impact=raw["impact"],
            approval=raw["approval"],
            cmdb_ci=raw.get("cmdb_ci") or None,
            assignment_group=raw.get("assignment_group") or None,
            correlation_id=raw.get("correlation_id", ""),
            start_date=raw.get("start_date", ""),
            end_date=raw.get("end_date", ""),
            implementation_plan=raw.get("implementation_plan", ""),
            backout_plan=raw.get("backout_plan", ""),
            test_plan=raw.get("test_plan", ""),
            justification=raw.get("justification", ""),
            close_code=raw.get("close_code", ""),
            close_notes=raw.get("close_notes", ""),
            created=raw.get("sys_created_on", ""),
            updated=raw.get("sys_updated_on", ""),
        )

    @staticmethod
    def _approval(raw: dict[str, Any]) -> Approval:
        raw = ServiceNowClient._flatten_record(raw)
        return Approval(
            sys_id=raw["sys_id"],
            change_sys_id=raw["sysapproval"],
            approver_sys_id=raw["approver"],
            group_sys_id=raw.get("group") or None,
            state=raw["state"],
            comments=raw.get("comments", ""),
            created=raw.get("sys_created_on", ""),
        )

    @staticmethod
    def _flatten_record(raw: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value.get("value") if isinstance(value, dict) and "value" in value else value
            for key, value in raw.items()
        }
