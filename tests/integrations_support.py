"""Helpers for integration-client tests: the mocks in-process, a fake clock, recorded sleeps."""

import httpx
from release_copilot_mocks.app import create_app
from release_copilot_mocks.settings import MockSettings, get_settings as mock_settings
from release_copilot_mocks.state import FaultRule, MockState

MOCK_BASE = "http://mocks"
MOCK = MockSettings()  # the mocks' test credentials


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Mocks:
    """The mock app plus handles to its state, for setting faults and reading the request log."""

    def __init__(self, scenario: str = "clean", **settings: object) -> None:
        self.app = create_app(scenario)
        if settings:
            overridden = MockSettings(**settings)
            self.app.dependency_overrides[mock_settings] = lambda: overridden
        self.state: MockState = self.app.state.mock

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app)

    def fault(self, path_prefix: str, status: int = 503, **kw: object) -> FaultRule:
        return self.state.add_fault(FaultRule(path_prefix=path_prefix, kind=kw.pop("kind", "status"), status=status, **kw))  # type: ignore[arg-type]

    def calls(self, path_prefix: str = "") -> list[dict]:
        return [r for r in self.state.requests if r["path"].startswith(path_prefix)]
