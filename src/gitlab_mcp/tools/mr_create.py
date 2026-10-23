"""Create-merge-request tool, with checked labels, assignees, and blockers 🆕."""

import re
from typing import Annotated, Any, Literal
from urllib.parse import quote
lazy from collections.abc import Sequence

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError
from gitlab_mcp.common import (
    HTTP_REQUEST_TIMEOUT,
    HTTP_SERVER_ERROR,
    REVIEW_NOTE_BODY_CAP,
    PositiveIid,
    Project,
    read_server_local_utf8_file,
)
from gitlab_mcp.tools.labels import validate_project_labels

MAX_CREATE_BLOCKERS = 20
MAX_CREATE_LABELS = 100
MAX_CREATE_ASSIGNEES = 100
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409


CREATE_ASSIGNEES_QUERY = """
query($usernames: [String!]) {
  users(usernames: $usernames) {
    nodes { id username }
  }
}
"""


class MergeRequestCreateResult(BaseModel):
    """The result of creating a merge request and its requested dependencies."""

    iid: int | None = Field(default=None, exclude_if=lambda value: value is None)
    url: str = Field(default="", exclude_if=lambda value: not value)
    applied: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    unknown: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    failed: dict[str, str] = Field(default_factory=dict, exclude_if=lambda value: not value)


async def _resolve_create_blockers(
    client: GitLabClient,
    project: str,
    blockers: Sequence[int],
) -> tuple[dict[int, int], dict[str, str]]:
    """Resolve same-project blocker IIDs to numeric global IDs before creating an MR."""
    resolved: dict[int, int] = {}
    failures: dict[str, str] = {}
    project_path = f"/projects/{quote(project, safe='')}"
    for iid in blockers:
        key = f"blocked_by:{iid}"
        try:
            blocker = await client.get(f"{project_path}/merge_requests/{iid}")
        except GitLabAPIError as exc:
            failures[key] = (
                "not found in this project or inaccessible"
                if exc.status == HTTP_NOT_FOUND
                else f"could not verify blocker: {exc}"
            )
            continue
        except ToolError as exc:
            failures[key] = f"could not verify blocker: {exc}"
            continue

        if not isinstance(blocker, dict) or type(blocker.get("iid")) is not int or blocker["iid"] != iid:
            failures[key] = "GitLab did not return this merge request IID"
            continue
        global_id = blocker.get("id")
        if type(global_id) is not int or global_id < 1:
            failures[key] = "GitLab did not return a numeric global merge request ID"
            continue
        resolved[iid] = global_id
    return resolved, failures


def _create_description(description: str | None, description_file: str | None) -> str:
    """Require exactly one description source and return its validated text."""
    if (description is None) == (description_file is None):
        raise ToolError("Set exactly one of description or description_file.")  # noqa: TRY003
    if description is not None:
        size = len(description.encode("utf-8"))
        if size > REVIEW_NOTE_BODY_CAP:
            raise ToolError(f"description exceeds {REVIEW_NOTE_BODY_CAP} UTF-8 bytes.")  # noqa: TRY003
        return description

    try:
        return read_server_local_utf8_file(
            description_file or "",
            path_field="description_file",
            size_field="description_file",
            byte_limit=REVIEW_NOTE_BODY_CAP,
        )
    except (OSError, ValueError) as exc:
        raise ToolError(f"Could not read description_file: {exc}") from exc  # noqa: TRY003


def _numeric_user_id(global_id: Any) -> int | None:
    """Return the positive numeric ID from a GitLab User global ID, or None when it is invalid."""
    if not isinstance(global_id, str):
        return None
    match = re.fullmatch(r"gid://gitlab/User/([0-9]+)", global_id)
    if match is None:
        return None
    try:
        user_id = int(match.group(1))
    except ValueError:
        return None
    return user_id if user_id > 0 else None


