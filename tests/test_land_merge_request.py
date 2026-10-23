"""Tests for the land_merge_request tool, run through a real in-memory MCP client 🚦."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp import mcp

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 2187
MR_URL = "https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/2187"
MERGE_URL = f"{MR_URL}/merge"
CANCEL_URL = f"{MR_URL}/cancel_merge_when_pipeline_succeeds"
HEAD_SHA = "6096f2d5a1b2c3d4e5f60718293a4b5c6d7e8f90"  # Full SHA, sent as a guard, so it must reach GitLab intact.
MERGE_SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"


def make_landing_node(**overrides: Any) -> dict[str, Any]:
    """Build the state fields that the read-back query returns. The default is an open MR with nothing queued."""
    node = {
        "state": "opened",
        "detailedMergeStatus": "MERGEABLE",
        "autoMergeStrategy": None,
        "mergeCommitSha": None,
        "targetBranch": "main",
        "diffRefs": {"headSha": HEAD_SHA},
        "headPipeline": {"status": "SUCCESS"},
    }
    return node | overrides


def graphql_response(node: dict[str, Any] | None) -> httpx.Response:
    """Wrap one merge request node in the project.mergeRequest envelope."""
    return httpx.Response(200, json={"data": {"project": {"mergeRequest": node}}})


def mock_write(mode: str, **response: Any) -> respx.Route:
    """Mock the write endpoint for `mode`. Cancel uses POST, and merge modes use PUT."""
    if mode == "cancel":
        return respx.post(CANCEL_URL).mock(**response)
    return respx.put(MERGE_URL).mock(**response)


async def call_land(**extra: Any) -> dict:
    """Call land_merge_request through the MCP protocol and return its data."""
    async with Client(mcp) as client:
        result = await client.call_tool("land_merge_request", {"project": PROJECT, "iid": IID, **extra})
    return result.structured_content


@pytest.mark.parametrize(
    ("mode", "squash", "remove_source_branch", "expected_body"),
    [
        ("auto", None, None, {"sha": HEAD_SHA, "auto_merge": True}),
        (
            "auto",
            True,
            False,
            {"sha": HEAD_SHA, "auto_merge": True, "squash": True, "should_remove_source_branch": False},
        ),
        ("now", None, None, {"sha": HEAD_SHA}),
        ("now", False, True, {"sha": HEAD_SHA, "squash": False, "should_remove_source_branch": True}),
    ],
    ids=["auto-defaults", "auto-all-flags", "now-defaults", "now-all-flags"],
)
@respx.mock
async def test_sends_merge_body_and_omits_unset_flags(gitlab_env, mode, squash, remove_source_branch, expected_body):
    """Each mode sends its body, and unset flags are left out."""
    # Arrange
    route = respx.put(MERGE_URL).mock(return_value=httpx.Response(200, json={"state": "opened"}))
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_landing_node()))

    # Act
    await call_land(sha=HEAD_SHA, mode=mode, squash=squash, remove_source_branch=remove_source_branch)

    # Assert
    assert json.loads(route.calls.last.request.content) == expected_body


@respx.mock
async def test_cancel_posts_to_cancel_endpoint(gitlab_env):
    """Cancel calls the cancel endpoint and reports a cleared queue, without the head SHA or pipeline."""
    # Arrange
    route = respx.post(CANCEL_URL).mock(return_value=httpx.Response(200, json={}))
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_landing_node()))

    # Act
    data = await call_land(mode="cancel")

    # Assert
    assert route.call_count == 1
    assert data == {"result": "cancelled", "state": "opened", "merge_status": "mergeable"}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"mode": "auto"}, "needs sha"),
        ({"mode": "auto", "sha": ""}, "needs sha"),
        ({"mode": "now"}, "needs sha"),
        ({"mode": "cancel", "sha": HEAD_SHA}, "takes only project and iid"),
        ({"mode": "cancel", "squash": True}, "takes only project and iid"),
        ({"mode": "cancel", "remove_source_branch": False}, "takes only project and iid"),
    ],
    ids=["auto-no-sha", "auto-empty-sha", "now-no-sha", "cancel-with-sha", "cancel-with-squash", "cancel-with-remove"],
)
@respx.mock
async def test_invalid_arguments_fail_before_any_request(gitlab_env, arguments, message):
    """Bad argument combinations fail before any request is sent."""
    # Arrange
    merge = respx.put(MERGE_URL).mock(return_value=httpx.Response(200, json={}))
    cancel = respx.post(CANCEL_URL).mock(return_value=httpx.Response(200, json={}))
    graphql = respx.post(GRAPHQL).mock(return_value=graphql_response(make_landing_node()))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_land(**arguments)
    assert not merge.called
    assert not cancel.called
    assert not graphql.called


@pytest.mark.parametrize(
    ("mode", "node_fields", "expected_result"),
    [
        ("auto", {"autoMergeStrategy": "merge_train"}, "merge_train"),
        ("auto", {"autoMergeStrategy": "add_to_merge_train_when_checks_pass"}, "merge_train"),
        ("auto", {"autoMergeStrategy": "ADD_TO_MERGE_TRAIN_WHEN_CHECKS_PASS"}, "merge_train"),
        ("auto", {"autoMergeStrategy": "merge_when_checks_pass"}, "auto_merge"),
        ("auto", {}, "pending"),
        ("now", {}, "pending"),
        ("now", {"state": "merged", "mergeCommitSha": MERGE_SHA}, "merged"),
        ("auto", {"state": "merged", "autoMergeStrategy": "merge_train"}, "merged"),
        ("cancel", {}, "cancelled"),
    ],
    ids=[
        "train",
        "train-add-when-checks-pass",
        "train-uppercase",
        "auto-merge",
        "auto-nothing-queued",
        "now-not-merged",
        "now-merged",
        "merged-beats-strategy",
        "cancel-cleared",
    ],
)
@respx.mock
async def test_maps_read_back_state_to_result(gitlab_env, mode, node_fields, expected_result):
    """The read-back state decides the result."""
    # Arrange
    mock_write(mode, return_value=httpx.Response(200, json={}))
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_landing_node(**node_fields)))

    # Act
    data = await call_land(mode=mode, sha=None if mode == "cancel" else HEAD_SHA)

    # Assert
    assert data["result"] == expected_result


@respx.mock
async def test_merge_train_result_names_the_queued_head_and_pipeline(gitlab_env):
    """A queued merge echoes the head SHA and the lowercased pipeline status that GitLab reports for it."""
    # Arrange
    respx.put(MERGE_URL).mock(return_value=httpx.Response(200, json={}))
    node = make_landing_node(autoMergeStrategy="MERGE_TRAIN", headPipeline={"status": "RUNNING"})
    respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_land(sha=HEAD_SHA)

    # Assert
    assert data == {
        "result": "merge_train",
        "state": "opened",
        "merge_status": "mergeable",
        "auto_merge_strategy": "merge_train",
        "head_sha": HEAD_SHA,
        "pipeline_status": "running",
    }


@pytest.mark.parametrize(
    ("node_fields", "absent_field"),
    [({"headPipeline": None}, "pipeline_status"), ({"diffRefs": None}, "head_sha")],
    ids=["no-pipeline", "no-diff-refs"],
)
@respx.mock
async def test_missing_head_sha_or_pipeline_is_left_out(gitlab_env, node_fields, absent_field):
    """A head with no pipeline, or GitLab with no diff refs, leaves that field out instead of guessing."""
    # Arrange
    respx.put(MERGE_URL).mock(return_value=httpx.Response(200, json={}))
    respx.post(GRAPHQL).mock(return_value=graphql_response(make_landing_node(**node_fields)))

    # Act
    data = await call_land(sha=HEAD_SHA)

    # Assert
    assert absent_field not in data


@respx.mock
async def test_returns_landing_fields_in_lowercase_and_omits_empty_ones(gitlab_env):
    """Strategy and status are lowercased, and empty fields are left out."""
    # Arrange
    respx.put(MERGE_URL).mock(return_value=httpx.Response(200, json={}))
    node = make_landing_node(autoMergeStrategy="MERGE_TRAIN")
    respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_land(sha=HEAD_SHA)

    # Assert
    assert data == {
        "result": "merge_train",
        "state": "opened",
        "merge_status": "mergeable",
        "auto_merge_strategy": "merge_train",
        "head_sha": HEAD_SHA,
        "pipeline_status": "success",
    }


@respx.mock
async def test_returns_merge_commit_sha_after_merge(gitlab_env):
    """A merged MR returns its merge commit SHA, with the head SHA and pipeline that were merged."""
    # Arrange
    respx.put(MERGE_URL).mock(return_value=httpx.Response(200, json={}))
    node = make_landing_node(state="merged", mergeCommitSha=MERGE_SHA)
    respx.post(GRAPHQL).mock(return_value=graphql_response(node))

    # Act
    data = await call_land(sha=HEAD_SHA, mode="now")

    # Assert
    assert data == {
        "result": "merged",
        "state": "merged",
        "merge_status": "mergeable",
        "merge_commit_sha": MERGE_SHA,
        "head_sha": HEAD_SHA,
        "pipeline_status": "success",
    }


@pytest.mark.parametrize("status", [405, 406, 409, 422])
@respx.mock
async def test_refused_merge_points_to_blockers(gitlab_env, status):
    """Refusal statuses keep GitLab's reason and point to the blockers tool."""
    # Arrange
    respx.put(MERGE_URL).mock(return_value=httpx.Response(status, json={"message": "Branch cannot be merged"}))

    # Act / Assert
    with pytest.raises(ToolError) as exc_info:
        await call_land(sha=HEAD_SHA)
    message = str(exc_info.value)
    assert f"HTTP {status}" in message
    assert "Branch cannot be merged" in message
    assert message.endswith("Check blockers with get_merge_request.")


