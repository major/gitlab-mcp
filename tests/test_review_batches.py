"""Behavioral tests for frozen, one-use review-note validation batches 📝."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote
lazy from collections.abc import AsyncIterator
lazy from pathlib import Path

import httpx
import pytest
import respx
from fastmcp import Client

from gitlab_mcp import mcp, review_batches

BASE = "https://gitlab.example.com/api/v4"
PROJECT = "group/project"
ENCODED_PROJECT = quote(PROJECT, safe="")
IID = 2187
HEAD = "a" * 40
BASE_SHA = "b" * 40
START_SHA = "c" * 40
MR_URL = f"{BASE}/projects/{ENCODED_PROJECT}/merge_requests/{IID}"
DIFF_URL = f"{MR_URL}/diffs"
DISCUSSIONS_URL = f"{MR_URL}/discussions"
GRAPHQL = "https://gitlab.example.com/api/graphql"
MR_CALLS_FOR_VALIDATE_AND_POST = 4
MR_CALLS_PER_PREFLIGHT = 2
DIFF_CALLS_FOR_VALIDATE_AND_POST = 2
SECOND_WRITE = 2
PEOPLE_REQUESTS = 3


@pytest.fixture
async def review_client(gitlab_env) -> AsyncIterator[Client]:
    """Keep one MCP lifespan open so review batch tokens remain available between calls."""
    async with Client(mcp) as client:
        yield client


def response(body: Any, *, status: int = 200) -> httpx.Response:
    """Wrap a payload in an HTTP response."""
    return httpx.Response(status, json=body)


def merge_request(*, sha: str = HEAD) -> dict[str, Any]:
    """Return the merge-request SHA fields used by review-note preflight."""
    return {"sha": sha, "diff_refs": {"base_sha": BASE_SHA, "start_sha": START_SHA, "head_sha": HEAD}}


def diff_file(path: str = "src.py", patch: str = "@@ -1,3 +1,3 @@\n one\n two\n three") -> dict[str, Any]:
    """Return one REST diff entry."""
    return {
        "old_path": path,
        "new_path": path,
        "new_file": False,
        "deleted_file": False,
        "renamed_file": False,
        "diff": patch,
    }


def add_preflight(*, mr_reply: Any = None) -> tuple[respx.Route, respx.Route]:
    """Register reusable MR and diff responses for validation and posting preflight."""
    mr_route = respx.get(MR_URL).mock(return_value=response(mr_reply or merge_request()))
    diff_route = respx.get(DIFF_URL).mock(return_value=response([diff_file()]))
    return mr_route, diff_route


def add_missing_blobs(path: str = "src.py") -> None:
    """Register safe 404 responses when an out-of-hunk line triggers content fallback."""
    for sha in (BASE_SHA, HEAD):
        url = f"{BASE}/projects/{ENCODED_PROJECT}/repository/files/{quote(path, safe='')}/raw?ref={sha}"
        respx.get(url).mock(return_value=httpx.Response(404, json={"message": "not found"}))


def create_response(request: httpx.Request) -> httpx.Response:
    """Return a successful discussion response that echoes the requested position."""
    payload = json.loads(request.content)
    return response(
        {"id": "discussion-1", "notes": [{"id": 101, "position": payload.get("position", {})}]},
        status=201,
    )


def note_args(
    *, notes: list[dict[str, Any]] | None = None, batch_id: str | None = None, **extra: Any
) -> dict[str, Any]:
    """Build review-tool arguments while omitting whichever mutually exclusive input is not supplied."""
    args: dict[str, Any] = {"project": PROJECT, "iid": IID, "head_sha": HEAD, **extra}
    if notes is not None:
        args["notes"] = notes
    if batch_id is not None:
        args["batch_id"] = batch_id
    return args


async def call_review(client: Client, **arguments: Any) -> Any:
    """Call post_review_notes through the persistent in-memory MCP client."""
    return await client.call_tool("post_review_notes", arguments, raise_on_error=False)


def error_text(result: Any) -> str:
    """Return readable text from an MCP error response."""
    return " ".join(getattr(item, "text", "") for item in result.content)


def assert_rejected(result: Any) -> str:
    """Assert a tool call failed and return its caller-facing error message."""
    assert result.is_error
    message = error_text(result)
    assert message
    return message


@respx.mock
async def test_validated_batch_posts_frozen_bodies_and_positions(
    review_client: Client,
    tmp_path: Path,
) -> None:
    """A token posts frozen inline, file-level, and general notes without rereading body files."""
    # Arrange
    mr_route, diff_route = add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    changed_body_path = tmp_path / "changed.txt"
    deleted_body_path = tmp_path / "deleted.txt"
    changed_body_path.write_text("original file body", encoding="utf-8")
    deleted_body_path.write_text("deleted file body", encoding="utf-8")
    notes = [
        {"file": "src.py", "line": 1, "body": "inline body"},
        {"file": "src.py", "body_path": str(changed_body_path)},
        {"body_path": str(deleted_body_path)},
    ]

    # Act
    prepared = await call_review(review_client, **note_args(notes=notes, validate_only=True))
    assert not prepared.is_error
    token_data = prepared.structured_content
    assert token_data["batch_id"]
    assert [row["status"] for row in token_data["results"]] == ["validated"] * 3
    batch_id = token_data["batch_id"]
    changed_body_path.write_text("changed after validation", encoding="utf-8")
    deleted_body_path.unlink()
    posted = await call_review(review_client, **note_args(batch_id=batch_id))

    # Assert
    assert not posted.is_error
    assert [row["status"] for row in posted.structured_content["results"]] == ["posted"] * 3
    requests = [json.loads(call.request.content) for call in post_route.calls]
    assert [request["body"] for request in requests] == ["inline body", "original file body", "deleted file body"]
    assert [request.get("position", {}) for request in requests] == [
        {
            "position_type": "text",
            "old_path": "src.py",
            "new_path": "src.py",
            "base_sha": BASE_SHA,
            "start_sha": START_SHA,
            "head_sha": HEAD,
            "old_line": 1,
            "new_line": 1,
        },
        {
            "position_type": "file",
            "old_path": "src.py",
            "new_path": "src.py",
            "base_sha": BASE_SHA,
            "start_sha": START_SHA,
            "head_sha": HEAD,
        },
        {},
    ]
    assert len(mr_route.calls) == MR_CALLS_FOR_VALIDATE_AND_POST
    assert len(diff_route.calls) == DIFF_CALLS_FOR_VALIDATE_AND_POST


@respx.mock
async def test_stale_live_head_consumes_token_without_writes(review_client: Client) -> None:
    """Posting reruns preflight and refuses a token if the live merge-request head changed."""
    # Arrange
    live_sha = HEAD

    def current_mr(_: httpx.Request) -> httpx.Response:
        """Return the current merge-request SHA when each preflight check runs."""
        return response(merge_request(sha=live_sha))

    mr_route = respx.get(MR_URL).mock(side_effect=current_mr)
    diff_route = respx.get(DIFF_URL).mock(return_value=response([diff_file()]))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"file": "src.py", "line": 1, "body": "review"}], validate_only=True),
    )
    batch_id = prepared.structured_content["batch_id"]
    live_sha = "d" * 40
    prior_mr_calls = len(mr_route.calls)
    prior_diff_calls = len(diff_route.calls)

    # Act
    stale = await call_review(review_client, **note_args(batch_id=batch_id))

    # Assert
    assert not stale.is_error
    assert stale.structured_content["results"] == [{"index": 0, "status": "unattempted"}]
    assert "stale" in stale.structured_content["warning"].lower()
    assert len(mr_route.calls) == prior_mr_calls + 1
    assert len(diff_route.calls) == prior_diff_calls
    assert not post_route.called
    replay = await call_review(review_client, **note_args(batch_id=batch_id))
    assert "send the notes again" in assert_rejected(replay).lower()


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [("project", "group/other"), ("iid", IID + 1), ("head_sha", "d" * 40)],
    ids=["project", "iid", "head-sha"],
)
@respx.mock
async def test_changed_batch_binding_is_rejected_without_gitlab_requests(
    review_client: Client,
    changed_field: str,
    changed_value: Any,
) -> None:
    """A token is bound to its project, IID, and SHA, and a mismatch consumes it before I/O."""
    # Arrange
    mr_route, diff_route = add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"file": "src.py", "line": 1, "body": "review"}], validate_only=True),
    )
    batch_id = prepared.structured_content["batch_id"]
    before = (len(mr_route.calls), len(diff_route.calls), len(post_route.calls))
    arguments = note_args(batch_id=batch_id)
    arguments[changed_field] = changed_value

    # Act
    mismatch = await call_review(review_client, **arguments)

    # Assert
    assert "send the notes again" in assert_rejected(mismatch).lower()
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == before
    replay = await call_review(review_client, **note_args(batch_id=batch_id))
    assert "send the notes again" in assert_rejected(replay).lower()
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == before


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({}, id="neither-input"),
        pytest.param(
            {"notes": [{"file": "src.py", "line": 1, "body": "review"}], "batch_id": "token"},
            id="both-inputs",
        ),
        pytest.param({"batch_id": "token", "validate_only": True}, id="validate-token"),
    ],
)
@respx.mock
async def test_invalid_input_combinations_fail_without_gitlab_requests(
    review_client: Client,
    arguments: dict[str, Any],
) -> None:
    """The caller supplies exactly one input mode, and tokens cannot be validation-only inputs."""
    # Arrange
    mr_route = respx.get(MR_URL).mock(return_value=response(merge_request()))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    result = await call_review(review_client, **note_args(**arguments))

    # Assert
    assert_rejected(result)
    assert not mr_route.called
    assert not post_route.called


@respx.mock
async def test_failed_validation_does_not_return_a_batch_token(review_client: Client) -> None:
    """A partially invalid review cannot be converted into a reusable posting token."""
    # Arrange
    mr_route, diff_route = add_preflight()
    add_missing_blobs()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    result = await call_review(
        review_client,
        **note_args(
            notes=[{"file": "src.py", "line": 99, "body": "not in the patch"}],
            validate_only=True,
        ),
    )

    # Assert
    assert not result.is_error
    assert "batch_id" not in result.structured_content
    assert result.structured_content["results"][0]["status"] == "failed"
    assert len(mr_route.calls) == MR_CALLS_PER_PREFLIGHT
    assert len(diff_route.calls) == 1
    assert not post_route.called


@respx.mock
async def test_unknown_and_replayed_tokens_are_rejected(review_client: Client) -> None:
    """Unknown tokens and tokens already claimed cannot trigger reads or writes."""
    # Arrange
    mr_route, diff_route = add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    unknown = await call_review(review_client, **note_args(batch_id="not-a-token"))
    assert "send the notes again" in assert_rejected(unknown).lower()
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"file": "src.py", "line": 1, "body": "review"}], validate_only=True),
    )
    batch_id = prepared.structured_content["batch_id"]

    # Act
    first = await call_review(review_client, **note_args(batch_id=batch_id))
    calls_after_first = (len(mr_route.calls), len(diff_route.calls), len(post_route.calls))
    replay = await call_review(review_client, **note_args(batch_id=batch_id))

    # Assert
    assert not first.is_error
    assert "send the notes again" in assert_rejected(replay).lower()
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == calls_after_first


@respx.mock
async def test_expired_batch_token_is_rejected_without_gitlab_requests(
    review_client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired token is rejected using a fake monotonic clock without sleeping."""
    # Arrange
    now = 100.0
    monkeypatch.setattr(review_batches, "time", SimpleNamespace(monotonic=lambda: now))
    mr_route, diff_route = add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"file": "src.py", "line": 1, "body": "review"}], validate_only=True),
    )
    batch_id = prepared.structured_content["batch_id"]
    prior = (len(mr_route.calls), len(diff_route.calls), len(post_route.calls))
    now += review_batches.ReviewBatchCache.TTL_SECONDS + 1

    # Act
    expired = await call_review(review_client, **note_args(batch_id=batch_id))

    # Assert
    assert "send the notes again" in assert_rejected(expired).lower()
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == prior


