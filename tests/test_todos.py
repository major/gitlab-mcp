"""Behavioral tests for GitLab to-dos through MCP. 🧪."""

import json

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp import mcp
from mcp_helpers import call_tool

GRAPHQL = "https://gitlab.example.com/api/graphql"
UNKNOWN = {"status": "unknown", "message": "Check GitLab before retrying."}
TODO_ID = 7
NAME_LIMIT = 200
TWO_CALLS = 2


@pytest.mark.parametrize(
    ("state", "states"), [("pending", ["pending"]), ("done", ["done"]), ("all", ["pending", "done"])]
)
@respx.mock
async def test_list_filters(gitlab_env, state, states):
    """One page sends all finder filters without local scanning."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {"currentUser": {"todos": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
            },
        )
    )
    # Act
    result = await call_tool("list_todos", {"state": state, "project_id": 3, "group_id": 4, "target_type": "ISSUE"})
    # Assert
    assert result["todos"].startswith("todo_id\tstate\t")
    assert json.loads(route.calls[0].request.content)["variables"] == {
        "states": states,
        "projectIds": ["3"],
        "groupIds": ["4"],
        "types": ["ISSUE"],
        "first": 20,
        "after": None,
    }
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("tool", "arguments", "field", "state"),
    [
        ("create_todo", {"target_id": "gid://gitlab/Issue/2"}, "todoCreate", "pending"),
        ("update_todo", {"todo_id": 5, "state": "done"}, "todoMarkDone", "done"),
        ("update_todo", {"todo_id": 5, "state": "pending"}, "todoRestore", "pending"),
    ],
)
@respx.mock
async def test_write_success(gitlab_env, tool, arguments, field, state):
    """Writes make one mutation and return the confirmed numeric ID."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(
            200, json={"data": {field: {"todo": {"id": "5", "state": state, "targetUrl": None}, "errors": []}}}
        )
    )
    # Act
    result = await call_tool(tool, arguments)
    # Assert
    assert result == {"status": "created" if tool == "create_todo" else "updated", "todo_id": 5, "state": state}
    assert route.call_count == 1


@respx.mock
async def test_unknown_write(gitlab_env):
    """An ambiguous mutation response never claims success."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(500))
    # Act
    result = await call_tool("create_todo", {"target_id": "gid://gitlab/Issue/2"})
    # Assert
    assert result == UNKNOWN
    assert route.call_count == 1


@respx.mock
async def test_invalid_target(gitlab_env):
    """Invalid global IDs are rejected before contacting GitLab."""
    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool("create_todo", {"target_id": "gid://gitlab/Issue/02"})
    assert not respx.calls


@pytest.mark.parametrize("model", ["Issue", "MergeRequest", "WorkItem", "DesignManagement::Design", "Epic"])
@respx.mock
async def test_create_supported_targets(gitlab_env, model):
    """Each supported target is passed directly to one mutation."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "todoCreate": {
                        "todo": {"id": 7, "state": "pending", "targetUrl": "https://example.com"},
                        "errors": [],
                    }
                }
            },
        )
    )
    target = f"gid://gitlab/{model}/3"
    # Act
    result = await call_tool("create_todo", {"target_id": target})
    # Assert
    assert result["todo_id"] == TODO_ID
    assert json.loads(route.calls[0].request.content)["variables"] == {"targetId": target}
    assert route.call_count == 1


@pytest.mark.parametrize(
    "body", [{"data": {"currentUser": None}}, {"data": {"currentUser": {"todos": None}}}, {"data": {"currentUser": {}}}]
)
@respx.mock
async def test_missing_list_is_not_empty(gitlab_env, body):
    """Missing connections are errors, not successful empty pages."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=body))
    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool("list_todos")


@respx.mock
async def test_rows_and_scoped_cursor(gitlab_env):
    """Rows are sanitized and cursors only resume matching filters."""
    # Arrange
    node = {
        "id": "8",
        "state": "pending",
        "action": "assigned",
        "targetType": "COMMIT",
        "targetUrl": None,
        "createdAt": "2026-01-02T00:00:00Z",
        "project": None,
        "targetEntity": {"name": "a\t" + "x" * 220},
    }
    route = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "currentUser": {
                        "todos": {"nodes": [node], "pageInfo": {"hasNextPage": True, "endCursor": "opaque"}}
                    }
                }
            },
        )
    )
    async with Client(mcp) as client:
        # Act
        result = await call_tool("list_todos", client=client)
        await call_tool("list_todos", {"cursor": result["next_cursor"]}, client=client)
        # Assert
        row = result["todos"].splitlines()[1].split("\t")
        assert row[:6] == ["8", "pending", "assigned", "COMMIT", "", ""]
        assert len(row[6]) == NAME_LIMIT
        assert row[6].startswith("a ")
        assert row[7:] == ["2026-01-02", ""]
        assert json.loads(route.calls[1].request.content)["variables"]["after"] == "opaque"
        for changed in [{"state": "all"}, {"project_id": 1}, {"group_id": 1}, {"target_type": "ISSUE"}, {"limit": 1}]:
            with pytest.raises(ToolError):
                await call_tool("list_todos", {"cursor": result["next_cursor"], **changed}, client=client)
        assert route.call_count == TWO_CALLS


@pytest.mark.parametrize(
    "arguments",
    [{"limit": 0}, {"limit": 101}, {"limit": True}, {"project_id": True}, {"group_id": 0}, {"cursor": "bad"}],
)
@respx.mock
async def test_bad_list_arguments(gitlab_env, arguments):
    """Bounds, strict integers, and malformed cursors fail before HTTP."""
    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool("list_todos", arguments)
    assert not respx.calls


@pytest.mark.parametrize(
    "body",
    [
        {"errors": [{"message": "oops"}]},
        {"data": {"todoCreate": None}},
        {"data": {"todoCreate": {"errors": ["bad"]}}},
        {"data": {"todoCreate": {"todo": {"id": True, "state": "pending"}}}},
        {"data": {"todoCreate": {"todo": {"id": "4", "state": "done"}}}},
    ],
)
@respx.mock
async def test_ambiguous_payload(gitlab_env, body):
    """GraphQL errors and invalid confirmations produce an unknown outcome."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=body))
    # Act
    result = await call_tool("create_todo", {"target_id": "gid://gitlab/Issue/2"})
    # Assert
    assert result == UNKNOWN
    assert route.call_count == 1


@pytest.mark.parametrize("status", [408, 500, 503])
@respx.mock
async def test_ambiguous_http(gitlab_env, status):
    """Timeout and server HTTP errors do not confirm a write."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(status))
    # Act / Assert
    assert await call_tool("update_todo", {"todo_id": 1, "state": "done"}) == UNKNOWN
    assert route.call_count == 1


@respx.mock
async def test_explicit_http_rejection(gitlab_env):
    """Explicit client errors remain visible to the caller."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(403))
    # Act / Assert
    with pytest.raises(ToolError, match="403"):
        await call_tool("update_todo", {"todo_id": 1, "state": "done"})
    assert route.call_count == 1
