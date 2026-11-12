"""Bounded GitLab to-do readers and mutations. 📝."""

import re
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.ci_common import (
    InvalidCursorError,
    cap,
    cell,
    decode_cursor,
    encode_cursor,
    parse_gid,
    raise_tool_error,
)
from gitlab_mcp.client import GitLabAPIError
lazy from gitlab_mcp.client import GitLabClient

TargetType = Literal[
    "COMMIT",
    "ISSUE",
    "WORKITEM",
    "MERGEREQUEST",
    "DESIGN",
    "ALERT",
    "PROJECT",
    "NAMESPACE",
    "KEY",
    "WIKIPAGEMETA",
    "EPIC",
]
Positive = Annotated[int, Field(strict=True, ge=1)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=100)]
HEADER = "todo_id\tstate\taction\ttarget_type\tproject\ttarget_id\tname\tcreated\ttarget_url"
# WorkItem uses a different ID scalar; its alias avoids a GraphQL field-type conflict.
QUERY = """query($states: [TodoStateEnum!], $projectIds: [ID!], $groupIds: [ID!],
                $types: [TodoTargetEnum!], $first: Int!, $after: String) {
  currentUser { todos(state: $states, projectId: $projectIds, groupId: $groupIds,
                      type: $types, first: $first, after: $after) {
    nodes { id state action targetType targetUrl createdAt project { fullPath }
      targetEntity { name ... on Issue { id } ... on MergeRequest { id } ... on WorkItem { workItemId: id } } }
    pageInfo { hasNextPage endCursor }
  } }
}"""


class TodoList(BaseModel):
    """One page of to-dos as fixed-column TSV."""

    todos: str
    next_cursor: str = Field(default="", exclude_if=lambda value: not value)


class TodoWriteResult(BaseModel):
    """A confirmed write or an unknown outcome."""

    status: Literal["created", "updated", "unknown"]
    todo_id: int | None = Field(default=None, exclude_if=lambda value: value is None)
    state: str | None = Field(default=None, exclude_if=lambda value: value is None)
    target_url: str | None = Field(default=None, exclude_if=lambda value: value is None)
    message: str | None = Field(default=None, exclude_if=lambda value: value is None)


def _number(value: object) -> int:
    """Accept only canonical positive numeric to-do IDs from GitLab."""
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise_tool_error("GitLab returned an invalid to-do ID.")
    return int(value)


def _row(node: dict[str, Any]) -> str:
    """Render one to-do, flattening free text into safe TSV cells."""
    project = node.get("project") or {}
    target = node.get("targetEntity") or {}
    values = (
        _number(node["id"]),
        node.get("state"),
        node.get("action"),
        node.get("targetType"),
        project.get("fullPath"),
        target.get("id") or target.get("workItemId"),
        cap(cell(target.get("name")), 200),
        (node.get("createdAt") or "")[:10],
        node.get("targetUrl"),
    )
    return "\t".join(cell(value) for value in values)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_todos(  # noqa: PLR0913, PLR0917
    ctx: Context,
    state: Literal["pending", "done", "all"] = "pending",
    project_id: Positive | None = None,
    group_id: Positive | None = None,
    target_type: TargetType | None = None,
    limit: PageSize = 20,
    cursor: str | None = None,
) -> TodoList:
    """List one page of your to-dos, like `glab api todos` (GitLab 18.6 baseline).

    Pending excludes snoozed items; results use GitLab's default ID-descending order. EPIC requires EE.
    TSV target IDs are available only for issues, merge requests and work items; others are blank.
    Pass next_cursor unchanged with the same filters and limit. Pages are not a snapshot when to-dos change.
    """
    client = get_client(ctx)
    scope = {
        "tool": "list_todos",
        "url": client.settings.url,
        "state": state,
        "project_id": project_id,
        "group_id": group_id,
        "target_type": target_type,
        "limit": limit,
    }
    after = None
    if cursor is not None:
        payload = decode_cursor(cursor)
        after = payload.get("after")
        if payload.get("scope") != scope or not isinstance(after, str) or not after.strip():
            raise InvalidCursorError
    variables = {
        "states": ["pending", "done"] if state == "all" else [state],
        "projectIds": [str(project_id)] if project_id is not None else None,
        "groupIds": [str(group_id)] if group_id is not None else None,
        "types": [target_type] if target_type else None,
        "first": limit,
        "after": after,
    }
    data = await client.graphql(QUERY, variables)
    try:
        connection = data["currentUser"]["todos"]
        nodes, page = connection["nodes"], connection["pageInfo"]
        if not isinstance(nodes, list) or len(nodes) > limit or not isinstance(page["hasNextPage"], bool):
            raise ValueError  # noqa: TRY301
        rows = [_row(node) for node in nodes]
        next_cursor = ""
        if page["hasNextPage"]:
            end = page["endCursor"]
            if not isinstance(end, str) or not end.strip():
                raise ValueError  # noqa: TRY301
            next_cursor = encode_cursor({"scope": scope, "after": end})
    except KeyError, TypeError, ValueError, AttributeError:
        raise_tool_error("GitLab returned an invalid to-do page.")
    return TodoList(todos="\n".join([HEADER, *rows]), next_cursor=next_cursor)


