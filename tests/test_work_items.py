"""Behavioral tests for the work-item GraphQL helpers 🧪."""

import base64
import hashlib
import json
from typing import Any, Literal, cast

import httpx
import pytest
import respx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from gitlab_mcp.work_items import (
    NOTE_BODY_BUDGET,
    read_work_item,
    read_work_item_for_edit,
    read_work_item_notes,
)
from mcp_helpers import call_tool as call_mcp_tool
lazy from gitlab_mcp.client import GitLabClient
lazy from gitlab_mcp.config import Settings

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 42
TEST_APP = FastMCP("work-item-tests")
EXPECTED_PAGE_REQUESTS = 2


@TEST_APP.tool
async def get_work_item(  # noqa: PLR0913, PLR0917 - mirrors the production wrapper arguments.
    project: str,
    iid: Any,
    scope: Any = "project",
    include_description: Any = True,
    description_limit: Any = 4000,
    cursor: Any = None,
) -> Any:
    """Expose the detail helper through an in-memory test server."""
    client = GitLabClient(Settings.from_env())
    try:
        return await read_work_item(
            client,
            project,
            cast("int", iid),
            cast("Literal['project', 'group']", scope),
            cast("bool", include_description),
            cast("int", description_limit),
            cast("str | None", cursor),
        )
    finally:
        await client.aclose()


@TEST_APP.tool
async def get_work_item_notes(
    project: str,
    iid: int,
    scope: Literal["project", "group"] = "project",
    cursor: Any = None,
    max_body_chars: int = 4000,
) -> Any:
    """Expose the notes helper through an in-memory test server."""
    client = GitLabClient(Settings.from_env())
    try:
        return await read_work_item_notes(client, project, iid, scope, cast("str | None", cursor), max_body_chars)
    finally:
        await client.aclose()


@TEST_APP.tool
async def get_work_item_source(
    project: str,
    iid: int,
    scope: Literal["project", "group"] = "project",
) -> Any:
    """Expose the exact edit-source reader through the in-memory test server."""
    client = GitLabClient(Settings.from_env())
    try:
        return await read_work_item_for_edit(client, project, iid, scope)
    finally:
        await client.aclose()


