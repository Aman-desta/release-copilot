"""T2: GitHubClient against the mocks."""

import pytest

from release_copilot.config import get_settings
from release_copilot.integrations.clients import github_client
from release_copilot.integrations.errors import AuthError
from tests.integrations_support import Mocks


def client(mocks: Mocks):
    return github_client(get_settings(), transport=mocks.transport())


async def test_compare_maps_commits_files_and_ticket_keys(mock_env):
    async with client(Mocks()) as gh:
        cmp = await gh.compare("v2.3.1", "release/2.4.0")
    assert cmp.total_commits == 8 == len(cmp.commits)
    assert [c.ticket_keys for c in cmp.commits][:2] == [("PAY-101",), ("PAY-102",)]
    assert cmp.commits[0].evidence_id == f"gh:commit:{cmp.commits[0].sha[:7]}"
    migration = next(f for f in cmp.files if "Migrations" in f.filename)
    assert migration.status == "added"
    assert cmp.evidence_id == "gh:compare:v2.3.1...release/2.4.0"


async def test_untracked_hotfix_has_no_ticket_and_no_pr(mock_env):
    async with client(Mocks("coverage_drop")) as gh:
        cmp = await gh.compare("v2.3.1", "release/2.4.0")
        hotfix = cmp.commits[-1]
        assert hotfix.title.startswith("Hotfix") and hotfix.ticket_keys == ()
        assert await gh.pulls_for_commit(hotfix.sha) == []
        tracked = await gh.pulls_for_commit(cmp.commits[0].sha)
    assert tracked[0].number == 411 and tracked[0].merged


async def test_list_pulls_follows_link_pagination(mock_env):
    mocks = Mocks()
    async with client(mocks) as gh:
        pulls = [p async for p in gh.list_pulls(state="closed", page_size=3)]
    assert len(pulls) == 8 and len({p.number for p in pulls}) == 8
    assert len(mocks.calls("/github/repos/acme-fin/payments-api/pulls")) == 3


async def test_reviews_and_checks(mock_env):
    async with client(Mocks("critical_cve")) as gh:
        reviews = await gh.reviews(418)
        checks = {c.name: c for c in await gh.check_runs("release/2.4.0")}
    assert reviews[0].state == "APPROVED"
    assert checks["CodeQL"].conclusion == "failure" and checks["build"].conclusion == "success"
    assert checks["CodeQL"].evidence_id.startswith("gh:check:CodeQL@")


async def test_security_alerts(mock_env):
    async with client(Mocks("critical_cve")) as gh:
        critical = await gh.dependabot_alerts(severity="critical")
        all_open = await gh.dependabot_alerts()
        code = await gh.code_scanning_alerts(ref="release/2.4.0")
    assert [(a.cve_id, a.package, a.patched_version) for a in critical] == [("CVE-2026-41922", "Acme.Shared.Serialization", "4.1.3")]
    assert {a.severity for a in all_open} == {"medium", "critical"}
    assert code[0].severity == "high" and code[0].path.endswith("RefundRepository.cs") and code[0].evidence_id == "gh:code-scanning:12"


async def test_create_release_is_idempotent_by_tag(mock_env):
    mocks = Mocks()
    async with client(mocks) as gh:
        first = await gh.create_release("v2.4.0", name="2.4.0", body="notes", target="release/2.4.0")
        again = await gh.create_release("v2.4.0", name="2.4.0", body="notes v2", target="release/2.4.0")
    assert first.draft and again.id == first.id
    assert len(mocks.state.world()["github"]["releases"]) == 1


async def test_bad_token_is_auth_error(mock_env):
    mock_env.setenv("RELEASE_COPILOT_GITHUB__TOKEN", "wrong")
    get_settings.cache_clear()
    async with client(Mocks()) as gh:
        with pytest.raises(AuthError, match="Bad credentials"):
            await gh.compare("v2.3.1", "release/2.4.0")
