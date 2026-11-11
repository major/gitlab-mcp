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
SECOND_CACHED_NOTE_NEW_LINE = 2
PEOPLE_REQUESTS = 3
WARNING_CAP = 500
TWO_POSTS = 2
THREE_NOTES = 3
FOUR_NOTES = 4
POSTED_NOTE_ID = 101


@pytest.fixture
async def review_client(gitlab_env) -> AsyncIterator[Client]:
    """Keep one MCP lifespan open so review batch tokens remain available between calls."""
    async with Client(mcp) as client:
        yield client


def response(body: Any, *, status: int = 200) -> httpx.Response:
    """Wrap a payload in an HTTP response."""
    return httpx.Response(status, json=body)


def merge_request(*, sha: str = HEAD, head_sha: str = HEAD) -> dict[str, Any]:
    """Return the merge-request SHA fields used by review-note preflight."""
    return {"sha": sha, "diff_refs": {"base_sha": BASE_SHA, "start_sha": START_SHA, "head_sha": head_sha}}


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


def add_head_change(paths: tuple[str, ...] = ()) -> None:
    """Register the merge-base and compare reads for a stale head, as a fast-forward that changed `paths`."""
    repository = f"{BASE}/projects/{ENCODED_PROJECT}/repository"
    respx.get(f"{repository}/merge_base").mock(
        side_effect=lambda request: response({"id": request.url.params.get_list("refs[]")[0]})
    )
    compare = {"diffs": [{"old_path": path, "new_path": path} for path in paths], "compare_timeout": False}
    respx.get(f"{repository}/compare").mock(return_value=response(compare))


def add_preflight(*, mr_reply: Any = None) -> tuple[respx.Route, respx.Route]:
    """Register reusable MR and diff responses for validation and posting preflight."""
    add_head_change()
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
        {"id": "discussion-1", "notes": [{"id": POSTED_NOTE_ID, "position": payload.get("position", {})}]},
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


def assert_unavailable(result: Any, *, head: str | None = HEAD) -> dict[str, Any]:
    """Check the safe, structured response for an unavailable single-use batch."""
    assert not result.is_error
    data = result.structured_content
    assert "results" not in data
    assert data["batch_error"] == "unavailable"
    assert data.get("current_head_sha") == head
    warning = data["warning"].lower()
    assert "unavailable" in warning
    assert "this call attempted no writes" in warning
    assert "prior attempts may have posted" in warning
    assert "wait for any in-flight attempt to finish" in warning
    assert "check prior results and gitlab before resubmitting" in warning
    assert "if a previous attempt may have written" in warning
    assert "check the head and note positions before resubmitting" in warning
    assert "explicit approve for any changed final text" in warning
    assert len(warning) <= WARNING_CAP
    assert "ttl_seconds" not in data
    assert "batch_id" not in data
    return data


@respx.mock
async def test_position_only_batch_subset_overrides_and_extra(review_client: Client, tmp_path: Path) -> None:
    """Selected positions use approved overrides in selection order without requiring unselected bodies."""
    # Arrange
    add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    body_path = tmp_path / "body.md"
    body_path.write_text("file body", encoding="utf-8")
    notes = [{"file": "src.py", "line": 1}, {"file": "src.py", "old_line": 2}, {"file": "src.py"}]
    prepared = await call_review(review_client, **note_args(notes=notes, validate_only=True))

    # Act
    posted = await call_review(
        review_client,
        **note_args(
            batch_id=prepared.structured_content["batch_id"],
            indices=[2, 0],
            body_overrides=[{"index": 2, "body_path": str(body_path)}, {"index": 0, "body": "inline"}],
            extra_notes=[{"body": "extra"}],
        ),
    )

    # Assert
    assert not posted.is_error
    requests = [json.loads(call.request.content) for call in post_route.calls]
    assert [request["body"] for request in requests] == ["file body", "inline", "extra"]
    assert requests[0]["position"]["position_type"] == "file"
    assert requests[1]["position"]["new_line"] == 1
    assert posted.structured_content["posted_count"] == THREE_NOTES


