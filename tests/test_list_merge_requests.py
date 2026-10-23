"""Tests for the list_merge_requests tool, run through a real in-memory MCP client 🧪."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
lazy from mcp.types import CallToolResult, TextContent

from gitlab_mcp.server import MAX_PAGES, mcp

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
WEB_URL_PREFIX = "https://gitlab.example.com/group/project/-/merge_requests/"
FULL_PAGE = 100  # The largest page GitLab serves, used when filtering client-side.


@pytest.fixture
def gitlab_env(monkeypatch):
    """Point the server at a fake self-managed GitLab instance."""
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com/api/v4/")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")


def make_node(iid: int, **overrides: Any) -> dict[str, Any]:
    """Build one merge request node shaped like the GitLab GraphQL response."""
    node = {
        "iid": str(iid),  # GraphQL returns IIDs as strings.
        "title": f"MR {iid}",
        "state": "opened",
        "draft": False,
        "updatedAt": "2026-10-20T12:00:00Z",
        "author": {"username": "jdoe"},
        "approved": False,
        "approvedBy": {"nodes": []},
        "resolvableDiscussionsCount": 0,
        "resolvedDiscussionsCount": 0,
    }
    return node | overrides


def make_page(*nodes: dict[str, Any], has_next: bool = False, cursor: str = "cursor-1") -> httpx.Response:
    """Wrap nodes in the project.mergeRequests envelope of one GraphQL page."""
    body = {
        "data": {
            "project": {
                "mergeRequests": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": list(nodes),
                },
            },
        },
    }
    return httpx.Response(200, json=body)


def sent_variables(route: respx.Route, index: int = -1) -> dict[str, Any]:
    """Return the GraphQL variables from one request the route received."""
    return json.loads(route.calls[index].request.content)["variables"]


async def call_tool_result(arguments: dict[str, Any] | None = None) -> CallToolResult:
    """Call list_merge_requests through the MCP protocol and return the raw result."""
    async with Client(mcp) as client:
        return await client.call_tool("list_merge_requests", {"project": PROJECT} | (arguments or {}))


async def call_tool(arguments: dict[str, Any] | None = None) -> dict:
    """Call list_merge_requests through the MCP protocol and return its data."""
    result = await call_tool_result(arguments)
    return result.structured_content


def iids(data: dict) -> list[int]:
    """Return the IIDs of the merge requests in a tool result."""
    return [mr["iid"] for mr in data["merge_requests"]]


@respx.mock
async def test_maps_fields_and_sends_variables(gitlab_env):
    """Nodes map to the summary shape and the default query variables are sent."""
    # Arrange
    node = make_node(
        7,
        title="Fix login",
        draft=True,
        approved=True,
        approvedBy={"nodes": [{"username": "reviewer"}]},
        resolvableDiscussionsCount=5,
        resolvedDiscussionsCount=2,
        author=None,
    )
    route = respx.post(GRAPHQL).mock(return_value=make_page(node))

    # Act
    data = await call_tool({"author": "jdoe", "limit": 5})

    # Assert
    assert route.calls.last.request.headers["PRIVATE-TOKEN"] == "glpat-test"
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "state": "opened",
        "authorUsername": "jdoe",
        "after": None,
        "first": 5,
    }
    assert data == {
        "web_url_prefix": WEB_URL_PREFIX,
        "merge_requests": [
            {
                "iid": 7,
                "title": "Fix login",
                "draft": True,
                "approved": True,
                "approved_by": ["reviewer"],
                "open_threads": 3,
                "updated": "2026-10-20",
            },
        ],
        "has_more": False,
    }


@respx.mock
async def test_defaults_list_open_mrs_by_any_author(gitlab_env):
    """With only a project, the tool asks for open MRs from any author, 20 at a time."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=make_page())

    # Act
    await call_tool()

    # Assert
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "state": "opened",
        "authorUsername": None,  # GitLab treats null as "no author filter".
        "after": None,
        "first": 20,
    }


