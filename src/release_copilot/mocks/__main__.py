"""Run the mocks: ``python -m release_copilot.mocks [--port 9100] [--scenario clean] [--reload]``."""

import argparse

import uvicorn

from release_copilot.mocks.settings import get_settings


def main() -> None:
    s = get_settings()
    parser = argparse.ArgumentParser(description="Release Copilot mock enterprise services")
    parser.add_argument("--host", default=s.host)
    parser.add_argument("--port", type=int, default=s.port)
    parser.add_argument("--scenario", default=s.default_scenario, help="default scenario (header X-Mock-Scenario overrides)")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    if args.reload:
        import os

        os.environ["RELEASE_COPILOT_MOCK_DEFAULT_SCENARIO"] = args.scenario
        uvicorn.run("release_copilot.mocks.asgi:app", host=args.host, port=args.port, reload=True)
    else:
        from release_copilot.mocks.app import create_app

        uvicorn.run(create_app(args.scenario), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