@respx.mock
async def test_validated_batch_posts_frozen_bodies_and_positions(
    review_client: Client,
    tmp_path: Path,
) -> None:
    """A token posts frozen inline, file-level, and general notes without rereading body files or head_sha."""
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
    assert token_data["ttl_seconds"] == review_batches.ReviewBatchCache.TTL_SECONDS
    assert "batch_error" not in token_data
    assert token_data["validated_count"] == THREE_NOTES
    batch_id = token_data["batch_id"]
    changed_body_path.write_text("changed after validation", encoding="utf-8")
    deleted_body_path.unlink()
    post_args = note_args(batch_id=batch_id)
    del post_args["head_sha"]  # 🪙 The batch already stores the validated SHA.
    posted = await call_review(review_client, **post_args)

    # Assert
    assert not posted.is_error
    assert "ttl_seconds" not in posted.structured_content
    assert "batch_error" not in posted.structured_content
    assert posted.structured_content["posted_count"] == THREE_NOTES
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
async def test_batch_body_override_keeps_coordinates_and_resolves_fresh_position(review_client: Client) -> None:
    """A sparse body override keeps cached coordinates and resolves positions during posting preflight."""
    # Arrange
    current_patch = "@@ -1,3 +1,3 @@\n one\n two\n three"

    def current_diff(_: httpx.Request) -> httpx.Response:
        """Return the current patch while keeping the merge-request SHA bound."""
        return response([diff_file(patch=current_patch)])

    mr_route = respx.get(MR_URL).mock(return_value=response(merge_request()))
    diff_route = respx.get(DIFF_URL).mock(side_effect=current_diff)
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(
        review_client,
        **note_args(
            notes=[
                {"file": "src.py", "line": 1, "body": "original first"},
                {"file": "src.py", "line": 2, "body": "original second"},
            ],
            validate_only=True,
        ),
    )
    batch_id = prepared.structured_content["batch_id"]
    current_patch = "@@ -1,2 +1,3 @@\n+ inserted\n one\n two"

    # Act
    posted = await call_review(
        review_client,
        **note_args(
            batch_id=batch_id,
            body_overrides=[{"index": 0, "body": "updated first"}],
        ),
    )

    # Assert
    assert not posted.is_error
    requests = [json.loads(call.request.content) for call in post_route.calls]
    assert [request["body"] for request in requests] == ["updated first", "original second"]
    assert requests[0]["position"] == {
        "position_type": "text",
        "old_path": "src.py",
        "new_path": "src.py",
        "base_sha": BASE_SHA,
        "start_sha": START_SHA,
        "head_sha": HEAD,
        "old_line": None,
        "new_line": 1,
    }
    assert requests[1]["position"]["old_line"] == 1
    assert requests[1]["position"]["new_line"] == SECOND_CACHED_NOTE_NEW_LINE
    assert len(mr_route.calls) == MR_CALLS_FOR_VALIDATE_AND_POST
    assert len(diff_route.calls) == DIFF_CALLS_FOR_VALIDATE_AND_POST


@pytest.mark.parametrize("with_overrides", [True, False], ids=["overrides", "unchanged"])
@respx.mock
async def test_cached_batch_repreview_freezes_full_batch(
    review_client: Client, tmp_path: Path, with_overrides: bool
) -> None:
    """Re-preview consumes the old batch and freezes all final bodies in a new single-use batch."""
    # Arrange
    add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    people_route = respx.post(GRAPHQL).mock(return_value=response({}))
    body = tmp_path / "body.md"
    body.write_text("file replacement", encoding="utf-8")
    preview = tmp_path / "preview.md"
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"body": "first"}, {"body": "second"}, {"body": "third"}], validate_only=True),
    )
    old = prepared.structured_content["batch_id"]
    overrides = [{"index": 0, "body": "inline replacement"}, {"index": 2, "body_path": str(body)}]

    # Act
    result = await call_review(
        review_client,
        **note_args(
            batch_id=old,
            validate_only=True,
            preview_path=str(preview),
            body_overrides=overrides if with_overrides else None,
            add_reviewers=["alice"],
        ),
    )

    # Assert
    assert not result.is_error
    new = result.structured_content["batch_id"]
    assert new != old
    text = preview.read_text(encoding="utf-8")
    expected = ["inline replacement", "second", "file replacement"] if with_overrides else ["first", "second", "third"]
    assert result.structured_content["notes_summary"] == {
        "general": [{"index": index, "head": value} for index, value in enumerate(expected)]
    }
    for index, value in enumerate(expected):
        assert f"cached index {index}" in text
        assert value in text
    assert not post_route.called
    assert not people_route.called
    body.unlink()
    assert_unavailable(await call_review(review_client, **note_args(batch_id=old)))
    posted = await call_review(review_client, **note_args(batch_id=new))
    assert posted.structured_content["posted_count"] == THREE_NOTES
    assert "notes_summary" not in posted.structured_content
    assert [json.loads(call.request.content)["body"] for call in post_route.calls] == expected
    assert_unavailable(await call_review(review_client, **note_args(batch_id=new)))


