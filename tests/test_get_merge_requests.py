"""Tests for the compact get_merge_requests tool through an in-memory MCP client 🧪."""

import json
from typing import Any
lazy from collections.abc import Callable

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from gitlab_mcp.common import MAX_PAGES
from gitlab_mcp.tools.mr_read import DESCRIPTION_CAP
from mcp_helpers import call_tool as call_mcp_tool

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
HEAD_SHA = "6096f2d5a1b2c3d4e5f60718293a4b5c6d7e8f90"
MR_REST = "https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/{iid}"
HIDDEN_BLOCKERS = 2
COMMITS_BEHIND = 5


def make_mr(iid: int, **overrides: Any) -> dict[str, Any]:
    """Build one compact GraphQL merge request node."""
    node = {
        "iid": str(iid),
        "title": f"Merge request {iid}",
        "description": "A compact description.",
        "state": "opened",
        "draft": False,
        "sourceBranch": "feature/example",
        "targetBranch": "main",
        "author": {"username": "jdoe"},
        "diffRefs": {"headSha": HEAD_SHA},
        "detailedMergeStatus": "MERGEABLE",
        "shouldBeRebased": False,
        "conflicts": False,
        "approved": True,
        "approvedBy": {"nodes": [{"username": "reviewer"}]},
        "headPipeline": {"status": "SUCCESS"},
    }
    return node | overrides


def batch_response(*nodes: dict[str, Any] | None) -> httpx.Response:
    """Wrap alias nodes in the project response used by the batch query."""
    return httpx.Response(
        200,
        json={"data": {"project": {f"mr_{index}": node for index, node in enumerate(nodes)}}},
    )


def sent_request(route: respx.Route) -> dict[str, Any]:
    """Return the JSON GraphQL request body sent to GitLab."""
    return json.loads(route.calls.last.request.content)


def sent_chunks(route: respx.Route) -> list[list[str]]:
    """Return the IID values sent in each GraphQL request, in request order."""
    return [
        [value for key, value in json.loads(call.request.content)["variables"].items() if key.startswith("iid")]
        for call in route.calls
    ]


def echo_batch(missing: frozenset[int] = frozenset()) -> Callable[[httpx.Request], httpx.Response]:
    """Answer each GraphQL request with one node per requested IID, or null for IIDs in `missing`."""

    def respond(request: httpx.Request) -> httpx.Response:
        """Build the alias response for the IIDs in one request."""
        variables = json.loads(request.content)["variables"]
        iids = [int(value) for key, value in variables.items() if key.startswith("iid")]
        return batch_response(*(None if iid in missing else make_mr(iid) for iid in iids))

    return respond


async def call_tool(iids: list[Any], **extra: Any) -> dict[str, Any]:
    """Call get_merge_requests with the default project and return its structured data."""
    return await call_mcp_tool("get_merge_requests", {"project": PROJECT, "iids": iids, **extra})


