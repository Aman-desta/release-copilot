"""FastAPI app that serves every mock enterprise service under its own prefix."""

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from release_copilot.mocks import middleware
from release_copilot.mocks.routers import admin, azure, github, jira, notify, servicenow, sonar
from release_copilot.mocks.settings import get_settings
from release_copilot.mocks.state import MockState


def create_app(default_scenario: str | None = None) -> FastAPI:
    app = FastAPI(title="Release Copilot: mock enterprise services", version="0.1.0")
    app.state.mock = MockState(active=default_scenario or get_settings().default_scenario)

    @app.exception_handler(HTTPException)
    async def vendor_errors(request: Request, exc: HTTPException) -> JSONResponse:
        # Vendors return their own error bodies, not FastAPI's {"detail": ...} wrapper.
        body: Any = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.get("/", include_in_schema=False)
    def index() -> dict[str, Any]:
        return {
            "services": {
                "jira": "/jira",
                "github": "/github",
                "azure_login": "/azure/login/{tenant}/oauth2/v2.0/token",
                "azure_arm": "/azure/management",
                "sonar": "/sonar",
                "servicenow": "/servicenow",
                "notify": "/notify/{teams|slack}/{channel}",
                "admin": "/_admin",
            },
            "active_scenario": app.state.mock.active,
            "docs": "/docs",
        }

    for module in (jira, github, sonar, servicenow, notify, admin):
        app.include_router(module.router)
    app.include_router(azure.login)
    app.include_router(azure.arm)
    middleware.install(app)
    return app
