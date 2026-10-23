"""Tests for resolving every open review thread on one merge request 🔓."""

import json
from typing import Any
lazy from collections.abc import Callable

import httpx
import pytest
import respx
from fastmcp import Client

from gitlab_mcp import mcp
from gitlab_mcp.common import MAX_PAGES

API = "https://gitlab.example.com/api/v4"
GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 2187
MR_PATH = f"/projects/group%2Fproject/merge_requests/{IID}"


def gid(thread_id: str) -> str:
    """Return the full GID that GitLab uses for a discussion."""
    return f"gid://gitlab/Discussion/{thread_id}"


def discussion(thread_id: str, *, resolvable: bool = True, resolved: bool = False) -> dict[str, Any]:
    """Build one discussion node with only the fields the ID query reads."""
    return {"id": gid(thread_id), "resolvable": resolvable, "resolved": resolved}


def page(nodes: list[dict[str, Any]], *, end_cursor: str | None = None) -> dict[str, Any]:
    """Build one GraphQL discussion page. A cursor means another page follows."""
    connection = {"pageInfo": {"hasNextPage": end_cursor is not None, "endCursor": end_cursor}, "nodes": nodes}
    return {"data": {"project": {"mergeRequest": {"discussions": connection}}}}


def count_response(resolvable: int, resolved: int) -> httpx.Response:
    """Return the GraphQL thread-count envelope GitLab sends for one merge request."""
    node = {"resolvableDiscussionsCount": resolvable, "resolvedDiscussionsCount": resolved}
    return httpx.Response(200, json={"data": {"project": {"mergeRequest": node}}})


