"""Tests for the land_merge_requests batch tool, run through a real in-memory MCP client 🚦."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp import create_app, mcp

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
MERGE_REQUESTS_URL = "https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests"
HEAD_SHA = "6096f2d5a1b2c3d4e5f60718293a4b5c6d7e8f90"
OTHER_SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
PIPELINE_GID = "gid://gitlab/Ci::Pipeline/731"


def merge_url(iid: int) -> str:
    """Return the REST merge endpoint of one merge request."""
    return f"{MERGE_REQUESTS_URL}/{iid}/merge"


def make_landing_node(**overrides: Any) -> dict[str, Any]:
    """Build the read-back state of an open MR that is queued for the train, unless overridden."""
    node = {
        "state": "opened",
        "detailedMergeStatus": "MERGEABLE",
        "autoMergeStrategy": "MERGE_TRAIN",
        "mergeCommitSha": None,
        "targetBranch": "main",
        "diffRefs": {"headSha": HEAD_SHA},
        "headPipeline": {"status": "SUCCESS"},
    }
    return node | overrides


def make_car(iid: int, status: str = "IDLE") -> dict[str, Any]:
    """Build one active merge train car in the shape of the TRAIN_POSITION_QUERY response."""
    return {"status": status, "mergeRequest": {"iid": str(iid)}, "pipeline": {"id": PIPELINE_GID}}


def mock_graphql(land_node: dict[str, Any], cars: list[dict[str, Any]]) -> respx.Route:
    """Answer the train position query with `cars`, and each land read-back with `land_node`."""

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "mergeTrains" in body["query"]:
            train = {"cars": {"nodes": cars}}
            return httpx.Response(200, json={"data": {"project": {"mergeTrains": {"nodes": [train]}}}})
        return httpx.Response(200, json={"data": {"project": {"mergeRequest": land_node}}})

    return respx.post(GRAPHQL).mock(side_effect=respond)


async def call_batch(**arguments: Any) -> dict[str, Any]:
    """Call land_merge_requests through the MCP protocol and return its data."""
    async with Client(mcp) as client:
        result = await client.call_tool("land_merge_requests", {"project": PROJECT, **arguments})
    return result.structured_content


@respx.mock
async def test_every_item_applied_with_train_positions(gitlab_env):
    """Each item lands in order, and each landing names its own position among the active cars."""
    # Arrange
    respx.put(merge_url(2187)).mock(return_value=httpx.Response(200, json={}))
    respx.put(merge_url(2188)).mock(return_value=httpx.Response(200, json={}))
    mock_graphql(make_landing_node(), [make_car(2186), make_car(2187), make_car(2188)])

    # Act
    data = await call_batch(items=[{"iid": 2187, "sha": HEAD_SHA}, {"iid": 2188, "sha": OTHER_SHA}])

    # Assert
    assert [(item["iid"], item["status"]) for item in data["results"]] == [(2187, "applied"), (2188, "applied")]
    assert [item["landing"]["train_position"] for item in data["results"]] == [2, 3]
    assert all("warning" not in item for item in data["results"])


@respx.mock
async def test_refused_item_does_not_stop_later_items(gitlab_env):
    """A 409 on the middle item marks that item failed with GitLab's reason, and the last item still lands."""
    # Arrange
    first = respx.put(merge_url(2186)).mock(return_value=httpx.Response(200, json={}))
    refused = respx.put(merge_url(2187)).mock(return_value=httpx.Response(409, json={"message": "Head moved"}))
    last = respx.put(merge_url(2188)).mock(return_value=httpx.Response(200, json={}))
    mock_graphql(make_landing_node(), [make_car(2186), make_car(2188)])

    # Act
    data = await call_batch(
        items=[{"iid": 2186, "sha": HEAD_SHA}, {"iid": 2187, "sha": HEAD_SHA}, {"iid": 2188, "sha": OTHER_SHA}],
    )

    # Assert
    assert [item["status"] for item in data["results"]] == ["applied", "failed", "applied"]
    failed = data["results"][1]
    assert "Head moved" in failed["warning"]
    assert failed["warning"].endswith("Check blockers with get_merge_request.")
    assert "landing" not in failed
    assert first.call_count == 1
    assert refused.call_count == 1
    assert last.call_count == 1


