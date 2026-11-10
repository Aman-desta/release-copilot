"""Review one release from the command line, traced (O1). The services come from settings (.env).

    uv run python -m release_copilot.review --scenario clean
    uv run python -m release_copilot.review --service payments-api --version 2.4.0 --previous 2.3.1

With the mocks running (``make up``) and the tracing backends (``make obs-up``), it prints the trace id: the same
trace in Phoenix and Langfuse. ``--scenario`` sends ``X-Mock-Scenario`` with every request (refused in production).
This is a developer entry point; the API service (S1) and the event trigger (S2) come later.
"""

import argparse
import asyncio
import uuid

from release_copilot.agents.supervisor import build_supervisor, run_supervisor
from release_copilot.agents.workers import span_names
from release_copilot.config import get_settings
from release_copilot.evidence.model import ReleaseIdentity
from release_copilot.observability.tracing import configure_tracing
from release_copilot.tools.toolbox import Toolbox


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m release_copilot.review", description="Review one release, traced.")
    parser.add_argument("--service", default="payments-api")
    parser.add_argument("--version", default="2.4.0")
    parser.add_argument("--previous", default="2.3.1", help="the version production runs now")
    parser.add_argument("--scenario", default=None, help="mock scenario (evals and demos only)")
    parser.add_argument("--run-id", default=None, help="default: a new random id")
    return parser.parse_args()


async def main() -> None:
    args = _args()
    settings = get_settings()
    agents, tools = span_names()
    tracing = configure_tracing(settings.tracing, env=settings.env, agents=agents, tools=tools, set_global=True)
    identity = ReleaseIdentity(service=args.service, version=args.version, previous_version=args.previous)
    run_id = args.run_id or f"run-{uuid.uuid4().hex[:12]}"
    try:
        async with Toolbox.from_settings(settings) as box:
            result = await run_supervisor(build_supervisor(box), identity, run_id=run_id, scenario=args.scenario)
    finally:
        if tracing is not None:
            tracing.shutdown()  # flushes the last spans before the process exits

    a = result.assessment
    print(f"run {run_id}: coverage {a.coverage.status}, {len(a.findings)} findings, {len(a.all_gaps())} gaps")
    for f in a.findings:
        print(f"  [{f.finding.severity}] {f.worker}: {f.finding.title}{'' if f.verified else '  (unverified)'}")
    print(f"usage: {result.usage['total_tokens']} tokens, ${result.usage['total_cost_usd']}")
    if result.trace_id is None:
        print("tracing is off (RELEASE_COPILOT_TRACING__ENABLED=true to turn it on)")
        return
    print(f"trace {result.trace_id}")
    if settings.tracing.phoenix_endpoint is not None:
        print("  Phoenix:  http://localhost:6006  (project release-copilot; search the trace id)")
    if settings.tracing.langfuse_endpoint is not None:
        print(f"  Langfuse: http://localhost:3000/project/release-copilot/traces/{result.trace_id}")


if __name__ == "__main__":
    asyncio.run(main())