@pytest.mark.parametrize(
    ("overrides", "expected_keys"),
    [
        (
            {},
            {"iid", "title", "author", "approved", "updated"},
        ),  # Defaults: draft, threads, and approvers are left out.
        ({"author": None}, {"iid", "title", "approved", "updated"}),  # A null author is left out too.
        (
            {"draft": True, "approvedBy": {"nodes": [{"username": "x"}]}, "resolvableDiscussionsCount": 1},
            {"iid", "title", "author", "draft", "approved", "approved_by", "open_threads", "updated"},
        ),
    ],
)
@respx.mock
async def test_empty_values_are_omitted(gitlab_env, overrides, expected_keys):
    """Empty values are left out of the structured output to save tokens."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=make_page(make_node(1, **overrides)))

    # Act
    data = await call_tool()

    # Assert
    assert set(data["merge_requests"][0]) == expected_keys


@respx.mock
async def test_text_content_omits_empty_values(gitlab_env):
    """The text the LLM reads uses the same trimmed shape as the structured output."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=make_page(make_node(1, author=None)))

    # Act
    result = await call_tool_result()

    # Assert
    block = result.content[0]
    assert isinstance(block, TextContent)
    text = json.loads(block.text)
    assert text["merge_requests"] == [{"iid": 1, "title": "MR 1", "approved": False, "updated": "2026-10-20"}]


@pytest.mark.parametrize(
    ("state", "expect_state_key"),
    [("all", True), ("opened", False), ("merged", False)],
)
@respx.mock
async def test_state_is_only_returned_when_listing_all(gitlab_env, state, expect_state_key):
    """The state field is only returned for state "all", because other callers already know it."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=make_page(make_node(1, state="opened")))

    # Act
    data = await call_tool({"state": state})

    # Assert
    mr = data["merge_requests"][0]
    assert ("state" in mr) is expect_state_key
    if expect_state_key:
        assert mr["state"] == "opened"


@pytest.mark.parametrize("state", ["opened", "closed", "locked", "merged", "all"])
@respx.mock
async def test_state_is_sent_to_gitlab(gitlab_env, state):
    """Every allowed state is passed through as the GraphQL state variable."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=make_page())

    # Act
    await call_tool({"state": state})

    # Assert
    assert sent_variables(route)["state"] == state


@pytest.mark.parametrize(
    ("filters", "expected_iids"),
    [
        ({"approved": True}, [1]),
        ({"approved": False}, [2]),
    ],
)
@respx.mock
async def test_approved_filter(gitlab_env, filters, expected_iids):
    """The approved filter keeps only MRs whose approval state matches."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=make_page(make_node(1, approved=True), make_node(2, approved=False)),
    )

    # Act
    data = await call_tool(filters)

    # Assert
    assert iids(data) == expected_iids


@pytest.mark.parametrize(
    ("filters", "expected_iids"),
    [
        ({"has_open_threads": True}, [1]),
        ({"has_open_threads": False}, [2, 3]),
    ],
)
@respx.mock
async def test_has_open_threads_filter(gitlab_env, filters, expected_iids):
    """Open threads are resolvable minus resolved, so 0 or missing counts mean none open."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=make_page(
            make_node(1, resolvableDiscussionsCount=3, resolvedDiscussionsCount=1),
            make_node(2, resolvableDiscussionsCount=2, resolvedDiscussionsCount=2),
            make_node(3, resolvableDiscussionsCount=None, resolvedDiscussionsCount=None),
        ),
    )

    # Act
    data = await call_tool(filters)

    # Assert
    assert iids(data) == expected_iids


