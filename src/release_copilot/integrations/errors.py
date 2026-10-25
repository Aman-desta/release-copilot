"""Error taxonomy shared by every integration client.

Callers (and agents, via the tool layer in T5) branch on the *type*, never on vendor status codes or bodies:

    IntegrationError
    ├── AuthError                 401: bad or expired credentials
    ├── PermissionDeniedError     403: authenticated, but not allowed (ACLs, business rules)
    ├── NotFoundError             404
    ├── ConflictError             409
    ├── InvalidRequestError       400 / 422: our request is wrong; never retried
    ├── RateLimitedError          429: retryable, carries retry_after
    ├── UpstreamUnavailableError  5xx, timeouts, connection failures: retryable
    ├── CircuitOpenError          the breaker is open: failed fast, no HTTP call made
    └── OperationFailedError      a long-running operation (slot swap) ended Failed or Canceled

Each error carries the service, method, path *template* (not the filled path: low cardinality, no IDs or PII),
the status, and the vendor's own message extracted from its error body.
"""

from typing import Any

import httpx

from release_copilot.integrations.retry import parse_retry_after


class IntegrationError(Exception):
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        service: str,
        method: str = "",
        path: str = "",
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.service = service
        self.method = method
        self.path = path
        self.status = status
        self.retry_after = retry_after

    def __str__(self) -> str:
        where = f"{self.method} {self.path}".strip()
        status = f" {self.status}" if self.status is not None else ""
        return f"{self.service}{status}: {self.message}" + (f" ({where})" if where else "")

    def to_dict(self) -> dict[str, Any]:
        """Structured form for tool results and logs (T5)."""
        return {
            "error_type": type(self).__name__,
            "service": self.service,
            "status": self.status,
            "message": self.message,
            "retryable": self.retryable,
        }


class AuthError(IntegrationError):
    pass


class PermissionDeniedError(IntegrationError):
    pass


class NotFoundError(IntegrationError):
    pass


class ConflictError(IntegrationError):
    pass


class InvalidRequestError(IntegrationError):
    pass


class RateLimitedError(IntegrationError):
    retryable = True


class UpstreamUnavailableError(IntegrationError):
    retryable = True


class CircuitOpenError(IntegrationError):
    pass


class OperationFailedError(IntegrationError):
    """A long-running operation (e.g. a slot swap) finished as Failed or Canceled. Not retryable as-is."""


_BY_STATUS: dict[int, type[IntegrationError]] = {
    400: InvalidRequestError,
    401: AuthError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: InvalidRequestError,
    429: RateLimitedError,
}


def vendor_message(body: Any) -> str | None:
    """Pull the human-readable message out of the vendors' different error shapes."""
    if not isinstance(body, dict):
        return None
    # Jira: {"errorMessages": [...], "errors": {field: msg}}
    if body.get("errorMessages") or body.get("errors") and isinstance(body["errors"], dict):
        parts = list(body.get("errorMessages") or []) + [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
        if parts:
            return "; ".join(map(str, parts))
    errors = body.get("errors")
    if isinstance(errors, list) and errors and all(isinstance(e, dict) for e in errors):
        # SonarQube: {"errors": [{"msg": ...}]}
        if all("msg" in e for e in errors):
            return "; ".join(str(e["msg"]) for e in errors)
        # GitHub validation: {"message": "Validation Failed", "errors": [{"resource", "field", "code"}]}
        details = "; ".join(f"{e.get('resource', '?')}.{e.get('field', '?')}: {e.get('code', e.get('message', '?'))}" for e in errors)
        return f"{body.get('message', 'Validation Failed')}: {details}"
    error = body.get("error")
    # Azure ARM: {"error": {"code", "message"}}; ServiceNow: {"error": {"message", "detail"}}
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
        detail = error.get("detail")
        if message:
            return f"{message}: {detail}" if detail else str(message)
    # Entra ID token endpoint: {"error": "invalid_client", "error_description": ...}
    if isinstance(error, str):
        return f"{error}: {body['error_description']}" if body.get("error_description") else error
    # GitHub (and many others): {"message": ...}
    if body.get("message"):
        return str(body["message"])
    return None


def error_from_response(service: str, response: httpx.Response, *, path: str) -> IntegrationError:
    status = response.status_code
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    message = vendor_message(body) or (response.text[:200] if response.text else response.reason_phrase) or "error"
    cls = _BY_STATUS.get(status) or (UpstreamUnavailableError if status >= 500 or status == 408 else IntegrationError)
    return cls(
        message,
        service=service,
        method=response.request.method,
        path=path,
        status=status,
        retry_after=parse_retry_after(response.headers.get("retry-after")),
    )


def error_from_transport(service: str, method: str, path: str, exc: httpx.TransportError) -> UpstreamUnavailableError:
    kind = "timeout" if isinstance(exc, httpx.TimeoutException) else "connection error"
    return UpstreamUnavailableError(f"{kind}: {type(exc).__name__}", service=service, method=method, path=path)