@pytest.mark.parametrize("complete", [True, False], ids=["complete", "missing-body"])
@respx.mock
async def test_position_only_batch_repreview_requires_all_bodies(
    review_client: Client, tmp_path: Path, complete: bool
) -> None:
    """Position-only re-preview keeps indices stable, or rejects missing bodies before requests and keeps the batch."""
    # Arrange
    add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [{"file": "src.py", "line": 1}, {"file": "src.py", "old_line": 2}]
    prepared = await call_review(review_client, **note_args(notes=notes, validate_only=True))
    batch_id = prepared.structured_content["batch_id"]
    overrides = [{"index": 0, "body": "first"}]
    if complete:
        overrides.append({"index": 1, "body": "second"})
    preview = tmp_path / "positions.md"
    before = len(respx.calls)

    # Act
    result = await call_review(
        review_client,
        **note_args(batch_id=batch_id, validate_only=True, preview_path=str(preview), body_overrides=overrides),
    )

    # Assert
    assert not post_route.called
    if complete:
        assert not result.is_error
        data = result.structured_content
        assert "batch_indices" not in data
        text = preview.read_text(encoding="utf-8")
        for index, body in enumerate(("first", "second")):
            assert f"cached index {index}" in text
            assert body in text
        posted = await call_review(review_client, **note_args(batch_id=data["batch_id"]))
        assert posted.structured_content["posted_count"] == TWO_POSTS
        assert [json.loads(call.request.content)["body"] for call in post_route.calls] == ["first", "second"]
        assert_unavailable(await call_review(review_client, **note_args(batch_id=batch_id)))
    else:
        assert "Cached index 1" in assert_rejected(result)
        assert len(respx.calls) == before
        assert not preview.exists()
        # Nothing was attempted, so the same handle still works once every body is supplied. ♻️
        retried = await call_review(
            review_client,
            **note_args(
                batch_id=batch_id,
                body_overrides=[*overrides, {"index": 1, "body": "second"}],
            ),
        )
        assert retried.structured_content["posted_count"] == TWO_POSTS


@respx.mock
async def test_repreview_selects_and_extends_batch(review_client: Client, tmp_path: Path) -> None:
    """Re-preview can drop cached notes and add extras, renumbering the new batch in posting order 🔢."""
    # Arrange
    add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [{"body": "keep"}, {"body": "drop"}, {"file": "src.py", "line": 1}]
    prepared = await call_review(review_client, **note_args(notes=notes, validate_only=True))
    old = prepared.structured_content["batch_id"]
    preview = tmp_path / "preview.md"

    # Act
    result = await call_review(
        review_client,
        **note_args(
            batch_id=old,
            validate_only=True,
            preview_path=str(preview),
            indices=[2, 0],
            body_overrides=[{"index": 2, "body": "low note"}],
            extra_notes=[{"body": "new finding"}],
        ),
    )

    # Assert
    assert not result.is_error
    data = result.structured_content
    assert data["validated_count"] == THREE_NOTES
    assert data["positions_ok"] == [0]
    assert data["notes_summary"] == {
        "src.py": [{"index": 0, "line": 1, "head": "low note"}],
        "general": [{"index": 1, "head": "keep"}, {"index": 2, "head": "new finding"}],
    }
    text = preview.read_text(encoding="utf-8")
    assert "cached index 2" in text
    assert "drop" not in text
    assert not post_route.called
    assert_unavailable(await call_review(review_client, **note_args(batch_id=old)))
    posted = await call_review(review_client, **note_args(batch_id=data["batch_id"]))
    assert posted.structured_content["posted_count"] == THREE_NOTES
    assert [json.loads(call.request.content)["body"] for call in post_route.calls] == [
        "low note",
        "keep",
        "new finding",
    ]


@pytest.mark.parametrize(
    "extra",
    [
        {"preview_path": "relative.md"},
        {"validate_only": False},
    ],
    ids=["relative-path", "posting-preview"],
)
@respx.mock
async def test_repreview_early_rejection_preserves_batch(
    review_client: Client, tmp_path: Path, extra: dict[str, Any]
) -> None:
    """Invalid re-preview arguments fail before the old handle is claimed."""
    # Arrange
    add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(review_client, **note_args(notes=[{"body": "draft"}], validate_only=True))
    batch_id = prepared.structured_content["batch_id"]
    arguments: dict[str, Any] = {"validate_only": True, "preview_path": str(tmp_path / "preview.md"), **extra}

    # Act
    rejected = await call_review(review_client, **note_args(batch_id=batch_id, **arguments))
    posted = await call_review(review_client, **note_args(batch_id=batch_id))

    # Assert
    assert_rejected(rejected)
    assert posted.structured_content["posted_count"] == 1
    assert len(post_route.calls) == 1


@respx.mock
async def test_unavailable_batch_repreview_returns_warning_preview(review_client: Client, tmp_path: Path) -> None:
    """An unavailable handle produces a readable warning preview instead of an empty artifact."""
    # Arrange
    respx.get(MR_URL).mock(return_value=response(merge_request()))
    preview = tmp_path / "unavailable.md"

    # Act
    result = await call_review(
        review_client,
        **note_args(batch_id="unknown", validate_only=True, preview_path=str(preview)),
    )

    # Assert
    data = result.structured_content
    assert data["preview_path"] == str(preview)
    assert data["batch_error"] == "unavailable"
    assert "batch_id" not in data
    text = preview.read_text(encoding="utf-8")
    assert "No new batch was created" in text
    assert data["warning"] in text
    assert all(call.request.method == "GET" for call in respx.calls)