def serve_graphql(
    pages: dict[str | None, dict[str, Any]],
    count: httpx.Response | Exception,
    calls: list[str],
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer discussion-ID pages by cursor and the count read, recording each call in order."""

    def serve(request: httpx.Request) -> httpx.Response:
        """Dispatch one GraphQL request to the page or count answer."""
        body = json.loads(request.content)
        if "discussions(" not in body["query"]:
            calls.append("count")
            if isinstance(count, Exception):
                raise count
            return count
        after = body["variables"].get("after")
        calls.append(f"list:{after}")
        return httpx.Response(200, json=pages[after])

    return serve


def serve_resolve(
    outcomes: dict[str, httpx.Response | Exception],
    calls: list[str],
) -> Callable[[httpx.Request], httpx.Response]:
    """Accept each resolve write, or fail it with the outcome given for that thread, recording the order."""

    def serve(request: httpx.Request) -> httpx.Response:
        """Record one resolve write and return its configured outcome."""
        thread_id = request.url.path.rsplit("/", 1)[-1]
        calls.append(f"resolve:{thread_id}")
        outcome = outcomes.get(thread_id, httpx.Response(200, json={"resolved": True}))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return serve


async def call_resolve() -> dict[str, Any]:
    """Call resolve_open_threads through the in-memory MCP client."""
    async with Client(mcp) as client:
        result = await client.call_tool("resolve_open_threads", {"project": PROJECT, "iid": IID})
    return result.structured_content


@respx.mock
async def test_resolves_only_open_resolvable_threads_across_two_pages(gitlab_env):
    """Resolved threads and plain comments are skipped, and pages are read in order before the writes."""
    # Arrange
    calls: list[str] = []
    pages = {
        None: page(
            [discussion("t1"), discussion("t2", resolved=True), discussion("t3", resolvable=False)], end_cursor="c1"
        ),
        "c1": page([discussion("t4")]),
    }
    respx.post(GRAPHQL).mock(side_effect=serve_graphql(pages, count_response(4, 4), calls))
    respx.route(method="PUT").mock(side_effect=serve_resolve({}, calls))

    # Act
    result = await call_resolve()

    # Assert
    assert calls == ["list:None", "list:c1", "resolve:t1", "resolve:t4", "count"]
    assert result == {"resolved": ["t1", "t4"], "open_threads": 0}


@respx.mock
async def test_zero_open_threads_makes_no_writes(gitlab_env):
    """With nothing open, the tool writes nothing and still reports the count read."""
    # Arrange
    calls: list[str] = []
    pages = {None: page([discussion("t1", resolved=True), discussion("t2", resolvable=False)])}
    respx.post(GRAPHQL).mock(side_effect=serve_graphql(pages, count_response(1, 1), calls))
    writes = respx.route(method="PUT").mock(side_effect=serve_resolve({}, calls))

    # Act
    result = await call_resolve()

    # Assert
    assert writes.call_count == 0
    assert calls == ["list:None", "count"]
    assert result == {"resolved": [], "open_threads": 0}


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (httpx.Response(403, json={"message": "Forbidden"}), "failed"),
        (httpx.Response(500, json={"message": "Server error"}), "unknown"),
        (httpx.ConnectError("connection reset"), "unknown"),
    ],
    ids=["api-403-failed", "server-500-unknown", "connection-error-unknown"],
)
@respx.mock
async def test_failed_thread_does_not_stop_later_threads(gitlab_env, failure, status):
    """A failed or uncertain resolve is reported as a problem, and the next open thread still resolves."""
    # Arrange
    calls: list[str] = []
    pages = {None: page([discussion("t1"), discussion("t2"), discussion("t3")])}
    # After t2 and t3 resolve, only the failed t1 is still open.
    respx.post(GRAPHQL).mock(side_effect=serve_graphql(pages, count_response(3, 2), calls))
    respx.route(method="PUT").mock(side_effect=serve_resolve({"t1": failure}, calls))

    # Act
    result = await call_resolve()

    # Assert
    assert calls == ["list:None", "resolve:t1", "resolve:t2", "resolve:t3", "count"]
    assert result["resolved"] == ["t2", "t3"]
    assert result["open_threads"] == 1
    [problem] = result["problems"]
    assert problem["iid"] == IID
    assert problem["thread_id"] == "t1"
    assert problem["status"] == status
    assert problem["resolved"] is None
    assert problem["warning"]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(403, json={"message": "Forbidden"}),
        httpx.ConnectError("connection reset"),
    ],
    ids=["api-error", "connection-error"],
)
@respx.mock
async def test_failed_count_read_omits_open_threads_without_failing_the_writes(gitlab_env, failure):
    """Resolved threads still count as success when the follow-up count cannot be read."""
    # Arrange
    calls: list[str] = []
    pages = {None: page([discussion("t1")])}
    respx.post(GRAPHQL).mock(side_effect=serve_graphql(pages, failure, calls))
    respx.route(method="PUT").mock(side_effect=serve_resolve({}, calls))

    # Act
    result = await call_resolve()

    # Assert
    assert calls == ["list:None", "resolve:t1", "count"]
    assert result == {"resolved": ["t1"]}


@pytest.mark.parametrize(
    ("total_pages", "expected_complete"),
    [(MAX_PAGES, True), (MAX_PAGES + 1, False)],
    ids=["exactly-the-page-limit", "one-page-over-the-limit"],
)
@respx.mock
async def test_page_limit_marks_result_incomplete_only_when_pages_remain(gitlab_env, total_pages, expected_complete):
    """Each scanned page holds one open thread. Pages past MAX_PAGES are never read."""
    # Arrange
    calls: list[str] = []
    cursors: list[str | None] = [None, *(f"c{number}" for number in range(1, total_pages))]
    pages = {
        cursor: page([discussion(f"t{number}")], end_cursor=f"c{number + 1}" if number < total_pages - 1 else None)
        for number, cursor in enumerate(cursors)
    }
    respx.post(GRAPHQL).mock(side_effect=serve_graphql(pages, count_response(total_pages, 0), calls))
    respx.route(method="PUT").mock(side_effect=serve_resolve({}, calls))

    # Act
    result = await call_resolve()

    # Assert
    scanned = min(total_pages, MAX_PAGES)
    expected_calls = (
        [f"list:{cursor}" for cursor in cursors[:scanned]]
        + [f"resolve:t{number}" for number in range(scanned)]
        + ["count"]
    )
    assert calls == expected_calls
    assert result["resolved"] == [f"t{number}" for number in range(scanned)]
    assert result.get("complete", True) is expected_complete


@respx.mock
async def test_tool_metadata_is_a_non_idempotent_write(gitlab_env):
    """The tool is marked as a non-idempotent, open-world write, and its docs ask for approval first."""
    # Arrange
    async with Client(mcp) as client:
        tools = await client.list_tools()

    # Act
    tool = next(tool for tool in tools if tool.name == "resolve_open_threads")

    # Assert
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is True
    assert "glab mr note resolve" in tool.description
    assert "approval" in tool.description.lower()