@pytest.mark.parametrize("status", [403, 404, 500])
@respx.mock
async def test_other_api_errors_pass_through_without_hint(gitlab_env, status):
    """Other API errors keep GitLab's reason and get no blockers hint."""
    # Arrange
    respx.put(MERGE_URL).mock(return_value=httpx.Response(status, json={"message": "Nope"}))

    # Act / Assert
    with pytest.raises(ToolError, match=f"HTTP {status} for") as exc_info:
        await call_land(sha=HEAD_SHA)
    assert "Check blockers" not in str(exc_info.value)


@pytest.mark.parametrize("mode", ["auto", "now", "cancel"])
@respx.mock
async def test_connection_error_during_write_reports_unknown_outcome(gitlab_env, mode):
    """A connection failure during the write says the outcome is unknown, and never re-reads."""
    # Arrange
    mock_write(mode, side_effect=httpx.ConnectError("connection reset"))
    graphql = respx.post(GRAPHQL).mock(return_value=graphql_response(make_landing_node()))

    # Act / Assert
    with pytest.raises(ToolError, match=r"outcome is unknown\. Check get_merge_request before retrying\.") as exc_info:
        await call_land(mode=mode, sha=None if mode == "cancel" else HEAD_SHA)
    assert "connection reset" in str(exc_info.value)
    assert not graphql.called


