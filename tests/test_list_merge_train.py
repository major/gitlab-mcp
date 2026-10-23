"""Tests for the list_merge_train tool, run through a real in-memory MCP client 🧪."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import MAX_PAGES, mcp

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
WEB_URL_PREFIX = "https://gitlab.example.com/group/project/-/merge_requests/"
HEADER = "target_branch\tposition\tiid\ttitle\tauthor\tstatus\tpipeline_id\tpipeline_status"


def make_car(historical_index: int | None, iid: int = 42, **overrides: Any) -> dict[str, Any]:
    """Build one active merge train car shaped like the GitLab GraphQL response."""
    car = {
        "index": historical_index,
        "status": "IDLE",
        "mergeRequest": {"iid": str(iid), "title": f"MR {iid}", "author": {"username": "jdoe"}},
        "pipeline": {"id": "gid://gitlab/Ci::Pipeline/731", "status": "SUCCESS"},
    }
    return car | overrides


def make_train(
    target_branch: str = "main",
    *cars: dict[str, Any],
    cars_has_next: bool = False,
    cars_cursor: str = "car-1",
) -> dict[str, Any]:
    """Build one merge train and its current page of active cars."""
    return {
        "targetBranch": target_branch,
        "cars": {
            "nodes": list(cars),
            "pageInfo": {"hasNextPage": cars_has_next, "endCursor": cars_cursor},
        },
    }


def make_page(
    *trains: dict[str, Any],
    has_next: bool = False,
    cursor: str = "train-1",
) -> httpx.Response:
    """Wrap trains in the project.mergeTrains envelope of one GraphQL page."""
    body = {
        "data": {
            "project": {
                "mergeTrains": {
                    "nodes": list(trains),
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                },
            },
        },
    }
    return httpx.Response(200, json=body)


def sent_variables(route: respx.Route, index: int = -1) -> dict[str, Any]:
    """Return the GraphQL variables from one request the route received."""
    return json.loads(route.calls[index].request.content)["variables"]


async def call_tool_result(arguments: dict[str, Any] | None = None):
    """Call list_merge_train through the MCP protocol and return the raw result."""
    async with Client(mcp) as client:
        return await client.call_tool("list_merge_train", {"project": PROJECT} | (arguments or {}))


async def call_tool(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call list_merge_train through the MCP protocol and return its structured data."""
    return (await call_tool_result(arguments)).structured_content


def rows(data: dict[str, Any]) -> list[dict[str, str]]:
    """Parse the TSV `entries` string into one dict per row, keyed by header."""
    header, *lines = data["entries"].split("\n")
    return [dict(zip(header.split("\t"), line.split("\t"), strict=True)) for line in lines]


@respx.mock
async def test_maps_fields_sanitizes_text_and_sends_active_query(gitlab_env):
    """The tool returns compact active car rows with a one-based position and numeric pipeline ID."""
    # Arrange
    car = make_car(
        1111,
        7,
        status="FRESH",
        mergeRequest={
            "iid": "7",
            "title": "Fix\tlogin\nflow",
            "author": {"username": "alice"},
        },
        pipeline={"id": "gid://gitlab/Ci::Pipeline/991", "status": "FAILED"},
    )
    route = respx.post(GRAPHQL).mock(return_value=make_page(make_train("release", car)))

    # Act
    data = await call_tool({"limit": 4})

    # Assert
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "targetBranches": None,
        "after": None,
        "carAfter": None,
        "first": 4,
    }
    payload = json.loads(route.calls.last.request.content)
    query = payload["query"]
    assert "mergeTrains(status: ACTIVE" in query
    assert "cars(activityStatus: ACTIVE" in query
    assert "index" not in query
    assert "mergeRequest { iid title author { username } }" in query
    assert "pipeline { id status }" in query
    assert data == {
        "web_url_prefix": WEB_URL_PREFIX,
        "entries": f"{HEADER}\nrelease\t1\t7\tFix login flow\talice\tfresh\t991\tfailed",
        "has_more": False,
    }


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {},
            {
                "target_branch": "main",
                "position": "1",
                "iid": "42",
                "title": "MR 42",
                "author": "jdoe",
                "status": "idle",
                "pipeline_id": "731",
                "pipeline_status": "success",
            },
        ),
        (
            {"index": None, "mergeRequest": {"iid": "43", "title": "No links", "author": None}, "pipeline": None},
            {
                "target_branch": "main",
                "position": "1",
                "iid": "43",
                "title": "No links",
                "author": "",
                "status": "idle",
                "pipeline_id": "",
                "pipeline_status": "",
            },
        ),
    ],
)
@respx.mock
async def test_every_row_has_all_columns_and_null_fields_are_blank(gitlab_env, overrides, expected):
    """Missing author and pipeline details stay as empty TSV cells, without affecting position."""
    # Arrange
    respx.post(
        GRAPHQL,
    ).mock(return_value=make_page(make_train("main", make_car(**({"historical_index": 0} | overrides)))))

    # Act
    data = await call_tool()

    # Assert
    assert rows(data) == [expected]


