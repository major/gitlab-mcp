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
    assert "next_cursor" not in result
    query = json.loads(route.calls[0].request.content)["query"]
    assert "targetEntity { name ... on Issue { id }" in query
    assert "... on WorkItem { workItemId: id }" in query
    assert "body" not in query


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
    request = json.loads(route.calls[0].request.content)
    assert f"{field}(input:" in request["query"]
    expected_variables = (
        {"targetId": arguments["target_id"]} if tool == "create_todo" else {"id": "gid://gitlab/Todo/5"}
    )
    assert request["variables"] == expected_variables
    assert ("$targetId: TodoableID!" if tool == "create_todo" else "$id: TodoID!") in request["query"]


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
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"project_id": True},
        {"group_id": 0},
        {"cursor": "bad"},
        {"cursor": ""},
        {"state": "invalid"},
        {"target_type": "Issue"},
    ],
)
@respx.mock
async def test_bad_list_arguments(gitlab_env, arguments):
    """Bounds, strict integers, and malformed cursors fail before HTTP."""
    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool("list_todos", arguments)
    assert not respx.calls


@pytest.mark.parametrize("tool", ["create_todo", "update_todo"])
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"errors": ["bad"]},
        {"todo": None, "errors": []},
        {"todo": {"id": True, "state": "pending"}, "errors": []},
        {"todo": {"id": "01", "state": "pending"}, "errors": []},
        {"todo": {"id": "gid://gitlab/Todo/4", "state": "pending"}, "errors": []},
        {"todo": {"id": "5", "state": "done"}, "errors": []},
        {"todo": {"id": "5", "state": "pending"}},
        {"todo": {"id": "5", "state": "pending", "targetUrl": 1}, "errors": []},
    ],
)
@respx.mock
async def test_ambiguous_payload(gitlab_env, tool, payload):
    """Missing, mismatched, and malformed confirmations never imply write success."""
    # Arrange
    field = "todoCreate" if tool == "create_todo" else "todoRestore"
    body = {"data": {field: payload}}
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=body))
    arguments = {"target_id": "gid://gitlab/Issue/2"} if tool == "create_todo" else {"todo_id": 5, "state": "pending"}
    # Act
    result = await call_tool(tool, arguments)
    # Assert
    assert result == UNKNOWN
    assert route.call_count == 1


@pytest.mark.parametrize("tool", ["create_todo", "update_todo"])
@pytest.mark.parametrize(
    "response",
    [
        httpx.ConnectError("connection lost"),
        httpx.Response(408),
        httpx.Response(500),
        httpx.Response(503),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"errors": [{"message": "oops"}]}),
        httpx.Response(200, json={"data": None}),
    ],
)
@respx.mock
async def test_uncertain_write_is_never_retried(gitlab_env, tool, response):
    """Transport, server, and unreadable GraphQL responses never confirm a write."""
    # Arrange
    route = respx.post(GRAPHQL).mock(side_effect=[response])
    arguments = {"target_id": "gid://gitlab/Issue/2"} if tool == "create_todo" else {"todo_id": 1, "state": "done"}
    # Act / Assert
    assert await call_tool(tool, arguments) == UNKNOWN
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


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("create_todo", {"target_id": "gid://gitlab/Issue/02"}),
        ("create_todo", {"target_id": "gid://gitlab/Issue/0"}),
        ("create_todo", {"target_id": "gid://gitlab/Todo/1"}),
        ("create_todo", {"target_id": "2"}),
        ("update_todo", {"todo_id": True, "state": "done"}),
        ("update_todo", {"todo_id": 0, "state": "done"}),
        ("update_todo", {"todo_id": "1", "state": "done"}),
        ("update_todo", {"todo_id": 1, "state": "all"}),
    ],
)
@respx.mock
async def test_invalid_write_arguments(gitlab_env, tool, arguments):
    """Invalid target IDs, strict numeric IDs, and update states cannot reach GitLab."""
    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(tool, arguments)
    assert not respx.calls


@pytest.mark.parametrize("end_cursor", [None, "", " ", 1])
@respx.mock
async def test_more_rows_require_a_usable_cursor(gitlab_env, end_cursor):
    """A page cannot silently claim completion when its continuation is unavailable."""
    # Arrange
    body = {"currentUser": {"todos": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": end_cursor}}}}
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": body}))
    # Act / Assert
    with pytest.raises(ToolError, match="invalid to-do page"):
        await call_tool("list_todos")
    assert route.call_count == 1


@respx.mock
async def test_unavailable_target_preserves_the_row_and_page_bound(gitlab_env):
    """Null targets keep their row, while excess rows are rejected rather than silently cut."""
    # Arrange
    node = {"id": "5", "state": "done", "project": {"fullPath": "group/project"}, "targetEntity": None}
    body = {"currentUser": {"todos": {"nodes": [node], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": body}))
    async with Client(mcp) as client:
        # Act
        result = await call_tool("list_todos", {"limit": 1}, client=client)
        # Assert
        row = result["todos"].splitlines()[1].split("\t")
        assert row == ["5", "done", "", "", "group/project", "", "", "", ""]
        assert "next_cursor" not in result
        body["currentUser"]["todos"]["nodes"].append(node)
        route.mock(return_value=httpx.Response(200, json={"data": body}))
        with pytest.raises(ToolError, match="invalid to-do page"):
            await call_tool("list_todos", {"limit": 1}, client=client)


@pytest.mark.parametrize(
    "target",
    [
        {"id": "gid://gitlab/Issue/5", "name": "Issue"},
        {"id": "gid://gitlab/MergeRequest/5", "name": "Merge request"},
        {"workItemId": "gid://gitlab/WorkItem/5", "name": "Work item"},
    ],
)
@respx.mock
async def test_target_id_fragments_share_one_tsv_column(gitlab_env, target):
    """The aliased work-item scalar maps to the same column as issue and merge-request IDs."""
    # Arrange
    node = {"id": "5", "targetEntity": target}
    body = {"currentUser": {"todos": {"nodes": [node], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": body}))
    # Act
    result = await call_tool("list_todos")
    # Assert
    row = result["todos"].splitlines()[1].split("\t")
    assert row[5] == target.get("id", target.get("workItemId"))
    assert row[6] == target["name"]


@respx.mock
async def test_update_cannot_confirm_a_different_todo(gitlab_env):
    """A valid numeric ID for another to-do does not confirm the requested update."""
    # Arrange
    body = {"data": {"todoMarkDone": {"todo": {"id": "2", "state": "done"}, "errors": []}}}
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=body))
    # Act / Assert
    assert await call_tool("update_todo", {"todo_id": 1, "state": "done"}) == UNKNOWN
    assert route.call_count == 1
