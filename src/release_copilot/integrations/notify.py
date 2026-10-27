"""Notifications to Microsoft Teams (Adaptive Card) and Slack (Block Kit), behind one interface.

Design rules:
- **Delivery is at least once:** sends are retried (``idempotent=True``), so a rare duplicate message is possible.
  Losing an approval request is worse than seeing it twice, and the system of record (ServiceNow) holds the
  real state; a message is only a pointer to it.
- **Untrusted text is escaped:** ticket titles and agent summaries can contain anything. For Slack, ``&``, ``<``
  and ``>`` are escaped, so content can't produce ``<!channel>`` pings or disguised links.
- **The webhook URL is the secret:** it's never logged (requests are logged as ``{webhook}``).
"""

from collections.abc import Sequence
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from release_copilot.integrations.errors import IntegrationError
from release_copilot.integrations.http import ServiceClient

Severity = Literal["info", "warning", "critical"]
WEBHOOK_LOG_PATH = "{webhook}"


class Notification(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(max_length=150)
    summary: str = Field(max_length=3000)
    severity: Severity = "info"
    facts: tuple[tuple[str, str], ...] = ()  # e.g. (("Change", "CHG0030027"), ("Risk", "Moderate"))
    link_text: str | None = None
    link_url: str | None = None


class Notifier(Protocol):
    channel: str

    async def send(self, notification: Notification) -> None: ...

    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------------------------- Teams

_TEAMS_COLOR = {"info": "Default", "warning": "Warning", "critical": "Attention"}


def split_webhook(url: str) -> tuple[str, str]:
    """``https://host/secret/path?x`` -> (``https://host``, ``/secret/path?x``). httpx appends "/" to a base URL,
    so a full webhook URL can't be the base: posting to it would hit ``…/path/`` (a redirect, or a 404)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}", parts.path + (f"?{parts.query}" if parts.query else "")


def teams_payload(n: Notification) -> dict[str, Any]:
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": n.title, "weight": "Bolder", "size": "Medium", "wrap": True, "color": _TEAMS_COLOR[n.severity]},
        {"type": "TextBlock", "text": n.summary, "wrap": True},
    ]
    if n.facts:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in n.facts]})
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if n.link_url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": n.link_text or "Open", "url": n.link_url}]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


class TeamsNotifier:
    def __init__(self, http: ServiceClient, *, webhook_path: str, channel: str = "teams") -> None:
        self._http = http  # base URL = the webhook's scheme and host only
        self._path = webhook_path  # the secret part; logged as {webhook}
        self.channel = channel

    async def send(self, notification: Notification) -> None:
        await self._http.request("POST", self._path, json=teams_payload(notification), idempotent=True, log_path=WEBHOOK_LOG_PATH)

    async def aclose(self) -> None:
        await self._http.aclose()


# ---------------------------------------------------------------------------------------------- Slack


def slack_escape(text: str) -> str:
    """Slack's required escaping for mrkdwn and plain text (https://api.slack.com/reference/surfaces/formatting)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_SLACK_ICON = {"info": ":information_source:", "warning": ":warning:", "critical": ":rotating_light:"}


def slack_payload(n: Notification) -> dict[str, Any]:
    title = slack_escape(n.title)
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": n.title[:150], "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{_SLACK_ICON[n.severity]} {slack_escape(n.summary)}"}},
    ]
    if n.facts:
        # Slack allows at most 10 fields per section.
        blocks.append({"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{slack_escape(k)}*\n{slack_escape(v)}"} for k, v in n.facts[:10]]})
    if n.link_url:
        blocks.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": n.link_text or "Open"}, "url": n.link_url}]})
    return {"text": title, "blocks": blocks}  # "text" is the fallback for notifications and screen readers


class SlackNotifier:
    def __init__(self, http: ServiceClient, *, webhook_path: str, channel: str = "slack") -> None:
        self._http = http  # base URL = the webhook's scheme and host only
        self._path = webhook_path  # the secret part; logged as {webhook}
        self.channel = channel

    async def send(self, notification: Notification) -> None:
        await self._http.request("POST", self._path, json=slack_payload(notification), idempotent=True, log_path=WEBHOOK_LOG_PATH)

    async def aclose(self) -> None:
        await self._http.aclose()


# ------------------------------------------------------------------------------------------ fan-out


async def notify_all(notifiers: Sequence[Notifier], notification: Notification) -> dict[str, IntegrationError | None]:
    """Send to every channel; one channel failing doesn't stop the others. Returns channel -> error (None = sent)."""
    results: dict[str, IntegrationError | None] = {}
    for notifier in notifiers:
        try:
            await notifier.send(notification)
            results[notifier.channel] = None
        except IntegrationError as exc:
            results[notifier.channel] = exc
    return results