def work_item(**overrides: Any) -> dict[str, Any]:
    """Build a work item with all supported widgets and one entry per connection."""
    node = {
        "id": "gid://gitlab/WorkItem/123",
        "iid": str(IID),
        "title": "Update platform docs",
        "description": "A long description",
        "state": "OPEN",
        "webUrl": "https://gitlab.example.com/group/project/-/work_items/42",
        "namespace": {"__typename": "Namespace", "fullPath": PROJECT},
        "workItemType": {"name": "Issue"},
        "widgets": [
            {
                "__typename": "WorkItemWidgetLabels",
                "labels": {"nodes": [{"title": "docs"}], "pageInfo": {"hasNextPage": False, "endCursor": None}},
            },
            {
                "__typename": "WorkItemWidgetAssignees",
                "assignees": {
                    "nodes": [{"username": "mhayden"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            },
            {
                "__typename": "WorkItemWidgetLinkedItems",
                "linkedItems": {
                    "nodes": [
                        {
                            "linkType": "BLOCKS",
                            "workItem": reference("group/other", 8, "Fix deployment"),
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            },
            {
                "__typename": "WorkItemWidgetHierarchy",
                "parent": reference(PROJECT, 3, "Parent item"),
                "children": {
                    "nodes": [reference(PROJECT, 43, "Child item")],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            },
        ],
    }
    return node | overrides


def reference(namespace_path: str, iid: int, title: str) -> dict[str, Any]:
    """Build a namespace-qualified work-item reference from GraphQL."""
    return {
        "id": f"gid://gitlab/WorkItem/{iid}",
        "iid": str(iid),
        "title": title,
        "webUrl": f"https://gitlab.example.com/{namespace_path}/-/work_items/{iid}",
        "namespace": {"__typename": "Namespace", "fullPath": namespace_path},
        "workItemType": {"name": "Issue"},
    }


def response(
    item: dict[str, Any] | None,
    *,
    scope: Literal["project", "group"] = "project",
    path: str = PROJECT,
) -> httpx.Response:
    """Wrap an item in the namespace.fullPath.workItem GraphQL result."""
    scope_type = "Project" if scope == "project" else "Group"
    scope_namespace = {"__typename": scope_type, "fullPath": path}
    namespace = {"__typename": "Namespace", "fullPath": path, "workItem": item}
    return httpx.Response(200, json={"data": {"scopeNamespace": scope_namespace, "namespace": namespace}})


def graphql_response(data: dict[str, Any]) -> httpx.Response:
    """Wrap arbitrary GraphQL data for response-shape error tests."""
    return httpx.Response(200, json={"data": data})


def encode_cursor(payload: Any) -> str:
    """Encode a test cursor payload without calling the production cursor helpers."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_cursor(cursor: str) -> dict[str, Any]:
    """Decode an emitted cursor so a behavioral test can corrupt one field."""
    return json.loads(base64.urlsafe_b64decode(cursor))


def request_variables(route: respx.Route) -> list[dict[str, Any]]:
    """Return all GraphQL variables sent to one mocked route."""
    return [json.loads(call.request.content)["variables"] for call in route.calls]


async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one helper on the test-only FastMCP app and return its structured result."""
    return await call_mcp_tool(name, arguments, app=TEST_APP)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"scope": "namespace"}, "scope must be either"),
        ({"project": " "}, "project must not be blank"),
        ({"iid": 0}, "iid must be a positive integer"),
        ({"iid": True}, "iid must be a positive integer"),
        ({"description_limit": -1}, "description_limit must be between 0 and 4000"),
        ({"description_limit": 4001}, "description_limit must be between 0 and 4000"),
        ({"description_limit": True}, "description_limit must be between 0 and 4000"),
        ({"include_description": 1}, "include_description must be a boolean"),
    ],
)
@respx.mock
async def test_detail_arguments_fail_before_request(gitlab_env, arguments, message):
    """Invalid helper arguments return clear errors without sending GraphQL requests."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(work_item()))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID, **arguments})
    assert route.call_count == 0


@respx.mock
async def test_project_details_map_widgets_and_explicit_completeness(gitlab_env):
    """Project details include labels, assignees, links, hierarchy, and exact query scope."""
    # Arrange
    mock_response = response(work_item())
    route = respx.post(GRAPHQL).mock(return_value=mock_response)

    # Act
    result = await call_tool("get_work_item", {"project": PROJECT, "iid": IID})

    # Assert
    assert request_variables(route) == [
        {
            "fullPath": PROJECT,
            "iid": str(IID),
            "includeDescription": True,
            "includeLabels": True,
            "includeAssignees": True,
            "includeLinkedItems": True,
            "includeChildren": True,
            "labelsAfter": None,
            "assigneesAfter": None,
            "linkedItemsAfter": None,
            "childrenAfter": None,
        },
    ]
    query = json.loads(route.calls.last.request.content)["query"]
    assert "scopeNamespace: project(fullPath: $fullPath)" in query
    assert "namespace(fullPath: $fullPath)" in query
    payload = mock_response.json()["data"]
    assert payload["scopeNamespace"]["__typename"] == "Project"
    assert payload["namespace"]["__typename"] == "Namespace"
    assert result == {
        "id": "gid://gitlab/WorkItem/123",
        "iid": IID,
        "title": "Update platform docs",
        "type": "Issue",
        "state": "OPEN",
        "namespace_path": PROJECT,
        "url": "https://gitlab.example.com/group/project/-/work_items/42",
        "description": "A long description",
        "description_hash": hashlib.sha256(b"A long description").hexdigest(),
        "labels": ["docs"],
        "labels_complete": True,
        "assignees": ["mhayden"],
        "assignees_complete": True,
        "linked_items": [
            {
                "id": "gid://gitlab/WorkItem/8",
                "iid": 8,
                "title": "Fix deployment",
                "type": "Issue",
                "namespace_path": "group/other",
                "url": "https://gitlab.example.com/group/other/-/work_items/8",
                "link_type": "BLOCKS",
            },
        ],
        "linked_items_complete": True,
        "parent": {
            "id": "gid://gitlab/WorkItem/3",
            "iid": 3,
            "title": "Parent item",
            "type": "Issue",
            "namespace_path": PROJECT,
            "url": "https://gitlab.example.com/group/project/-/work_items/3",
        },
        "children": [
            {
                "id": "gid://gitlab/WorkItem/43",
                "iid": 43,
                "title": "Child item",
                "type": "Issue",
                "namespace_path": PROJECT,
                "url": "https://gitlab.example.com/group/project/-/work_items/43",
            },
        ],
        "children_complete": True,
    }


@respx.mock
async def test_group_scope_is_explicit_and_namespace_qualified(gitlab_env):
    """Group scope queries the group full path and reports the namespace URL."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=response(
            work_item(
                iid="7",
                namespace={"__typename": "Namespace", "fullPath": "group"},
                webUrl="https://gitlab.example.com/groups/group/-/work_items/7",
                widgets=[],
            ),
            scope="group",
            path="group",
        ),
    )

    # Act
    result = await call_tool("get_work_item", {"project": "group", "iid": 7, "scope": "group"})

    # Assert
    variables = request_variables(route)[0]
    assert variables["fullPath"] == "group"
    assert variables["iid"] == "7"
    query = json.loads(route.calls.last.request.content)["query"]
    assert "scopeNamespace: group(fullPath: $fullPath)" in query
    assert "namespace(fullPath: $fullPath)" in query
    payload = route.calls.last.response.json()["data"]
    assert payload["scopeNamespace"]["__typename"] == "Group"
    assert payload["namespace"]["__typename"] == "Namespace"
    assert result["namespace_path"] == "group"
    assert result["url"] == "https://gitlab.example.com/groups/group/-/work_items/7"
    assert result["missing_widgets"] == [
        "WorkItemWidgetLabels",
        "WorkItemWidgetAssignees",
        "WorkItemWidgetLinkedItems",
        "WorkItemWidgetHierarchy",
    ]
    assert result["labels_complete"] is False


@pytest.mark.parametrize(
    ("include_description", "description_limit", "include_query_field", "expected_description"),
    [(False, 4000, False, None), (True, 4, True, "A lo"), (True, 0, False, None)],
)
@respx.mock
async def test_description_can_be_skipped_or_truncated(
    gitlab_env, include_description, description_limit, include_query_field, expected_description
):
    """Details omit skipped descriptions and report when a requested description was cut."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(work_item()))

    # Act
    result = await call_tool(
        "get_work_item",
        {
            "project": PROJECT,
            "iid": IID,
            "include_description": include_description,
            "description_limit": description_limit,
        },
    )

    # Assert
    assert request_variables(route)[0]["includeDescription"] is include_query_field
    query = json.loads(route.calls.last.request.content)["query"]
    assert "description @include(if: $includeDescription)" in query
    assert result.get("description") == expected_description
    assert result.get("description_truncated", False) is (expected_description == "A lo")


@respx.mock
async def test_detail_cursor_resumes_each_incomplete_connection(gitlab_env):
    """An opaque argument-bound cursor advances pending connections and skips completed ones."""
    # Arrange
    first_node = work_item()
    first_node["widgets"][0]["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "labels-next"}
    second_node = work_item()
    second_node["widgets"][0]["labels"]["nodes"] = [{"title": "second-page"}]
    second_node["widgets"][0]["labels"]["pageInfo"] = {"hasNextPage": False, "endCursor": "labels-last"}
    route = respx.post(GRAPHQL).mock(
        side_effect=[response(first_node), response(second_node)],
    )

    # Act
    first = await call_tool("get_work_item", {"project": PROJECT, "iid": IID})
    second = await call_tool(
        "get_work_item",
        {"project": PROJECT, "iid": IID, "cursor": first["next_cursor"]},
    )

    # Assert
    first_variables, second_variables = request_variables(route)
    assert first_variables["labelsAfter"] is None
    assert first_variables["includeLabels"] is True
    assert second_variables["labelsAfter"] == "labels-next"
    assert second_variables["includeLabels"] is True
    assert second_variables["includeAssignees"] is False
    assert second_variables["includeLinkedItems"] is False
    assert second_variables["includeChildren"] is False
    assert first["labels"] == ["docs"]
    assert first["labels_complete"] is False
    assert first["next_cursor"]
    assert second["labels"] == ["second-page"]
    assert second["labels_complete"] is True
    assert "next_cursor" not in second
    assert route.call_count == EXPECTED_PAGE_REQUESTS


@respx.mock
async def test_detail_cursor_tracks_each_connection_separately(gitlab_env):
    """The detail cursor carries independent after cursors for parallel widget connections."""
    # Arrange
    first_node = work_item()
    first_node["widgets"][0]["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "label-cursor"}
    first_node["widgets"][1]["assignees"]["pageInfo"] = {"hasNextPage": True, "endCursor": "assignee-cursor"}
    first_node["widgets"][3]["children"]["pageInfo"] = {"hasNextPage": True, "endCursor": "child-cursor"}
    route = respx.post(GRAPHQL).mock(return_value=response(first_node))

    # Act
    first = await call_tool("get_work_item", {"project": PROJECT, "iid": IID})
    await call_tool("get_work_item", {"project": PROJECT, "iid": IID, "cursor": first["next_cursor"]})

    # Assert
    second_variables = request_variables(route)[1]
    assert second_variables["labelsAfter"] == "label-cursor"
    assert second_variables["assigneesAfter"] == "assignee-cursor"
    assert second_variables["childrenAfter"] == "child-cursor"
    assert second_variables["linkedItemsAfter"] is None


@pytest.mark.parametrize(
    "changed_arguments",
    [
        {"scope": "group"},
        {"project": "group/other"},
        {"iid": IID + 1},
        {"include_description": False},
        {"description_limit": 100},
    ],
)
@respx.mock
async def test_detail_cursor_is_bound_to_arguments(gitlab_env, changed_arguments):
    """Detail cursors cannot be reused for a different scope, item, or description request."""
    # Arrange
    node = work_item()
    node["widgets"][0]["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "labels-next"}
    route = respx.post(GRAPHQL).mock(return_value=response(node))

    # Act
    result = await call_tool("get_work_item", {"project": PROJECT, "iid": IID})

    # Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool(
            "get_work_item",
            {"project": PROJECT, "iid": IID, "cursor": result["next_cursor"], **changed_arguments},
        )
    assert route.call_count == 1


@pytest.mark.parametrize("cursor", [123, "not-a-cursor", encode_cursor([]), encode_cursor({})])
@respx.mock
async def test_malformed_detail_cursors_fail_before_request(gitlab_env, cursor):
    """Detail cursors with invalid encoding or payload shapes are rejected early."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(work_item()))

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID, "cursor": cursor})
    assert route.call_count == 0


