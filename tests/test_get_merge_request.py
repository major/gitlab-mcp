"""Tests for the get_merge_request tool, run through a real in-memory MCP client 🧪."""

import hashlib
import json
import re
from types import SimpleNamespace
from typing import Any
lazy from collections.abc import Callable

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp import mcp
from gitlab_mcp.common import MAX_PAGES
from gitlab_mcp.tools import mr_read
from gitlab_mcp.tools.mr_read import DESCRIPTION_CAP, DESCRIPTION_PAGE_MAX, ISSUE_KEY_SCAN_CAP, THREAD_INDEX_CAP
from mcp_helpers import call_tool as call_mcp_tool

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 2187
AUTHOR_ID = 314
PIPELINE_ID = 123
BLOCKER_IIDS = [2100, 2101]
HIDDEN_BLOCKERS = 3
PIPELINE_JOBS = "https://gitlab.example.com/api/v4/projects/group%2Fproject/pipelines/123/jobs"
MR_REST = "https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/2187"
HEAD_SHA = "6096f2d5a1b2c3d4e5f60718293a4b5c6d7e8f90"  # Full SHA. Write tools need it exact, so output keeps all of it.
POSITION = {"oldPath": "docs/sop.md", "newPath": "docs/sop.md", "oldLine": None, "newLine": 212}
RENAMED_POSITION = {"oldPath": "docs/old.md", "newPath": "docs/new.md", "oldLine": None, "newLine": 3}
DELETED_LINE_POSITION = {"oldPath": "docs/sop.md", "newPath": "docs/sop.md", "oldLine": 40, "newLine": None}


def make_mr(**overrides: Any) -> dict[str, Any]:
    """Build one merge request node shaped like the GraphQL response."""
    node = {
        "iid": str(IID),  # GraphQL returns IIDs as strings.
        "title": "Rebuild support",
        "description": "Adds the rebuild flag.",
        "state": "opened",
        "draft": False,
        "sourceBranch": "feat/rebuild",
        "targetBranch": "main",
        "author": {"id": f"gid://gitlab/User/{AUTHOR_ID}", "username": "bkearney"},
        "diffRefs": {"headSha": HEAD_SHA},
        "detailedMergeStatus": "MERGEABLE",
        "shouldBeRebased": False,
        "conflicts": False,
        "approved": False,
        "approvedBy": {"nodes": []},
        "reviewers": {"nodes": []},
        "headPipeline": {"status": "SUCCESS"},
        "autoMergeStrategy": None,
        "diffStatsSummary": {"additions": 483, "deletions": 15, "fileCount": 7},
        "resolvableDiscussionsCount": 0,
        "resolvedDiscussionsCount": 0,
        "userDiscussionsCount": 0,
        "discussions": make_discussions(),
    }
    return node | overrides


def make_discussions(*nodes: dict[str, Any], has_next: bool = False) -> dict[str, Any]:
    """Wrap discussion nodes in the `discussions` connection of one page."""
    return {"pageInfo": {"hasNextPage": has_next}, "nodes": list(nodes)}


def make_note(
    *,
    system: bool = False,
    author: str | None = "mhayden",
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the first note of a discussion. A null position means a general thread."""
    return {"system": system, "author": {"username": author} if author else None, "position": position}


def make_discussion(
    thread_id: str,
    *,
    note: dict[str, Any] | None = None,
    resolvable: bool = True,
    resolved: bool = False,
) -> dict[str, Any]:
    """Build one discussion node with one note. The default note is a human comment on no line."""
    return {
        "id": f"gid://gitlab/Discussion/{thread_id}",
        "resolvable": resolvable,
        "resolved": resolved,
        "notes": {"count": 1, "nodes": [note or make_note()]},
    }


def graphql_response(node: dict[str, Any] | None) -> httpx.Response:
    """Wrap one merge request node in the project.mergeRequest envelope."""
    return httpx.Response(200, json={"data": {"project": {"mergeRequest": node}}})


def sent_variables(route: respx.Route) -> dict[str, Any]:
    """Return the GraphQL variables from the last request the route received."""
    return json.loads(route.calls.last.request.content)["variables"]


async def call_tool(*, client: Client | None = None, **extra: Any) -> dict:
    """Call get_merge_request with the default project and IID and return its data."""
    return await call_mcp_tool("get_merge_request", {"project": PROJECT, "iid": IID, **extra}, client=client)


@respx.mock
async def test_maps_fields_and_sends_variables(gitlab_env):
    """Every field maps to its output name, and the IID is sent as a string."""
    # Arrange
    node = make_mr(
        draft=True,
        conflicts=True,
        shouldBeRebased=True,
        approved=True,
        approvedBy={"nodes": [{"username": "mhayden"}]},
        reviewers={"nodes": [{"username": "mhayden", "mergeRequestInteraction": {"reviewState": "REVIEWED"}}]},
        headPipeline={"status": "FAILED"},
        detailedMergeStatus="DISCUSSIONS_NOT_RESOLVED",
        autoMergeStrategy="merge_train",
        resolvableDiscussionsCount=4,
        resolvedDiscussionsCount=2,
        discussions=make_discussions(make_discussion("open-1", note=make_note(position=POSITION))),
    )
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool(include_description=True)

    # Assert
    assert route.calls.last.request.headers["PRIVATE-TOKEN"] == "glpat-test"
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "iid": str(IID),
        "includeLabels": False,
        "includeThreads": True,
        "includeCommits": False,
    }
    assert data == {
        "iid": IID,
        "title": "Rebuild support",
        "state": "opened",
        "draft": True,
        "author": "bkearney",
        "author_id": AUTHOR_ID,
        "source_branch": "feat/rebuild",
        "target_branch": "main",
        "head_sha": HEAD_SHA,
        "description": "Adds the rebuild flag.",
        "merge_status": "discussions_not_resolved",
        "should_be_rebased": True,
        "conflicts": True,
        "approved": True,
        "approved_by": ["mhayden"],
        "reviewers": [{"username": "mhayden", "review_state": "reviewed"}],
        "pipeline_status": "failed",
        "auto_merge_strategy": "merge_train",
        "changes": {"files": 7, "additions": 483, "deletions": 15},
        "threads": {
            "open_count": 2,  # 4 resolvable minus 2 resolved, from the count fields.
            "resolved_count": 2,
            "open": [
                {"id": "open-1", "by": "mhayden", "notes": 1, "path": "docs/sop.md", "line": 212},
            ],
            "index_complete": True,
        },
    }


@respx.mock
async def test_default_raw_result_remains_unwrapped(gitlab_env):
    """The first page keeps the existing top-level detail fields in raw MCP structured content."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act
    async with Client(mcp) as client:
        result = await client.call_tool("get_merge_request", {"project": PROJECT, "iid": IID})

    # Assert
    assert route.call_count == 1
    assert sent_variables(route)["includeCommits"] is False
    assert result.structured_content == {
        "iid": IID,
        "title": "Rebuild support",
        "state": "opened",
        "author": "bkearney",
        "author_id": AUTHOR_ID,
        "source_branch": "feat/rebuild",
        "target_branch": "main",
        "head_sha": HEAD_SHA,
        "merge_status": "mergeable",
        "approved": False,
        "pipeline_status": "success",
        "changes": {"files": 7, "additions": 483, "deletions": 15},
        "threads": {"open_count": 0, "resolved_count": 0, "index_complete": True},
    }


@pytest.mark.parametrize(
    ("pipeline", "diff_refs", "counts", "approved", "expected"),
    [
        (
            {"id": "gid://gitlab/Ci::Pipeline/123", "status": "SUCCESS", "sha": "pipe"},
            {"headSha": HEAD_SHA},
            (5, 2),
            True,
            (HEAD_SHA, "success", 123, "pipe", True, 3),
        ),
        (None, None, (None, None), False, ("", "", None, "", False, 0)),
        ({"id": None, "status": None, "sha": None}, None, (1, 0), False, ("", "", None, "", False, 1)),
    ],
)
@respx.mock
async def test_status_view_is_minimal_and_ignores_detail_options(  # noqa: PLR0913, PLR0917
    gitlab_env, pipeline, diff_refs, counts, approved, expected
):
    """Status returns all seven keys with one minimal request, even with conflicting detail options."""
    # Arrange
    node = make_mr(
        headPipeline=pipeline,
        diffRefs=diff_refs,
        approved=approved,
        detailedMergeStatus="BLOCKED_STATUS",
        resolvableDiscussionsCount=counts[0],
        resolvedDiscussionsCount=counts[1],
    )
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_merge_request",
            {
                "project": PROJECT,
                "iid": IID,
                "view": "status",
                "include_jobs": True,
                "include_description": False,
                "description_offset": 1,
                "description_limit": 0,
                "include_description_hash": True,
                "include_labels": True,
                "include_threads": True,
                "include_commits": True,
                "include_commits_behind": True,
                "section": "missing",
                "headings_only": True,
                "find": ["x"],
                "find_context": 2,
                "issue_key_pattern": "[",
                "issue_key_prefixes": ["INVALID"],
            },
        )

    # Assert
    head, status, pipeline_id, sha, is_approved, open_count = expected
    assert result.structured_content == {
        "head_sha": head,
        "pipeline_status": status,
        "latest_pipeline_id": pipeline_id,
        "pipeline_sha": sha,
        "merge_status": "blocked_status",
        "approved": is_approved,
        "open_thread_count": open_count,
    }
    assert json.loads(result.content[0].text) == result.structured_content
    assert route.call_count == 1
    assert len(respx.calls) == 1
    assert sent_variables(route) == {"fullPath": PROJECT, "iid": str(IID)}
    query = json.loads(route.calls.last.request.content)["query"]
    for field in (
        "diffRefs",
        "headSha",
        "headPipeline",
        "status",
        "id",
        "sha",
        "detailedMergeStatus",
        "approved",
        "resolvableDiscussionsCount",
        "resolvedDiscussionsCount",
    ):
        assert re.search(rf"\b{field}\b", query)
    for field in ("title", "description", "discussions", "approvedBy", "reviewers", "commits", "mergeRequestEventType"):
        assert not re.search(rf"\b{field}\b", query)