async def _write(
    client: GitLabClient, field: str, variables: dict[str, Any], expected_state: str, expected_id: int | None = None
) -> TodoWriteResult:
    """Send one mutation and report uncertainty rather than guessing its outcome."""
    variable = "$targetId: TodoableID!" if field == "todoCreate" else "$id: TodoID!"
    argument = "targetId: $targetId" if field == "todoCreate" else "id: $id"
    query = f"mutation({variable}) {{ {field}(input: {{{argument}}}) {{ todo {{ id state targetUrl }} errors }} }}"
    try:
        payload = await client.mutate(query, variables, field)
        if payload.get("errors") != []:
            raise ValueError  # noqa: TRY301
        todo = payload["todo"]
        number = _number(todo["id"])
        state = todo["state"]
        url = todo.get("targetUrl")
        if (
            state != expected_state
            or (expected_id is not None and number != expected_id)
            or (url is not None and not isinstance(url, str))
        ):
            raise ValueError  # noqa: TRY301
        return TodoWriteResult(
            status="created" if field == "todoCreate" else "updated", todo_id=number, state=state, target_url=url
        )
    except GitLabAPIError as exc:
        if 400 <= exc.status < 500 and exc.status != 408:  # noqa: PLR2004
            raise
    except ToolError, KeyError, TypeError, ValueError, AttributeError:
        pass
    return TodoWriteResult(status="unknown", message="Check GitLab before retrying.")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True, "idempotentHint": False})
async def create_todo(ctx: Context, target_id: str) -> TodoWriteResult:
    """Create a to-do like `glab api graphql` (GitLab 18.6 baseline).

    Pass a target global ID, not a project IID or numeric to-do ID. Supports Issue, MergeRequest,
    WorkItem, DesignManagement::Design and Epic. Use target_id from list_todos where available,
    or id from get_work_item for work items.
    No retry occurs. For duplicates or unknown outcomes, check GitLab before retrying.
    """
    model, _ = parse_gid(target_id)
    if model not in {"Issue", "MergeRequest", "WorkItem", "DesignManagement::Design", "Epic"}:
        raise_tool_error("Unsupported to-do target global ID.")
    return await _write(get_client(ctx), "todoCreate", {"targetId": target_id}, "pending")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True, "idempotentHint": False})
async def update_todo(ctx: Context, todo_id: Positive, state: Literal["pending", "done"]) -> TodoWriteResult:
    """Mark a to-do done or restore it, like `glab api graphql` (GitLab 18.6 baseline).

    Use the numeric todo_id from list_todos. No retry occurs. For an unknown outcome, check GitLab before retrying.
    """
    return await _write(
        get_client(ctx),
        "todoMarkDone" if state == "done" else "todoRestore",
        {"id": f"gid://gitlab/Todo/{todo_id}"},
        state,
        todo_id,
    )
