"""Merge request helpers and thread models shared by several tool areas 🔀."""

import re
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field

from gitlab_mcp.common import DIFF_PAGE_SIZE
from gitlab_mcp.errors import MergeRequestNotFoundError, ProjectNotFoundError
lazy from gitlab_mcp.client import GitLabClient

DIFF_HUNK_PATTERN = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class ThreadLocation(BaseModel):
    """Where a thread points. General threads have none of these fields."""

    path: str = Field(default="", exclude_if=lambda v: not v)  # The new path, or the old one if the file is gone.
    old_path: str = Field(default="", exclude_if=lambda v: not v)  # Only when it differs from `path`.
    line: int = Field(default=0, exclude_if=lambda v: not v)  # New-file line.
    old_line: int = Field(default=0, exclude_if=lambda v: not v)  # Only for a comment on a deleted line.


class ThreadRef(ThreadLocation):
    """An open thread."""

    id: str
    by: str = Field(default="", exclude_if=lambda v: not v)  # Author of the first note.
    notes: int  # Notes in the thread, including replies.


class ThreadIndex(BaseModel):
    """Thread counts and open threads."""

    open_count: int
    resolved_count: int
    open: list[ThreadRef] = Field(default_factory=list, exclude_if=lambda v: not v)
    index_complete: bool


def position_fields(position: dict[str, Any] | None) -> dict[str, Any]:
    """Map a GraphQL note position to the location fields, stating each fact once.

    Only fields with a value are returned, so the models fill in the rest.
    General threads have no position, so the result is empty.
    """
    position = position or {}
    old_path, new_path = position.get("oldPath"), position.get("newPath")
    path = new_path or old_path
    new_line, old_line = position.get("newLine"), position.get("oldLine")
    fields: dict[str, Any] = {}
    if path:
        fields["path"] = path
    if old_path and old_path != path:
        fields["old_path"] = old_path
    # A comment on an unchanged line has both line numbers, and the new one is enough.
    # A comment on a deleted line has only oldLine, so that one is kept.
    if new_line is not None:
        fields["line"] = new_line
    elif old_line is not None:
        fields["old_line"] = old_line
    return fields


def author_display(author: dict[str, Any] | None) -> str:
    """Return a short display label for a note author, or empty for a deleted author."""
    if author is None:
        return ""
    if author.get("bot") is not True:
        return author.get("username") or ""

    name = author.get("name")
    if isinstance(name, str) and name.strip() and name.strip() != "****":
        return name

    author_id = author.get("id")
    id_fragment = str(author_id).rsplit("/", 1)[-1] if author_id else "unknown"
    return f"bot:{id_fragment}"


async def fetch_merge_request(
    client: GitLabClient,
    query: str,
    project: str,
    iid: int,
    **variables: Any,
) -> dict[str, Any]:
    """Run a query for one merge request and return its node.

    Raises ProjectNotFoundError or MergeRequestNotFoundError when GitLab has no
    such project or merge request, because it returns null for both.
    """
    data = await client.graphql(query, {"fullPath": project, "iid": str(iid), **variables})
    if data["project"] is None:
        raise ProjectNotFoundError(project)
    node = data["project"]["mergeRequest"]
    if node is None:
        raise MergeRequestNotFoundError(project, iid)
    return node


def diff_refs_head_sha(diff_refs: dict[str, Any] | None) -> str:
    """Return the full head commit SHA. Null diffRefs means unknown, so it is empty.

    The full SHA is kept on purpose. Write tools send it back as a guard, and GitLab compares it exactly.
    """
    return diff_refs["headSha"] if diff_refs else ""


async def fetch_diff_page(
    client: GitLabClient,
    project: str,
    iid: int,
    page: int,
) -> tuple[list[dict[str, Any]], int | None]:
    """Fetch one page of merge request diffs and return the files and the next page number."""
    return await client.get_page(
        f"{mr_rest_path(project, iid)}/diffs",
        per_page=DIFF_PAGE_SIZE,
        page=page,
    )


def full_sha(value: Any) -> bool:
    """Return whether a value is exactly a 40-character hexadecimal commit SHA."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value) is not None


def mr_rest_path(project: str, iid: int) -> str:
    """Return the REST path of one merge request.

    The project path is URL-encoded, so `group/project` becomes `group%2Fproject`.
    """
    encoded = quote(project, safe="")
    return f"/projects/{encoded}/merge_requests/{iid}"