@respx.mock
async def test_status_view_rejects_malformed_pipeline_id(gitlab_env):
    """Malformed pipeline IDs use the same error as the full view."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=graphql_response(make_mr(headPipeline={"id": "bad", "status": "SUCCESS"}))
    )

    # Act and assert
    with pytest.raises(ToolError, match="unexpected format"):
        await call_tool(view="status")
    assert route.call_count == 1
    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    ("count", "has_next", "truncated"),
    [(0, False, False), (2, False, False), (50, False, False), (50, True, True), (51, False, True)],
    ids=["empty", "normal", "exact-cap", "next-page", "oversized-response"],
)
@respx.mock
async def test_optional_commits_show_newest_page_in_oldest_first_order(gitlab_env, count, has_next, truncated):
    """Return only short SHAs and subjects, reversing the newest page without extra requests."""
    # Arrange
    nodes = [{"sha": f"{i:09x}" + "0" * 31, "title": f"Subject {i}", "message": "secret body"} for i in range(count)]
    connection = {"nodes": nodes, "pageInfo": {"hasNextPage": has_next}}
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(commits=connection)))

    # Act
    data = await call_tool(include_commits=True)

    # Assert
    request = json.loads(route.calls.last.request.content)
    assert route.call_count == 1
    assert request["variables"]["includeCommits"] is True
    assert "commits(first: 50) @include(if: $includeCommits)" in request["query"]
    assert data["commits"] == [f"{node['sha'][:9]} {node['title']}" for node in reversed(nodes[:50])]
    assert data.get("commits_truncated", False) is truncated
    assert ("commits_truncated" in data) is truncated


@pytest.mark.parametrize(
    ("arguments", "include_threads", "expects_threads"),
    [
        ({}, True, True),
        ({"include_threads": False}, False, False),
    ],
    ids=["default-includes-threads", "opt-out-skips-threads"],
)
@respx.mock
async def test_include_threads_controls_graphql_fetch_and_output(
    gitlab_env, arguments, include_threads, expects_threads
):
    """Thread output is the default, and opting out skips its GraphQL data and conversion."""
    # Arrange
    node = make_mr(
        resolvableDiscussionsCount=1,
        discussions=make_discussions(make_discussion("open-1", note=make_note(position=POSITION))),
    )
    if not include_threads:
        del node["discussions"]
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool(**arguments)

    # Assert
    request = json.loads(route.calls.last.request.content)
    assert request["variables"]["includeThreads"] is include_threads
    assert "discussions(first: 100) @include(if: $includeThreads)" in request["query"]
    assert ("threads" in data) is expects_threads
    assert ("open_thread_count" in data) is not expects_threads
    assert ("resolved_thread_count" in data) is not expects_threads
    if expects_threads:
        assert data["threads"]["open"] == [
            {"id": "open-1", "by": "mhayden", "notes": 1, "path": "docs/sop.md", "line": 212},
        ]


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((0, 0, 0), (0, 0, None)),
        ((6, 2, 9), (4, 2, 3)),
        ((0, 0, 1), (0, 0, 1)),
        ((None, None, None), (0, 0, None)),
    ],
    ids=["zero-counts", "nonzero-counts", "comment-only", "null-counts"],
)
@respx.mock
async def test_thread_counts_are_returned_when_thread_index_is_skipped(gitlab_env, counts, expected):
    """Thread counts, including comment-only threads, remain available without fetching discussion details."""
    # Arrange
    resolvable, resolved, user = counts
    node = make_mr(resolvableDiscussionsCount=resolvable, resolvedDiscussionsCount=resolved, userDiscussionsCount=user)
    del node["discussions"]
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool(include_threads=False)

    # Assert
    request = json.loads(route.calls.last.request.content)
    assert route.call_count == 1
    assert request["variables"]["includeThreads"] is False
    assert "discussions(first: 100) @include(if: $includeThreads)" in request["query"]
    assert (data["open_thread_count"], data["resolved_thread_count"], data.get("comment_thread_count")) == expected
    assert "threads" not in data


def blockers_response(visible: list[int], hidden: int = 0) -> httpx.Response:
    """Wrap visible blocker IIDs in the aliased reply of the blockers query for one merge request."""
    node = {
        "blockingMergeRequests": {"visibleMergeRequests": [{"iid": str(iid)} for iid in visible], "hiddenCount": hidden}
    }
    return httpx.Response(200, json={"data": {"project": {"mr_0": node}}})


def serve_mr_and_blockers(node: dict[str, Any], blockers: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the blockers query with `blockers`, and every other GraphQL query with the merge request node."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Route by query text, the way GitLab would answer the two different queries."""
        if "blockingMergeRequests" in json.loads(request.content)["query"]:
            return blockers
        return graphql_response(node)

    return handler


@respx.mock
async def test_blocked_merge_request_lists_visible_and_hidden_blockers(gitlab_env):
    """A blocked merge request gets its blocker IIDs from one extra query, plus the count of hidden ones."""
    # Arrange
    node = make_mr(detailedMergeStatus="BLOCKED_STATUS")
    route = respx.post(GRAPHQL).mock(
        side_effect=serve_mr_and_blockers(node, blockers_response(BLOCKER_IIDS, HIDDEN_BLOCKERS))
    )

    # Act
    data = await call_tool()

    # Assert
    queries = [json.loads(call.request.content)["query"] for call in route.calls]
    assert ["blockingMergeRequests" in query for query in queries] == [False, True]
    assert sent_variables(route) == {"fullPath": PROJECT, "iid0": str(IID)}
    assert data["merge_status"] == "blocked_status"
    assert data["blocked_by"] == BLOCKER_IIDS
    assert data["hidden_blockers"] == HIDDEN_BLOCKERS


@pytest.mark.parametrize("merge_status", ["MERGEABLE", "DISCUSSIONS_NOT_RESOLVED"])
@respx.mock
async def test_unblocked_merge_request_sends_no_blockers_query(gitlab_env, merge_status):
    """Only blocked merge requests look up blockers, so other reads keep their single query."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(detailedMergeStatus=merge_status)))

    # Act
    data = await call_tool()

    # Assert
    assert route.call_count == 1
    assert "blockingMergeRequests" not in json.loads(route.calls.last.request.content)["query"]
    assert "blocked_by" not in data
    assert "hidden_blockers" not in data


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {"errors": [{"message": "Field 'blockingMergeRequests' doesn't exist on type 'MergeRequest'"}]}),
        (500, {"message": "Boom"}),
    ],
    ids=["field-missing-on-this-edition", "http-500"],
)
@respx.mock
async def test_failed_blockers_query_keeps_the_merge_request_read(gitlab_env, status, body):
    """Blocker data is extra, so a failed lookup leaves the blocker fields out and the read still succeeds."""
    # Arrange
    node = make_mr(detailedMergeStatus="BLOCKED_STATUS")
    respx.post(GRAPHQL).mock(side_effect=serve_mr_and_blockers(node, httpx.Response(status, json=body)))

    # Act
    data = await call_tool()

    # Assert
    assert data["iid"] == IID
    assert data["merge_status"] == "blocked_status"
    assert "blocked_by" not in data
    assert "hidden_blockers" not in data


@respx.mock
async def test_commits_behind_is_not_read_unless_asked(gitlab_env):
    """Without the flag, the divergence count is never requested, so the extra REST call is saved."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))
    rest = respx.get(MR_REST).mock(return_value=httpx.Response(200, json={"diverged_commits_count": 4}))

    # Act
    data = await call_tool()

    # Assert
    assert rest.call_count == 0
    assert "commits_behind" not in data


@pytest.mark.parametrize("count", [0, 7, None], ids=["zero", "positive", "null"])
@respx.mock
async def test_include_commits_behind_reads_the_divergence_count(gitlab_env, count):
    """The flag asks REST for the count. Zero is a real answer, and a null count leaves the field out."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))
    rest = respx.get(MR_REST).mock(return_value=httpx.Response(200, json={"diverged_commits_count": count}))

    # Act
    data = await call_tool(include_commits_behind=True)

    # Assert
    assert rest.calls.last.request.url.params["include_diverged_commits_count"] == "true"
    assert data.get("commits_behind") == count
    assert ("commits_behind" in data) is (count is not None)


@pytest.mark.parametrize("status", [403, 500], ids=["forbidden", "server-error"])
@respx.mock
async def test_failed_commits_behind_read_keeps_the_merge_request(gitlab_env, status):
    """A refused divergence read leaves the field out, and the merge request read still succeeds."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))
    respx.get(MR_REST).mock(return_value=httpx.Response(status, json={"message": "Boom"}))

    # Act
    data = await call_tool(include_commits_behind=True)

    # Assert
    assert data["iid"] == IID
    assert "commits_behind" not in data