@respx.mock
async def test_repreview_failed_validation_keeps_old_batch(review_client: Client, tmp_path: Path) -> None:
    """A failed subset re-preview renumbers selected notes and extras but keeps the original batch ♻️."""
    # Arrange
    add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [{"body": "draft"}, {"body": "drop"}, {"file": "src.py", "line": 1, "body": "inline"}]
    prepared = await call_review(review_client, **note_args(notes=notes, validate_only=True))
    batch_id = prepared.structured_content["batch_id"]
    preview = tmp_path / "failure.md"

    # Act
    result = await call_review(
        review_client,
        **note_args(
            batch_id=batch_id,
            validate_only=True,
            preview_path=str(preview),
            indices=[2, 0],
            body_overrides=[{"index": 2, "body": "bad [truncated]"}],
            extra_notes=[{"file": "src.py", "line": 2, "body": "extra"}],
        ),
    )

    # Assert
    assert not result.is_error
    assert "batch_id" not in result.structured_content
    assert result.structured_content["validated_count"] == TWO_POSTS
    assert result.structured_content["positions_ok"] == [2]
    assert result.structured_content["results"][0]["index"] == 0
    assert result.structured_content["results"][0]["status"] == "failed"
    assert "Reason:" in preview.read_text(encoding="utf-8")
    assert result.structured_content.get("batch_retained") is True
    assert not post_route.called
    posted = await call_review(review_client, **note_args(batch_id=batch_id))
    assert posted.structured_content["posted_count"] == THREE_NOTES
    assert [json.loads(call.request.content)["body"] for call in post_route.calls] == ["draft", "drop", "inline"]


@respx.mock
async def test_batch_indices_post_in_supplied_order_and_keep_original_result_indices(review_client: Client) -> None:
    """A noncontiguous subset posts in caller order and reports full-batch indices."""
    # Arrange
    add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [{"file": "src.py", "line": line, "body": f"note {line}"} for line in (1, 2, 3)]
    prepared = await call_review(review_client, **note_args(notes=notes, validate_only=True))

    # Act
    posted = await call_review(
        review_client,
        **note_args(batch_id=prepared.structured_content["batch_id"], indices=[2, 0]),
    )

    # Assert
    assert not posted.is_error
    assert posted.structured_content["posted_count"] == TWO_POSTS
    requests = [json.loads(call.request.content) for call in post_route.calls]
    assert [request["body"] for request in requests] == ["note 3", "note 1"]


@pytest.mark.parametrize(
    "indices",
    [
        pytest.param([], id="empty"),
        pytest.param([0, 0], id="duplicate"),
        pytest.param([-1], id="negative"),
        pytest.param([2], id="out-of-range"),
    ],
)
@respx.mock
async def test_invalid_batch_indices_are_rejected_without_gitlab_writes(
    review_client: Client,
    indices: list[int],
) -> None:
    """Empty, duplicate, negative, or out-of-range selection indices cannot post notes."""
    # Arrange
    mr_route, diff_route = add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(
        review_client,
        **note_args(
            notes=[
                {"file": "src.py", "line": 1, "body": "first"},
                {"file": "src.py", "line": 2, "body": "second"},
            ],
            validate_only=True,
        ),
    )
    before = (len(mr_route.calls), len(diff_route.calls), len(post_route.calls))

    # Act
    rejected = await call_review(
        review_client,
        **note_args(batch_id=prepared.structured_content["batch_id"], indices=indices),
    )

    # Assert
    assert_rejected(rejected)
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == before


@respx.mock
async def test_subset_override_and_extra_notes_post_then_add_people(review_client: Client) -> None:
    """Overrides use original indices, extras post after the subset, and people follow every post."""
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
    notes = [{"file": "src.py", "line": line, "body": f"original {line}"} for line in (1, 2, 3)]
    prepared = await call_review(review_client, **note_args(notes=notes, validate_only=True))

    # Act
    posted = await call_review(
        review_client,
        **note_args(
            batch_id=prepared.structured_content["batch_id"],
            indices=[2, 0],
            body_overrides=[{"index": 2, "body": "replacement third"}],
            extra_notes=[{"file": "src.py", "line": 2, "body": "extra inline"}, {"body": "extra general"}],
            add_reviewers=["alice"],
            add_assignees=["bob"],
        ),
    )

    # Assert
    assert not posted.is_error
    assert posted.structured_content["posted_count"] == FOUR_NOTES
    assert posted.structured_content["note_ids"] == [POSTED_NOTE_ID] * FOUR_NOTES
    assert posted.structured_content["people"]["applied"] == ["add_reviewers", "add_assignees"]
    assert [json.loads(call.request.content)["body"] for call in post_route.calls] == [
        "replacement third",
        "original 1",
        "extra inline",
        "extra general",
    ]
    assert json.loads(post_route.calls[2].request.content)["position"]["new_line"] == SECOND_CACHED_NOTE_NEW_LINE
    request_order = [
        "discussion" if "query" not in json.loads(call.request.content) else "people"
        for call in respx.calls
        if call.request.method == "POST"
    ]
    assert request_order == ["discussion"] * 4 + ["people"] * 3
    assert len(people_route.calls) == PEOPLE_REQUESTS


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative-to-body-dir"])
@respx.mock
async def test_batch_body_path_override_reads_replacement_body(
    review_client: Client, tmp_path: Path, relative: bool
) -> None:
    """A cached note body can be replaced by a server-local body path, absolute or relative to body_dir."""
    # Arrange
    add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    body_path = tmp_path / "replacement.txt"
    body_path.write_text("replacement from path", encoding="utf-8")
    override = {"index": 0, "body_path": body_path.name if relative else str(body_path)}
    extra: dict[str, Any] = {"body_dir": str(tmp_path)} if relative else {}
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"file": "src.py", "line": 1, "body": "cached body"}], validate_only=True),
    )

    # Act
    posted = await call_review(
        review_client,
        **note_args(batch_id=prepared.structured_content["batch_id"], body_overrides=[override], **extra),
    )

    # Assert
    assert not posted.is_error
    assert json.loads(post_route.calls[0].request.content)["body"] == "replacement from path"