@respx.mock
async def test_positions_follow_active_car_order_not_historical_index(gitlab_env):
    """Several active cars get consecutive positions even when historical indexes are null or large."""
    # Arrange
    cars = [make_car(None, 1), make_car(1111, 2)]
    respx.post(GRAPHQL).mock(return_value=make_page(make_train("main", *cars)))

    # Act
    data = await call_tool({"limit": 2})

    # Assert
    assert [row["position"] for row in rows(data)] == ["1", "2"]


@respx.mock
async def test_empty_train_connection_returns_header_only(gitlab_env):
    """An empty outer connection is complete even if its pageInfo says more may exist."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=make_page(has_next=True))

    # Act
    data = await call_tool()

    # Assert
    assert data == {"web_url_prefix": WEB_URL_PREFIX, "entries": HEADER, "has_more": False}


@respx.mock
async def test_target_branch_filter_is_sent_as_one_branch(gitlab_env):
    """A supplied target branch becomes the GraphQL targetBranches filter."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=make_page())

    # Act
    await call_tool({"target_branch": "release/1.0"})

    # Assert
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "targetBranches": ["release/1.0"],
        "after": None,
        "carAfter": None,
        "first": 20,
    }


@respx.mock
async def test_inner_cars_are_paginated_before_the_next_train(gitlab_env):
    """Car cursors stay on the current train, then the outer cursor advances and resets cars."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            make_page(
                make_train("main", make_car(1111, 1), cars_has_next=True, cars_cursor="car-next"),
                has_next=True,
                cursor="train-next",
            ),
            make_page(
                make_train("main", make_car(None, 2, status="STALE"), cars_cursor="car-last"),
                has_next=True,
                cursor="train-next",
            ),
            make_page(make_train("release", make_car(1111, 3)), has_next=False),
        ],
    )

    # Act
    data = await call_tool({"limit": 3})

    # Assert
    variables = [sent_variables(route, index) for index in range(route.call_count)]
    assert [(item["after"], item["carAfter"], item["first"]) for item in variables] == [
        (None, None, 3),
        (None, "car-next", 2),
        ("train-next", None, 1),
    ]
    assert [row["iid"] for row in rows(data)] == ["1", "2", "3"]
    assert [row["target_branch"] for row in rows(data)] == ["main", "main", "release"]
    assert [row["position"] for row in rows(data)] == ["1", "2", "1"]
    assert data["has_more"] is False


@pytest.mark.parametrize(
    ("cars_has_next", "trains_has_next", "expected"),
    [(False, False, False), (True, False, True), (False, True, True)],
)
@respx.mock
async def test_exact_limit_reports_more_from_either_connection(gitlab_env, cars_has_next, trains_has_next, expected):
    """Reaching the requested count reports whether either nested connection has more rows."""
    # Arrange
    train = make_train("main", make_car(0), cars_has_next=cars_has_next)
    respx.post(GRAPHQL).mock(return_value=make_page(train, has_next=trains_has_next))

    # Act
    data = await call_tool({"limit": 1})

    # Assert
    assert len(rows(data)) == 1
    assert data["has_more"] is expected


@respx.mock
async def test_scan_stops_after_max_pages(gitlab_env):
    """The tool makes at most MAX_PAGES calls and reports more when the scan cap is reached."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=lambda request: make_page(
            make_train("main"), has_next=True, cursor=f"train-{len(route.calls) + 1}"
        ),
    )

    # Act
    data = await call_tool()

    # Assert
    assert route.call_count == MAX_PAGES
    assert data == {"web_url_prefix": WEB_URL_PREFIX, "entries": HEADER, "has_more": True}


@respx.mock
async def test_missing_project_is_reported(gitlab_env):
    """GitLab returns project null for unknown or invisible projects."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"project": None}}))

    # Act / Assert
    with pytest.raises(ToolError, match="not found, or the token cannot see it"):
        await call_tool()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            httpx.Response(200, json={"data": None, "errors": [{"message": "Bad query"}]}),
            "GitLab GraphQL error: Bad query",
        ),
        (httpx.Response(401), "invalid, expired, or revoked"),
    ],
)
@respx.mock
async def test_graphql_and_http_errors_are_reported(gitlab_env, response, message):
    """GraphQL and HTTP errors use the shared client error handling."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=response)

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool()


@respx.mock
async def test_network_error_is_reported(gitlab_env):
    """A connection failure uses the shared client hint for the GitLab instance."""
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


async def test_tool_is_read_only(gitlab_env):
    """Tool metadata marks merge train listing as read-only."""
    # Arrange
    async with Client(mcp) as client:
        tools = await client.list_tools()

    # Assert
    tool = next(tool for tool in tools if tool.name == "list_merge_train")
    assert tool.annotations.read_only_hint is True