@pytest.mark.parametrize("state", ["merged", "closed"])
@respx.mock
async def test_finished_merge_request_skips_the_divergence_read(gitlab_env, state):
    """Only open MRs get a count, because a merged or closed source branch no longer moves."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(state=state)))
    rest = respx.get(MR_REST).mock(return_value=httpx.Response(200, json={"diverged_commits_count": 4}))

    # Act
    data = await call_tool(include_commits_behind=True)

    # Assert
    assert rest.call_count == 0
    assert "commits_behind" not in data


@respx.mock
async def test_merge_request_includes_creation_date(gitlab_env):
    """The detail query returns the creation date without the timestamp."""
    # Arrange
    node = make_mr(createdAt="2025-06-17T12:34:56Z")
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool()

    # Assert
    query = json.loads(route.calls.last.request.content)["query"]
    assert "createdAt" in query
    assert data["created_at"] == "2025-06-17"


@pytest.mark.parametrize("overrides", [{}, {"createdAt": None}], ids=["missing", "null"])
@respx.mock
async def test_merge_request_omits_missing_creation_date(gitlab_env, overrides):
    """A missing or null creation timestamp leaves the detail field out."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(**overrides)))

    # Act
    data = await call_tool()

    # Assert
    assert "created_at" not in data


@respx.mock
async def test_compact_batch_does_not_request_or_return_creation_date(gitlab_env):
    """Compact batch reads do not add a creation date, even if a mock node contains one."""
    # Arrange
    node = make_mr(createdAt="2025-06-17T12:34:56Z")
    route = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(200, json={"data": {"project": {"mr_0": node}}}),
    )

    # Act
    async with Client(mcp) as client:
        result = await client.call_tool("get_merge_requests", {"project": PROJECT, "iids": [IID]})

    # Assert
    query = json.loads(route.calls.last.request.content)["query"]
    assert "createdAt" not in query
    assert "created_at" not in result.structured_content["merge_requests"][0]


@respx.mock
async def test_merged_merge_request_includes_date_and_username(gitlab_env):
    """A merged merge request includes its date and merging user's username."""
    # Arrange
    node = make_mr(
        state="merged",
        mergedAt="2025-06-17T12:34:56Z",
        mergeUser={"username": "mhayden"},
    )
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool()

    # Assert
    query = json.loads(route.calls.last.request.content)["query"]
    assert "mergedAt" in query
    assert "mergeUser { username }" in query
    assert data["merged_at"] == "2025-06-17"
    assert data["merged_by"] == "mhayden"


@respx.mock
async def test_batch_merged_metadata_omits_missing_and_open_values(gitlab_env):
    """Batch results include provided merged metadata and omit null or non-merged values."""
    # Arrange
    merged_with_user = make_mr(
        state="merged",
        mergedAt="2025-06-17T12:34:56Z",
        mergeUser={"username": "mhayden"},
    )
    merged_without_user = make_mr(
        state="merged",
        mergedAt="2025-06-18T00:00:00Z",
        mergeUser=None,
    )
    opened = make_mr()
    route = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "project": {
                        "mr_0": merged_with_user,
                        "mr_1": merged_without_user,
                        "mr_2": opened,
                    },
                },
            },
        ),
    )

    # Act
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_merge_requests",
            {"project": PROJECT, "iids": [IID, IID + 1, IID + 2]},
        )
    merge_requests = result.structured_content["merge_requests"]

    # Assert
    query = json.loads(route.calls.last.request.content)["query"]
    assert "mergedAt" in query
    assert "mergeUser { username }" in query
    assert merge_requests[0]["merged_at"] == "2025-06-17"
    assert merge_requests[0]["merged_by"] == "mhayden"
    assert merge_requests[1]["merged_at"] == "2025-06-18"
    assert "merged_by" not in merge_requests[1]
    assert {"merged_at", "merged_by"}.isdisjoint(merge_requests[2])


@respx.mock
async def test_include_jobs_returns_failed_jobs_with_numeric_ids(gitlab_env):
    """Explicit job lookup returns failed job IDs and names, even for a successful pipeline."""
    # Arrange
    node = make_mr(headPipeline={"status": "SUCCESS", "id": "gid://gitlab/Ci::Pipeline/123", "sha": HEAD_SHA})
    graphql = respx.post(GRAPHQL).mock(return_value=graphql_response(node))
    jobs = respx.get(PIPELINE_JOBS).mock(
        return_value=httpx.Response(200, json=[{"id": 456, "name": "unit tests"}]),
    )

    # Act
    data = await call_tool(include_jobs=True)

    # Assert
    assert dict(jobs.calls.last.request.url.params) == {"scope[]": "failed", "per_page": "100", "page": "1"}
    assert (
        "headPipeline { status id sha mergeRequestEventType }"
        in json.loads(graphql.calls.last.request.content)["query"]
    )
    assert data["latest_pipeline_id"] == PIPELINE_ID
    assert data["pipeline_sha"] == HEAD_SHA
    assert data["failed_jobs"] == "id\tname\n456\tunit tests"
    assert "jobs_complete" not in data


@respx.mock
async def test_default_does_not_request_pipeline_jobs(gitlab_env):
    """The default lookup keeps the existing response and makes no REST jobs request."""
    # Arrange
    node = make_mr(headPipeline={"status": "SUCCESS", "id": "gid://gitlab/Ci::Pipeline/123"})
    graphql = respx.post(GRAPHQL).mock(return_value=graphql_response(node))
    jobs = respx.get(PIPELINE_JOBS).mock(return_value=httpx.Response(200, json=[]))

    # Act
    data = await call_tool()

    # Assert
    assert (
        "headPipeline { status id sha mergeRequestEventType }"
        in json.loads(graphql.calls.last.request.content)["query"]
    )
    assert jobs.call_count == 0
    assert data["latest_pipeline_id"] == PIPELINE_ID
    assert "failed_jobs" not in data
    assert "jobs_complete" not in data


@pytest.mark.parametrize("include_threads", [True, False])
@pytest.mark.parametrize(
    ("pipeline", "expected_sha"),
    [
        ({"status": "RUNNING", "sha": HEAD_SHA}, HEAD_SHA),
        ({"status": "RUNNING", "sha": "a" * 40}, "a" * 40),
        (None, None),
        ({"status": "RUNNING"}, None),
        ({"status": "RUNNING", "sha": None}, None),
        ({"status": "RUNNING", "sha": ""}, None),
    ],
    ids=["matching", "different", "no-pipeline", "missing-sha", "null-sha", "empty-sha"],
)
@respx.mock
async def test_pipeline_sha_comes_from_existing_query_without_extra_requests(
    gitlab_env, include_threads, pipeline, expected_sha
):
    """Return the head pipeline SHA from the existing query and make no extra requests."""
    # Arrange
    graphql = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(headPipeline=pipeline)))

    # Act
    data = await call_tool(include_threads=include_threads, include_jobs=False)

    # Assert
    assert data["head_sha"] == HEAD_SHA
    if expected_sha is None:
        assert "pipeline_sha" not in data
    else:
        assert data["pipeline_sha"] == expected_sha
    assert (
        "headPipeline { status id sha mergeRequestEventType }"
        in json.loads(graphql.calls.last.request.content)["query"]
    )
    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    ("pipeline", "expected_event_type"),
    [
        ({"status": "SUCCESS", "sha": "a" * 40, "mergeRequestEventType": "MERGED_RESULT"}, "merged_result"),
        ({"status": "SUCCESS", "sha": "a" * 40, "mergeRequestEventType": "MERGE_TRAIN"}, "merge_train"),
        ({"status": "SUCCESS", "sha": HEAD_SHA, "mergeRequestEventType": "DETACHED"}, "detached"),
        ({"status": "SUCCESS", "sha": "a" * 40, "mergeRequestEventType": None}, None),
        (None, None),
    ],
    ids=["merged-result", "merge-train", "detached", "stale-branch-pipeline", "no-pipeline"],
)
@respx.mock
async def test_pipeline_event_type_explains_sha_mismatch(gitlab_env, pipeline, expected_event_type):
    """Say which kind of merge request pipeline ran, so a different SHA is not mistaken for a stale run."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(headPipeline=pipeline)))

    # Act
    data = await call_tool(include_jobs=False)

    # Assert
    assert data.get("pipeline_event_type") == expected_event_type
    assert len(respx.calls) == 1


@respx.mock
async def test_include_jobs_without_pipeline_makes_no_rest_request(gitlab_env):
    """An explicitly requested job lookup skips REST when the merge request has no pipeline."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(headPipeline=None)))
    jobs = respx.get(PIPELINE_JOBS).mock(return_value=httpx.Response(200, json=[]))

    # Act
    data = await call_tool(include_jobs=True)

    # Assert
    assert jobs.call_count == 0
    assert "pipeline_status" not in data
    assert "pipeline_sha" not in data
    assert "latest_pipeline_id" not in data
    assert "failed_jobs" not in data
    assert "jobs_complete" not in data