@respx.mock
async def test_deduplicates_in_order_and_reports_missing_iids(gitlab_env):
    """One aliased request preserves the first requested IID order, tracks missing MRs, and sends no activity reads."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=batch_response(make_mr(9), None, make_mr(4), make_mr(3)))
    activity_reads = mock_activity(9)

    # Act
    data = await call_tool([9, 2, 9, 4, 3])

    # Assert
    assert route.call_count == 1
    assert all(read.call_count == 0 for read in activity_reads.values())
    assert sent_request(route)["variables"] == {
        "fullPath": PROJECT,
        "includeDescription": False,
        "includeActivity": False,
        "iid0": "9",
        "iid1": "2",
        "iid2": "4",
        "iid3": "3",
    }
    assert [mr["iid"] for mr in data["merge_requests"]] == [9, 4, 3]
    assert data["missing_iids"] == [2]
    assert data["merge_requests"][0] == {
        "iid": 9,
        "title": "Merge request 9",
        "state": "opened",
        "author": "jdoe",
        "source_branch": "feature/example",
        "target_branch": "main",
        "head_sha": HEAD_SHA,
        "merge_status": "mergeable",
        "approved": True,
        "approved_by": ["reviewer"],
        "pipeline_status": "success",
    }

    query = sent_request(route)["query"]
    for index in range(4):
        assert f"mr_{index}: mergeRequest(iid: $iid{index})" in query
        assert f'"{[9, 2, 4, 3][index]}"' not in query
    assert "fragment CompactMergeRequest on MergeRequest" in query
    assert "diffRefs { headSha }" in query
    for excluded_selection in ("reviewers", "discussions", "notes", "diffStats", "resolvableDiscussionsCount"):
        assert excluded_selection not in query


def blockers_node(visible: list[int], hidden: int = 0) -> dict[str, Any]:
    """Build the aliased node of the blockers query for one merge request."""
    return {
        "blockingMergeRequests": {"visibleMergeRequests": [{"iid": str(iid)} for iid in visible], "hiddenCount": hidden}
    }


def serve_batch_with_blockers(
    statuses: dict[int, str],
    blockers: dict[int, dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the batch query with one node per IID, and the blockers query with the nodes in `blockers`."""

    def respond(request: httpx.Request) -> httpx.Response:
        """Route by query text. Blocker nodes are answered in request order, one alias per IID."""
        body = json.loads(request.content)
        iids = [int(value) for key, value in body["variables"].items() if key.startswith("iid")]
        if "blockingMergeRequests" in body["query"]:
            nodes = {f"mr_{index}": blockers[iid] for index, iid in enumerate(iids)}
            return httpx.Response(200, json={"data": {"project": nodes}})
        return batch_response(*(make_mr(iid, detailedMergeStatus=statuses[iid]) for iid in iids))

    return respond


@respx.mock
async def test_only_blocked_merge_requests_share_one_blockers_query(gitlab_env):
    """Two blocked MRs out of three share one blockers query, which asks for their IIDs only."""
    # Arrange
    statuses = {9: "BLOCKED_STATUS", 2: "MERGEABLE", 4: "BLOCKED_STATUS"}
    blockers = {9: blockers_node([100, 101]), 4: blockers_node([], hidden=HIDDEN_BLOCKERS)}
    route = respx.post(GRAPHQL).mock(side_effect=serve_batch_with_blockers(statuses, blockers))

    # Act
    data = await call_tool([9, 2, 4])

    # Assert
    blocker_calls = [
        call for call in route.calls if "blockingMergeRequests" in json.loads(call.request.content)["query"]
    ]
    assert len(blocker_calls) == 1
    assert json.loads(blocker_calls[0].request.content)["variables"] == {"fullPath": PROJECT, "iid0": "9", "iid1": "4"}
    first, unblocked, second = data["merge_requests"]
    assert first["blocked_by"] == [100, 101]
    assert "hidden_blockers" not in first
    assert {"blocked_by", "hidden_blockers"}.isdisjoint(unblocked)
    assert "blocked_by" not in second
    assert second["hidden_blockers"] == HIDDEN_BLOCKERS


@respx.mock
async def test_failed_blockers_query_keeps_the_batch(gitlab_env):
    """A failed blockers lookup leaves the blocker fields out, and the batch still returns every merge request."""

    # Arrange
    def respond(request: httpx.Request) -> httpx.Response:
        """Fail only the blockers query, so the main batch read is answered as usual."""
        if "blockingMergeRequests" in json.loads(request.content)["query"]:
            return httpx.Response(500, json={"message": "Boom"})
        return batch_response(make_mr(9, detailedMergeStatus="BLOCKED_STATUS"), make_mr(2))

    respx.post(GRAPHQL).mock(side_effect=respond)

    # Act
    data = await call_tool([9, 2])

    # Assert
    assert [mr["iid"] for mr in data["merge_requests"]] == [9, 2]
    assert "blocked_by" not in data["merge_requests"][0]
    assert "hidden_blockers" not in data["merge_requests"][0]