@respx.mock
async def test_detail_cursor_rejects_invalid_connection_state(gitlab_env):
    """An included detail connection must carry its after cursor."""
    # Arrange
    node = work_item()
    node["widgets"][0]["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "labels-next"}
    route = respx.post(GRAPHQL).mock(return_value=response(node))

    # Act
    result = await call_tool("get_work_item", {"project": PROJECT, "iid": IID})
    payload = decode_cursor(result["next_cursor"])
    payload["connections"]["labels"]["after"] = None
    malformed_cursor = encode_cursor(payload)

    # Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID, "cursor": malformed_cursor})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("connection", "message"),
    [
        (None, "invalid WorkItemWidgetLabels.labels connection"),
        ({"pageInfo": {"hasNextPage": False}}, "invalid WorkItemWidgetLabels.labels connection"),
        ({"nodes": [], "pageInfo": None}, "invalid pagination data"),
        ({"nodes": [], "pageInfo": {"hasNextPage": "no"}}, "invalid pagination data"),
        (
            {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": 1}},
            "invalid cursor for WorkItemWidgetLabels.labels",
        ),
        (
            {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": ""}},
            "did not return a cursor",
        ),
    ],
)
@respx.mock
async def test_invalid_detail_connection_pagination_is_reported(gitlab_env, connection, message):
    """Malformed widget connections and cursors fail with no partial result."""
    # Arrange
    node = work_item()
    node["widgets"][0]["labels"] = connection
    route = respx.post(GRAPHQL).mock(return_value=response(node))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@respx.mock
async def test_linked_item_requires_graphql_web_url(gitlab_env):
    """The helper does not guess a namespace-qualified URL when GraphQL omits webUrl."""
    # Arrange
    node = work_item()
    linked_item = node["widgets"][2]["linkedItems"]["nodes"][0]["workItem"]
    linked_item["webUrl"] = None
    route = respx.post(GRAPHQL).mock(return_value=response(node))

    # Act / Assert
    with pytest.raises(ToolError, match="without its namespace path or web URL"):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("namespace", None, "without a namespace"),
        ("namespace", {"__typename": "Namespace", "fullPath": ""}, "without its namespace path or web URL"),
        ("iid", "not-an-iid", "without a valid IID"),
        ("iid", "0", "without a positive IID"),
        ("id", None, "without a valid id field"),
        ("title", None, "without a valid title field"),
    ],
)
@respx.mock
async def test_invalid_linked_work_item_references_are_errors(gitlab_env, field, value, message):
    """Linked references must include valid IDs and namespace-qualified URL data."""
    # Arrange
    node = work_item()
    linked = node["widgets"][2]["linkedItems"]["nodes"][0]["workItem"]
    linked[field] = value
    route = respx.post(GRAPHQL).mock(return_value=response(node))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("widgets", "message"),
    [
        (
            [
                {
                    "__typename": "WorkItemWidgetLinkedItems",
                    "linkedItems": {"nodes": [None], "pageInfo": {"hasNextPage": False}},
                },
            ],
            "invalid linked work item",
        ),
        (
            [
                {
                    "__typename": "WorkItemWidgetHierarchy",
                    "parent": "not-an-object",
                    "children": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                },
            ],
            "invalid parent work item",
        ),
        (
            [
                {
                    "__typename": "WorkItemWidgetHierarchy",
                    "parent": None,
                    "children": {"nodes": [None], "pageInfo": {"hasNextPage": False}},
                },
            ],
            "invalid child work item",
        ),
    ],
)
@respx.mock
async def test_invalid_linked_and_hierarchy_nodes_are_errors(gitlab_env, widgets, message):
    """Malformed linked, parent, and child nodes do not become partial details."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(work_item(widgets=widgets)))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("item", "scope", "message"),
    [
        (None, "project", "Work item #42 not found"),
        (work_item(), "group", "not the requested group scope"),
    ],
)
@respx.mock
async def test_missing_item_and_wrong_scope_are_errors(gitlab_env, item, scope, message):
    """Missing work items and namespace type mismatches never fall back to another scope."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(item))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID, "scope": scope})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"namespace": {"__typename": "Namespace", "workItem": work_item()}}, "Work item #42 not found"),
        ({"scopeNamespace": "not-an-object"}, "invalid project or group scope response"),
        (
            {
                "scopeNamespace": {"__typename": "Project", "fullPath": "different/path"},
                "namespace": {"__typename": "Namespace", "workItem": work_item()},
            },
            "different full path",
        ),
        ({"scopeNamespace": {"__typename": "Project", "fullPath": PROJECT}}, "Work item #42 not found"),
        (
            {
                "scopeNamespace": {"__typename": "Project", "fullPath": PROJECT},
                "namespace": "not-an-object",
            },
            "invalid namespace response",
        ),
        (
            {
                "scopeNamespace": {"__typename": "Project", "fullPath": PROJECT},
                "namespace": {"__typename": "Namespace", "workItem": "not-an-object"},
            },
            "invalid work item response",
        ),
    ],
)
@respx.mock
async def test_invalid_scope_and_namespace_responses_are_reported(gitlab_env, data, message):
    """Malformed scope roots and Namespace responses fail rather than falling back."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(data))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("widgets", "message"),
    [
        (None, "without a valid widgets list"),
        ([{"labels": {}}], "without a GraphQL type name"),
        (
            [
                {"__typename": "WorkItemWidgetLabels"},
                {"__typename": "WorkItemWidgetLabels"},
            ],
            "duplicate WorkItemWidgetLabels widgets",
        ),
    ],
)
@respx.mock
async def test_invalid_widget_shapes_raise_clear_errors(gitlab_env, widgets, message):
    """Malformed widgets are not converted into misleading empty details."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(work_item(widgets=widgets)))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": None}, "without a valid id field"),
        ({"title": None}, "without a valid title field"),
        (
            {
                "widgets": [
                    {
                        "__typename": "WorkItemWidgetLabels",
                        "labels": {"nodes": [None], "pageInfo": {"hasNextPage": False}},
                    },
                ]
            },
            "invalid label widget entry",
        ),
    ],
)
@respx.mock
async def test_invalid_detail_values_are_reported(gitlab_env, overrides, message):
    """Unexpected required fields and malformed label nodes are not silently dropped."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(work_item(**overrides)))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"iid": "43"}, "different work item IID"),
        ({"iid": None}, "without a valid IID"),
        ({"description": 7}, "invalid work item description"),
    ],
)
@respx.mock
async def test_invalid_detail_iid_and_description_are_errors(gitlab_env, overrides, message):
    """Details reject mismatched IIDs and malformed descriptions."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(work_item(**overrides)))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@respx.mock