@respx.mock
async def test_include_jobs_omits_empty_failed_job_list(gitlab_env):
    """An empty failed-job result is omitted and remains complete."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=graphql_response(
            make_mr(headPipeline={"status": "SUCCESS", "id": "gid://gitlab/Ci::Pipeline/123"})
        ),
    )
    jobs = respx.get(PIPELINE_JOBS).mock(return_value=httpx.Response(200, json=[]))

    # Act
    data = await call_tool(include_jobs=True)

    # Assert
    assert jobs.call_count == 1
    assert "failed_jobs" not in data
    assert "jobs_complete" not in data


@respx.mock
async def test_include_jobs_reads_multiple_pages(gitlab_env):
    """Failed jobs are returned in page order after all pages are read."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=graphql_response(
            make_mr(headPipeline={"status": "FAILED", "id": "gid://gitlab/Ci::Pipeline/123"})
        ),
    )

    def serve_page(request: httpx.Request) -> httpx.Response:
        """Return the failed job row for the requested page and link to the next page."""
        page = int(request.url.params["page"])
        next_page = "2" if page == 1 else ""
        return httpx.Response(
            200,
            json=[{"id": page, "name": f"failed-{page}"}],
            headers={"X-Next-Page": next_page},
        )

    jobs = respx.get(PIPELINE_JOBS).mock(side_effect=serve_page)

    # Act
    data = await call_tool(include_jobs=True)

    # Assert
    assert [call.request.url.params["page"] for call in jobs.calls] == ["1", "2"]
    assert data["failed_jobs"] == "id\tname\n1\tfailed-1\n2\tfailed-2"
    assert "jobs_complete" not in data


@respx.mock
async def test_include_jobs_reports_incomplete_at_page_cap(gitlab_env):
    """The scan stops at MAX_PAGES and marks the returned failed-job list incomplete."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=graphql_response(
            make_mr(headPipeline={"status": "FAILED", "id": "gid://gitlab/Ci::Pipeline/123"})
        ),
    )

    def serve_page(request: httpx.Request) -> httpx.Response:
        """Keep returning a next-page marker so the bounded scan reaches its cap."""
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[{"id": page, "name": f"failed-{page}"}],
            headers={"X-Next-Page": str(page + 1)},
        )

    jobs = respx.get(PIPELINE_JOBS).mock(side_effect=serve_page)

    # Act
    data = await call_tool(include_jobs=True)

    # Assert
    assert jobs.call_count == MAX_PAGES
    assert data["jobs_complete"] is False
    assert data["failed_jobs"].splitlines()[0] == "id\tname"


@pytest.mark.parametrize(
    "pipeline_id",
    [None, "123", "gid://gitlab/Ci::Pipeline/not-a-number", "gid://gitlab/Other/123"],
    ids=["missing", "not-a-gid", "not-numeric", "wrong-model"],
)
@respx.mock
async def test_include_jobs_rejects_malformed_pipeline_id(gitlab_env, pipeline_id):
    """A malformed pipeline GID raises an error instead of hiding failed jobs."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=graphql_response(make_mr(headPipeline={"status": "FAILED", "id": pipeline_id})),
    )
    jobs = respx.get(PIPELINE_JOBS).mock(return_value=httpx.Response(200, json=[]))

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(include_jobs=True)
    assert jobs.call_count == 0


@pytest.mark.parametrize(
    ("overrides", "absent_keys"),
    [
        ({"draft": False}, {"draft"}),
        ({"author": None}, {"author"}),
        ({"description": None}, {"description", "description_truncated"}),
        ({"description": ""}, {"description", "description_truncated"}),
        ({"diffRefs": None}, {"head_sha"}),
        ({"detailedMergeStatus": None}, {"merge_status"}),
        ({"shouldBeRebased": False, "conflicts": False}, {"should_be_rebased", "conflicts"}),
        ({"approvedBy": {"nodes": []}, "reviewers": {"nodes": []}}, {"approved_by", "reviewers"}),
        ({"headPipeline": None}, {"pipeline_status"}),
        ({"autoMergeStrategy": None}, {"auto_merge_strategy"}),
        ({"diffStatsSummary": None}, {"changes"}),
    ],
)
@respx.mock
async def test_empty_fields_are_omitted(gitlab_env, overrides, absent_keys):
    """Empty and false values are left out, but `approved` is always present."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(**overrides)))

    # Act
    data = await call_tool(include_description=True)

    # Assert
    assert absent_keys.isdisjoint(data)
    assert data["approved"] is False
    assert "threads" in data


@respx.mock
async def test_reviewer_without_interaction_has_no_review_state(gitlab_env):
    """A reviewer with no interaction record is listed by username only."""
    # Arrange
    node = make_mr(reviewers={"nodes": [{"username": "mhayden", "mergeRequestInteraction": None}]})
    respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool()

    # Assert
    assert data["reviewers"] == [{"username": "mhayden"}]


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("MERGEABLE", "mergeable"),
        ("CI_MUST_PASS", "ci_must_pass"),
        ("SOME_FUTURE_STATUS", "some_future_status"),  # Unknown values pass through, lowercased.
    ],
)
@respx.mock
async def test_merge_status_is_lowercased_and_unknown_values_pass_through(gitlab_env, raw_status, expected):
    """GitLab's detailed merge status is lowercased without a fixed list of allowed values."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(detailedMergeStatus=raw_status)))

    # Act
    data = await call_tool()

    # Assert
    assert data["merge_status"] == expected


@pytest.mark.parametrize(
    "strategy",
    ["merge_when_checks_pass", "merge_train", "add_to_merge_train_when_checks_pass"],
)
@respx.mock
async def test_auto_merge_strategy_is_returned_raw(gitlab_env, strategy):
    """The auto-merge strategy is passed through exactly as GitLab sends it."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(autoMergeStrategy=strategy)))

    # Act
    data = await call_tool()

    # Assert
    assert data["auto_merge_strategy"] == strategy


@pytest.mark.parametrize(
    ("description_limit", "length", "expected"),
    [
        (1, 1, (1, False, None)),
        (1, 2, (1, True, 1)),
        (4, 5, (5, False, None)),  # A limit of 4 leaves room for 1 extra character (4 // 4).
        (DESCRIPTION_CAP, DESCRIPTION_CAP, (DESCRIPTION_CAP, False, None)),
        (DESCRIPTION_CAP, DESCRIPTION_CAP + 1000, (DESCRIPTION_CAP + 1000, False, None)),
        (DESCRIPTION_CAP, DESCRIPTION_CAP + 1001, (DESCRIPTION_CAP, True, DESCRIPTION_CAP)),
        # 📑 A raised limit reads a long templated description in one call, and still pages past its tail.
        (DESCRIPTION_PAGE_MAX, 8000, (8000, False, None)),
        (DESCRIPTION_PAGE_MAX, DESCRIPTION_PAGE_MAX * 5 // 4 + 1, (DESCRIPTION_PAGE_MAX, True, DESCRIPTION_PAGE_MAX)),
    ],
)
@respx.mock
async def test_description_obeys_requested_limit(gitlab_env, description_limit, length, expected):
    """Descriptions stop near the requested limit, with a tail up to a quarter of it, and report any continuation."""
    expected_length, expected_truncated, next_offset = expected
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description="d" * length)))

    # Act
    data = await call_tool(include_description=True, description_limit=description_limit)

    # Assert
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "iid": str(IID),
        "includeLabels": False,
        "includeThreads": True,
        "includeCommits": False,
    }
    assert len(data["description"]) == expected_length
    assert ("description_truncated" in data) is expected_truncated
    assert data.get("next_description_offset") == next_offset
    assert ("next_description_offset" in data) is (next_offset is not None)


@respx.mock
async def test_description_pages_reconstruct_unicode_text_without_gaps_or_overlaps(gitlab_env):
    """Character offsets page Unicode descriptions in order with no gaps or repeated characters."""
    # Arrange
    description = "A😀e\u0301BCdéFG"
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description=description)))
    pages = []
    offset = 0

    # Act
    async with Client(mcp) as client:
        while True:
            data = await call_tool(
                include_description=True, description_limit=3, description_offset=offset, client=client
            )
            pages.append(data["description"])
            next_offset = data.get("next_description_offset")
            if next_offset is None:
                break
            assert next_offset == offset + len(data["description"])
            offset = next_offset

    # Assert
    assert "".join(pages) == description
    assert pages == [description[0:3], description[3:6], description[6:9], description[9:12]]
    assert route.call_count == len(pages)
    variables = [json.loads(call.request.content)["variables"] for call in route.calls]
    assert all(item == {"fullPath": PROJECT, "iid": str(IID)} for item in variables[1:])


@respx.mock
async def test_default_description_cap_pages_reconstruct_long_text(gitlab_env):
    """Default-sized pages reconstruct descriptions longer than 8000 characters in exactly three calls."""
    # Arrange
    # The 1001-character tail is just over a quarter of the cap, so it cannot ride along with the second page.
    description = "a" * DESCRIPTION_CAP + "b" * DESCRIPTION_CAP + "c" * 1001
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description=description)))

    # Act
    async with Client(mcp) as client:
        first = await call_tool(include_description=True, client=client)
        second = await call_tool(
            include_description=True, description_offset=first["next_description_offset"], client=client
        )
        third = await call_tool(
            include_description=True, description_offset=second["next_description_offset"], client=client
        )
    pages = [first["description"], second["description"], third["description"]]

    # Assert
    assert route.call_count == len(pages)
    assert [len(page) for page in pages] == [DESCRIPTION_CAP, DESCRIPTION_CAP, 1001]
    assert first["next_description_offset"] == DESCRIPTION_CAP
    assert second["next_description_offset"] == DESCRIPTION_CAP * 2
    assert {"description_truncated", "next_description_offset"}.isdisjoint(third)
    assert "".join(pages) == description


@respx.mock
async def test_description_offset_returns_only_description_page_with_no_extra_reads(gitlab_env, monkeypatch):
    """A continuation reads only the description, even with options that normally need extra reads."""
    # Arrange
    description = "prefix body KEY-89 suffix"
    node = make_mr(
        title="TITLE-1",
        description=description,
        detailedMergeStatus="BLOCKED_STATUS",
        headPipeline={"status": "FAILED", "id": "gid://gitlab/Ci::Pipeline/123"},
    )
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))
    jobs = respx.get(PIPELINE_JOBS).mock(return_value=httpx.Response(200, json=[]))
    rest = respx.get(MR_REST).mock(return_value=httpx.Response(200, json={"diverged_commits_count": 1}))
    monkeypatch.setattr(mr_read, "_extract_issue_keys", lambda *args: pytest.fail("Continuation extracted issue keys"))
    page_start = len("prefix ")
    page_end = page_start + len("body")

    # Act
    data = await call_tool(
        include_description=True,
        description_limit=4,
        description_offset=page_start,
        include_jobs=True,
        include_labels=True,
        include_threads=True,
        include_commits_behind=True,
        include_commits=True,
        issue_key_pattern=r"KEY-[0-9]+",
    )

    # Assert
    assert route.call_count == 1
    assert jobs.call_count == rest.call_count == 0
    request = json.loads(route.calls.last.request.content)
    assert request["variables"] == {"fullPath": PROJECT, "iid": str(IID)}
    assert re.sub(r"\s+", " ", request["query"]).strip() == (
        "query($fullPath: ID!, $iid: String!) { project(fullPath: $fullPath) "
        "{ mergeRequest(iid: $iid) { description } } }"
    )
    assert data == {
        "iid": IID,
        "description": "body",
        "description_truncated": True,
        "next_description_offset": page_end,
        "description_hash": hashlib.sha256(description.encode("utf-8")).hexdigest(),
    }


@respx.mock
async def test_description_hash_compares_full_raw_text_across_pages_and_edits(gitlab_env):
    """Unicode and CRLF stay unchanged in the full hash, including edits outside the page."""
    # Arrange
    description = "é😀\r\ne\u0301 body END-9"
    changed = "É😀\r\ne\u0301 body END-9"
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            graphql_response(make_mr(description=description)),
            graphql_response({"description": description}),
            graphql_response({"description": changed}),
        ],
    )

    # Act
    async with Client(mcp) as client:
        first = await call_tool(
            include_description=True, include_description_hash=True, description_limit=3, client=client
        )
        second = await call_tool(include_description=True, description_offset=3, description_limit=3, client=client)
        edited = await call_tool(include_description=True, description_offset=3, description_limit=3, client=client)

    # Assert
    assert route.call_count == len([first, second, edited])
    assert first["description_hash"] == second["description_hash"] == hashlib.sha256(description.encode()).hexdigest()
    assert second["description"] == edited["description"] == description[3:6]
    assert edited["description_hash"] == hashlib.sha256(changed.encode()).hexdigest()
    assert edited["description_hash"] != first["description_hash"]


@pytest.mark.parametrize("description", [None, ""], ids=["null", "empty"])
@respx.mock
async def test_description_hash_for_null_or_empty_text(gitlab_env, description):
    """Null and empty descriptions have the same full hash and no description fields."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            graphql_response(make_mr(description=description)),
            graphql_response({"description": description}),
        ],
    )

    # Act
    first = await call_tool(include_description=True, include_description_hash=True)
    continuation = await call_tool(include_description=True, description_offset=1)

    # Assert
    empty_hash = hashlib.sha256(b"").hexdigest()
    assert route.call_count == len([first, continuation])
    assert first["description_hash"] == empty_hash
    assert {"description", "description_truncated", "next_description_offset"}.isdisjoint(first)
    assert continuation == {"iid": IID, "description_hash": empty_hash}


