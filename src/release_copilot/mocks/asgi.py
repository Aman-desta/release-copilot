"""ASGI entry point for ``uvicorn release_copilot.mocks.asgi:app`` (and ``--reload``)."""

from release_copilot.mocks.app import create_app

app = create_app()