async def test_detail_null_description_is_omitted(gitlab_env):
    """A null description is a valid empty detail field."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(work_item(description=None)))

    # Act
    result = await call_tool("get_work_item", {"project": PROJECT, "iid": IID})

    # Assert
    assert "description" not in result
    assert "description_truncated" not in result
    assert route.call_count == 1


@respx.mock
async def test_edit_source_reads_full_description_and_global_id(gitlab_env):
    """The edit helper returns the exact description instead of the compact cap."""
    # Arrange
    full_description = "d" * 10000
    route = respx.post(GRAPHQL).mock(return_value=response(work_item(description=full_description)))

    # Act
    source = await call_tool("get_work_item_source", {"project": PROJECT, "iid": IID})

    # Assert
    assert source == {"id": "gid://gitlab/WorkItem/123", "description": full_description}
    assert "description @include" not in json.loads(route.calls.last.request.content)["query"]


@respx.mock
async def test_edit_source_treats_null_description_as_empty(gitlab_env):
    """A work item with a null description has an empty, editable description."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(work_item(description=None)))

    # Act
    source = await call_tool("get_work_item_source", {"project": PROJECT, "iid": IID})

    # Assert
    assert source == {"id": "gid://gitlab/WorkItem/123", "description": ""}
    assert "scopeNamespace: project(fullPath: $fullPath)" in json.loads(route.calls.last.request.content)["query"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"iid": "43"}, "different work item IID"),
        ({"description": 7}, "invalid full work item description"),
    ],
)
@respx.mock
async def test_edit_source_rejects_mismatched_iid_and_invalid_description(gitlab_env, overrides, message):
    """Edit source reads require the requested IID and a string description."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(work_item(**overrides)))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item_source", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


def note(
    body: str,
    *,
    system: bool = False,
    username: str | None = "mhayden",
    created_at: str = "2026-10-21T10:00:00Z",
) -> dict[str, Any]:
    """Build one note node, including null authors and system notes."""
    return {
        "system": system,
        "body": body,
        "createdAt": created_at,
        "author": {"username": username} if username else None,
    }


def notes_response(  # noqa: PLR0913 - fixture exposes optional response fields for behavior coverage.
    discussions: list[dict[str, Any]],
    *,
    has_next: bool = False,
    end_cursor: str | None = None,
    item_iid: int | str | None = IID,
    scope: Literal["project", "group"] = "project",
    path: str = PROJECT,
) -> httpx.Response:
    """Wrap discussion and nested note pages in a work-item notes widget."""
    item = {
        "widgets": [
            {
                "__typename": "WorkItemWidgetNotes",
                "discussions": {
                    "nodes": discussions,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                },
            },
        ],
    }
    if item_iid is not None:
        item["iid"] = str(item_iid)
    return response(item, scope=scope, path=path)


@respx.mock
async def test_notes_filter_system_notes_cap_bodies_and_paginate_discussions(gitlab_env):
    """Human notes survive system filtering, body caps, date trimming, and cursor output."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=notes_response(
            [
                {
                    "notes": {
                        "nodes": [
                            note("automated", system=True),
                            note("x" * 20, username=None),
                            note("short", created_at="2026-10-22T11:00:00Z"),
                        ],
                        "pageInfo": {"hasNextPage": False},
                    },
                },
            ],
            has_next=True,
            end_cursor="discussion-next",
        ),
    )

    # Act
    result = await call_tool(
        "get_work_item_notes",
        {"project": PROJECT, "iid": IID, "max_body_chars": 5},
    )

    # Assert
    assert request_variables(route) == [{"fullPath": PROJECT, "iid": str(IID), "after": None}]
    query = json.loads(route.calls.last.request.content)["query"]
    assert "scopeNamespace: project(fullPath: $fullPath)" in query
    assert "namespace(fullPath: $fullPath)" in query
    assert "workItem(iid: $iid) {\n      iid" in query
    assert result["notes"] == [
        {"body": "xxxxx", "at": "2026-10-21", "body_truncated": True},
        {"by": "mhayden", "body": "short", "at": "2026-10-22"},
    ]
    assert result["discussions_complete"] is False
    assert result["notes_truncated"] is True
    assert result["next_cursor"]