@pytest.mark.parametrize(
    ("arguments", "include_hash"),
    [({}, False), ({"include_description_hash": False}, False), ({"include_description_hash": True}, True)],
    ids=["default", "explicit-false", "opt-in"],
)
@respx.mock
async def test_first_description_page_hash_is_opt_in(gitlab_env, arguments, include_hash):
    """The first page includes a full-description hash only when requested."""
    # Arrange
    description = "FIRST-1 then SECOND-2"
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description=description)))

    # Act
    data = await call_tool(include_description=True, description_limit=5, **arguments)

    # Assert
    assert route.call_count == 1
    assert data["description"] == description[:5]
    assert data["issue_keys"] == ["FIRST-1", "SECOND-2"]
    assert ("description_hash" in data) is include_hash
    if include_hash:
        assert data["description_hash"] == hashlib.sha256(description.encode()).hexdigest()


@pytest.mark.parametrize(
    "arguments",
    [{}, {"include_description_hash": False}, {"include_description_hash": True}],
    ids=["default", "explicit-false", "explicit-true"],
)
@respx.mock
async def test_continuation_hash_is_always_included(gitlab_env, arguments):
    """Continuations include the full hash regardless of the first-page hash option."""
    # Arrange
    description = "one two"
    route = respx.post(GRAPHQL).mock(return_value=graphql_response({"description": description}))

    # Act
    data = await call_tool(include_description=True, description_offset=4, **arguments)

    # Assert
    assert route.call_count == 1
    assert data == {
        "iid": IID,
        "description": "two",
        "description_hash": hashlib.sha256(description.encode()).hexdigest(),
    }


@pytest.mark.parametrize("extra_offset", [0, 5], ids=["at-end", "past-end"])
@respx.mock
async def test_description_offset_at_or_beyond_end_omits_description_and_continuation(gitlab_env, extra_offset):
    """An offset at the end returns no description page or continuation offset."""
    # Arrange
    description = "short KEY-2"
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description=description)))

    # Act
    data = await call_tool(include_description=True, description_offset=len(description) + extra_offset)

    # Assert
    assert route.call_count == 1
    assert data == {"iid": IID, "description_hash": hashlib.sha256(description.encode()).hexdigest()}


@pytest.mark.parametrize(
    "arguments",
    [
        {"include_description_hash": True},
        {"include_description_hash": True, "include_description": False},
        {"include_description_hash": True, "include_description": True, "description_limit": 0},
        {"include_description_hash": True, "include_description": False, "description_offset": 1},
        {
            "include_description_hash": True,
            "include_description": True,
            "description_limit": 0,
            "description_offset": 1,
        },
    ],
    ids=[
        "omitted-description",
        "explicit-false",
        "zero-limit",
        "continuation-without-description",
        "continuation-zero-limit",
    ],
)
@respx.mock
async def test_description_hash_requires_included_description_and_positive_limit(gitlab_env, arguments):
    """A requested hash requires fetching the description and fails before any HTTP request otherwise."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act / Assert
    with pytest.raises(ToolError, match=r"include_description.*description_limit"):
        await call_tool(**arguments)
    assert route.call_count == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"issue_key_pattern": "["},
        {"issue_key_pattern": r"KEY-[0-9]+", "issue_key_prefixes": []},
        {"issue_key_prefixes": ["invalid"]},
    ],
    ids=["invalid-regex", "conflicting-filters", "invalid-prefix"],
)
@respx.mock
async def test_continuation_validates_issue_key_options_before_request(gitlab_env, arguments):
    """Description-only pages still validate issue-key options before fetching GitLab."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response({"description": "text"}))

    # Act / Assert
    with pytest.raises(ToolError, match="issue_key"):
        await call_tool(include_description=True, description_offset=1, **arguments)
    assert route.call_count == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"description_offset": 1},
        {"include_description": False, "description_offset": 1},
        {"include_description": True, "description_limit": 0, "description_offset": 1},
    ],
    ids=["omitted-description", "explicit-false", "zero-limit"],
)
@respx.mock
async def test_description_offset_requires_included_description_and_positive_limit(gitlab_env, arguments):
    """A nonzero offset requires an included description and a positive description limit."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act / Assert
    with pytest.raises(ToolError, match=r"description_offset.*include_description.*description_limit"):
        await call_tool(**arguments)
    assert route.call_count == 0


@respx.mock
async def test_negative_description_offset_is_rejected_before_request(gitlab_env):
    """Negative character offsets are rejected before any GitLab request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(include_description=True, description_offset=-1)
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("args", "expects_description"),
    [
        ({}, False),  # Default: the description is left out to save tokens.
        ({"include_description": False}, False),
        ({"include_description": True}, True),  # Opt-in: the description comes back.
    ],
    ids=["default", "explicit-false", "opt-in"],
)
@respx.mock
async def test_description_is_omitted_by_default_and_included_on_opt_in(gitlab_env, args, expects_description):
    """The description is only returned when the caller opts in."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act
    data = await call_tool(**args)

    # Assert
    assert ("description" in data) is expects_description
    assert ("description_truncated" in data) is False
    assert "next_description_offset" not in data
    assert data["title"] == "Rebuild support"


@pytest.mark.parametrize(
    "skip_args",
    [{"include_description": False}, {"include_description": True, "description_limit": 0}],
    ids=["explicit-false", "zero-limit"],
)
@respx.mock
async def test_skipping_description_omits_field_and_flag(gitlab_env, skip_args):
    """Skipping the description leaves out its text and cut flag, even though GraphQL still fetches it for keys."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description="d" * 5000)))

    # Act
    data = await call_tool(**skip_args)

    # Assert
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "iid": str(IID),
        "includeLabels": False,
        "includeThreads": True,
        "includeCommits": False,
    }
    assert {"description", "description_truncated", "next_description_offset"}.isdisjoint(data)
    assert data["title"] == "Rebuild support"


