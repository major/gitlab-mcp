"""Tests for the get_merge_request tool, run through a real in-memory MCP client 🧪."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import DESCRIPTION_CAP, MAX_PAGES, THREAD_INDEX_CAP, mcp
from mcp_helpers import call_tool as call_mcp_tool

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 2187
PIPELINE_JOBS = "https://gitlab.example.com/api/v4/projects/group%2Fproject/pipelines/123/jobs"
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
        "author": {"username": "bkearney"},
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


async def call_tool(**extra: Any) -> dict:
    """Call get_merge_request with the default project and IID and return its data."""
    return await call_mcp_tool("get_merge_request", {"project": PROJECT, "iid": IID, **extra})


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
        "includeDescription": True,
        "includeJobs": False,
    }
    assert data == {
        "iid": IID,
        "title": "Rebuild support",
        "state": "opened",
        "draft": True,
        "author": "bkearney",
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
    node = make_mr(headPipeline={"status": "SUCCESS", "id": "gid://gitlab/Ci::Pipeline/123"})
    graphql = respx.post(GRAPHQL).mock(return_value=graphql_response(node))
    jobs = respx.get(PIPELINE_JOBS).mock(
        return_value=httpx.Response(200, json=[{"id": 456, "name": "unit tests"}]),
    )

    # Act
    data = await call_tool(include_jobs=True)

    # Assert
    assert sent_variables(graphql)["includeJobs"] is True
    assert dict(jobs.calls.last.request.url.params) == {"scope[]": "failed", "per_page": "100", "page": "1"}
    assert (
        "headPipeline { status id @include(if: $includeJobs) }"
        in json.loads(
            graphql.calls.last.request.content,
        )["query"]
    )
    assert data["failed_jobs"] == "id\tname\n456\tunit tests"
    assert "jobs_complete" not in data


@respx.mock
async def test_default_does_not_request_pipeline_jobs(gitlab_env):
    """The default lookup keeps the existing response and makes no REST jobs request."""
    # Arrange
    graphql = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))
    jobs = respx.get(PIPELINE_JOBS).mock(return_value=httpx.Response(200, json=[]))

    # Act
    data = await call_tool()

    # Assert
    assert sent_variables(graphql)["includeJobs"] is False
    assert jobs.call_count == 0
    assert "failed_jobs" not in data
    assert "jobs_complete" not in data


@respx.mock
async def test_include_jobs_without_pipeline_makes_no_rest_request(gitlab_env):
    """An explicitly requested job lookup skips REST when the merge request has no pipeline."""
    # Arrange
    graphql = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(headPipeline=None)))
    jobs = respx.get(PIPELINE_JOBS).mock(return_value=httpx.Response(200, json=[]))

    # Act
    data = await call_tool(include_jobs=True)

    # Assert
    assert sent_variables(graphql)["includeJobs"] is True
    assert jobs.call_count == 0
    assert "pipeline_status" not in data
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


@pytest.mark.parametrize("pipeline_id", ["123", "gid://gitlab/Ci::Pipeline/not-a-number", "gid://gitlab/Other/123"])
@respx.mock
async def test_include_jobs_rejects_malformed_pipeline_id(gitlab_env, pipeline_id):
    """A malformed pipeline GID raises an error instead of hiding failed jobs."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=graphql_response(make_mr(headPipeline={"status": "FAILED", "id": pipeline_id})),
    )
    jobs = respx.get(PIPELINE_JOBS).mock(return_value=httpx.Response(200, json=[]))

    # Act / Assert
    with pytest.raises(ToolError, match="pipeline ID format"):
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
    ("description_limit", "length", "expected_length", "expected_truncated"),
    [
        (1, 1, 1, False),
        (1, 2, 1, True),
        (DESCRIPTION_CAP, DESCRIPTION_CAP, DESCRIPTION_CAP, False),
        (DESCRIPTION_CAP, DESCRIPTION_CAP + 1, DESCRIPTION_CAP, True),
    ],
)
@respx.mock
async def test_description_obeys_requested_limit(
    gitlab_env, description_limit, length, expected_length, expected_truncated
):
    """Descriptions stop at the requested limit and report whether text was cut."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr(description="d" * length)))

    # Act
    data = await call_tool(include_description=True, description_limit=description_limit)

    # Assert
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "iid": str(IID),
        "includeDescription": True,
        "includeJobs": False,
    }
    assert len(data["description"]) == expected_length
    assert ("description_truncated" in data) is expected_truncated
    request = json.loads(route.calls.last.request.content)
    assert "description @include(if: $includeDescription)" in request["query"]


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
    """The description is only requested and returned when the caller opts in."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act
    data = await call_tool(**args)

    # Assert
    assert sent_variables(route)["includeDescription"] is expects_description
    assert ("description" in data) is expects_description
    assert ("description_truncated" in data) is False
    assert data["title"] == "Rebuild support"


@pytest.mark.parametrize(
    "skip_args",
    [{"include_description": False}, {"include_description": True, "description_limit": 0}],
    ids=["explicit-false", "zero-limit"],
)
@respx.mock
async def test_skipping_description_omits_field_and_flag(gitlab_env, skip_args):
    """Skipping the description also skips the GraphQL field and cut flag."""
    # Arrange
    node = make_mr()
    del node["description"]
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_tool(**skip_args)

    # Assert
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "iid": str(IID),
        "includeDescription": False,
        "includeJobs": False,
    }
    assert {"description", "description_truncated"}.isdisjoint(data)
    assert data["title"] == "Rebuild support"


@pytest.mark.parametrize("description_limit", [-1, DESCRIPTION_CAP + 1])
@respx.mock
async def test_description_limit_must_be_between_zero_and_cap(gitlab_env, description_limit):
    """The requested description limit cannot exceed the supported range."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=graphql_response(make_mr()))

    # Act / Assert
    with pytest.raises(ToolError):
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


@respx.mock
async def test_missing_project_is_reported(gitlab_env):
    """GitLab returns project null for unknown or invisible projects."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"project": None}}))

    # Act / Assert
    with pytest.raises(ToolError, match="not found, or the token cannot see it"):
        await call_tool()


@respx.mock
async def test_missing_merge_request_is_reported(gitlab_env):
    """A null mergeRequest names the IID and the project."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=graphql_response(None))

    # Act / Assert
    with pytest.raises(ToolError, match=r"Merge request !2187 not found in 'group/project'"):
        await call_tool()


@respx.mock
async def test_graphql_errors_are_reported(gitlab_env):
    """GraphQL errors reach the agent unchanged."""
    # Arrange
    body = {"data": None, "errors": [{"message": "Query is too complex"}]}
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=body))

    # Act / Assert
    with pytest.raises(ToolError, match="GitLab GraphQL error: Query is too complex"):
        await call_tool()
