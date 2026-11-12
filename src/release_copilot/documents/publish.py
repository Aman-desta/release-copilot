"""Publishing a release document as a **draft** GitHub release.

Ensure state, not "do action": the outcome is "the release's tag has a draft whose notes are this document",
whatever state GitHub was in before (no release yet, a draft with older notes, the same notes already). Two rules:

- **Never touch a published release.** Once a release is out, its notes are what people read; a later run must not
  rewrite them. That is refused (``ReleaseAlreadyPublished``), not "fixed".
- **Only the latest valid version.** Two runs for the same release can finish out of order; the slower one must
  not overwrite the newer document. The store decides what is latest, not the order the runs finished in.
"""

from release_copilot.documents.store import DocumentRecord, DocumentStore
from release_copilot.evidence.model import ReleaseIdentity
from release_copilot.integrations.github import GitHubClient
from release_copilot.integrations.models import Release


class PublishRefused(Exception):
    """The document was stored but not published, on purpose. The message says why; it never holds content."""


class ReleaseAlreadyPublished(PublishRefused):
    pass


class NotLatestVersion(PublishRefused):
    pass


def release_tag(identity: ReleaseIdentity) -> str:
    return f"v{identity.version}"


async def ensure_draft_release(
    github: GitHubClient, store: DocumentStore, identity: ReleaseIdentity, record: DocumentRecord
) -> Release:
    if record.status != "valid":
        raise PublishRefused(f"document v{record.version} failed validation; only valid documents are published")
    latest = await store.latest(record.release, status="valid")
    if latest is None or latest.version != record.version:
        raise NotLatestVersion(f"document v{record.version} is not the latest valid version (v{latest and latest.version})")

    tag, name = release_tag(identity), f"{identity.service} {identity.version}"
    release = next((r for r in await github.list_releases() if r.tag_name == tag), None)
    if release is None:
        # create_release is idempotent by tag: if another run created it meanwhile, this returns that one,
        # which then goes through the same checks as a release found by the lookup.
        release = await github.create_release(tag, name=name, body=record.markdown, target=identity.release_branch, draft=True)
    if not release.draft:
        raise ReleaseAlreadyPublished(f"release {tag} is already published; its notes are not changed")
    if release.body != record.markdown or release.name != name:
        release = await github.update_release(release.id, name=name, body=record.markdown)
    return release
