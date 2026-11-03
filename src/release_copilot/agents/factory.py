"""Build and run evidence workers: each gets its own read-only tools, and the implementation its spec names."""

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from release_copilot.agents.custom import build_custom_worker
from release_copilot.agents.findings import WorkerReport
from release_copilot.agents.prebuilt import build_prebuilt_worker
from release_copilot.agents.workers import WorkerSpec
from release_copilot.evidence.model import ReleaseIdentity
from release_copilot.tools.toolbox import Toolbox


def worker_tools(spec: WorkerSpec, box: Toolbox) -> list[BaseTool]:
    """The spec's tools, from the read-only set: a spec that names a write tool fails here, at build time."""
    available = {t.name: t for t in box.tools()}
    unknown = sorted(set(spec.tools) - set(available))
    if unknown:
        raise ValueError(f"{spec.name} worker: no read-only tools named {unknown}")
    return [available[name] for name in spec.tools]


def build_worker(spec: WorkerSpec, box: Toolbox, model: BaseChatModel) -> CompiledStateGraph:
    builder = build_prebuilt_worker if spec.implementation == "prebuilt" else build_custom_worker
    return builder(spec, worker_tools(spec, box), model)


async def run_worker(worker: CompiledStateGraph, identity: ReleaseIdentity, *, run_id: str) -> WorkerReport:
    result = await worker.ainvoke({"identity": identity}, {"configurable": {"run_id": run_id}})
    return result["report"]
