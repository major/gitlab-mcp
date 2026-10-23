"""Tests for the list_merge_requests tool, run through a real in-memory MCP client 🧪."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError
lazy from mcp.types import CallToolResult, TextContent

from gitlab_mcp.common import MAX_PAGES
from gitlab_mcp.tools.mr_read import TSV_COLUMNS
from mcp_helpers import call_tool_result as call_mcp_tool_result

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
WEB_URL_PREFIX = "https://gitlab.example.com/group/project/-/merge_requests/"
FULL_PAGE = 100  # The largest page GitLab serves, used when filtering client-side.
HEADER = "iid\ttitle\tauthor\tstate\tdraft\tapproved\tapproved_by\topen_threads\tupdated"


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
    """Call list_merge_requests with the default project and return the raw result."""
    return await call_mcp_tool_result("list_merge_requests", {"project": PROJECT} | (arguments or {}))


async def call_tool(arguments: dict[str, Any] | None = None) -> dict:
    """Call list_merge_requests with the default project and return its data."""
    result = await call_tool_result(arguments)
    return result.structured_content


def rows(data: dict) -> list[dict[str, str]]:
    """Parse the TSV `merge_requests` string into one dict per row, keyed by header."""
    header, *lines = data["merge_requests"].split("\n")
    return [dict(zip(header.split("\t"), line.split("\t"), strict=True)) for line in lines]


def iids(data: dict) -> list[int]:
    """Return the IIDs of the merge requests in a tool result."""
    return [int(row["iid"]) for row in rows(data)]


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
        "search": None,
        "createdAfter": None,
        "updatedAfter": None,
        "targetBranches": None,
        "includeSourceBranch": False,
        "includeTargetBranch": False,
        "includeLabels": False,
        "after": None,
        "first": 5,
    }
    assert data == {
        "web_url_prefix": WEB_URL_PREFIX,
        "merge_requests": (f"{HEADER}\n7\tFix login\t\topened\ttrue\ttrue\treviewer\t3\t2026-10-20"),
        "has_more": False,
    }


@respx.mock
async def test_filters_and_optional_columns(gitlab_env):
    """Server-side filters are sent, and requested optional data is appended safely to TSV."""
    # Arrange
    node = make_node(
        12,
        sourceBranch="feature\tline\nbreak",
        labels={
            "nodes": [{"title": "Group::One"}, {"title": "path\tname"}],
            "pageInfo": {"hasNextPage": True},
        },
    )
    route = respx.post(GRAPHQL).mock(return_value=make_page(node))

    # Act
    data = await call_tool(
        {
            "created_after": "2026-10-01T12:30:00+02:00",
            "updated_after": "2026-10-02T00:00:00Z",
            "target_branch": "release/1",
            "include_source_branch": True,
            "include_labels": True,
            "limit": 1,
        },
    )

    # Assert
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "state": "opened",
        "authorUsername": None,
        "search": None,
        "createdAfter": "2026-10-01T10:30:00Z",
        "updatedAfter": "2026-10-02T00:00:00Z",
        "targetBranches": ["release/1"],
        "includeSourceBranch": True,
        "includeTargetBranch": False,
        "includeLabels": True,
        "after": None,
        "first": 1,
    }
    query = json.loads(route.calls.last.request.content)["query"]
    assert "createdAfter: $createdAfter" in query
    assert "updatedAfter: $updatedAfter" in query
    assert "targetBranches: $targetBranches" in query
    assert "sourceBranch @include(if: $includeSourceBranch)" in query
    assert "labels(first: 100) @include(if: $includeLabels)" in query
    row = rows(data)[0]
    assert list(row) == [*HEADER.split("\t"), "source_branch", "labels"]
    assert row["source_branch"] == "feature line break"
    assert row["labels"] == json.dumps(["Group::One", "path\tname"], ensure_ascii=False, separators=(",", ":"))
    assert data["labels_complete"] is False


@pytest.mark.parametrize(
    ("arguments", "expected_columns"),
    [
        ({}, {}),
        ({"include_target_branch": True}, {"target_branch": "release/1"}),
        ({"include_source_branch": True}, {"source_branch": "feature/x"}),
        (
            {"include_target_branch": True, "include_source_branch": True},
            {"source_branch": "feature/x", "target_branch": "release/1"},  # Source comes first, then target.
        ),
    ],
    ids=["default-omits-both", "target-only", "source-only", "source-then-target"],
)
@respx.mock
async def test_target_branch_column_is_optional_and_follows_source_branch(gitlab_env, arguments, expected_columns):
    """The target branch column is sent only when asked, and it appears right after the source branch column."""
    # Arrange
    node = make_node(5, sourceBranch="feature/x", targetBranch="release/1")
    route = respx.post(GRAPHQL).mock(return_value=make_page(node))

    # Act
    data = await call_tool(arguments)

    # Assert
    assert sent_variables(route)["includeTargetBranch"] is arguments.get("include_target_branch", False)
    query = json.loads(route.calls.last.request.content)["query"]
    assert "targetBranch @include(if: $includeTargetBranch)" in query
    row = rows(data)[0]
    assert list(row) == [*HEADER.split("\t"), *expected_columns]
    assert {column: row[column] for column in expected_columns} == expected_columns


@pytest.mark.parametrize(
    "timestamp",
    ["2026-10-01", "2026-10-01T12:30:00", "not-a-timestamp"],
    ids=["date-only", "timezone-missing", "invalid"],
)
@respx.mock
async def test_timestamps_must_be_timezone_aware_datetimes_before_network(gitlab_env, timestamp):
    """Date-only, timezone-naive, and invalid timestamp filters fail before any request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=make_page())

    # Act / Assert
    with pytest.raises(ToolError, match="timezone-aware ISO 8601 datetime"):
        await call_tool({"created_after": timestamp})
    assert route.call_count == 0


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
        "search": None,
        "createdAfter": None,
        "updatedAfter": None,
        "targetBranches": None,
        "includeSourceBranch": False,
        "includeTargetBranch": False,
        "includeLabels": False,
        "after": None,
        "first": 20,
    }