@respx.mock
async def test_token_does_not_survive_a_new_mcp_lifespan(gitlab_env) -> None:
    """The validation cache belongs to one server lifespan, not the process."""
    # Arrange
    mr_route, diff_route = add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    async with Client(mcp) as first_client:
        prepared = await call_review(
            first_client,
            **note_args(notes=[{"file": "src.py", "line": 1, "body": "review"}], validate_only=True),
        )
    batch_id = prepared.structured_content["batch_id"]
    before = (len(mr_route.calls), len(diff_route.calls), len(post_route.calls))

    # Act
    async with Client(mcp) as restarted_client:
        restarted = await call_review(restarted_client, **note_args(batch_id=batch_id))

    # Assert
    assert "send the notes again" in assert_rejected(restarted).lower()
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == before


@respx.mock
async def test_concurrent_calls_claim_a_token_once(review_client: Client) -> None:
    """Synchronous token claiming allows no more than one concurrent posting attempt."""
    # Arrange
    mr_route, diff_route = add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"file": "src.py", "line": 1, "body": "review"}], validate_only=True),
    )
    arguments = note_args(batch_id=prepared.structured_content["batch_id"])

    # Act
    first, second = await asyncio.gather(
        call_review(review_client, **arguments),
        call_review(review_client, **arguments),
    )

    # Assert
    assert sum(not result.is_error for result in (first, second)) == 1
    assert sum(result.is_error for result in (first, second)) == 1
    assert len(post_route.calls) == 1
    assert len(mr_route.calls) == MR_CALLS_FOR_VALIDATE_AND_POST
    assert len(diff_route.calls) == DIFF_CALLS_FOR_VALIDATE_AND_POST