@pytest.mark.parametrize(
    "reread",
    [
        {"return_value": httpx.Response(500, json={"message": "Boom"})},
        {"side_effect": httpx.ConnectError("connection reset")},
        {"return_value": graphql_response(None)},
    ],
    ids=["api-error", "connection-error", "mr-not-found"],
)
@respx.mock
async def test_failed_read_back_returns_pending_with_warning(gitlab_env, reread):
    """A failed read after an accepted write returns pending with a warning."""
    # Arrange
    respx.put(MERGE_URL).mock(return_value=httpx.Response(200, json={}))
    respx.post(GRAPHQL).mock(**reread)

    # Act
    data = await call_land(sha=HEAD_SHA)

    # Assert
    assert data["result"] == "pending"
    assert "accepted" in data["warning"]
    assert set(data) == {"result", "warning"}


PIPELINE_GID = "gid://gitlab/Ci::Pipeline/731"
EXPECTED_POSITION = 2  # The MR is second of three active cars.
TRAIN_MARKER = "mergeTrains"


def make_car(iid: int, status: str = "IDLE", pipeline_gid: str | None = PIPELINE_GID) -> dict[str, Any]:
    """Build one active merge train car in the shape of the TRAIN_POSITION_QUERY response."""
    pipeline = {"id": pipeline_gid} if pipeline_gid else None
    # GraphQL IIDs are strings, like the real response.
    return {"status": status, "mergeRequest": {"iid": str(iid)}, "pipeline": pipeline}


def make_train_page(*cars: dict[str, Any]) -> httpx.Response:
    """Wrap active cars in the project.mergeTrains envelope of the position query."""
    body = {"data": {"project": {"mergeTrains": {"nodes": [{"cars": {"nodes": list(cars)}}]}}}}
    return httpx.Response(200, json=body)


def mock_graphql(land_node: dict[str, Any], train_reply: httpx.Response | Exception) -> respx.Route:
    """Answer the train position query with `train_reply`, and every other GraphQL call with the land read-back."""

    def respond(request: httpx.Request) -> httpx.Response:
        if TRAIN_MARKER not in json.loads(request.content)["query"]:
            return graphql_response(land_node)
        if isinstance(train_reply, Exception):
            raise train_reply
        return train_reply

    return respx.post(GRAPHQL).mock(side_effect=respond)


