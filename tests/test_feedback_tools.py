"""Behavioral tests for the thin feedback tool wrappers 🧪."""

import json
from typing import Any, Literal

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import mcp

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 7
WORK_ITEM_ID = "gid://gitlab/WorkItem/123"
MR = f"https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/{IID}"
WORK_ITEM_READ_AND_MUTATE_REQUESTS = 2


@pytest.fixture
def router(gitlab_env):
    """Give each test an isolated mock router."""
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


async def call_tool(name: str, **arguments: Any) -> dict[str, Any]:
    """Call one registered production tool through the MCP protocol."""
    async with Client(mcp) as client:
        result = await client.call_tool(name, {"project": PROJECT, "iid": IID, **arguments})
    return result.structured_content


def work_item_response(scope: Literal["project", "group"], path: str = PROJECT) -> httpx.Response:
    """Build a fully scoped work item response with no optional widgets."""
    namespace_type = "Project" if scope == "project" else "Group"
    work_item = {
        "id": WORK_ITEM_ID,
        "iid": str(IID),
        "title": "Update platform docs",
        "description": "Keep this description.",
        "state": "OPEN",
        "webUrl": f"https://gitlab.example.com/{path}/-/work_items/{IID}",
        "namespace": {"__typename": "Namespace", "fullPath": path},
        "workItemType": {"name": "Epic" if scope == "group" else "Issue"},
        "widgets": [],
    }
    return httpx.Response(
        200,
        json={
            "data": {
                "scopeNamespace": {"__typename": namespace_type, "fullPath": path},
                "namespace": {"__typename": "Namespace", "fullPath": path, "workItem": work_item},
            }
        },
    )


def notes_response() -> httpx.Response:
    """Build one discussion containing a system note and one human note."""
    work_item = {
        "iid": str(IID),
        "widgets": [
            {
                "__typename": "WorkItemWidgetNotes",
                "discussions": {
                    "nodes": [
                        {
                            "notes": {
                                "nodes": [
                                    {
                                        "system": True,
                                        "body": "changed the title",
                                        "createdAt": "2026-10-20T11:00:00Z",
                                        "author": {"username": "bot"},
                                    },
                                    {
                                        "system": False,
                                        "body": "Please add a test.",
                                        "createdAt": "2026-10-21T12:00:00Z",
                                        "author": {"username": "reviewer"},
                                    },
                                ],
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        ],
    }
    return httpx.Response(
        200,
        json={
            "data": {
                "scopeNamespace": {"__typename": "Project", "fullPath": PROJECT},
                "namespace": {"__typename": "Namespace", "fullPath": PROJECT, "workItem": work_item},
            }
        },
    )


def operation(request: httpx.Request) -> str:
    """Return one GraphQL operation's query text."""
    return json.loads(request.content)["query"]


async def test_get_work_item_uses_explicit_project_and_group_scopes(gitlab_env, router):
    """The wrapper forwards project and group scope to the scoped GraphQL reader."""
    # Arrange
    calls: list[tuple[Literal["project", "group"], str]] = [("project", PROJECT), ("group", "group")]
    route = router.post(GRAPHQL).mock(
        side_effect=[work_item_response(scope, path) for scope, path in calls],
    )

    # Act
    project_result = await call_tool("get_work_item")
    group_result = await call_tool("get_work_item", project="group", scope="group")

    # Assert
    assert project_result["title"] == "Update platform docs"
    assert project_result["type"] == "Issue"
    assert group_result["namespace_path"] == "group"
    assert group_result["type"] == "Epic"
    requests = [json.loads(call.request.content) for call in route.calls]
    assert [body["variables"]["fullPath"] for body in requests] == [PROJECT, "group"]
    assert "scopeNamespace: project(fullPath: $fullPath)" in requests[0]["query"]
    assert "scopeNamespace: group(fullPath: $fullPath)" in requests[1]["query"]


async def test_list_work_item_notes_omits_system_notes(gitlab_env, router):
    """The production wrapper returns human notes and omits GitLab system notes."""
    # Arrange
    route = router.post(GRAPHQL).mock(return_value=notes_response())

    # Act
    result = await call_tool("list_work_item_notes")

    # Assert
    assert result["notes"] == [{"by": "reviewer", "body": "Please add a test.", "at": "2026-10-21"}]
    assert result["discussions_complete"] is True
    assert result["notes_truncated"] is False
    assert json.loads(route.calls[0].request.content)["variables"] == {
        "fullPath": PROJECT,
        "iid": str(IID),
        "after": None,
    }


async def test_edit_merge_request_description_writes_the_full_replacement(gitlab_env, router):
    """The production MR tool reads the description and sends its exact full replacement once."""
    # Arrange
    router.get(MR).mock(return_value=httpx.Response(200, json={"description": "Before. Keep this. After."}))
    put = router.put(MR).mock(return_value=httpx.Response(200, json={}))

    # Act
    result = await call_tool("edit_merge_request_description", old_text="Keep this.", new_text="Keep all this.")

    # Assert
    assert result["status"] == "applied"
    assert json.loads(put.calls[0].request.content) == {"description": "Before. Keep all this. After."}
    assert put.call_count == 1


async def test_edit_work_item_description_sends_the_global_id_mutation(gitlab_env, router):
    """The production work-item tool reads the scoped item and sends one global-ID update mutation."""
    # Arrange
    source = {
        "data": {
            "scopeNamespace": {"__typename": "Project", "fullPath": PROJECT},
            "namespace": {
                "__typename": "Namespace",
                "fullPath": PROJECT,
                "workItem": {"id": WORK_ITEM_ID, "iid": str(IID), "description": "Plan old text."},
            },
        }
    }
    updated = {"data": {"workItemUpdate": {"errors": [], "workItem": {"id": WORK_ITEM_ID}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        """Return the source on read and the update payload on mutation."""
        return httpx.Response(200, json=updated if "workItemUpdate" in operation(request) else source)

    route = router.post(GRAPHQL).mock(side_effect=handler)

    # Act
    result = await call_tool("edit_work_item_description", old_text="old text", new_text="new text")

    # Assert
    mutations = [
        json.loads(call.request.content)["variables"]
        for call in route.calls
        if "workItemUpdate" in operation(call.request)
    ]
    assert result["status"] == "applied"
    assert mutations == [{"input": {"id": WORK_ITEM_ID, "descriptionWidget": {"description": "Plan new text."}}}]
    assert route.call_count == WORK_ITEM_READ_AND_MUTATE_REQUESTS


@pytest.mark.parametrize(
    ("old_text", "new_text", "message"),
    [("", "new", "must not be empty"), ("same", "same", "identical")],
)
async def test_description_edit_validation_sends_no_requests(gitlab_env, router, old_text, new_text, message):
    """Invalid exact-text edits fail before any read or write request."""
    # Arrange
    get = router.get(MR).mock(return_value=httpx.Response(200, json={"description": "same"}))
    put = router.put(MR).mock(return_value=httpx.Response(200, json={}))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("edit_merge_request_description", old_text=old_text, new_text=new_text)
    assert get.call_count == put.call_count == 0
    assert router.calls.call_count == 0


async def test_missing_environment_is_reported_lazily_without_network(gitlab_env, router, monkeypatch):
    """A missing token becomes a tool error when the wrapper needs its client, without a network call."""
    # Arrange
    monkeypatch.delenv("GITLAB_TOKEN")
    route = router.post(GRAPHQL).mock(return_value=work_item_response("project"))

    # Act / Assert
    with pytest.raises(ToolError, match="GITLAB_TOKEN is not set"):
        await call_tool("get_work_item")
    assert route.call_count == 0
