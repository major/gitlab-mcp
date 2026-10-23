"""Tests for the compact get_merge_requests tool through an in-memory MCP client 🧪."""

import json
from typing import Any
lazy from collections.abc import Callable

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from gitlab_mcp.tools.mr_read import DESCRIPTION_CAP
from mcp_helpers import call_tool as call_mcp_tool

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
HEAD_SHA = "6096f2d5a1b2c3d4e5f60718293a4b5c6d7e8f90"


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
    """One aliased request preserves the first requested IID order and tracks missing MRs."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=batch_response(make_mr(9), None, make_mr(4), make_mr(3)))

    # Act
    data = await call_tool([9, 2, 9, 4, 3])

    # Assert
    assert route.call_count == 1
    assert sent_request(route)["variables"] == {
        "fullPath": PROJECT,
        "includeDescription": False,
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


@respx.mock
async def test_more_than_twenty_raw_iids_are_allowed_when_unique_count_is_bounded(gitlab_env):
    """Repeated raw inputs do not count against the unique IID limit."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=batch_response(make_mr(7)))

    # Act
    data = await call_tool([7] * 25)

    # Assert
    assert route.call_count == 1
    assert sent_request(route)["variables"] == {"fullPath": PROJECT, "includeDescription": False, "iid0": "7"}
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
