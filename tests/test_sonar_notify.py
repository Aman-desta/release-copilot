"""T4: SonarClient and the Teams/Slack notifiers, against the mocks."""

import pytest

from release_copilot.config import get_settings
from release_copilot.integrations.clients import notifiers, sonar_client
from release_copilot.integrations.errors import NotFoundError
from release_copilot.integrations.notify import Notification, notify_all, slack_escape, slack_payload
from tests.integrations_support import Mocks

BRANCH = "release/2.4.0"


# ---------------------------------------------------------------------------------------------- sonar


async def test_measures_read_value_and_period(mock_env):
    async with sonar_client(get_settings(), transport=Mocks().transport()) as sonar:
        m = await sonar.measures(BRANCH)
    assert m.get("coverage") == 84.2 and m.get("new_coverage") == 88.0  # new_* arrives under period.value
    assert m.get("tests") == 1402 and m.evidence_id == "sonar:measures:release/2.4.0"


async def test_quality_gate_passes_on_clean(mock_env):
    async with sonar_client(get_settings(), transport=Mocks().transport()) as sonar:
        gate = await sonar.quality_gate(BRANCH)
    assert gate.passed and gate.failing == () and gate.evidence_id == "sonar:gate:release/2.4.0"


@pytest.mark.parametrize("scenario,metric,actual", [("coverage_drop", "new_coverage", "41.5"), ("critical_cve", "vulnerabilities", "1")])
async def test_quality_gate_failures_name_the_condition(mock_env, scenario, metric, actual):
    async with sonar_client(get_settings(), transport=Mocks(scenario).transport()) as sonar:
        gate = await sonar.quality_gate(BRANCH)
    assert not gate.passed
    [failing] = gate.failing
    assert (failing.metric, failing.actual) == (metric, actual)


async def test_unknown_branch(mock_env):
    async with sonar_client(get_settings(), transport=Mocks().transport()) as sonar:
        with pytest.raises(NotFoundError, match="not found"):
            await sonar.quality_gate("release/9.9.9")


# -------------------------------------------------------------------------------------------- notify

NOTE = Notification(
    title="payments-api 2.4.0 needs approval",
    summary="Moderate risk: additive DB migration (PAY-102). <!channel> & <https://evil.example|click>",
    severity="warning",
    facts=(("Change", "CHG0030027"), ("Risk", "Moderate")),
    link_text="Open change",
    link_url="http://localhost:9100/servicenow/CHG0030027",
)


async def test_teams_and_slack_are_delivered(mock_env):
    mock_env.setenv("RELEASE_COPILOT_NOTIFY__SLACK_WEBHOOK_URL", "http://localhost:9100/notify/slack/payments-releases")
    get_settings.cache_clear()
    mocks = Mocks()
    channels = notifiers(get_settings(), transport=mocks.transport())
    results = await notify_all(channels, NOTE)
    for n in channels:
        await n.aclose()
    assert results == {"teams": None, "slack": None}
    sent = {n["kind"]: n["payload"] for n in mocks.state.notifications}
    card = sent["teams"]["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert {"title": "Risk", "value": "Moderate"} in card["content"]["body"][2]["facts"]
    assert sent["slack"]["text"] == NOTE.title and sent["slack"]["blocks"][0]["type"] == "header"


def test_slack_text_is_escaped():
    payload = slack_payload(NOTE)
    summary = payload["blocks"][1]["text"]["text"]
    assert "<!channel>" not in summary and "&lt;!channel&gt;" in summary  # no @channel ping from ticket text
    assert "<https://evil.example|click>" not in summary  # no disguised link
    assert slack_escape("a & b") == "a &amp; b"


async def test_one_channel_failing_does_not_stop_the_others(mock_env):
    mock_env.setenv("RELEASE_COPILOT_NOTIFY__SLACK_WEBHOOK_URL", "http://localhost:9100/notify/slack/payments-releases")
    get_settings.cache_clear()
    mocks = Mocks()
    mocks.fault("/notify/teams", status=500)
    channels = notifiers(get_settings(), transport=mocks.transport())
    results = await notify_all(channels, NOTE)
    assert results["teams"] is not None and results["slack"] is None
    assert len([c for c in mocks.calls("/notify/teams")]) > 1  # at-least-once: retried before giving up


async def test_webhook_secret_never_logged(mock_env, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="release_copilot.integrations")
    [teams] = notifiers(get_settings(), transport=Mocks().transport())
    await teams.send(NOTE)
    assert "teams-hook-secret" not in caplog.text and "{webhook}" in caplog.text