@respx.mock
async def test_group_notes_use_group_scope_root(gitlab_env):
    """Notes lookup resolves a group without probing or falling back to a project."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=notes_response([], scope="group", path="group"))

    # Act
    result = await call_tool("get_work_item_notes", {"project": "group", "iid": IID, "scope": "group"})

    # Assert
    query = json.loads(route.calls.last.request.content)["query"]
    assert "scopeNamespace: group(fullPath: $fullPath)" in query
    assert result["discussions_complete"] is True
    assert result["notes_truncated"] is False


@respx.mock
async def test_notes_reject_a_different_returned_iid(gitlab_env):
    """Notes do not accept a different work item returned for the requested IID."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=notes_response([], item_iid=IID + 1))

    # Act / Assert
    with pytest.raises(ToolError, match="different work item IID"):
        await call_tool("get_work_item_notes", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@pytest.mark.parametrize("item_iid", [None, "not-an-iid", "0"])
@respx.mock
async def test_notes_report_invalid_returned_iid_field(gitlab_env, item_iid):
    """Missing or invalid notes query IIDs identify the response field in the error."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=notes_response([], item_iid=item_iid))

    # Act / Assert
    with pytest.raises(ToolError, match="notes query field"):
        await call_tool("get_work_item_notes", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@pytest.mark.parametrize(
    "changed_arguments",
    [
        {"scope": "group"},
        {"project": "group/other"},
        {"iid": IID + 1},
        {"max_body_chars": 1},
    ],
)
@respx.mock
async def test_notes_cursor_resumes_and_is_bound_to_arguments(gitlab_env, changed_arguments):
    """The cursor resumes the next discussion page and cannot move to changed arguments."""
    # Arrange
    first = notes_response([], has_next=True, end_cursor="d-2")
    second = notes_response([{"notes": {"nodes": [note("next page")], "pageInfo": {"hasNextPage": False}}}])
    route = respx.post(GRAPHQL).mock(side_effect=[first, second])

    # Act
    first_result = await call_tool("get_work_item_notes", {"project": PROJECT, "iid": IID})
    second_result = await call_tool(
        "get_work_item_notes",
        {"project": PROJECT, "iid": IID, "cursor": first_result["next_cursor"]},
    )

    # Assert
    assert [variables["after"] for variables in request_variables(route)] == [None, "d-2"]
    assert second_result["notes"][0]["body"] == "next page"
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool(
            "get_work_item_notes",
            {
                "project": PROJECT,
                "iid": IID,
                "cursor": first_result["next_cursor"],
                **changed_arguments,
            },
        )
    assert route.call_count == EXPECTED_PAGE_REQUESTS


@pytest.mark.parametrize(
    "cursor",
    [
        123,
        "not-a-cursor",
        "",
        encode_cursor([]),
        encode_cursor({}),
        encode_cursor(
            {
                "version": 1,
                "scope": "project",
                "project": PROJECT,
                "iid": IID,
                "max_body_chars": 4000,
                "after": 123,
            },
        ),
    ],
)
@respx.mock
async def test_invalid_notes_cursor_fails_before_request(gitlab_env, cursor):
    """Malformed or explicitly empty cursors cannot restart a notes scan silently."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=notes_response([]))

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("get_work_item_notes", {"project": PROJECT, "iid": IID, "cursor": cursor})
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("discussion_count", "expected_notes", "expected_truncated"),
    [(2, 2, False), (9, 8, True)],
)
@respx.mock
async def test_notes_respect_total_body_budget(gitlab_env, discussion_count, expected_notes, expected_truncated):
    """The total response body budget bounds the output even when many notes are returned."""
    # Arrange
    discussions = [
        {"notes": {"nodes": [note("x" * 4000)], "pageInfo": {"hasNextPage": False}}} for _ in range(discussion_count)
    ]
    respx.post(GRAPHQL).mock(return_value=notes_response(discussions))

    # Act
    result = await call_tool("get_work_item_notes", {"project": PROJECT, "iid": IID})

    # Assert
    assert len(result["notes"]) == expected_notes
    assert sum(len(entry["body"]) for entry in result["notes"]) <= NOTE_BODY_BUDGET
    assert result["notes_truncated"] is expected_truncated