@respx.mock
async def test_include_labels_returns_label_page_and_numeric_author_id(gitlab_env):
    """The optional labels connection and author ID come from the existing GraphQL request."""
    # Arrange
    node = make_mr(
        labels={"nodes": [{"title": "backend"}, {"title": "urgent"}], "pageInfo": {"hasNextPage": True}},
    )
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool(include_labels=True)

    # Assert
    request = json.loads(route.calls.last.request.content)
    assert route.call_count == 1
    assert request["variables"]["includeLabels"] is True
    assert "author { id username }" in request["query"]
    assert "labels(first: 100) @include(if: $includeLabels)" in request["query"]
    assert "pageInfo { hasNextPage }" in request["query"]
    assert data["author_id"] == AUTHOR_ID
    assert data["labels"] == ["backend", "urgent"]
    assert data["labels_truncated"] is True


@respx.mock
async def test_labels_are_skipped_by_default(gitlab_env):
    """The default result neither requests nor returns labels."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act
    data = await call_tool()

    # Assert
    request = json.loads(route.calls.last.request.content)
    assert route.call_count == 1
    assert request["variables"]["includeLabels"] is False
    assert "labels(first: 100) @include(if: $includeLabels)" in request["query"]
    assert {"labels", "labels_truncated"}.isdisjoint(data)


@pytest.mark.parametrize(
    ("args", "expected_keys"),
    [
        ({}, ["TITLE-1", "BODY-2"]),
        ({"include_description": True, "description_limit": 0}, ["TITLE-1", "BODY-2"]),
        ({"issue_key_prefixes": ["BODY"]}, ["BODY-2"]),
        ({"issue_key_pattern": r"BODY-[0-9]+"}, ["BODY-2"]),
    ],
    ids=["default", "zero-limit", "prefixes", "custom-pattern"],
)
@respx.mock
async def test_issue_keys_scan_description_without_returning_it(gitlab_env, args, expected_keys):
    """Description keys come back even when the description text itself is left out to save tokens."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(title="TITLE-1", description="BODY-2")))

    # Act
    data = await call_tool(**args)

    # Assert
    request = json.loads(route.calls.last.request.content)
    assert route.call_count == 1
    # The mock always sends a description, so check that the query really asks GitLab for it. 🔎
    assert "iid title description state" in request["query"]
    assert "includeDescription" not in request["query"]
    assert "includeDescription" not in request["variables"]
    assert data["issue_keys"] == expected_keys
    assert {"description", "description_truncated", "next_description_offset"}.isdisjoint(data)


@respx.mock
async def test_null_author_omits_author_id(gitlab_env):
    """A deleted or banned author is safely omitted along with its numeric ID."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(author=None)))

    # Act
    data = await call_tool()

    # Assert
    assert route.call_count == 1
    assert "author" not in data
    assert "author_id" not in data


@respx.mock
async def test_issue_keys_deduplicate_in_source_order_before_description_truncation(gitlab_env):
    """Issue keys use the full fetched description and keep first-seen title/description order."""
    # Arrange
    description = "one ALPHA-9 TWO BETA_2-4 ALPHA-9 GAMMA-3"
    node = make_mr(title="ALPHA-9 intro", description=description)
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool(include_description=True, description_limit=8)

    # Assert
    assert route.call_count == 1
    assert data["description"] == "one ALPH"
    assert data["description_truncated"] is True
    assert data["issue_keys"] == ["ALPHA-9", "BETA_2-4", "GAMMA-3"]
    assert "issue_keys_truncated" not in data


@pytest.mark.parametrize(
    ("title", "description", "arguments", "expected"),
    [
        ("Fix CVE-2024-12345", "", {}, []),
        ("Security fixes", "Fix CVE-2023-9876", {"include_description": True}, []),
        (
            "JIRA-42 fixes CVE-2024-12345",
            "CVE-2023-9876 and JIRA-43.",
            {"include_description": True},
            ["JIRA-42", "JIRA-43"],
        ),
        ("Fix JIRA-42.", "", {}, ["JIRA-42"]),
        (
            "ALAS-2026-1234, DSA-5432-1, RHSA-2026:1234",
            "PROJ-3880 fixes ABC-2026-0501 and CVE-2022-41946. See PROJ-3445.",
            {"include_description": True},
            ["PROJ-3880", "PROJ-3445"],
        ),
        ("CVE-123 and CVE-2024", "", {}, ["CVE-123", "CVE-2024"]),
        ("CVE-2024-12345 and CVE-2024", "", {}, ["CVE-2024"]),
        ("CVE-2024 and CVE-2024-12345", "", {}, ["CVE-2024"]),
        ("CVE-2024-abc and JIRA-7-123abc", "", {}, ["CVE-2024", "JIRA-7"]),
        (
            "CVE-2024-12345 and JIRA-42",
            "CVE-2023-9876",
            {"include_description": True, "issue_key_prefixes": ["CVE"]},
            ["CVE-2024", "CVE-2023"],
        ),
        ("CVE-2024-12345", "", {"issue_key_pattern": r"CVE-[0-9]+"}, ["CVE-2024"]),
        ("CVE-2024-12345", "", {"issue_key_pattern": r"CVE-[0-9]+-[0-9]+"}, ["CVE-2024-12345"]),
    ],
    ids=[
        "cve-in-title",
        "cve-in-description",
        "jira-keys-next-to-cves",
        "jira-at-sentence-end",
        "advisory-ids-next-to-jira-keys",
        "bare-cve-keys",
        "full-cve-before-bare-key",
        "bare-key-before-full-cve",
        "suffix-with-letters",
        "explicit-cve-prefix",
        "custom-partial-cve-pattern",
        "custom-full-cve-pattern",
    ],
)
@respx.mock
async def test_issue_key_extraction_skips_advisory_ids(gitlab_env, title, description, arguments, expected):
    """Default extraction skips advisory IDs while preserving caller-selected issue key behavior."""
    # Arrange
    node = make_mr(title=title, description=description)
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool(**arguments)

    # Assert
    assert route.call_count == 1
    assert data.get("issue_keys", []) == expected
    assert ("issue_keys" in data) is bool(expected)
    assert "issue_keys_truncated" not in data


@pytest.mark.parametrize(
    ("prefixes", "expected"),
    [
        (None, ["ALPHA-1", "BETA_2-4"]),
        (["BETA_2"], ["BETA_2-4"]),
        (["GAMMA"], []),
    ],
    ids=["all", "literal-prefix", "no-match"],
)
@respx.mock
async def test_issue_key_prefixes_filter_literal_prefixes(gitlab_env, prefixes, expected):
    """Optional validated prefixes filter matching keys literally, without changing fetch count."""
    # Arrange
    node = make_mr(title="ALPHA-1", description="BETA_2-4 ALPHA-1")
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool(include_description=True, issue_key_prefixes=prefixes)

    # Assert
    assert route.call_count == 1
    if expected:
        assert data["issue_keys"] == expected
    else:
        assert "issue_keys" not in data


@respx.mock
async def test_custom_issue_key_pattern_returns_full_matches_in_source_order(gitlab_env):
    """Custom patterns return full, case-preserved matches and deduplicate in title/description order."""
    # Arrange
    node = make_mr(title="task-1 then REF#2", description="REF#2 then task-1")
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool(
        include_description=True,
        issue_key_pattern=r"(?i:(task-[0-9]+))|(?i:(ref#[0-9]+))",
    )

    # Assert
    assert route.call_count == 1
    assert data["issue_keys"] == ["task-1", "REF#2"]
    assert "issue_keys_truncated" not in data


@respx.mock
async def test_custom_issue_key_pattern_scans_raw_description_beyond_display_cap(gitlab_env):
    """Custom extraction scans raw text beyond the displayed description cap."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=graphql_response(make_mr(title="no-key", description="padding TASK-22")),
    )

    # Act
    data = await call_tool(include_description=True, description_limit=1, issue_key_pattern=r"TASK-[0-9]+")

    # Assert
    assert data["issue_keys"] == ["TASK-22"]
    assert data["description"] == "p"


@pytest.mark.parametrize("pattern", [r"(?=x)", r"(?=.)"], ids=["no-match", "empty-matches"])
@respx.mock
async def test_custom_issue_key_pattern_omits_no_and_empty_matches(gitlab_env, pattern):
    """No matches and zero-length matches produce no issue key and no truncation flag."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(title="text")))

    # Act
    data = await call_tool(issue_key_pattern=pattern)

    # Assert
    assert route.call_count == 1
    assert "issue_keys" not in data
    assert "issue_keys_truncated" not in data


@pytest.mark.parametrize(
    ("arguments", "error_message"),
    [
        ({"issue_key_pattern": "["}, "issue_key_pattern"),
        ({"issue_key_pattern": ""}, "issue_key_pattern"),
        ({"issue_key_pattern": "x" * 257}, "issue_key_pattern"),
        (
            {"issue_key_pattern": r"KEY-[0-9]+", "issue_key_prefixes": []},
            "cannot be used with issue_key_prefixes",
        ),
    ],
    ids=["invalid-regex", "empty-pattern", "pattern-too-long", "conflicting-prefix-filter"],
)
@respx.mock
async def test_invalid_custom_issue_key_patterns_fail_before_request(gitlab_env, arguments, error_message):
    """Invalid, oversized, or conflicting custom patterns are rejected before any GitLab request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act / Assert
    with pytest.raises(ToolError, match=error_message):
        await call_tool(**arguments)
    assert route.call_count == 0