@respx.mock
async def test_stale_head_with_body_override_has_no_writes(review_client: Client) -> None:
    """Overrides do not bypass the fresh SHA check or trigger discussion writes on stale batches."""
    # Arrange
    live_sha = HEAD

    def current_mr(_: httpx.Request) -> httpx.Response:
        """Return the live SHA for each preflight check."""
        return response(merge_request(sha=live_sha))

    mr_route = respx.get(MR_URL).mock(side_effect=current_mr)
    diff_route = respx.get(DIFF_URL).mock(return_value=response([diff_file()]))
    add_head_change()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"file": "src.py", "line": 1, "body": "cached"}], validate_only=True),
    )
    live_sha = "d" * 40
    prior_reads = (len(mr_route.calls), len(diff_route.calls))

    # Act
    stale = await call_review(
        review_client,
        **note_args(
            batch_id=prepared.structured_content["batch_id"],
            body_overrides=[{"index": 0, "body": "replacement"}],
        ),
    )

    # Assert
    assert not stale.is_error
    assert "stale" in stale.structured_content["warning"].lower()
    assert (len(mr_route.calls), len(diff_route.calls)) == (prior_reads[0] + 1, prior_reads[1])
    assert not post_route.called


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param([{"index": -1, "body": "bad"}], id="negative-index"),
        pytest.param([{"index": 1, "body": "out of range"}], id="out-of-range-index"),
        pytest.param([{"index": 0}], id="missing-body-source"),
        pytest.param([{"index": 0, "body": "text", "body_path": "/server/note.txt"}], id="multiple-body-sources"),
        pytest.param(
            [{"index": 0, "body": "first"}, {"index": 0, "body": "duplicate"}],
            id="duplicate-index",
        ),
    ],
)
@respx.mock
async def test_invalid_body_overrides_have_no_gitlab_writes(
    review_client: Client,
    overrides: list[dict[str, Any]],
) -> None:
    """Negative, duplicate, or out-of-range override indices are rejected before GitLab writes."""
    # Arrange
    mr_route, diff_route = add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"file": "src.py", "line": 1, "body": "cached"}], validate_only=True),
    )
    prior = (len(mr_route.calls), len(diff_route.calls), len(post_route.calls))

    # Act
    result = await call_review(
        review_client,
        **note_args(batch_id=prepared.structured_content["batch_id"], body_overrides=overrides),
    )

    # Assert
    assert_rejected(result)
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == prior


@respx.mock
async def test_stale_batch_is_kept_and_repreviewed_on_the_new_head(review_client: Client, tmp_path: Path) -> None:
    """A stale post writes nothing and keeps the batch, so it can be re-checked on the new head without bodies 🔁."""
    # Arrange
    new_head = "d" * 40
    live_sha = HEAD

    def current_mr(_: httpx.Request) -> httpx.Response:
        """Return the current merge-request SHA when each preflight check runs."""
        return response(merge_request(sha=live_sha, head_sha=live_sha))

    respx.get(MR_URL).mock(side_effect=current_mr)
    diff_route = respx.get(DIFF_URL).mock(return_value=response([diff_file()]))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [{"file": "src.py", "line": 1, "body": "inline review"}, {"body": "general review"}]
    prepared = await call_review(review_client, **note_args(notes=notes, validate_only=True))
    batch_id = prepared.structured_content["batch_id"]
    live_sha = new_head
    add_head_change(paths=("src.py",))
    prior_diff_calls = len(diff_route.calls)

    # Act: a reordered stale post, then a re-preview of only the general note on the new head.
    stale = await call_review(review_client, **note_args(batch_id=batch_id, head_sha=None, indices=[1, 0]))
    repreview = await call_review(
        review_client,
        **note_args(
            batch_id=batch_id,
            head_sha=new_head,
            indices=[1],
            validate_only=True,
            preview_path=str(tmp_path / "repreview.md"),
        ),
    )

    # Assert
    data = stale.structured_content
    # Indices are cached batch indices, so the inline note stays 0 although it was sent second. 🔢
    assert data["unattempted"] == [1, 0]
    assert data["current_head_sha"] == new_head
    assert data.get("batch_retained") is True
    assert 0 < data["ttl_seconds"] <= review_batches.ReviewBatchCache.TTL_SECONDS
    assert data["affected_note_indices"] == [0]
    assert len(diff_route.calls) == prior_diff_calls
    assert not post_route.called
    new_batch = repreview.structured_content["batch_id"]
    assert repreview.structured_content["validated_count"] == 1
    posted = await call_review(review_client, **note_args(batch_id=new_batch, head_sha=new_head))
    assert posted.structured_content["posted_count"] == 1
    assert [json.loads(call.request.content)["body"] for call in post_route.calls] == ["general review"]
    assert_unavailable(await call_review(review_client, **note_args(batch_id=batch_id)), head=new_head)


