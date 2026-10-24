"""Auth for outbound integrations, as ``httpx.Auth`` flows (so every retry re-runs auth).

- ``basic_auth``: Jira (email + API token), ServiceNow (integration user)
- ``BearerToken``: GitHub PAT, SonarQube token: a static token
- ``OAuth2ClientCredentials``: Entra ID / Azure ARM: a token from the token endpoint, cached and refreshed (YOUR PART)
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Callable, Generator
from typing import Any

import httpx
from pydantic import SecretStr

from release_copilot.integrations.errors import AuthError, error_from_response, error_from_transport


def basic_auth(user: str, password: SecretStr) -> httpx.BasicAuth:
    return httpx.BasicAuth(user, password.get_secret_value())


class BearerToken(httpx.Auth):
    def __init__(self, token: SecretStr) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        yield request

    def __repr__(self) -> str:
        return "BearerToken(**********)"


class OAuth2ClientCredentials(httpx.Auth):
    """OAuth 2.0 client-credentials flow (RFC 6749 §4.4), as used by Entra ID for Azure ARM.

    YOUR PART. The contract is in tests/test_oauth.py. Requirements:
    - Fetch a token from ``token_url`` (form-encoded ``grant_type=client_credentials``, ``client_id``,
      ``client_secret``, ``scope``) on first use; cache it with its expiry (``expires_in``).
    - Refresh *before* expiry: when less than ``refresh_skew`` seconds remain (but never more than half the
      token's lifetime, so short-lived tokens are not refreshed on every call).
    - Single flight: concurrent requests that need a token trigger exactly ONE token request (``asyncio.Lock``,
      and re-check the cache after acquiring it).
    - If the API still answers 401 (token revoked or clock skew), drop the cached token, fetch a new one and
      retry the request ONCE. A second 401 is returned to the caller (it becomes an ``AuthError``).
    - A failed token request raises ``AuthError`` with the vendor's message (``error_description``).
    - Never log or repr the secret or the token.
    """

    requires_response_body = False

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: SecretStr,
        scope: str,
        http: httpx.AsyncClient,
        refresh_skew: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        service: str = "azure",
    ) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self._client_secret = client_secret
        self.scope = scope
        self._http = http  # used only for the token endpoint; injectable for tests
        self.refresh_skew = refresh_skew
        self._clock = clock
        self.service = service
        self._lock = asyncio.Lock()
        self._access_token: str | None = None
        self._refresh_at = 0.0
        self.token_requests = 0  # observable for tests and metrics

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self._get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        if response.status_code == 401:
            token = await self._refresh_token_if_current(token)
            request.headers["Authorization"] = f"Bearer {token}"
            yield request

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        raise RuntimeError("OAuth2ClientCredentials is async-only; use httpx.AsyncClient")

    def __repr__(self) -> str:
        return f"OAuth2ClientCredentials(client_id={self.client_id!r}, scope={self.scope!r})"

    async def _get_token(self) -> str:
        if self._access_token is not None and self._clock() < self._refresh_at:
            return self._access_token
        async with self._lock:
            if self._access_token is not None and self._clock() < self._refresh_at:
                return self._access_token
            return await self._fetch_and_cache_token()

    async def _refresh_token_if_current(self, sent_token: str) -> str:
        async with self._lock:
            if self._access_token != sent_token:
                if self._access_token is not None:
                    return self._access_token
            self._access_token = None
            self._refresh_at = 0.0
            return await self._fetch_and_cache_token()

    async def _fetch_and_cache_token(self) -> str:
        """Fetch and cache a token from the token endpoint."""
        self.token_requests += 1
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self._client_secret.get_secret_value(),
            "scope": self.scope,
        }

        try:
            response = await self._http.post(self.token_url, data=data)
        except httpx.TransportError as exc:
            raise error_from_transport(self.service, "POST", "token", exc) from exc

        if response.status_code != 200:
            error = error_from_response(self.service, response, path="token")
            if response.status_code == 400:
                raise AuthError(
                    f"{self.service} token request failed: {error.message}",
                    service=self.service,
                    method=error.method,
                    path=error.path,
                    status=error.status,
                ) from error
            raise error

        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AuthError(f"{self.service} token response was not valid JSON", service=self.service) from exc
        token = body.get("access_token") if isinstance(body, dict) else None
        ttl = body.get("expires_in") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token or isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
            raise AuthError(f"{self.service} token response is missing a valid access_token or expires_in", service=self.service)

        self._access_token = token
        self._refresh_at = self._clock() + ttl - min(self.refresh_skew, ttl / 2)
        return token