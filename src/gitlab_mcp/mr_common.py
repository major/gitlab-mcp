"""Merge request helpers and thread models shared by several tool areas 🔀."""

import re
from dataclasses import dataclass
from itertools import batched
from typing import Any
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from gitlab_mcp.client import GitLabConnectionError
from gitlab_mcp.common import DIFF_PAGE_SIZE, GRAPHQL_MR_CHUNK
from gitlab_mcp.errors import MergeRequestNotFoundError, ProjectNotFoundError
lazy from gitlab_mcp.client import GitLabClient

DIFF_HUNK_PATTERN = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Selection for one aliased merge request. This field is Premium-only, so it is never part of the main queries.
BLOCKERS_SELECTION = "blockingMergeRequests { visibleMergeRequests { iid } hiddenCount }"
# Usernames are checked in one query. GitLab leaves unknown names out of the answer instead of failing.
USERS_QUERY = """
query($usernames: [String!]) {
  users(usernames: $usernames) {
    nodes { username }
  }
}
"""

REVIEWERS_MUTATION = """
mutation($projectPath: ID!, $iid: String!, $usernames: [String!]!, $mode: MutationOperationMode) {
  mergeRequestSetReviewers(
    input: {projectPath: $projectPath, iid: $iid, reviewerUsernames: $usernames, operationMode: $mode}
  ) {
    errors
  }
}
"""

ASSIGNEES_MUTATION = """
mutation($projectPath: ID!, $iid: String!, $usernames: [String!]!, $mode: MutationOperationMode) {
  mergeRequestSetAssignees(
    input: {projectPath: $projectPath, iid: $iid, assigneeUsernames: $usernames, operationMode: $mode}
  ) {
    errors
  }
}
"""

# Maps each person list to its mutation and the payload field that holds the errors.
PEOPLE_MUTATIONS = {
    "reviewers": (REVIEWERS_MUTATION, "mergeRequestSetReviewers"),
    "assignees": (ASSIGNEES_MUTATION, "mergeRequestSetAssignees"),
}


@dataclass(frozen=True)
class Blockers:
    """The merge requests that block one merge request."""

    iids: list[int]  # IIDs of the blockers the token can see.
    hidden: int  # Blockers the token cannot see.


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


async def fetch_blockers(client: GitLabClient, project: str, iids: list[int]) -> dict[int, Blockers]:
    """Return the blocking merge requests of each IID, using one GraphQL query per chunk of IIDs.

    Callers should pass only blocked merge requests, because the field needs GitLab Premium.
    GitLab errors propagate as ToolError, so each caller decides whether blocker data is optional.

    Raises:
        ProjectNotFoundError: When GitLab has no such project, or the token cannot see it.
    """
    blockers: dict[int, Blockers] = {}
    for chunk in batched(iids, GRAPHQL_MR_CHUNK, strict=False):
        blockers |= await _fetch_blockers_chunk(client, project, list(chunk))
    return blockers


async def _fetch_blockers_chunk(client: GitLabClient, project: str, iids: list[int]) -> dict[int, Blockers]:
    """Fetch the blockers for one chunk of IIDs in one GraphQL query, using one alias per IID (`mr_0`, `mr_1`, ...)."""
    declarations = ["$fullPath: ID!"]
    variables: dict[str, Any] = {"fullPath": project}
    selections = []
    for index, iid in enumerate(iids):
        variable = f"iid{index}"
        declarations.append(f"${variable}: String!")
        variables[variable] = str(iid)
        selections.append(f"mr_{index}: mergeRequest(iid: ${variable}) {{ {BLOCKERS_SELECTION} }}")

    query = (
        f"query({', '.join(declarations)}) {{\n"
        "  project(fullPath: $fullPath) {\n"
        f"    {' '.join(selections)}\n"
        "  }\n"
        "}\n"
    )
    data = await client.graphql(query, variables)
    if data["project"] is None:
        raise ProjectNotFoundError(project)
    return {iid: _to_blockers(data["project"][f"mr_{index}"]) for index, iid in enumerate(iids)}