@pytest.mark.parametrize("other_head", ["d" * 40, "invalid"], ids=["newer-head", "invalid-head"])
@respx.mock
async def test_posting_batch_with_another_head_keeps_batch(review_client: Client, other_head: str) -> None:
    """Posting with a head the batch was not validated on is refused before any request and keeps the batch."""
    # Arrange
    add_preflight()
    respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"file": "src.py", "line": 1, "body": "review"}], validate_only=True),
    )
    batch_id = prepared.structured_content["batch_id"]
    calls_before = len(respx.calls)

    # Act
    mismatch = await call_review(review_client, **note_args(batch_id=batch_id, head_sha=other_head))

    # Assert
    assert "still valid" in assert_rejected(mismatch).lower()
    assert len(respx.calls) == calls_before
    posted = await call_review(review_client, **note_args(batch_id=batch_id))
    assert posted.structured_content["posted_count"] == 1


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [("project", "group/other"), ("iid", IID + 1)],
    ids=["project", "iid"],
)
@respx.mock
async def test_changed_batch_binding_is_rejected_without_gitlab_requests(
    review_client: Client,
    changed_field: str,
    changed_value: Any,
) -> None:
    """A token is bound to its project and IID, and a mismatch consumes it before I/O."""
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
    assert "does not match" in assert_rejected(mismatch).lower()
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == before
    replay = await call_review(review_client, **note_args(batch_id=batch_id))
    assert_unavailable(replay)
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == (before[0] + 1, *before[1:])


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({}, id="neither-input"),
        pytest.param({"notes": [{"file": "src.py", "line": 1, "body": "review"}], "head_sha": None}, id="notes-no-sha"),
        pytest.param(
            {"notes": [{"file": "src.py", "line": 1, "body": "review"}], "batch_id": "token"},
            id="both-inputs",
        ),
        pytest.param({"batch_id": "token", "validate_only": True}, id="validate-token"),
        pytest.param(
            {
                "notes": [{"file": "src.py", "line": 1, "body": "review"}],
                "body_overrides": [{"index": 0, "body": "replacement"}],
            },
            id="overrides-with-notes",
        ),
        pytest.param(
            {"notes": [{"file": "src.py", "line": 1, "body": "review"}], "indices": [0]},
            id="indices-without-batch",
        ),
        pytest.param({"body_overrides": [{"index": 0, "body": "replacement"}]}, id="overrides-without-batch"),
        pytest.param(
            {"batch_id": "token", "validate_only": True, "body_overrides": [{"index": 0, "body": "replacement"}]},
            id="overrides-with-validation",
        ),
        pytest.param(
            {
                "notes": [{"file": "src.py", "line": 1, "body": "review"}],
                "extra_notes": [{"file": "src.py", "line": 2, "body": "extra"}],
            },
            id="extra-notes-without-batch",
        ),
        pytest.param(
            {"batch_id": "token", "validate_only": True, "extra_notes": [{"body": "extra"}]},
            id="extra-notes-with-validation",
        ),
        pytest.param(
            {"notes": [{"file": "src.py", "line": 1, "body_path": "01.md"}], "body_dir": "relative/notes"},
            id="relative-body-dir",
        ),
    ],
)
@respx.mock
async def test_invalid_input_combinations_fail_without_gitlab_requests(
    review_client: Client,
    arguments: dict[str, Any],
) -> None:
    """The caller supplies one input mode and a preview path for cached validation."""
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
async def test_invalid_extra_note_blocks_every_write_and_reports_its_index(review_client: Client) -> None:
    """One extra note with a bad line stops the cached notes and the good extra from posting."""
    # Arrange
    add_preflight()
    add_missing_blobs()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [{"file": "src.py", "line": line, "body": f"cached {line}"} for line in (1, 2)]
    prepared = await call_review(review_client, **note_args(notes=notes, validate_only=True))

    # Act
    result = await call_review(
        review_client,
        **note_args(
            batch_id=prepared.structured_content["batch_id"],
            extra_notes=[{"body": "good extra"}, {"file": "src.py", "line": 99, "body": "bad extra"}],
        ),
    )

    # Assert
    assert not result.is_error
    rows = result.structured_content["results"]
    assert [(row["index"], row["status"]) for row in rows] == [(3, "failed")]
    assert result.structured_content["validated_count"] == THREE_NOTES
    assert "line 99" in rows[0]["reason"]
    assert "No notes were posted" in result.structured_content["warning"]
    assert not post_route.called