@respx.mock
async def test_connection_error_marks_item_unknown_and_is_not_retried(gitlab_env):
    """A connection failure during a write marks that item unknown, and the batch goes on without a retry."""
    # Arrange
    broken = respx.put(merge_url(2187)).mock(side_effect=httpx.ConnectError("connection reset"))
    respx.put(merge_url(2188)).mock(return_value=httpx.Response(200, json={}))
    mock_graphql(make_landing_node(), [make_car(2188)])

    # Act
    data = await call_batch(items=[{"iid": 2187, "sha": HEAD_SHA}, {"iid": 2188, "sha": OTHER_SHA}])

    # Assert
    assert [item["status"] for item in data["results"]] == ["unknown", "applied"]
    assert "connection reset" in data["results"][0]["warning"]
    assert "outcome is unknown" in data["results"][0]["warning"]
    assert broken.call_count == 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"items": [{"iid": 2187, "sha": HEAD_SHA}, {"iid": 2187, "sha": HEAD_SHA}]}, "Repeated IIDs: 2187"),
        ({"items": [{"iid": 2187, "sha": HEAD_SHA}, {"iid": 2188}]}, "needs sha"),
        ({"items": [{"iid": 2187, "sha": HEAD_SHA}], "mode": "cancel"}, "takes only project and iid"),
        ({"items": [{"iid": 2187}], "mode": "cancel", "squash": False}, "takes only project and iid"),
        ({"items": [{"iid": 2187}], "mode": "cancel", "remove_source_branch": False}, "takes only project and iid"),
        ({"items": []}, "items"),
        ({"items": [{"iid": n, "sha": HEAD_SHA} for n in range(1, 22)]}, "items"),
    ],
    ids=[
        "duplicate-iid",
        "missing-sha-later-item",
        "cancel-with-sha",
        "cancel-squash-false",
        "cancel-remove-false",
        "empty-batch",
        "too-many-items",
    ],
)
@respx.mock
async def test_invalid_batch_fails_before_any_request(gitlab_env, arguments, message):
    """A bad batch is refused before the first write, so the valid first item is never sent."""
    # Arrange
    catch_all = respx.route().mock(return_value=httpx.Response(200, json={}))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_batch(**arguments)
    assert catch_all.call_count == 0


@pytest.mark.parametrize("case", ["merged", "pending", "refused", "unknown", "cancelled"])
@respx.mock
async def test_compact_singleton_landing(gitlab_env, case):
    """The compact batch tool preserves singleton write and per-item outcomes."""
    # Arrange
    cancel = case == "cancelled"
    url = f"{MERGE_REQUESTS_URL}/2187/cancel_merge_when_pipeline_succeeds" if cancel else merge_url(2187)
    route = respx.post(url) if cancel else respx.put(url)
    if case == "unknown":
        route.mock(side_effect=httpx.ConnectError("connection reset"))
    elif case == "refused":
        route.mock(return_value=httpx.Response(409, json={"message": "Head moved"}))
    else:
        route.mock(return_value=httpx.Response(200, json={}))
        if case == "pending":
            respx.post(GRAPHQL).mock(return_value=httpx.Response(500))
        else:
            mock_graphql(
                make_landing_node(state="merged" if case == "merged" else "opened", autoMergeStrategy=None), []
            )
    arguments = {"project": PROJECT, "items": [{"iid": 2187} if cancel else {"iid": 2187, "sha": HEAD_SHA}]}
    if cancel:
        arguments["mode"] = "cancel"

    # Act
    async with Client(create_app("compact")) as client:
        response = await client.call_tool("land_merge_requests", arguments)

    # Assert
    assert not response.is_error
    item = response.structured_content["results"][0]
    assert item["iid"] == 2187
    assert item["status"] == {"refused": "failed", "unknown": "unknown"}.get(case, "applied")
    if case in {"merged", "pending", "cancelled"}:
        assert item["landing"]["result"] == case
    if case == "pending":
        assert "could not be read" in item["landing"]["warning"]
    if case in {"refused", "unknown"}:
        assert "landing" not in item
        assert ("Head moved" if case == "refused" else "outcome is unknown") in item["warning"]
    assert route.call_count == 1
    if not cancel:
        assert json.loads(route.calls.last.request.content) == {"sha": HEAD_SHA, "auto_merge": True}
