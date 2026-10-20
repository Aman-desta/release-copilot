"""Per-service auth checks matching each vendor's scheme and error shape (as FastAPI dependencies)."""

import base64
import secrets
import time

from fastapi import Depends, HTTPException, Request

from release_copilot.mocks.settings import MockSettings, get_settings
from release_copilot.mocks.state import MockState, get_state


def _basic(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return None
    return user, password


def _bearer(request: Request, schemes: tuple[str, ...] = ("bearer",)) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token if scheme.lower() in schemes and token else None


def _eq(a: str | None, b: str) -> bool:
    return a is not None and secrets.compare_digest(a, b)


def jira_auth(request: Request, s: MockSettings = Depends(get_settings)) -> None:
    if not s.auth_enabled:
        return
    creds = _basic(request)
    if not creds or not (_eq(creds[0], s.jira_email) and _eq(creds[1], s.jira_api_token)):
        raise HTTPException(401, {"errorMessages": ["Client must be authenticated to access this resource."], "errors": {}})


def github_auth(request: Request, s: MockSettings = Depends(get_settings)) -> None:
    if not s.auth_enabled:
        return
    if not _eq(_bearer(request, ("bearer", "token")), s.github_token):
        raise HTTPException(401, {"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest"})


def sonar_auth(request: Request, s: MockSettings = Depends(get_settings)) -> None:
    if not s.auth_enabled:
        return
    basic = _basic(request)  # SonarQube also accepts the token as the Basic-auth username
    if not (_eq(_bearer(request), s.sonar_token) or (basic and _eq(basic[0], s.sonar_token))):
        raise HTTPException(401, {"errors": [{"msg": "Authentication required"}]})


def servicenow_auth(request: Request, s: MockSettings = Depends(get_settings)) -> None:
    if not s.auth_enabled:
        return
    creds = _basic(request)
    if not creds or not (_eq(creds[0], s.servicenow_user) and _eq(creds[1], s.servicenow_password)):
        raise HTTPException(401, {"error": {"message": "User Not Authenticated", "detail": "Required to provide Auth information"}, "status": "failure"})


def azure_auth(
    request: Request, s: MockSettings = Depends(get_settings), state: MockState = Depends(get_state)
) -> None:
    if not s.auth_enabled:
        return
    token = _bearer(request)
    expiry = state.azure_tokens.get(token or "")
    if expiry is None:
        raise HTTPException(401, {"error": {"code": "InvalidAuthenticationToken", "message": "The access token is invalid."}})
    if expiry < time.time():
        raise HTTPException(401, {"error": {"code": "ExpiredAuthenticationToken", "message": "The access token expiry UTC time is earlier than current UTC time."}})