@respx.mock
async def test_unknown_write_outcome_stops_batch_and_consumes_token(review_client: Client) -> None:
    """An uncertain write stops later notes, skips people changes, and consumes the token."""
    # Arrange
    mr_route, diff_route = add_preflight()
    write_count = 0

    def disconnect_second(request: httpx.Request) -> httpx.Response:
        """Succeed once, then simulate an uncertain connection failure."""
        nonlocal write_count
        write_count += 1
        if write_count == SECOND_WRITE:
            raise httpx.ConnectError("connection closed", request=request)  # noqa: TRY003
        return create_response(request)

    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=disconnect_second)
    people_route = respx.post(GRAPHQL).mock(return_value=response({"data": {"users": {"nodes": []}}}))
    notes = [{"file": "src.py", "line": line, "body": f"note {line}"} for line in (1, 2, 3)]
    prepared = await call_review(review_client, **note_args(notes=notes, validate_only=True))
    batch_id = prepared.structured_content["batch_id"]

    # Act
    posted = await call_review(
        review_client,
        **note_args(batch_id=batch_id, add_reviewers=["alice"], add_assignees=["bob"]),
    )

    # Assert
    assert not posted.is_error
    assert [row["status"] for row in posted.structured_content["results"]] == ["posted", "unknown", "unattempted"]
    assert "outcome is unknown" in posted.structured_content["warning"]
    assert len(post_route.calls) == DIFF_CALLS_FOR_VALIDATE_AND_POST
    assert not people_route.called
    assert len(mr_route.calls) == MR_CALLS_FOR_VALIDATE_AND_POST
    assert len(diff_route.calls) == DIFF_CALLS_FOR_VALIDATE_AND_POST
    replay = await call_review(review_client, **note_args(batch_id=batch_id))
    assert "send the notes again" in assert_rejected(replay).lower()


