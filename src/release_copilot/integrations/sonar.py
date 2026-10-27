"""SonarQube Web API client: coverage and other measures, and the quality gate, per branch."""

from collections.abc import Sequence

from release_copilot.integrations.http import ServiceClient
from release_copilot.integrations.models import Measures, QualityCondition, QualityGate

DEFAULT_METRICS = (
    "coverage", "new_coverage", "bugs", "vulnerabilities", "security_hotspots", "code_smells",
    "duplicated_lines_density", "ncloc", "tests", "test_failures",
)


class SonarClient:
    def __init__(self, http: ServiceClient, *, project_key: str) -> None:
        self._http = http
        self.project_key = project_key

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "SonarClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def measures(self, branch: str, metrics: Sequence[str] = DEFAULT_METRICS) -> Measures:
        """Measures for ``branch``. New-code metrics (``new_*``) arrive under ``period.value``, the rest under ``value``."""
        raw = await self._http.get_json(
            "/api/measures/component",
            params={"component": self.project_key, "branch": branch, "metricKeys": ",".join(metrics)},
        )
        values: dict[str, float] = {}
        for m in raw["component"].get("measures", []):
            value = m.get("value", (m.get("period") or {}).get("value"))
            if value is not None:
                values[m["metric"]] = float(value)
        return Measures(project_key=self.project_key, branch=branch, values=values)

    async def quality_gate(self, branch: str) -> QualityGate:
        raw = (await self._http.get_json(
            "/api/qualitygates/project_status", params={"projectKey": self.project_key, "branch": branch}
        ))["projectStatus"]
        return QualityGate(
            project_key=self.project_key,
            branch=branch,
            status=raw["status"],
            conditions=tuple(
                QualityCondition(
                    metric=c["metricKey"],
                    comparator=c["comparator"],
                    threshold=c["errorThreshold"],
                    actual=c.get("actualValue"),
                    status=c["status"],
                )
                for c in raw.get("conditions", [])
            ),
        )