@pytest.mark.parametrize(
    "filters",
    [
        {"approved": True},
        {"approved": False},
        {"has_open_threads": True},
        {"approved": True, "has_open_threads": False},
    ],
)
@respx.mock
async def test_client_side_filter_fetches_full_pages(gitlab_env, filters):
    """A client-side filter asks GitLab for 100 nodes per page, whatever the limit."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=make_page())

    # Act
    await call_tool(filters | {"limit": 3})

    # Assert
    assert sent_variables(route)["first"] == FULL_PAGE


@respx.mock
async def test_follows_end_cursor_until_limit_is_filled(gitlab_env):
    """The tool requests the next page with endCursor and stops once the limit is met."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            make_page(make_node(1, approved=False), make_node(2, approved=True), has_next=True, cursor="c1"),
            make_page(make_node(3, approved=True), make_node(4, approved=True), has_next=True, cursor="c2"),
        ],
    )

    # Act
    data = await call_tool({"approved": True, "limit": 2})

    # Assert
    afters = [sent_variables(route, index=i)["after"] for i in range(route.call_count)]
    assert afters == [None, "c1"]
    assert iids(data) == [2, 3]
    assert data["has_more"] is True


@pytest.mark.parametrize(
    ("nodes", "has_next", "limit", "expected_has_more"),
    [
        ([make_node(1), make_node(2)], False, 5, False),  # Everything fits on one last page.
        ([make_node(1), make_node(2)], True, 2, True),  # Limit reached, but GitLab has more.
        ([make_node(1), make_node(2), make_node(3)], False, 2, True),  # Filled more than the limit.
    ],
)
@respx.mock
async def test_has_more_reflects_remaining_results(gitlab_env, nodes, has_next, limit, expected_has_more):
    """has_more is true when GitLab or the limit says there are more MRs."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=make_page(*nodes, has_next=has_next))

    # Act
    data = await call_tool({"limit": limit})

    # Assert
    assert len(data["merge_requests"]) == min(len(nodes), limit)
    assert data["has_more"] is expected_has_more


@respx.mock
async def test_scan_stops_after_max_pages(gitlab_env):
    """A filter that never matches stops after MAX_PAGES requests and reports has_more."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=make_page(make_node(1, approved=False), has_next=True),
    )

    # Act
    data = await call_tool({"approved": True})

    # Assert
    assert route.call_count == MAX_PAGES
    assert data == {"web_url_prefix": WEB_URL_PREFIX, "merge_requests": [], "has_more": True}


@respx.mock
async def test_missing_project_is_reported(gitlab_env):
    """GitLab returns project null for unknown or invisible projects."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"project": None}}))

    # Act / Assert
    with pytest.raises(ToolError, match="not found, or the token cannot see it"):
        await call_tool()


@respx.mock
async def test_graphql_errors_are_joined_into_one_tool_error(gitlab_env):
    """Every GraphQL error message reaches the agent."""
    # Arrange
    body = {"data": None, "errors": [{"message": "first problem"}, {"message": "second problem"}]}
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=body))

    # Act / Assert
    with pytest.raises(ToolError, match="GitLab GraphQL error: first problem; second problem"):
        await call_tool()


@respx.mock
async def test_http_error_gets_a_hint(gitlab_env):
    """An HTTP 401 becomes a ToolError with the token hint."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(401))

    # Act / Assert
    with pytest.raises(ToolError, match="invalid, expired, or revoked"):
        await call_tool()


@respx.mock
async def test_network_failure_names_the_instance(gitlab_env):
    """A connection error says which instance could not be reached."""
    # Arrange
    respx.post(GRAPHQL).mock(side_effect=httpx.ConnectError("boom"))

    # Act / Assert
    with pytest.raises(ToolError, match=r"Could not reach https://gitlab\.example\.com: boom"):
        await call_tool()


@pytest.mark.parametrize("limit", [0, 101])
async def test_limit_outside_range_is_rejected(gitlab_env, limit):
    """Limits below 1 or above 100 fail validation before any request is made."""
    # Act / Assert
    with pytest.raises(ToolError, match="limit"):
        await call_tool({"limit": limit})