@pytest.mark.parametrize(
    ("overrides", "expected_row"),
    [
        (
            {},
            {
                "iid": "1",
                "title": "MR 1",
                "author": "jdoe",
                "state": "opened",
                "draft": "false",
                "approved": "false",
                "approved_by": "",
                "open_threads": "0",
                "updated": "2026-10-20",
            },
        ),
        (
            {"author": None, "draft": True, "resolvableDiscussionsCount": 4, "resolvedDiscussionsCount": 1},
            {
                "iid": "1",
                "title": "MR 1",
                "author": "",
                "state": "opened",
                "draft": "true",
                "approved": "false",
                "approved_by": "",
                "open_threads": "3",
                "updated": "2026-10-20",
            },
        ),
    ],
)
@respx.mock
async def test_every_row_has_every_column(gitlab_env, overrides, expected_row):
    """Each row has all TSV columns, and empty values show as empty cell, false, or 0."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=make_page(make_node(1, **overrides)))

    # Act
    data = await call_tool()

    # Assert
    row = rows(data)[0]
    assert list(row) == list(TSV_COLUMNS)
    assert row == expected_row


@respx.mock
async def test_text_content_matches_structured_content(gitlab_env):
    """The text the LLM reads is the same TSV as the structured output."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=make_page(make_node(1, author=None)))

    # Act
    result = await call_tool_result()

    # Assert
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert json.loads(block.text) == result.structured_content


@pytest.mark.parametrize(
    ("request_state", "node_state"),
    [("opened", "opened"), ("closed", "closed"), ("locked", "locked"), ("merged", "merged"), ("all", "merged")],
)
@respx.mock
async def test_state_column_is_filled_for_every_state(gitlab_env, request_state, node_state):
    """The state column is always filled, so callers never have to guess it."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=make_page(make_node(1, state=node_state)))

    # Act
    data = await call_tool({"state": request_state})

    # Assert
    assert rows(data)[0]["state"] == node_state


@respx.mock
async def test_title_tabs_and_line_breaks_become_single_spaces(gitlab_env):
    """A tab or line break in a title would break the TSV row, so each run becomes one space."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=make_page(make_node(1, title="Fix\tthe\n\r\tlogin")))

    # Act
    data = await call_tool()

    # Assert
    assert len(rows(data)) == 1
    assert rows(data)[0]["title"] == "Fix the login"


@respx.mock
async def test_multiple_approvers_are_comma_separated(gitlab_env):
    """Every approver appears in approved_by, separated by commas."""
    # Arrange
    approvers = {"nodes": [{"username": "alice"}, {"username": "bob"}]}
    respx.post(GRAPHQL).mock(return_value=make_page(make_node(1, approved=True, approvedBy=approvers)))

    # Act
    data = await call_tool()

    # Assert
    assert rows(data)[0]["approved_by"] == "alice,bob"


@respx.mock
async def test_no_merge_requests_returns_header_only(gitlab_env):
    """With no matches, the TSV is just the header line."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=make_page())

    # Act
    data = await call_tool()

    # Assert
    assert data["merge_requests"] == HEADER


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


@respx.mock
async def test_search_is_sent_across_pages_with_other_filters(gitlab_env):
    """Search is sent on every page alongside the existing server-side filters."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            make_page(make_node(1, approved=False), make_node(2, approved=True), has_next=True, cursor="c1"),
            make_page(make_node(3, approved=True), has_next=True, cursor="c2"),
        ],
    )

    # Act
    data = await call_tool(
        {"author": "jdoe", "approved": True, "limit": 2, "search": "release note", "state": "closed"},
    )

    # Assert
    variables = [sent_variables(route, index=index) for index in range(route.call_count)]
    assert variables == [
        {
            "fullPath": PROJECT,
            "state": "closed",
            "authorUsername": "jdoe",
            "search": "release note",
            "createdAfter": None,
            "updatedAfter": None,
            "targetBranches": None,
            "includeSourceBranch": False,
            "includeTargetBranch": False,
            "includeLabels": False,
            "after": None,
            "first": FULL_PAGE,
        },
        {
            "fullPath": PROJECT,
            "state": "closed",
            "authorUsername": "jdoe",
            "search": "release note",
            "createdAfter": None,
            "updatedAfter": None,
            "targetBranches": None,
            "includeSourceBranch": False,
            "includeTargetBranch": False,
            "includeLabels": False,
            "after": "c1",
            "first": FULL_PAGE,
        },
    ]
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
    assert len(rows(data)) == min(len(nodes), limit)
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
    assert data == {"web_url_prefix": WEB_URL_PREFIX, "merge_requests": HEADER, "has_more": True}


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