@respx.mock
async def test_custom_issue_key_pattern_reports_match_timeout_without_partial_results(gitlab_env, monkeypatch):
    """A regex timeout raises an actionable error instead of returning matches found before the timeout."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(title="KEY-1")))
    timeouts: list[float] = []

    def matches_then_times_out(text: str, *, timeout: float):
        """Yield one apparent match, then simulate the regex engine timing out."""
        timeouts.append(timeout)
        yield re.match(r"KEY-[0-9]+", "KEY-1")
        raise TimeoutError

    monkeypatch.setattr(
        mr_read.regex,
        "compile",
        lambda _pattern: SimpleNamespace(finditer=matches_then_times_out),
    )

    # Act / Assert
    with pytest.raises(ToolError, match=r"timeout.*Simplify issue_key_pattern"):
        await call_tool(issue_key_pattern=r"KEY-[0-9]+")
    assert route.call_count == 1
    assert timeouts == [0.05]


@respx.mock
async def test_custom_issue_key_pattern_scan_cap_marks_even_empty_results_truncated(gitlab_env):
    """A cut custom scan marks truncation even when the only key is beyond the scan boundary."""
    # Arrange
    node = make_mr(title="x" * ISSUE_KEY_SCAN_CAP + " KEY-2")
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool(issue_key_pattern=r"KEY-[0-9]+")

    # Assert
    assert route.call_count == 1
    assert "issue_keys" not in data
    assert data["issue_keys_truncated"] is True


@respx.mock
async def test_custom_issue_key_pattern_skips_oversized_matches_and_marks_truncated(gitlab_env):
    """A full custom match longer than 256 characters is skipped and reported as truncated."""
    # Arrange
    node = make_mr(title="A" + "x" * 256 + "Z")
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool(issue_key_pattern=r"A.{256}Z")

    # Assert
    assert route.call_count == 1
    assert "issue_keys" not in data
    assert data["issue_keys_truncated"] is True


@respx.mock
async def test_custom_issue_key_pattern_caps_at_one_hundred_unique_keys(gitlab_env):
    """Custom results keep the first 100 unique matches and mark truncation when another key is found."""
    # Arrange
    title = "KEY-0"
    description = " ".join(f"KEY-{number}" for number in range(1, 101))
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(title=title, description=description)))

    # Act
    data = await call_tool(include_description=True, issue_key_pattern=r"KEY-[0-9]+")

    # Assert
    assert route.call_count == 1
    assert data["issue_keys"] == [f"KEY-{number}" for number in range(100)]
    assert data["issue_keys_truncated"] is True


@pytest.mark.parametrize("prefixes", [["a"], ["A.*"], ["A" * 33], ["A", "B"] + ["C"] * 19])
@respx.mock
async def test_invalid_issue_key_prefixes_fail_before_request(gitlab_env, prefixes):
    """Invalid or oversized issue-key prefix filters are rejected before calling GitLab."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act / Assert
    with pytest.raises(ToolError, match="issue_key_prefixes"):
        await call_tool(include_description=True, issue_key_prefixes=prefixes)
    assert route.call_count == 0


@respx.mock
async def test_issue_keys_are_capped_at_one_hundred(gitlab_env):
    """Issue-key results keep the first 100 unique keys and mark the result as truncated."""
    # Arrange
    title = "KEY-0"
    description = " ".join(f"KEY-{number}" for number in range(1, 101))
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(title=title, description=description)))

    # Act
    data = await call_tool(include_description=True)

    # Assert
    assert route.call_count == 1
    assert data["issue_keys"] == [f"KEY-{number}" for number in range(100)]
    assert data["issue_keys_truncated"] is True


@pytest.mark.parametrize(
    ("description_limit", "message"),
    [
        (-1, "greater than or equal to 0"),
        (DESCRIPTION_PAGE_MAX + 1, f"less than or equal to {DESCRIPTION_PAGE_MAX}"),
    ],
)
@respx.mock
async def test_description_limit_must_be_between_zero_and_page_max(gitlab_env, description_limit, message):
    """The requested description limit cannot exceed the supported range, and the error names the bound."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool(description_limit=description_limit)
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("note_overrides", "expected"),
    [
        ({"position": POSITION}, {"by": "mhayden", "notes": 1, "path": "docs/sop.md", "line": 212}),
        (
            {"position": RENAMED_POSITION},
            {"by": "mhayden", "notes": 1, "path": "docs/new.md", "old_path": "docs/old.md", "line": 3},
        ),
        ({"position": DELETED_LINE_POSITION}, {"by": "mhayden", "notes": 1, "path": "docs/sop.md", "old_line": 40}),
        ({"position": None}, {"by": "mhayden", "notes": 1}),  # General thread: no file or line.
        ({"position": None, "author": None}, {"notes": 1}),  # Null author: no by.
    ],
    ids=["same-path", "renamed", "deleted-line", "general", "general-deleted-author"],
)
@respx.mock
async def test_open_thread_ref_drops_empty_and_repeated_fields(gitlab_env, note_overrides, expected):
    """A thread ref names its file once, shows old_path or old_line only when needed, and drops empty fields."""
    # Arrange
    thread = make_discussion("open", note=make_note(**note_overrides))
    respx.post(GRAPHQL).mock(
        return_value=graphql_response(make_mr(resolvableDiscussionsCount=1, discussions=make_discussions(thread))),
    )

    # Act
    data = await call_tool()

    # Assert
    assert data["threads"]["open"] == [{"id": "open"} | expected]


@pytest.mark.parametrize(
    ("author", "expected"),
    [
        (
            {"username": "review-bot", "bot": True, "name": "****", "id": "gid://gitlab/User/123456"},
            "bot:123456",
        ),
        (
            {"username": "review-bot", "bot": True, "name": "Review Assistant", "id": "gid://gitlab/User/123456"},
            "Review Assistant",
        ),
        ({"username": "alice", "bot": False, "name": "Alice Example", "id": "gid://gitlab/User/42"}, "alice"),
        (None, None),
    ],
    ids=["short-bot", "named-bot", "human", "deleted-author"],
)
@respx.mock
async def test_thread_index_displays_bot_authors_without_exposing_long_username(gitlab_env, author, expected):
    """Thread index display uses compact bot labels and leaves human usernames unchanged."""
    # Arrange
    note = make_note(position=POSITION) | {"author": author}
    route = respx.post(GRAPHQL).mock(
        return_value=graphql_response(
            make_mr(
                resolvableDiscussionsCount=1,
                discussions=make_discussions(make_discussion("open", note=note)),
            ),
        ),
    )

    # Act
    data = await call_tool()

    # Assert
    expected_fields = {"by": expected} if expected is not None else {}
    assert data["threads"]["open"] == [
        {"id": "open", "notes": 1, "path": "docs/sop.md", "line": 212} | expected_fields,
    ]
    query = json.loads(route.calls.last.request.content)["query"]
    assert "author { username bot name id }" in query


@respx.mock
async def test_thread_index_lists_only_open_human_threads(gitlab_env):
    """Only resolvable, unresolved threads that start with a human note are listed."""
    # Arrange
    discussions = [
        make_discussion("open-diff", note=make_note(position=POSITION)),
        make_discussion("open-general"),
        make_discussion("resolved", resolved=True, note=make_note(position=POSITION)),
        make_discussion("plain-comment", resolvable=False),
        make_discussion("system", note=make_note(system=True)),
    ]
    node = make_mr(
        resolvableDiscussionsCount=10,  # Counts are not recomputed from the nodes above.
        resolvedDiscussionsCount=4,
        discussions=make_discussions(*discussions),
    )
    respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool()

    # Assert
    assert data["threads"] == {
        "open_count": 6,
        "resolved_count": 4,
        "open": [
            {"id": "open-diff", "by": "mhayden", "notes": 1, "path": "docs/sop.md", "line": 212},
            {"id": "open-general", "by": "mhayden", "notes": 1},
        ],
        "index_complete": True,
    }


@pytest.mark.parametrize(
    ("thread_count", "has_next", "expected_listed", "expected_complete"),
    [
        (THREAD_INDEX_CAP, False, THREAD_INDEX_CAP, True),  # Exactly at the cap is still complete.
        (THREAD_INDEX_CAP + 1, False, THREAD_INDEX_CAP, False),  # More open threads than the cap.
        (1, True, 1, False),  # GitLab has another page of discussions we did not read.
    ],
)
@respx.mock
async def test_index_complete_says_when_threads_are_missing(
    gitlab_env,
    thread_count,
    has_next,
    expected_listed,
    expected_complete,
):
    """The index keeps the first open threads in API order and flags any it could not list."""
    # Arrange
    discussions = [make_discussion(f"t{i}") for i in range(thread_count)]
    node = make_mr(
        resolvableDiscussionsCount=thread_count,
        resolvedDiscussionsCount=0,
        discussions=make_discussions(*discussions, has_next=has_next),
    )
    respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool()

    # Assert
    threads = data["threads"]
    assert [ref["id"] for ref in threads["open"]] == [f"t{i}" for i in range(expected_listed)]
    assert threads["open_count"] == thread_count
    assert threads["index_complete"] is expected_complete


@respx.mock
async def test_null_thread_counts_count_as_zero(gitlab_env):
    """GitLab can send null counts for an MR with no threads, which mean zero."""
    # Arrange
    node = make_mr(resolvableDiscussionsCount=None, resolvedDiscussionsCount=None)
    respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool()

    # Assert
    assert data["threads"] == {"open_count": 0, "resolved_count": 0, "index_complete": True}


@pytest.mark.parametrize("offset", [0, 1], ids=["detail", "continuation"])
@respx.mock
async def test_missing_project_is_reported(gitlab_env, offset):
    """GitLab returns project null for unknown or invisible projects."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"project": None}}))

    # Act / Assert
    with pytest.raises(ToolError, match="not found, or the token cannot see it"):
        await call_tool(include_description=True, description_offset=offset)