@respx.mock
async def test_extra_notes_over_the_call_cap_consume_the_handle_without_writes(review_client: Client) -> None:
    """A cached note plus 20 extras exceeds the 20-note call cap, so nothing posts and the handle is used."""
    # Arrange
    mr_route, diff_route = add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(review_client, **note_args(notes=[{"body": "cached"}], validate_only=True))
    batch_id = prepared.structured_content["batch_id"]
    before = (len(mr_route.calls), len(diff_route.calls))

    # Act
    rejected = await call_review(
        review_client,
        **note_args(batch_id=batch_id, extra_notes=[{"body": f"extra {index}"} for index in range(20)]),
    )
    after_reject = (len(mr_route.calls), len(diff_route.calls))
    replayed = await call_review(review_client, **note_args(batch_id=batch_id))

    # Assert
    assert "at most 20" in assert_rejected(rejected)
    assert after_reject == before
    assert not post_route.called
    assert replayed.structured_content["batch_error"] == "unavailable"


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
    assert "ttl_seconds" not in result.structured_content
    assert result.structured_content["results"][0]["status"] == "failed"
    assert len(mr_route.calls) == MR_CALLS_PER_PREFLIGHT
    assert len(diff_route.calls) == 1
    assert not post_route.called


@respx.mock
async def test_unknown_and_replayed_tokens_are_rejected(review_client: Client) -> None:
    """Unknown and claimed tokens only trigger one diagnostic head read each."""
    # Arrange
    mr_route, diff_route = add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    unknown = await call_review(review_client, **note_args(batch_id="not-a-token"))
    assert_unavailable(unknown)
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
    assert_unavailable(replay)
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == (
        calls_after_first[0] + 1,
        *calls_after_first[1:],
    )


