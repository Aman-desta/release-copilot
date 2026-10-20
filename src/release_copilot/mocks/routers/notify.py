"""Incoming-webhook mocks for Microsoft Teams and Slack. The secret URL is the auth, as with the real ones.

- ``POST /notify/teams/{channel}``: Teams answers ``1`` (text) on success; the body needs ``text`` or ``attachments``
- ``POST /notify/slack/{channel}``: Slack answers ``ok``; the body needs ``text`` or ``blocks``

Messages are stored per scenario world; inspect them with ``GET /_admin/notifications``.
"""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from release_copilot.mocks.state import MockState, current_scenario, get_state

router = APIRouter(prefix="/notify", tags=["notify"])


def _store(state: MockState, kind: str, channel: str, payload: dict[str, Any]) -> None:
    state.notifications.append(
        {"at": datetime.now(timezone.utc).isoformat(), "scenario": current_scenario.get(), "kind": kind, "channel": channel, "payload": payload}
    )


@router.post("/teams/{channel}", response_class=PlainTextResponse)
def teams(channel: str, payload: dict[str, Any] = Body(...), state: MockState = Depends(get_state)) -> str:
    if not payload.get("text") and not payload.get("attachments"):
        raise HTTPException(400, "Text is required")
    _store(state, "teams", channel, payload)
    return "1"


@router.post("/slack/{channel}", response_class=PlainTextResponse)
def slack(channel: str, payload: dict[str, Any] = Body(...), state: MockState = Depends(get_state)) -> str:
    if not payload.get("text") and not payload.get("blocks"):
        raise HTTPException(400, "no_text")
    _store(state, "slack", channel, payload)
    return "ok"