@respx.mock
async def test_missing_notes_widget_is_an_error(gitlab_env):
    """A missing notes widget is not mistaken for an empty notes list."""
    # Arrange
    route = respx.post(
        GRAPHQL,
    ).mock(
        return_value=response({"iid": str(IID), "widgets": [{"__typename": "WorkItemWidgetLabels"}]}),
    )

    # Act / Assert
    with pytest.raises(ToolError, match="did not return the work item's notes widget"):
        await call_tool("get_work_item_notes", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("widget", "message"),
    [
        ({"__typename": "WorkItemWidgetNotes", "discussions": None}, "invalid work-item discussions connection"),
        (
            {
                "__typename": "WorkItemWidgetNotes",
                "discussions": {"nodes": [], "pageInfo": None},
            },
            "invalid work-item discussion pagination data",
        ),
        (
            {
                "__typename": "WorkItemWidgetNotes",
                "discussions": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": 1},
                },
            },
            "did not return a cursor",
        ),
    ],
)
@respx.mock
async def test_invalid_notes_discussion_pages_are_reported(gitlab_env, widget, message):
    """Malformed discussion connections and pagination data return clear errors."""
    # Arrange
    route = respx.post(
        GRAPHQL,
    ).mock(return_value=response({"iid": str(IID), "widgets": [widget]}))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item_notes", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("discussion", "message"),
    [
        (None, "invalid work-item discussion"),
        ({"notes": None}, "invalid work-item discussion"),
        ({"notes": {"nodes": None, "pageInfo": {"hasNextPage": False}}}, "invalid notes connection"),
        ({"notes": {"nodes": [], "pageInfo": None}}, "invalid notes connection"),
        ({"notes": {"nodes": [], "pageInfo": {"hasNextPage": "no"}}}, "invalid notes pagination data"),
    ],
)
@respx.mock
async def test_invalid_nested_note_pages_are_reported(gitlab_env, discussion, message):
    """Malformed discussions and nested note-page metadata do not look like empty notes."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=notes_response([discussion]),
    )

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item_notes", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("note_node", "message"),
    [
        (None, "invalid work-item note"),
        (note("body") | {"body": 7}, "invalid work-item note body"),
        (note("body") | {"createdAt": 7}, "invalid work-item note timestamp"),
    ],
)
@respx.mock
async def test_invalid_human_note_values_are_reported(gitlab_env, note_node, message):
    """Malformed note bodies, timestamps, and nodes produce safe tool errors."""
    # Arrange
    response_data = notes_response(
        [{"notes": {"nodes": [note_node], "pageInfo": {"hasNextPage": False}}}],
    )
    route = respx.post(GRAPHQL).mock(return_value=response_data)

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item_notes", {"project": PROJECT, "iid": IID})
    assert route.call_count == 1


@respx.mock
async def test_notes_allow_null_authors_and_missing_timestamps(gitlab_env):
    """Deleted authors and absent timestamps map to empty optional fields."""
    # Arrange
    notes = [
        {
            "system": False,
            "body": "body",
            "createdAt": "2026-10-21T10:00:00Z",
            "author": {"username": 123},
        },
        {"system": False, "body": "", "author": None},
    ]
    route = respx.post(
        GRAPHQL,
    ).mock(
        return_value=notes_response([{"notes": {"nodes": notes, "pageInfo": {"hasNextPage": False}}}]),
    )

    # Act
    result = await call_tool("get_work_item_notes", {"project": PROJECT, "iid": IID})

    # Assert
    assert result["notes"] == [{"body": "body", "at": "2026-10-21"}, {"body": "", "at": ""}]
    assert route.call_count == 1