async def _resolve_create_assignees(
    client: GitLabClient,
    names: Sequence[str],
) -> tuple[list[int], list[str], dict[str, str]]:
    """Resolve assignee usernames to numeric IDs, retaining failures per requested spelling."""
    requested: dict[str, str] = {}
    for name in names:
        requested.setdefault(name.lower(), name)
    if not requested:
        return [], [], {}

    try:
        data = await client.graphql(CREATE_ASSIGNEES_QUERY, {"usernames": sorted(requested)})
    except ToolError as exc:
        return [], [], {f"assignees:{name}": f"could not verify user: {exc}" for name in requested.values()}

    users = data.get("users") if isinstance(data, dict) else None
    nodes = users.get("nodes") if isinstance(users, dict) else None
    if not isinstance(nodes, list):
        return [], [], {f"assignees:{name}": "could not verify user: invalid response" for name in requested.values()}

    known = {
        node["username"].lower(): node.get("id")
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get("username"), str)
    }
    assignee_ids: list[int] = []
    applied_names: list[str] = []
    failures: dict[str, str] = {}
    for normalized, name in requested.items():
        if normalized not in known:
            failures[f"assignees:{name}"] = "unknown user"
            continue
        user_id = _numeric_user_id(known[normalized])
        if user_id is None:
            failures[f"assignees:{name}"] = "could not verify user: invalid GitLab user ID"
            continue
        assignee_ids.append(user_id)
        applied_names.append(name)
    return assignee_ids, applied_names, failures


def _include_create_assignees(body: dict[str, Any], assignee_ids: Sequence[int]) -> None:
    """Add assignee IDs only when at least one username was verified."""
    if assignee_ids:
        body["assignee_ids"] = list(assignee_ids)


def _uncertain_write_error(error: GitLabAPIError) -> bool:
    """Return true for HTTP statuses that can hide a committed write."""
    return error.status == HTTP_REQUEST_TIMEOUT or error.status >= HTTP_SERVER_ERROR


async def _post_create_request(
    client: GitLabClient,
    path: str,
    body: dict[str, Any],
    failures: dict[str, str],
) -> tuple[dict[str, Any] | None, MergeRequestCreateResult | None]:
    """Post the new merge request and return either its response or a terminal result."""
    try:
        response = await client.request("POST", path, json=body)
    except GitLabConnectionError:
        return None, MergeRequestCreateResult(unknown=["create"], failed=failures)
    except GitLabAPIError as exc:
        if _uncertain_write_error(exc):
            return None, MergeRequestCreateResult(unknown=["create"], failed=failures)
        failures["create"] = str(exc)
        return None, MergeRequestCreateResult(failed=failures)
    if not isinstance(response, dict):
        return None, MergeRequestCreateResult(unknown=["create"], failed=failures)
    return response, None


async def _post_create_dependency(
    client: GitLabClient,
    path: str,
    global_id: int,
) -> tuple[Literal["applied", "failed", "unknown"], str]:
    """Post one dependency and distinguish definite failures from uncertain outcomes."""
    try:
        await client.request("POST", path, blocking_merge_request_id=global_id)
    except GitLabConnectionError as exc:
        return "unknown", str(exc)
    except GitLabAPIError as exc:
        if _uncertain_write_error(exc):
            return "unknown", str(exc)
        reason = str(exc)
        if exc.status == HTTP_CONFLICT:
            reason += " The existing dependency was not confirmed; check before retrying."
        return "failed", reason
    except ToolError as exc:
        return "failed", str(exc)
    return "applied", ""