def graphql_bodies(route: respx.Route, marker: str) -> list[dict[str, Any]]:
    """Return the GraphQL request bodies whose query text contains `marker`."""
    bodies = [json.loads(call.request.content) for call in route.calls]
    return [body for body in bodies if marker in body["query"]]


@pytest.mark.parametrize(
    ("pipeline_gid", "expected_pipeline_id"),
    [(PIPELINE_GID, 731), (None, None)],
    ids=["with-pipeline", "no-pipeline"],
)
@respx.mock
async def test_merge_train_result_reports_position_among_active_cars(gitlab_env, pipeline_gid, expected_pipeline_id):
    """The MR is second of three active cars, so its position is 2 and its car status and pipeline come along."""
    # Arrange
    respx.put(MERGE_URL).mock(return_value=httpx.Response(200, json={}))
    cars = [
        make_car(2186, status="MERGING"),
        make_car(IID, status="IDLE", pipeline_gid=pipeline_gid),
        make_car(2188, status="FRESH"),
    ]
    graphql = mock_graphql(make_landing_node(autoMergeStrategy="MERGE_TRAIN"), make_train_page(*cars))

    # Act
    data = await call_land(sha=HEAD_SHA)

    # Assert
    assert data["train_position"] == EXPECTED_POSITION
    assert data["train_status"] == "idle"
    assert data.get("train_pipeline_id") == expected_pipeline_id
    assert graphql_bodies(graphql, TRAIN_MARKER)[0]["variables"] == {"fullPath": PROJECT, "targetBranches": ["main"]}


@respx.mock
async def test_mr_missing_from_active_cars_leaves_train_fields_out(gitlab_env):
    """When the MR is not among the active cars, the result has no train fields and no warning."""
    # Arrange
    respx.put(MERGE_URL).mock(return_value=httpx.Response(200, json={}))
    cars = [make_car(2186), make_car(2188)]
    mock_graphql(make_landing_node(autoMergeStrategy="MERGE_TRAIN"), make_train_page(*cars))

    # Act
    data = await call_land(sha=HEAD_SHA)

    # Assert
    assert data == {
        "result": "merge_train",
        "state": "opened",
        "merge_status": "mergeable",
        "auto_merge_strategy": "merge_train",
        "head_sha": HEAD_SHA,
        "pipeline_status": "success",
    }


@pytest.mark.parametrize(
    "train_reply",
    [
        httpx.Response(500, json={"message": "Boom"}),
        httpx.Response(200, json={"errors": [{"message": "Boom"}]}),
        httpx.ConnectError("connection reset"),
    ],
    ids=["api-error", "graphql-error", "connection-error"],
)
@respx.mock
async def test_failed_position_read_keeps_the_landing_with_a_warning(gitlab_env, train_reply):
    """A failed position read leaves the land result intact and says the position is missing."""
    # Arrange
    respx.put(MERGE_URL).mock(return_value=httpx.Response(200, json={}))
    mock_graphql(make_landing_node(autoMergeStrategy="MERGE_TRAIN"), train_reply)

    # Act
    data = await call_land(sha=HEAD_SHA)

    # Assert
    assert data["result"] == "merge_train"
    assert data["auto_merge_strategy"] == "merge_train"
    assert "train_position" not in data
    assert "could not be read" in data["warning"]


@pytest.mark.parametrize(
    ("mode", "node_fields"),
    [
        ("auto", {"autoMergeStrategy": "MERGE_WHEN_CHECKS_PASS"}),
        ("auto", {}),
        ("now", {"state": "merged", "mergeCommitSha": MERGE_SHA}),
        ("auto", {"state": "merged", "autoMergeStrategy": "MERGE_TRAIN"}),
        ("cancel", {}),
    ],
    ids=["auto-merge", "nothing-queued", "now-merged", "merged-beats-train", "cancelled"],
)
@respx.mock
async def test_non_train_results_do_not_read_the_train(gitlab_env, mode, node_fields):
    """Only a merge_train result asks for the train position. Other results send just the read-back."""
    # Arrange
    mock_write(mode, return_value=httpx.Response(200, json={}))
    graphql = mock_graphql(make_landing_node(**node_fields), make_train_page(make_car(IID)))

    # Act
    await call_land(mode=mode, sha=None if mode == "cancel" else HEAD_SHA)

    # Assert
    assert graphql_bodies(graphql, TRAIN_MARKER) == []
    assert graphql.call_count == 1