@respx.mock
async def test_expired_batch_token_only_reads_head(
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
    now += review_batches.ReviewBatchCache.TTL_SECONDS

    # Act
    expired = await call_review(review_client, **note_args(batch_id=batch_id))

    # Assert
    assert_unavailable(expired)
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == (prior[0] + 1, *prior[1:])


@respx.mock
async def test_batch_remains_claimable_just_before_expiration(
    review_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifetime metadata matches the fake-clock expiry boundary."""
    # Arrange
    now = 100.0
    monkeypatch.setattr(review_batches, "time", SimpleNamespace(monotonic=lambda: now))
    add_preflight()
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"file": "src.py", "line": 1, "body": "review"}], validate_only=True),
    )
    now += prepared.structured_content["ttl_seconds"] - 0.01

    # Act
    posted = await call_review(review_client, **note_args(batch_id=prepared.structured_content["batch_id"]))

    # Assert
    assert not posted.is_error
    assert posted.structured_content["posted_count"] == 1
    assert len(post_route.calls) == 1


@respx.mock
async def test_batch_that_expires_during_a_stale_call_is_not_kept(
    review_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch that expires while a call that wrote nothing is running is dropped, not put back ⏰."""
    # Arrange
    clock = {"now": 100.0}
    monkeypatch.setattr(review_batches, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    new_head = "d" * 40
    add_preflight()
    prepared = await call_review(
        review_client,
        **note_args(notes=[{"file": "src.py", "line": 1, "body": "review"}], validate_only=True),
    )
    clock["now"] += prepared.structured_content["ttl_seconds"] - 0.01

    def slow_stale_read(_: httpx.Request) -> httpx.Response:
        """Return a newer head after the batch's lifetime runs out."""
        clock["now"] += 1
        return response(merge_request(sha=new_head, head_sha=new_head))

    respx.get(MR_URL).mock(side_effect=slow_stale_read)

    # Act
    stale = await call_review(review_client, **note_args(batch_id=prepared.structured_content["batch_id"]))

    # Assert
    data = stale.structured_content
    assert data["unattempted"] == [0]
    assert "batch_retained" not in data
    assert "ttl_seconds" not in data
    replay = await call_review(review_client, **note_args(batch_id=prepared.structured_content["batch_id"]))
    assert_unavailable(replay, head=new_head)


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
    assert_unavailable(restarted)
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == (before[0] + 1, *before[1:])


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
    assert all(not result.is_error for result in (first, second))
    unavailable = next(result for result in (first, second) if result.structured_content.get("batch_error"))
    assert_unavailable(unavailable)
    posted = next(result for result in (first, second) if not result.structured_content.get("batch_error"))
    assert posted.structured_content["posted_count"] == 1
    assert len(post_route.calls) == 1
    assert len(mr_route.calls) == MR_CALLS_FOR_VALIDATE_AND_POST + 1
    assert len(diff_route.calls) == DIFF_CALLS_FOR_VALIDATE_AND_POST


@pytest.mark.parametrize(
    ("live_sha", "supplied_sha", "expected_status", "diff_refs"),
    [
        (HEAD.upper(), HEAD, "current", {}),
        ("D" * 40, HEAD.upper(), "stale", {"diff_refs": None}),
        ("D" * 40, None, "validate the notes again", {}),
    ],
    ids=["current-no-diff-refs", "stale-null-diff-refs", "omitted-sha"],
)
@respx.mock
async def test_unavailable_token_reports_live_sha_without_diff_refs(
    review_client: Client,
    live_sha: str,
    supplied_sha: str | None,
    expected_status: str,
    diff_refs: dict[str, Any],
) -> None:
    """A single MR read compares normalized SHAs without requiring pending diff refs."""
    # Arrange
    mr_route = respx.get(MR_URL).mock(return_value=response({"sha": live_sha, **diff_refs}))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    people_route = respx.post(GRAPHQL).mock(return_value=response({}))

    # Act
    result = await call_review(
        review_client,
        **note_args(
            batch_id="secret-batch-token",
            head_sha=supplied_sha,
            add_reviewers=["alice"],
            add_assignees=["bob"],
        ),
    )

    # Assert
    data = assert_unavailable(result, head=live_sha.lower())
    assert expected_status in data["warning"].lower()
    if expected_status == "current":
        assert "cached notes and positions remain unavailable" in data["warning"].lower()
    assert data["posted_count"] == 0
    assert "people" not in data
    assert "secret-batch-token" not in data["warning"]
    assert len(mr_route.calls) == 1
    assert len(respx.calls) == 1
    assert not post_route.called
    assert not people_route.called


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(response({}), id="missing-sha"),
        pytest.param(response({"sha": None}), id="null-sha"),
        pytest.param(response({"sha": 123}), id="numeric-sha"),
        pytest.param(response({"sha": "invalid secret"}), id="invalid-sha"),
        pytest.param(response([{"sha": HEAD}]), id="not-an-object"),
        pytest.param(httpx.Response(200, content=b"secret invalid json"), id="invalid-json"),
        pytest.param(response({"message": "secret API error"}, status=404), id="not-found"),
        pytest.param(httpx.ConnectError("secret connection error"), id="connection-error"),
    ],
)
@respx.mock
async def test_unavailable_token_with_unknown_head_safely_hides_diagnostic_failure(
    review_client: Client,
    reply: httpx.Response | Exception,
) -> None:
    """Malformed MR responses and read errors leave the head unknown without leaking details."""
    # Arrange
    route = respx.get(MR_URL)
    if isinstance(reply, Exception):
        route.mock(side_effect=reply)
    else:
        route.mock(return_value=reply)

    # Act
    result = await call_review(review_client, **note_args(batch_id="secret-batch-token"))

    # Assert
    data = assert_unavailable(result, head=None)
    assert "unknown" in data["warning"].lower()
    assert "secret" not in data["warning"].lower()
    assert len(route.calls) == 1
    assert len(respx.calls) == 1


@pytest.mark.parametrize("head_sha", ["", "a" * 39, "g" * 40], ids=["empty", "short", "not-hex"])
@respx.mock
async def test_unavailable_token_rejects_invalid_sha_without_a_read(review_client: Client, head_sha: str) -> None:
    """An unavailable token with an invalid supplied SHA never triggers diagnostic I/O."""
    # Arrange / Act
    result = await call_review(review_client, **note_args(batch_id="unknown", head_sha=head_sha))

    # Assert
    data = assert_unavailable(result, head=None)
    assert "unknown" in data["warning"].lower()
    assert "40-character" in data["warning"]
    assert not respx.calls


@respx.mock
async def test_unavailable_token_when_gitlab_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing client and cache still return safe unavailable recovery guidance."""
    # Arrange
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)

    # Act
    async with Client(mcp) as client:
        result = await call_review(client, **note_args(batch_id="unknown"))

    # Assert
    data = assert_unavailable(result, head=None)
    assert "unknown" in data["warning"].lower()
    assert not respx.calls


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
    assert posted.structured_content["posted_count"] == 1
    assert [row["status"] for row in posted.structured_content["results"]] == ["unknown"]
    assert posted.structured_content["unattempted"] == [2]
    assert "outcome is unknown" in posted.structured_content["warning"]
    assert len(post_route.calls) == DIFF_CALLS_FOR_VALIDATE_AND_POST
    assert not people_route.called
    assert len(mr_route.calls) == MR_CALLS_FOR_VALIDATE_AND_POST
    assert len(diff_route.calls) == DIFF_CALLS_FOR_VALIDATE_AND_POST
    replay = await call_review(review_client, **note_args(batch_id=batch_id))
    assert_unavailable(replay)
    assert len(post_route.calls) == SECOND_WRITE
    assert not people_route.called
    assert len(mr_route.calls) == MR_CALLS_FOR_VALIDATE_AND_POST + 1
    assert len(diff_route.calls) == DIFF_CALLS_FOR_VALIDATE_AND_POST


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
    assert posted.structured_content["posted_count"] == TWO_POSTS
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
    assert_unavailable(oldest)
    assert (len(mr_route.calls), len(diff_route.calls), len(post_route.calls)) == (before[0] + 1, *before[1:])
    latest = await call_review(review_client, **note_args(batch_id=tokens[-1]))
    assert not latest.is_error
    assert len(post_route.calls) == 1