def serve_batch(overrides: dict[int, dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the batch query with one node per IID, with the fields in `overrides` changed for that IID."""

    def respond(request: httpx.Request) -> httpx.Response:
        """Build the alias response for the IIDs in one request."""
        iids = [int(value) for key, value in json.loads(request.content)["variables"].items() if key.startswith("iid")]
        return batch_response(*(make_mr(iid, **overrides.get(iid, {})) for iid in iids))

    return respond


@respx.mock
async def test_commits_behind_is_read_only_for_open_merge_requests(gitlab_env):
    """Each open MR gets one divergence read. A merged MR gets no read and no field."""
    # Arrange
    respx.post(GRAPHQL).mock(side_effect=serve_batch({2: {"state": "merged"}}))
    open_zero = respx.get(MR_REST.format(iid=9)).mock(
        return_value=httpx.Response(200, json={"diverged_commits_count": 0}),
    )
    merged = respx.get(MR_REST.format(iid=2)).mock(return_value=httpx.Response(200, json={}))
    open_five = respx.get(MR_REST.format(iid=4)).mock(
        return_value=httpx.Response(200, json={"diverged_commits_count": COMMITS_BEHIND}),
    )

    # Act
    data = await call_tool([9, 2, 4], include_commits_behind=True)

    # Assert
    assert open_zero.call_count == 1
    assert open_five.call_count == 1
    assert merged.call_count == 0
    assert [mr.get("commits_behind") for mr in data["merge_requests"]] == [0, None, COMMITS_BEHIND]
    assert "commits_behind" not in data["merge_requests"][1]


@respx.mock
async def test_commits_behind_is_not_read_unless_asked(gitlab_env):
    """Without the flag, no divergence read is sent and no MR gets the field."""
    # Arrange
    respx.post(GRAPHQL).mock(side_effect=serve_batch({}))
    rest = respx.get(MR_REST.format(iid=9)).mock(
        return_value=httpx.Response(200, json={"diverged_commits_count": 3}),
    )

    # Act
    data = await call_tool([9])

    # Assert
    assert rest.call_count == 0
    assert "commits_behind" not in data["merge_requests"][0]


@respx.mock
async def test_failed_commits_behind_read_leaves_only_that_merge_request_without_it(gitlab_env):
    """One refused divergence read leaves that MR without the field, and the other MRs keep theirs."""
    # Arrange
    respx.post(GRAPHQL).mock(side_effect=serve_batch({}))
    respx.get(MR_REST.format(iid=9)).mock(return_value=httpx.Response(500, json={"message": "Boom"}))
    respx.get(MR_REST.format(iid=4)).mock(
        return_value=httpx.Response(200, json={"diverged_commits_count": COMMITS_BEHIND})
    )

    # Act
    data = await call_tool([9, 4], include_commits_behind=True)

    # Assert
    first, second = data["merge_requests"]
    assert "commits_behind" not in first
    assert second["commits_behind"] == COMMITS_BEHIND


@respx.mock
async def test_more_than_twenty_raw_iids_are_allowed_when_unique_count_is_bounded(gitlab_env):
    """Repeated raw inputs do not count against the unique IID limit."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=batch_response(make_mr(7)))

    # Act
    data = await call_tool([7] * 25)

    # Assert
    assert route.call_count == 1
    assert sent_request(route)["variables"] == {
        "fullPath": PROJECT,
        "includeDescription": False,
        "includeActivity": False,
        "iid0": "7",
    }
    assert [mr["iid"] for mr in data["merge_requests"]] == [7]


@respx.mock
async def test_all_missing_iids_succeed(gitlab_env):
    """Null aliases produce an empty result list and keep all missing IIDs in request order."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=batch_response(None, None))

    # Act
    data = await call_tool([12, 8])

    # Assert
    assert route.call_count == 1
    assert data == {"merge_requests": [], "missing_iids": [12, 8]}


@pytest.mark.parametrize(
    ("iid_count", "expected_sizes"),
    [(1, [1]), (8, [8]), (9, [8, 1]), (20, [8, 8, 4])],
    ids=["one", "chunk-limit", "spills-to-second-query", "three-queries"],
)
@respx.mock
async def test_batches_are_split_into_queries_of_at_most_eight_iids(gitlab_env, iid_count, expected_sizes):
    """Each GraphQL query carries at most eight aliases, and every requested MR comes back in order."""
    # Arrange
    route = respx.post(GRAPHQL).mock(side_effect=echo_batch())
    iids = list(range(1, iid_count + 1))

    # Act
    data = await call_tool(iids)

    # Assert
    assert [len(chunk) for chunk in sent_chunks(route)] == expected_sizes
    assert [mr["iid"] for mr in data["merge_requests"]] == iids
    assert "missing_iids" not in data


@respx.mock
async def test_missing_iid_in_a_later_query_keeps_requested_order(gitlab_env):
    """Twenty reversed IIDs run as three queries, and a miss in the last query is still reported."""
    # Arrange
    missing = 2
    route = respx.post(GRAPHQL).mock(side_effect=echo_batch(missing=frozenset({missing})))
    requested = list(range(20, 0, -1))

    # Act
    data = await call_tool(requested)

    # Assert
    assert [len(chunk) for chunk in sent_chunks(route)] == [8, 8, 4]
    assert [chunk[0] for chunk in sent_chunks(route)] == ["20", "12", "4"]
    assert [mr["iid"] for mr in data["merge_requests"]] == [iid for iid in requested if iid != missing]
    assert data["missing_iids"] == [missing]


@pytest.mark.parametrize(
    "iids",
    [[], [0], [-1], [True], ["3"], list(range(1, 22))],
    ids=["empty", "zero", "negative", "bool", "string", "over-20-unique"],
)
@respx.mock
async def test_invalid_iid_lists_are_rejected_before_network(gitlab_env, iids):
    """Empty, non-positive, non-integer, and over-limit IID inputs make no request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=batch_response())

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(iids)
    assert route.call_count == 0


@pytest.mark.parametrize("description_limit", [-1, DESCRIPTION_CAP + 1])
@respx.mock
async def test_invalid_description_limits_are_rejected_before_network(gitlab_env, description_limit):
    """Description limits outside zero through 4000 make no request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=batch_response(make_mr(1)))

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool([1], description_limit=description_limit)
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("options", "expected_include", "expected_limit", "expected_truncated"),
    [
        ({"include_description": True, "description_limit": 3}, True, 3, True),
        ({"include_description": True}, True, DESCRIPTION_CAP, True),
        ({"include_description": True, "description_limit": 0}, False, 0, False),
        ({}, False, DESCRIPTION_CAP, False),
    ],
    ids=["capped", "default-limit", "zero-skips", "description-skipped-by-default"],
)
@respx.mock
async def test_description_is_conditional_capped_and_skipped_by_default(
    gitlab_env,
    options,
    expected_include,
    expected_limit,
    expected_truncated,
):
    """The directive receives the effective include flag and output obeys the cap."""
    # Arrange
    description = "d" * (DESCRIPTION_CAP + 1)
    route = respx.post(GRAPHQL).mock(return_value=batch_response(make_mr(1, description=description)))

    # Act
    data = await call_tool([1], **options)

    # Assert
    assert sent_request(route)["variables"]["includeDescription"] is expected_include
    query = sent_request(route)["query"]
    assert "description @include(if: $includeDescription)" in query
    mr = data["merge_requests"][0]
    if expected_include:
        assert len(mr["description"]) == expected_limit
        assert ("description_truncated" in mr) is expected_truncated
    else:
        assert {"description", "description_truncated"}.isdisjoint(mr)


@respx.mock
async def test_empty_fields_are_omitted_and_empty_missing_list_is_absent(gitlab_env):
    """False and empty optional facts follow the singular detail model's omission convention."""
    # Arrange
    node = make_mr(
        1,
        draft=False,
        author=None,
        diffRefs=None,
        detailedMergeStatus=None,
        shouldBeRebased=False,
        conflicts=False,
        approvedBy={"nodes": []},
        headPipeline=None,
    )
    respx.post(GRAPHQL).mock(return_value=batch_response(node))

    # Act
    data = await call_tool([1])

    # Assert
    mr = data["merge_requests"][0]
    omitted = {
        "draft",
        "author",
        "head_sha",
        "merge_status",
        "should_be_rebased",
        "conflicts",
        "approved_by",
        "pipeline_status",
    }
    assert omitted.isdisjoint(mr)
    assert "missing_iids" not in data
    assert mr["approved"] is True


@respx.mock
async def test_null_project_raises_project_not_found(gitlab_env):
    """A null project uses the existing project-not-found tool error."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"project": None}}))

    # Act / Assert
    with pytest.raises(ToolError, match=r"Project .* not found"):
        await call_tool([1])
    assert route.call_count == 1


@respx.mock
async def test_graphql_errors_propagate(gitlab_env):
    """A GraphQL error is not converted into a missing-IID result."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"errors": [{"message": "denied"}]}))

    # Act / Assert
    with pytest.raises(ToolError, match="denied"):
        await call_tool([1])
    assert route.call_count == 1


@respx.mock
async def test_needs_mr_pipeline_flags_open_merge_requests_with_no_head_pipeline(gitlab_env):
    """Only an open MR in ci_must_pass with no head pipeline at all gets the flag."""
    # Arrange
    waiting = {"detailedMergeStatus": "CI_MUST_PASS"}
    respx.post(GRAPHQL).mock(
        side_effect=serve_batch(
            {
                1: waiting | {"headPipeline": None},
                2: waiting,  # The default head pipeline passed, so GitLab is only catching up.
                3: waiting | {"headPipeline": {"status": "RUNNING"}},
                4: waiting | {"headPipeline": None, "state": "closed"},
            }
        )
    )

    # Act
    data = await call_tool([1, 2, 3, 4])

    # Assert
    assert [mr.get("needs_mr_pipeline", False) for mr in data["merge_requests"]] == [True, False, False, False]


ACTIVITY_AT = "2026-10-21T09:00:00.000Z"  # Raw GitLab form, with milliseconds.
ACTIVITY_AT_UTC = "2026-10-21T09:00:00Z"
PUSHED_AT = "2026-10-20T08:00:00.000Z"
PUSHED_AT_UTC = "2026-10-20T08:00:00Z"
ACTIVITY_FIELDS = {"updated_at", "last_pushed_at", "last_activity", "activity_incomplete"}
ACTIVITY_READS = ("versions", "notes", "resource_label_events", "resource_state_events", "resource_milestone_events")


def note(body: str, *, at: str = ACTIVITY_AT, system: bool = False) -> dict[str, Any]:
    """Build one REST note by jdoe, with the same created and updated time."""
    return {"body": body, "system": system, "author": {"username": "jdoe"}, "created_at": at, "updated_at": at}


def event(*, at: str = ACTIVITY_AT, **change: Any) -> dict[str, Any]:
    """Build one REST resource event by jdoe. `change` holds the event's own fields, like `action` and `label`."""
    return {"created_at": at, "user": {"username": "jdoe"}, **change}


def pages(*bodies: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    """Serve one REST list page per `page` query value, with X-Next-Page on every page but the last."""

    def respond(request: httpx.Request) -> httpx.Response:
        """Answer with the page named by the request's `page` query value."""
        page = int(request.url.params.get("page", "1"))
        next_page = str(page + 1) if page < len(bodies) else ""
        return httpx.Response(200, json=bodies[page - 1], headers={"X-Next-Page": next_page})

    return respond


def server_error(_request: httpx.Request) -> httpx.Response:
    """Answer any REST request with a 500, like a flaky GitLab read."""
    return httpx.Response(500, json={"message": "Boom"})


def mock_activity(iid: int, **reads: Callable[[httpx.Request], httpx.Response]) -> dict[str, respx.Route]:
    """Mock each activity read of one MR. Reads not named in `reads` return one empty page."""
    return {
        read: respx.get(f"{MR_REST.format(iid=iid)}/{read}").mock(side_effect=reads.get(read, pages([])))
        for read in ACTIVITY_READS
    }


@respx.mock
async def test_include_activity_reports_push_update_and_newest_activity_per_mr(gitlab_env):
    """Each MR gets its own newest activity, even when one is a label change and the other a comment."""
    # Arrange
    graphql = respx.post(GRAPHQL).mock(
        side_effect=serve_batch(
            {9: {"updatedAt": "2026-10-25T12:00:00.000Z"}, 4: {"updatedAt": "2026-10-27T10:00:00.000Z"}}
        ),
    )
    first_reads = mock_activity(
        9,
        versions=pages([{"created_at": PUSHED_AT}]),
        notes=pages([note("Looks good.")]),
        resource_label_events=pages([event(at="2026-10-25T12:00:00.000Z", action="add", label={"name": "tech-debt"})]),
    )
    mock_activity(
        4,
        versions=pages([{"created_at": "2026-10-26T07:00:00.000Z"}]),
        notes=pages([note("Please rebase.", at="2026-10-27T10:00:00.000Z")]),
        resource_label_events=pages([event(at="2026-10-22T10:00:00.000Z", action="remove", label={"name": "wip"})]),
    )

    # Act
    data = await call_tool([9, 4], include_activity=True)

    # Assert
    first, second = data["merge_requests"]
    assert first["updated_at"] == "2026-10-25T12:00:00Z"
    assert first["last_pushed_at"] == PUSHED_AT_UTC
    assert first["last_activity"] == {
        "at": "2026-10-25T12:00:00Z",
        "by": "jdoe",
        "kind": "label",
        "summary": "added label tech-debt",
    }
    assert second["last_activity"] == {
        "at": "2026-10-27T10:00:00Z",
        "by": "jdoe",
        "kind": "comment",
        "summary": "comment (14 chars)",
    }
    assert "activity_incomplete" not in first
    assert "activity_incomplete" not in second
    assert sent_request(graphql)["variables"]["includeActivity"] is True
    assert dict(first_reads["notes"].calls.last.request.url.params) == {
        "sort": "desc",
        "order_by": "created_at",
        "per_page": "20",
    }


@respx.mock
async def test_newest_activity_is_compared_by_instant_and_written_in_utc(gitlab_env):
    """A Z event newer than a +02:00 note wins, even though its text sorts lower. Output is UTC to the second."""
    # Arrange
    respx.post(GRAPHQL).mock(side_effect=serve_batch({9: {"updatedAt": "2026-10-25T12:00:00.123+00:00"}}))
    mock_activity(
        9,
        versions=pages([{"created_at": "2026-10-20T10:00:00.500+02:00"}]),  # 08:00:00 UTC
        notes=pages([note("Ship it.", at="2026-10-21T11:00:00+02:00")]),  # 09:00 UTC
        resource_label_events=pages([event(at="2026-10-21T10:00:00.000Z", action="add", label={"name": "tech-debt"})]),
    )

    # Act
    data = await call_tool([9], include_activity=True)

    # Assert
    mr = data["merge_requests"][0]
    assert mr["updated_at"] == "2026-10-25T12:00:00Z"
    assert mr["last_pushed_at"] == "2026-10-20T08:00:00Z"
    assert mr["last_activity"] == {
        "at": "2026-10-21T10:00:00Z",
        "by": "jdoe",
        "kind": "label",
        "summary": "added label tech-debt",
    }


@respx.mock
async def test_empty_activity_lists_leave_out_last_activity_and_the_flag(gitlab_env):
    """With no pushes, notes, or events, only updated_at from GraphQL comes back."""
    # Arrange
    respx.post(GRAPHQL).mock(side_effect=serve_batch({9: {"updatedAt": "2026-10-25T12:00:00.000Z"}}))
    mock_activity(9)

    # Act
    data = await call_tool([9], include_activity=True)

    # Assert
    mr = data["merge_requests"][0]
    assert mr["updated_at"] == "2026-10-25T12:00:00Z"
    assert {"last_pushed_at", "last_activity", "activity_incomplete"}.isdisjoint(mr)


@pytest.mark.parametrize(
    ("read", "payload", "kind", "summary"),
    [
        (
            "notes",
            note("line one\nline two " + "y" * 100, system=True),
            "system",
            "line one line two " + "y" * 61 + "…",
        ),
        ("resource_label_events", event(action="add", label=None), "label", "added label"),
        (
            "resource_milestone_events",
            event(action="add", milestone={"title": "v1.0"}),
            "milestone",
            "added milestone v1.0",
        ),
        ("resource_state_events", event(state="merged"), "state", "merged"),
    ],
    ids=["system-note-cut-to-one-line", "label-deleted", "milestone-added", "state-merged"],
)
@respx.mock
async def test_last_activity_summary_names_the_change(gitlab_env, read, payload, kind, summary):
    """Each kind of activity gets a short summary, and a deleted label leaves out its name."""
    # Arrange
    respx.post(GRAPHQL).mock(side_effect=serve_batch({}))
    mock_activity(9, **{read: pages([payload])})

    # Act
    data = await call_tool([9], include_activity=True)

    # Assert
    assert data["merge_requests"][0]["last_activity"] == {
        "at": ACTIVITY_AT_UTC,
        "by": "jdoe",
        "kind": kind,
        "summary": summary,
    }


@pytest.mark.parametrize(
    ("page_count", "expected_calls", "expected_incomplete"),
    [(2, 2, False), (MAX_PAGES + 1, MAX_PAGES, True)],
    ids=["follows-x-next-page-to-the-last-page", "stops-at-the-page-cap"],
)
@respx.mock
async def test_event_scan_takes_the_last_event_of_the_last_page_read(
    gitlab_env,
    page_count,
    expected_calls,
    expected_incomplete,
):
    """The scan follows X-Next-Page, keeps the last event of the last page read, and flags a cut-off scan."""
    # Arrange
    respx.post(GRAPHQL).mock(side_effect=serve_batch({}))
    state_pages = [[event(state=f"first-{page}"), event(state=f"last-{page}")] for page in range(1, page_count + 1)]
    routes = mock_activity(9, resource_state_events=pages(*state_pages))

    # Act
    data = await call_tool([9], include_activity=True)

    # Assert
    mr = data["merge_requests"][0]
    assert routes["resource_state_events"].call_count == expected_calls
    assert mr["last_activity"]["summary"] == f"last-{expected_calls}"
    assert ("activity_incomplete" in mr) is expected_incomplete


@pytest.mark.parametrize(
    ("failing_read", "expected_last_pushed_at"),
    [("versions", None), ("resource_state_events", PUSHED_AT_UTC)],
    ids=["versions-fail", "state-events-fail"],
)
@respx.mock
async def test_failed_activity_read_flags_the_merge_request_and_keeps_other_reads(
    gitlab_env,
    failing_read,
    expected_last_pushed_at,
):
    """A failed read flags activity_incomplete, but the other reads and the MR fields still return."""
    # Arrange
    respx.post(GRAPHQL).mock(side_effect=serve_batch({}))
    reads = {
        "versions": pages([{"created_at": PUSHED_AT}]),
        "resource_label_events": pages([event(action="add", label={"name": "tech-debt"})]),
    }
    reads[failing_read] = server_error
    mock_activity(9, **reads)

    # Act
    data = await call_tool([9], include_activity=True)

    # Assert
    mr = data["merge_requests"][0]
    assert mr["activity_incomplete"] is True
    assert mr.get("last_pushed_at") == expected_last_pushed_at
    assert mr["last_activity"]["summary"] == "added label tech-debt"
    assert mr["title"] == "Merge request 9"