@respx.mock
async def test_people_changes_follow_every_successful_batch_post(review_client: Client) -> None:
    """Reviewers and assignees are updated only after every token-backed note posts."""
    # Arrange
    add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    def answer_people(request: httpx.Request) -> httpx.Response:
        """Return known users and successful reviewer or assignee mutations."""
        query = json.loads(request.content)["query"]
        if "users(" in query:
            return response({"data": {"users": {"nodes": [{"username": "alice"}, {"username": "bob"}]}}})
        mutation = "mergeRequestSetReviewers" if "mergeRequestSetReviewers" in query else "mergeRequestSetAssignees"
        return response({"data": {mutation: {"errors": []}}})

    people_route = respx.post(GRAPHQL).mock(side_effect=answer_people)
    notes = [
        {"file": "src.py", "line": 1, "body": "first"},
        {"file": "src.py", "line": 1, "body": "second"},
    ]
    prepared = await call_review(review_client, **note_args(notes=notes, validate_only=True))

    # Act
    posted = await call_review(
        review_client,
        **note_args(batch_id=prepared.structured_content["batch_id"], add_reviewers=["alice"], add_assignees=["bob"]),
    )

    # Assert
    assert not posted.is_error
    assert [row["status"] for row in posted.structured_content["results"]] == ["posted", "posted"]
    assert posted.structured_content["people"]["applied"] == ["add_reviewers", "add_assignees"]
    request_order = [
        "discussion" if "query" not in json.loads(call.request.content) else "people"
        for call in respx.calls
        if call.request.method == "POST"
    ]
    assert request_order == ["discussion", "discussion", "people", "people", "people"]
    assert len(post_route.calls) == DIFF_CALLS_FOR_VALIDATE_AND_POST
    assert len(people_route.calls) == PEOPLE_REQUESTS


@pytest.mark.parametrize("cache_limit", ["count", "bytes"], ids=["batch-count", "cache-bytes"])
@respx.mock
async def test_oldest_token_is_evicted_when_cache_limit_is_reached(
    review_client: Client,
    monkeypatch: pytest.MonkeyPatch,
    cache_limit: str,
) -> None:
    """The oldest cached batch is evicted by either the batch-count or byte limit."""
    # Arrange
    if cache_limit == "count":
        monkeypatch.setattr(review_batches.ReviewBatchCache, "MAX_BATCHES", 2)
        token_count = 3
    else:
        monkeypatch.setattr(review_batches.ReviewBatchCache, "MAX_BATCHES", 10)
        monkeypatch.setattr(review_batches.ReviewBatchCache, "MAX_CACHE_BYTES", 350)
        token_count = 4
    mr_route, diff_route = add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    tokens = []
    for index in range(token_count):
        prepared = await call_review(
            review_client,
            **note_args(notes=[{"file": "src.py", "line": 1, "body": f"n{index}"}], validate_only=True),
        )
        tokens.append(prepared.structured_content["batch_id"])
    before = (len(mr_route.calls), len(diff_route.calls), len(post_route.calls))

    # Act
    oldest = await call_review(review_client, **note_args(batch_id=tokens[0]))

    # Assert
    assert "send the notes again" in assert_rejected(oldest).lower()
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == before
    latest = await call_review(review_client, **note_args(batch_id=tokens[-1]))
    assert not latest.is_error
    assert len(post_route.calls) == 1
