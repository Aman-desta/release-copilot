"""Shared async HTTP client for every integration (story T1).

One ``ServiceClient`` per upstream service. ``request()`` runs one *logical* call:

    breaker check → send (auth flow attached) → success? return
                                         └─ error → map to IntegrationError → retryable and safe and within
                                                     the deadline? wait (Retry-After or jittered backoff) → retry
                                                     otherwise raise

Logging: one line per attempt with service, method, path *template*, status, attempt and duration, plus the
correlation id. Query strings, bodies and headers are never logged (they can carry tokens and PII).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx

from release_copilot.integrations.breaker import CircuitBreaker
from release_copilot.integrations.context import CORRELATION_HEADER, get_correlation_id
from release_copilot.integrations.errors import (
    IntegrationError,
    UpstreamUnavailableError,
    error_from_response,
    error_from_transport,
)
from release_copilot.integrations.retry import RetryPolicy

log = logging.getLogger("release_copilot.integrations.http")
USER_AGENT = "release-copilot/0.1"


class ServiceClient:
    def __init__(
        self,
        service: str,
        base_url: str,
        *,
        auth: httpx.Auth | None = None,
        timeout: httpx.Timeout | float = httpx.Timeout(10.0, connect=5.0),
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.service = service
        self.retry = retry or RetryPolicy()
        self.breaker = breaker or CircuitBreaker(service)
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=auth,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})},
            transport=transport,
            follow_redirects=False,  # a redirect would resend credentials elsewhere
        )

    async def __aenter__(self) -> "ServiceClient":
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool | None = None,
        ok: Iterable[int] = (200, 201, 202, 204),
    ) -> httpx.Response:
        """Send ``method path`` (``path`` may hold ``{placeholders}`` filled from ``path_params``)."""
        method = method.upper()
        # Each parameter is percent-encoded as ONE path segment: "a/b" or "../x" can't change the route.
        url = path.format(**{k: quote(str(v), safe="") for k, v in (path_params or {}).items()})
        expected = set(ok)
        started = self._clock()
        attempt = 0
        while True:
            attempt += 1
            self.breaker.before_call(method, path)
            error, response = await self._attempt(method, url, path, attempt, params, json, data, headers)
            if error is None and response is not None and response.status_code in expected:
                self.breaker.record_success()
                return response
            if error is None:
                assert response is not None
                error = error_from_response(self.service, response, path=path)
            self._record_for_breaker(error)

            if attempt >= self.retry.max_attempts or not self.retry.is_retryable(method, error, idempotent=idempotent):
                raise error
            wait = self.retry.backoff(attempt, error.retry_after)
            if self._clock() - started + wait > self.retry.deadline_seconds:
                log.warning("%s %s %s: giving up, retry would exceed the %.0fs deadline", self.service, method, path, self.retry.deadline_seconds)
                raise error
            log.info("%s %s %s: retrying in %.2fs after %s", self.service, method, path, wait, type(error).__name__)
            await self._sleep(wait)

    async def _attempt(
        self, method: str, url: str, path: str, attempt: int, params: Any, json: Any, data: Any, headers: Mapping[str, str] | None
    ) -> tuple[IntegrationError | None, httpx.Response | None]:
        request_headers = dict(headers or {})
        if correlation_id := get_correlation_id():
            request_headers[CORRELATION_HEADER] = correlation_id
        t0 = time.perf_counter()
        try:
            response = await self._http.request(method, url, params=params, json=json, data=data, headers=request_headers)
        except httpx.TransportError as exc:
            self._log(method, path, None, attempt, t0, type(exc).__name__)
            return error_from_transport(self.service, method, path, exc), None
        except IntegrationError as exc:
            # Raised by the auth flow (e.g. the token endpoint failed): same retry and breaker rules as the call itself.
            self._log(method, path, exc.status, attempt, t0, type(exc).__name__)
            exc.method, exc.path = exc.method or method, exc.path or path
            return exc, None
        self._log(method, path, response.status_code, attempt, t0, None)
        return None, response

    def _record_for_breaker(self, error: IntegrationError) -> None:
        if isinstance(error, UpstreamUnavailableError):
            self.breaker.record_failure()
        else:
            # 4xx says nothing about upstream health: don't count it, but free a half-open trial slot.
            self.breaker.release_trial()

    def _log(self, method: str, path: str, status: int | None, attempt: int, t0: float, failure: str | None) -> None:
        ms = (time.perf_counter() - t0) * 1000
        level = logging.INFO if failure is None and status is not None and status < 400 else logging.WARNING
        log.log(
            level,
            "%s %s %s -> %s attempt=%d ms=%.1f",
            self.service, method, path, status if status is not None else failure, attempt, ms,
            extra={
                "service": self.service, "http_method": method, "path_template": path, "status": status,
                "attempt": attempt, "duration_ms": round(ms, 1), "correlation_id": get_correlation_id(), "failure": failure,
            },
        )

    # Convenience wrappers ------------------------------------------------------------------------------

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        return (await self.request("GET", path, **kwargs)).json()

    async def post_json(self, path: str, **kwargs: Any) -> Any:
        response = await self.request("POST", path, **kwargs)
        return response.json() if response.content else None