async def _apply_create_dependencies(  # noqa: PLR0913, PLR0917 - each input is part of dependency creation.
    client: GitLabClient,
    project_path: str,
    created_iid: int,
    blockers: Sequence[int],
    global_ids: dict[int, int],
    failures: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Add dependencies sequentially, stopping if a write outcome becomes unknown."""
    applied = ["create"]
    unknown: list[str] = []
    for index, blocker_iid in enumerate(blockers):
        key = f"blocked_by:{blocker_iid}"
        status, reason = await _post_create_dependency(
            client,
            f"{project_path}/merge_requests/{created_iid}/blocks",
            global_ids[blocker_iid],
        )
        if status == "applied":
            applied.append(key)
        elif status == "failed":
            failures[key] = reason
        else:
            unknown.append(key)
            failures.update(
                {
                    f"blocked_by:{remaining}": "not attempted after an unknown dependency outcome"
                    for remaining in blockers[index + 1 :]
                },
            )
            break
    return applied, unknown


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def create_merge_request(  # noqa: PLR0913, PLR0917 - each argument is a user-facing create option.
    ctx: Context,
    project: Project,
    source_branch: Annotated[str, Field(description="Existing source branch. No branch is pushed or created.")],
    target_branch: Annotated[str, Field(description="Target branch in the project.")],
    title: Annotated[str, Field(description="Nonblank merge request title.")],
    description: Annotated[
        str | None,
        Field(
            description="Merge request description. Set this or description_file, but not both. Empty text is valid."
        ),
    ] = None,
    description_file: Annotated[
        str | None,
        Field(description="Absolute server-local UTF-8 file path, up to 100000 bytes. Set this or description."),
    ] = None,
    labels: Annotated[
        list[str] | None,
        Field(
            max_length=MAX_CREATE_LABELS,
            description="Existing project or ancestor-group labels to set, up to 100 labels.",
        ),
    ] = None,
    assignees: Annotated[
        list[str] | None,
        Field(max_length=MAX_CREATE_ASSIGNEES, description="Usernames to assign, up to 100 users."),
    ] = None,
    remove_source_branch: Annotated[
        bool | None,
        Field(description="Delete the source branch after the merge. Omit for the project default."),
    ] = None,
    blocked_by: Annotated[
        list[PositiveIid] | None,
        Field(
            max_length=MAX_CREATE_BLOCKERS,
            description="Up to 20 unique same-project blocker IIDs to add after creation.",
        ),
    ] = None,
) -> MergeRequestCreateResult:
    """Create a merge request, like `glab mr create`, without pushing or creating branches.

    Exactly one description source is required. A description file is read on this server and must be an absolute,
    regular UTF-8 file no larger than 100000 bytes. Labels are checked against project and ancestor-group labels;
    unknown or unchecked labels are omitted and reported while creation continues.
    Assignee usernames are checked in one GraphQL lookup; unknown, invalid, or unchecked users are omitted and reported.
    Blockers are resolved in this project before creation. After creation, dependency requests run sequentially.
    `applied`, `unknown`, and `failed` report each outcome. A network error or server error can leave a write's
    outcome unknown. Check GitLab before retrying. Do not retry blindly, especially when creation is unknown. Requires
    the `api` scope;
    merge request blocking dependencies require GitLab Premium or Ultimate.
    """
    for name, value in (("source_branch", source_branch), ("target_branch", target_branch), ("title", title)):
        if not value.strip():
            raise ToolError(f"{name} must not be blank.")  # noqa: TRY003
    description_text = _create_description(description, description_file)
    unique_blockers = list(dict.fromkeys(blocked_by or []))

    client = get_client(ctx)
    failures: dict[str, str] = {}
    valid_labels: list[str] = []
    if labels:
        valid_labels, label_failures = await validate_project_labels(client, project, labels)
        failures.update({f"labels:{name}": reason for name, reason in label_failures.items()})

    resolved_blockers, blocker_failures = await _resolve_create_blockers(client, project, unique_blockers)
    if blocker_failures:
        failures.update(blocker_failures)
        return MergeRequestCreateResult(failed=failures)

    assignee_ids, applied_assignees, assignee_failures = await _resolve_create_assignees(client, assignees or [])
    failures.update(assignee_failures)

    body: dict[str, Any] = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description_text,
    }
    if valid_labels:
        body["labels"] = ",".join(valid_labels)
    _include_create_assignees(body, assignee_ids)
    if remove_source_branch is not None:
        body["remove_source_branch"] = remove_source_branch
    project_path = f"/projects/{quote(project, safe='')}"
    response, terminal = await _post_create_request(client, f"{project_path}/merge_requests", body, failures)
    if terminal is not None:
        return terminal
    if response is None:
        return MergeRequestCreateResult(unknown=["create"], failed=failures)

    created_iid = response.get("iid")
    created_url = response.get("web_url")
    if type(created_iid) is not int or created_iid < 1 or not isinstance(created_url, str) or not created_url:
        return MergeRequestCreateResult(
            iid=created_iid if type(created_iid) is int and created_iid > 0 else None,
            url=created_url if isinstance(created_url, str) else "",
            unknown=["create"],
            failed=failures,
        )

    applied, unknown = await _apply_create_dependencies(
        client,
        project_path,
        created_iid,
        unique_blockers,
        resolved_blockers,
        failures,
    )
    applied[1:1] = [f"assignees:{name}" for name in applied_assignees]

    return MergeRequestCreateResult(
        iid=created_iid,
        url=created_url,
        applied=applied,
        unknown=unknown,
        failed=failures,
    )