@pytest.mark.parametrize("offset", [0, 1], ids=["detail", "continuation"])
@respx.mock
async def test_missing_merge_request_is_reported(gitlab_env, offset):
    """A null mergeRequest names the IID and the project."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(None))

    # Act / Assert
    with pytest.raises(ToolError, match=r"Merge request !2187 not found in 'group/project'"):
        await call_tool(include_description=True, description_offset=offset)


@respx.mock
async def test_graphql_errors_are_reported(gitlab_env):
    """GraphQL errors reach the agent unchanged."""
    # Arrange
    body = {"data": None, "errors": [{"message": "Query is too complex"}]}
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=body))

    # Act / Assert
    with pytest.raises(ToolError, match="GitLab GraphQL error: Query is too complex"):
        await call_tool()


SECTIONED_DESCRIPTION = (
    "Intro\n\n## Summary\n\nShort summary.\n\n## Scope\n\nTwo files.\n\n```md\n## Not a heading\n```\n"
)


@respx.mock
async def test_find_renders_exact_lines_offsets_and_merged_context(gitlab_env):
    """Find preserves CRLF, tabs, Unicode offsets, and overlapping context at both ends."""
    # Arrange
    description = "é start\r\nalpha\tBETA\r\nnear\nalpha BETA\nend"
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description=description)))

    # Act
    data = await call_tool(include_description=True, find=["alpha", "BETA"], find_context=2)

    # Assert
    assert data["find_matches"] == (
        "line\toffset\tterms\tcut\ttext\n"
        '1\t0\t\t\t"é start\\r"\n'
        '2\t9\t1,2\t\t"alpha\\tBETA\\r"\n'
        '3\t21\t\t\t"near"\n'
        '4\t26\t1,2\t\t"alpha BETA"\n'
        '5\t37\t\t\t"end"'
    )
    assert data["description_hash"] == hashlib.sha256(description.encode()).hexdigest()
    assert (
        not {"description", "description_truncated", "next_description_offset", "find_missing", "find_truncated"}
        & data.keys()
    )


@respx.mock
async def test_find_scans_after_hit_cap_for_missing_terms(gitlab_env):
    """Terms beyond the rendered hit cap are found, while genuinely missing terms are reported."""
    # Arrange
    description = "hit\n" * 101 + "late"
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description=description)))

    # Act
    data = await call_tool(include_description=True, find=["hit", "late", "absent"])

    # Assert
    assert len(data["find_matches"].splitlines()) == 101  # noqa: PLR2004 - header plus 100 hit lines.
    assert data["find_missing"] == ["absent"]
    assert data["find_truncated"] is True


@pytest.mark.parametrize(
    ("description", "limit", "context", "expected"),
    [
        ("oversized hit", 4, 0, 'line\toffset\tterms\tcut\ttext\n1\t0\t1\ttrue\t"over"'),
        ("before\nhit\nafter", 3, 1, 'line\toffset\tterms\tcut\ttext\n2\t7\t1\t\t"hit"'),
        ("hit\nhit", 3, 0, 'line\toffset\tterms\tcut\ttext\n1\t0\t1\t\t"hit"'),
        # Blank context lines cost no budget, so context must follow only the rendered hits.
        ("hit\n\n" * 101, 3, 1, 'line\toffset\tterms\tcut\ttext\n1\t0\t1\t\t"hit"\n2\t4\t\t\t""'),
    ],
)
@respx.mock
async def test_find_budget_omits_or_cuts_rows(gitlab_env, description, limit, context, expected):
    """Find prioritizes hits, clips an oversized hit, and reports omitted hits or context."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description=description)))

    # Act
    data = await call_tool(include_description=True, find=["hit"], find_context=context, description_limit=limit)

    # Assert
    assert data["find_matches"] == expected
    assert data["find_truncated"] is True


@pytest.mark.parametrize("description", [None, ""])
@respx.mock
async def test_find_empty_description(gitlab_env, description):
    """Null and empty descriptions have no hits and share the empty-string hash."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description=description)))

    # Act
    data = await call_tool(include_description=True, find=["one", "two"])

    # Assert
    assert data["find_matches"] == "line\toffset\tterms\tcut\ttext"
    assert data["find_missing"] == ["one", "two"]
    assert data["description_hash"] == hashlib.sha256(b"").hexdigest()
    assert "find_truncated" not in data


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"find": ["x"]}, "find requires include_description=true"),
        ({"include_description": True, "find": ["x"], "section": "Scope"}, "find cannot be combined"),
        ({"include_description": True, "find": ["x"], "headings_only": True}, "find cannot be combined"),
        ({"include_description": True, "find": ["x"], "description_offset": 1}, "description_offset must be 0"),
        ({"include_description": True, "find": ["x"], "description_limit": 0}, "description_limit greater than 0"),
        ({"find_context": 1}, "find_context requires find"),
        ({"include_description": True, "find": []}, "1 to 20 terms"),
        ({"include_description": True, "find": [""]}, "must not contain CR or LF"),
        ({"include_description": True, "find": ["a\nb"]}, "must not contain CR or LF"),
        ({"include_description": True, "find": ["a\rb"]}, "must not contain CR or LF"),
        ({"include_description": True, "find": ["x" * 201]}, "1 to 200 characters"),
        ({"include_description": True, "find": ["x"] * 21}, "20"),
    ],
)
@respx.mock
async def test_find_validation_precedes_requests(gitlab_env, arguments, message):
    """Invalid find arguments fail with a clear message and without making a GitLab request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act / Assert
    with pytest.raises(ToolError, match=re.escape(message)):
        await call_tool(**arguments)
    assert not route.called


@pytest.mark.parametrize(
    ("description", "arguments", "expected"),
    [
        pytest.param(SECTIONED_DESCRIPTION, {"headings_only": True}, {"headings": ["Summary", "Scope"]}, id="headings"),
        pytest.param("No headings here.", {"headings_only": True}, {"headings": []}, id="no-headings"),
        pytest.param(
            SECTIONED_DESCRIPTION,
            {"section": "scope"},
            {"description": "Two files.\n\n```md\n## Not a heading\n```"},
            id="one-section",
        ),
        pytest.param(
            SECTIONED_DESCRIPTION,
            {"section": ["Summary", "Scope"], "description_limit": 20},
            {
                "sections": [
                    {"heading": "Summary", "content": "Short summary."},
                    {"heading": "Scope", "content": "Two fi", "truncated": True},
                ]
            },
            id="section-list-shares-limit",
        ),
    ],
)
@respx.mock
async def test_focused_description_reads_return_only_requested_parts(gitlab_env, description, arguments, expected):
    """headings_only and section return only the requested parts, matched like work item sections."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description=description)))

    # Act
    data = await call_tool(include_description=True, **arguments)

    # Assert
    focused_fields = {"description", "description_truncated", "next_description_offset", "headings", "sections"}
    assert {key: value for key, value in data.items() if key in focused_fields} == expected


@pytest.mark.parametrize(
    ("arguments", "message", "reaches_gitlab"),
    [
        pytest.param(
            {"include_description": True, "section": "Missing"},
            "Available headings: 'Summary', 'Scope'",
            True,
            id="no-match",
        ),
        pytest.param(
            {"include_description": True, "section": "Scope", "description_offset": 5},
            "read the whole description",
            False,
            id="continuation-page",
        ),
        pytest.param({"headings_only": True}, "require include_description=true", False, id="no-description"),
        pytest.param(
            {"include_description": True, "section": "Scope", "headings_only": True},
            "cannot be used together",
            False,
            id="section-with-headings-only",
        ),
        pytest.param(
            {"include_description": True, "headings_only": True, "description_limit": 0},
            "require description_limit greater than 0",
            False,
            id="zero-limit",
        ),
    ],
)
@respx.mock
async def test_focused_description_errors_are_clear(gitlab_env, arguments, message, reaches_gitlab):
    """Bad focused reads fail with an actionable message, and option errors fail before any request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description=SECTIONED_DESCRIPTION)))

    # Act / Assert
    with pytest.raises(ToolError, match=re.escape(message)):
        await call_tool(**arguments)
    assert route.called is reaches_gitlab