def _to_blockers(node: dict[str, Any] | None) -> Blockers:
    """Convert one aliased merge request node. A missing node or a null connection means no blockers."""
    connection = node["blockingMergeRequests"] if node else None
    if connection is None:
        return Blockers(iids=[], hidden=0)
    # GraphQL sends IIDs as strings, so they become ints to match the REST and tool output.
    visible = [int(merge_request["iid"]) for merge_request in connection["visibleMergeRequests"]]
    return Blockers(iids=visible, hidden=connection["hiddenCount"])


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


# Shown when GitLab waits for CI that no current pipeline can satisfy. 🚦
PIPELINE_STALL_HINT = (
    "GitLab waits for CI (ci_must_pass), but the MR has no head pipeline. This often happens after a retarget "
    "with no new push. If none starts soon, start one with create_merge_request_pipeline."
)


def needs_mr_pipeline(state: str, merge_status: str, pipeline_status: str) -> bool:
    """Return True when an open MR waits for CI (`ci_must_pass`) but has no head pipeline at all.

    A common cause is a retarget with no new push: nothing starts a pipeline for the new target. 🔁
    Other statuses are left out on purpose. Active pipelines show `ci_still_running`, and a failed or
    manual pipeline needs a fix or a click, not a new pipeline.
    """
    return state == "opened" and merge_status == "ci_must_pass" and not pipeline_status


def mr_rest_path(project: str, iid: int) -> str:
    """Return the REST path of one merge request.

    The project path is URL-encoded, so `group/project` becomes `group%2Fproject`.
    """
    encoded = quote(project, safe="")
    return f"/projects/{encoded}/merge_requests/{iid}"


@dataclass
class PeopleOutcome:
    """What one set of people changes did. Each list holds step names, or `kind:name` items in `failed`."""

    applied: list[str]  # Steps GitLab confirmed, such as add_reviewers.
    unknown: list[str]  # Steps whose write may have reached GitLab. Check before retrying.
    failed: dict[str, str]  # Step or item -> reason.


async def known_usernames(client: GitLabClient, usernames: set[str]) -> dict[str, str]:
    """Return the known users among `usernames`, keyed by lowercased name.

    GitLab leaves unknown names out of the answer without an error, so a missing
    name means unknown. Matching ignores case, because usernames ignore case too.
    """
    data = await client.graphql(USERS_QUERY, {"usernames": sorted(usernames)})
    return {node["username"].lower(): node["username"] for node in data["users"]["nodes"]}


async def set_people(
    client: GitLabClient,
    project: str,
    iid: int,
    jobs: list[tuple[str, str, str, list[str]]],
) -> PeopleOutcome:
    """Change reviewers or assignees of one merge request, and report each step's outcome.

    Each job is (kind, step, mode, names). `kind` is "reviewers" or "assignees", `step` names the
    outcome, and `mode` is "APPEND" or "REMOVE". Jobs with no names are skipped. All names are checked
    with one lookup, then each job with known names sends one mutation. A failure in one job never stops the others.
    """
    outcome = PeopleOutcome(applied=[], unknown=[], failed={})
    requested = [job for job in jobs if job[3]]
    if not requested:
        return outcome
    try:
        known = await known_usernames(client, {name for *_, names in requested for name in names})
    except ToolError as exc:
        outcome.failed["usernames"] = str(exc)
        return outcome
    for kind, step, mode, names in requested:
        found = []
        for name in names:
            if name.lower() in known:
                found.append(known[name.lower()])
            else:
                outcome.failed[f"{kind}:{name}"] = "unknown user"
        if not found:
            continue
        mutation, payload_field = PEOPLE_MUTATIONS[kind]
        variables = {"projectPath": project, "iid": str(iid), "usernames": found, "mode": mode}
        try:
            await client.mutate(mutation, variables, payload_field)
        except GitLabConnectionError:
            # The write may have reached GitLab, so it is not reported as failed.
            outcome.unknown.append(step)
        except ToolError as exc:
            outcome.failed[step] = str(exc)
        else:
            outcome.applied.append(step)
    return outcome
