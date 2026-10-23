"""Tests for safe, sequential inline review-note posting through the MCP client 📝."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from gitlab_mcp.server import MAX_PAGES, mcp

BASE = "https://gitlab.example.com/api/v4"
PROJECT = "group/project"
ENCODED_PROJECT = "group%2Fproject"
IID = 2187
HEAD = "a" * 40
BASE_SHA = "b" * 40
START_SHA = "c" * 40
POSTED_NOTE_ID = 101
RESPONSE_NOTE_ID = 909
MISMATCHED_LINE = 99
TWO_POSTS = 2
THREE_PAGES = 3
OLD_CONTEXT_LINE = 8
NEW_CONTEXT_LINE = 11
MR_URL = f"{BASE}/projects/{ENCODED_PROJECT}/merge_requests/{IID}"
DIFF_URL = f"{MR_URL}/diffs"
DISCUSSIONS_URL = f"{MR_URL}/discussions"


def response(body: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    """Wrap a JSON payload in an HTTP response."""
    return httpx.Response(status, json=body, headers=headers)


def merge_request(
    *,
    sha: str = HEAD,
    base_sha: str = BASE_SHA,
    start_sha: str = START_SHA,
    head_sha: str = HEAD,
) -> dict[str, Any]:
    """Return the REST merge-request fields used by review-note preflight."""
    return {"sha": sha, "diff_refs": {"base_sha": base_sha, "start_sha": start_sha, "head_sha": head_sha}}


def diff_file(
    old_path: str,
    new_path: str | None = None,
    patch: str = "@@ -1 +1 @@\n-old\n+new",
    **flags: bool,
) -> dict[str, Any]:
    """Build one REST diff entry, including GitLab's old and new paths."""
    return {
        "old_path": old_path,
        "new_path": new_path or old_path,
        "new_file": False,
        "deleted_file": False,
        "renamed_file": old_path != (new_path or old_path),
        "diff": patch,
        **flags,
    }


def add_preflight(
    pages: list[tuple[list[dict[str, Any]], int | None]],
    *,
    merge_requests: list[dict[str, Any]] | None = None,
) -> tuple[respx.Route, respx.Route]:
    """Register fake merge-request and paged diff responses."""
    mr_responses = merge_requests or [merge_request(), merge_request()]
    mr_route = respx.get(MR_URL).mock(side_effect=[response(item) for item in mr_responses])
    page_responses = [
        response(files, headers={"X-Next-Page": str(next_page)} if next_page is not None else {})
        for files, next_page in pages
    ]
    diff_route = respx.get(DIFF_URL).mock(side_effect=page_responses)
    return mr_route, diff_route


def create_response(
    request: httpx.Request, *, discussion_id: str = "discussion-1", note_id: int = 101
) -> httpx.Response:
    """Return a successful discussion response that echoes the requested position."""
    payload = json.loads(request.content)
    return response(
        {
            "id": discussion_id,
            "notes": [{"id": note_id, "position": payload["position"]}],
        },
        status=201,
    )


def review_note(file: str, line: int, body: str = "Please check this.", **extra: Any) -> dict[str, Any]:
    """Build an inline note on a new-side line."""
    return {"file": file, "line": line, "body": body, **extra}


async def call_tool(
    *, notes: list[dict[str, Any]], head_sha: str = HEAD, validate_only: bool = False
) -> dict[str, Any]:
    """Call post_review_notes through the real in-memory MCP client."""
    result = await call_tool_result(notes=notes, head_sha=head_sha, validate_only=validate_only)
    return result.structured_content


async def call_tool_result(*, notes: list[dict[str, Any]], head_sha: str = HEAD, validate_only: bool = False) -> Any:
    """Call the tool and return its protocol result, including input validation errors."""
    async with Client(mcp) as client:
        return await client.call_tool(
            "post_review_notes",
            {
                "project": PROJECT,
                "iid": IID,
                "head_sha": head_sha,
                "notes": notes,
                "validate_only": validate_only,
            },
            raise_on_error=False,
        )


@respx.mock
async def test_validate_only_checks_new_and_old_side_positions_without_posting(gitlab_env):
    """Validation returns each exact position after both SHA checks, without writes."""
    # Arrange
    patch = "@@ -5 +5,2 @@\n context\n+added"
    mr_route, diff_route = add_preflight([([diff_file("src.py", patch=patch)], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [
        review_note("src.py", 6),
        {"file": "src.py", "old_line": 5, "body": "Review the existing line."},
    ]

    # Act
    data = await call_tool(notes=notes, validate_only=True)

    # Assert
    assert data["results"] == [
        {
            "index": 0,
            "status": "validated",
            "position": {"path": "src.py", "line": 6},
        },
        {
            "index": 1,
            "status": "validated",
            "position": {"path": "src.py", "line": 5},
        },
    ]
    assert "warning" not in data
    assert len(mr_route.calls) == TWO_POSTS
    assert len(diff_route.calls) == 1
    assert not post_route.called


@pytest.mark.parametrize("failure", ["invalid-line", "stale-sha", "invalid-body"])
@respx.mock
async def test_validate_only_failures_remain_unattempted_without_posting(gitlab_env, failure):
    """Validation failures keep unattempted results and never create discussions."""
    # Arrange
    mr_route, diff_route = add_preflight([([diff_file("src.py")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    head_sha = "d" * 40 if failure == "stale-sha" else HEAD
    note = review_note("src.py", 99 if failure == "invalid-line" else 1)
    if failure == "invalid-body":
        note["body"] = " \n\t"

    # Act
    data = await call_tool(notes=[note], head_sha=head_sha, validate_only=True)

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert data["warning"]
    assert not post_route.called
    if failure == "invalid-body":
        assert "Body preflight failed" in data["warning"]
        assert not mr_route.called
        assert not diff_route.called
    elif failure == "stale-sha":
        assert "stale" in data["warning"]
        assert len(mr_route.calls) == 1
        assert not diff_route.called
    else:
        assert "not a usable diff line" in data["warning"]
        assert len(mr_route.calls) == 1
        assert len(diff_route.calls) == 1


@respx.mock
async def test_validates_lines_and_derives_positions_from_added_context_removed_renamed_and_deleted_diffs(
    gitlab_env,
    tmp_path,
):
    """All requested lines map to real diff coordinates and use both paths for renames."""
    # Arrange
    added = diff_file("added.py", patch="@@ -0,0 +1,2 @@\n+one\n+two", new_file=True)
    context = diff_file("context.py", patch="@@ -5 +5 @@\n keep")
    removed = diff_file("removed.py", patch="@@ -1,2 +1 @@\n same\n-gone")
    renamed = diff_file("old.py", "new.py", "@@ -7 +7 @@\n keep")
    deleted = diff_file("gone.py", patch="@@ -1,2 +0,0 @@\n-one\n-two", deleted_file=True)
    body_path = tmp_path / "review.txt"
    body_path.write_text("Read from the server.\n", encoding="utf-8")
    add_preflight([([added, context, removed, renamed, deleted], None)])
    expected_positions = [
        {
            "position_type": "text",
            "base_sha": BASE_SHA,
            "start_sha": START_SHA,
            "head_sha": HEAD,
            "old_path": "added.py",
            "new_path": "added.py",
            "old_line": None,
            "new_line": 2,
        },
        {
            "position_type": "text",
            "base_sha": BASE_SHA,
            "start_sha": START_SHA,
            "head_sha": HEAD,
            "old_path": "context.py",
            "new_path": "context.py",
            "old_line": 5,
            "new_line": 5,
        },
        {
            "position_type": "text",
            "base_sha": BASE_SHA,
            "start_sha": START_SHA,
            "head_sha": HEAD,
            "old_path": "removed.py",
            "new_path": "removed.py",
            "old_line": 2,
            "new_line": None,
        },
        {
            "position_type": "text",
            "base_sha": BASE_SHA,
            "start_sha": START_SHA,
            "head_sha": HEAD,
            "old_path": "old.py",
            "new_path": "new.py",
            "old_line": 7,
            "new_line": 7,
        },
        {
            "position_type": "text",
            "base_sha": BASE_SHA,
            "start_sha": START_SHA,
            "head_sha": HEAD,
            "old_path": "gone.py",
            "new_path": "gone.py",
            "old_line": 2,
            "new_line": None,
        },
    ]
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [
        review_note("added.py", 2),
        review_note("context.py", 5),
        {"file": "removed.py", "old_line": 2, "body": "This line was removed."},
        review_note("new.py", 7),
        {"file": "gone.py", "old_line": 2, "body_path": str(body_path)},
    ]

    # Act
    data = await call_tool(notes=notes)

    # Assert
    sent = [json.loads(call.request.content) for call in post_route.calls]
    assert [item["position"] for item in sent] == expected_positions
    assert sent[-1]["body"] == "Read from the server.\n"
    assert [item["status"] for item in data["results"]] == ["posted"] * 5
    assert data["results"][0]["position"] == {"path": "added.py", "line": 2}
    assert data["results"][2]["position"] == {"path": "removed.py", "old_line": 2}
    assert data["results"][3]["position"] == {"path": "new.py", "old_path": "old.py", "line": 7}
    assert data["results"][4]["position"] == {"path": "gone.py", "old_line": 2}
    assert data["results"][0]["note_id"] == POSTED_NOTE_ID
    assert data["results"][0]["discussion_id"] == "discussion-1"


@respx.mock
async def test_reads_all_file_bodies_before_any_gitlab_request(gitlab_env, tmp_path):
    """A bad later body path prevents even the preflight reads and all writes."""
    # Arrange
    first_body = tmp_path / "first.txt"
    first_body.write_text("valid body", encoding="utf-8")
    mr_route, diff_route = add_preflight([([diff_file("src.py")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(return_value=response({"id": "unused", "notes": []}, status=201))
    notes = [
        {"file": "src.py", "line": 1, "body_path": str(first_body)},
        {"file": "src.py", "line": 1, "body_path": str(tmp_path / "missing.txt")},
    ]

    # Act
    data = await call_tool(notes=notes)

    # Assert
    assert [item["status"] for item in data["results"]] == ["unattempted", "unattempted"]
    assert "Body preflight failed" in data["warning"]
    assert not mr_route.called
    assert not diff_route.called
    assert not post_route.called


@respx.mock
async def test_stale_head_sha_stops_before_diff_scan_or_post(gitlab_env):
    """The caller's SHA must match both REST head fields before diff scanning begins."""
    # Arrange
    mr_route, diff_route = add_preflight(
        [([diff_file("src.py")], None)],
        merge_requests=[merge_request()],
    )
    post_route = respx.post(DISCUSSIONS_URL).mock(return_value=response({"id": "unused", "notes": []}, status=201))

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)], head_sha="d" * 40)

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "stale" in data["warning"]
    assert data["current_head_sha"] == HEAD
    assert len(mr_route.calls) == 1
    assert not diff_route.called
    assert not post_route.called


@respx.mock
async def test_stale_head_sha_reports_source_sha_when_diff_head_differs(gitlab_env):
    """A stale supplied SHA reports the MR source SHA, not the diff-ref head SHA."""
    # Arrange
    source_sha = "d" * 40
    mr_route, diff_route = add_preflight(
        [([diff_file("src.py")], None)],
        merge_requests=[merge_request(sha=source_sha, head_sha=HEAD)],
    )
    post_route = respx.post(DISCUSSIONS_URL).mock(return_value=response({"id": "unused", "notes": []}, status=201))
    notes = [review_note("src.py", 1), review_note("src.py", 1, "Second note.")]

    # Act
    data = await call_tool(notes=notes, head_sha=HEAD)

    # Assert
    assert data["results"] == [
        {"index": 0, "status": "unattempted"},
        {"index": 1, "status": "unattempted"},
    ]
    assert data["current_head_sha"] == source_sha
    assert len(mr_route.calls) == 1
    assert not diff_route.called
    assert not post_route.called


@respx.mock
async def test_rechecks_diff_refs_after_scan_before_first_post(gitlab_env):
    """A head change during diff scanning stops the batch without posting."""
    # Arrange
    changed = merge_request(sha="d" * 40, head_sha="d" * 40)
    mr_route, _ = add_preflight(
        [([diff_file("src.py")], None)],
        merge_requests=[merge_request(), changed],
    )
    post_route = respx.post(DISCUSSIONS_URL).mock(return_value=response({"id": "unused", "notes": []}, status=201))

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    assert len(mr_route.calls) == TWO_POSTS
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "changed during preflight" in data["warning"]
    assert not post_route.called


@respx.mock
async def test_diff_scan_checks_all_pages_to_confirm_path_is_unique(gitlab_env):
    """The scan checks every available page, even after finding the requested path."""
    # Arrange
    mr_route, diff_route = add_preflight(
        [
            ([diff_file("elsewhere.py")], 2),
            ([diff_file("wanted.py", patch="@@ -1 +1 @@\n keep")], 3),
            ([diff_file("unused.py")], None),
        ],
    )
    first_post = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    found = await call_tool(notes=[review_note("wanted.py", 1)])

    # Assert
    assert [item["status"] for item in found["results"]] == ["posted"]
    assert len(diff_route.calls) == THREE_PAGES
    assert len(mr_route.calls) == TWO_POSTS
    assert len(first_post.calls) == 1


@respx.mock
async def test_missing_path_scan_stops_at_page_cap(gitlab_env):
    """A path absent from a completed scan is rejected before any write."""
    # Arrange
    missing_pages: list[tuple[list[dict[str, Any]], int | None]] = [
        ([diff_file(f"other-{index}.py")], index + 2) for index in range(2)
    ]
    missing_pages[-1] = (missing_pages[-1][0], None)
    missing_mr, missing_diff = add_preflight(missing_pages)
    missing_post = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    missing = await call_tool(notes=[review_note("absent.py", 1)])

    # Assert
    assert [item["status"] for item in missing["results"]] == ["unattempted"]
    assert len(missing_diff.calls) == TWO_POSTS
    assert len(missing_mr.calls) == 1
    assert "absent.py" in missing["warning"]
    assert not missing_post.called


@respx.mock
async def test_same_path_on_later_page_is_rejected_as_ambiguous(gitlab_env):
    """A second old or new path match on a later page invalidates the full batch."""
    # Arrange
    mr_route, diff_route = add_preflight(
        [
            ([diff_file("wanted.py")], 2),
            ([diff_file("wanted.py")], None),
        ],
    )
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("wanted.py", 1)])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "ambiguous" in data["warning"]
    assert len(diff_route.calls) == TWO_POSTS
    assert len(mr_route.calls) == 1
    assert not post_route.called


@respx.mock
async def test_matching_path_at_page_cap_is_rejected_when_more_pages_remain(gitlab_env):
    """Finding a path on the last allowed page is insufficient when uniqueness is unknown."""
    # Arrange
    pages: list[tuple[list[dict[str, Any]], int | None]] = [
        ([diff_file("wanted.py")], index + 2) for index in range(MAX_PAGES)
    ]
    mr_route, diff_route = add_preflight(pages)
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("wanted.py", 1)])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "incomplete" in data["warning"]
    assert len(diff_route.calls) == MAX_PAGES
    assert len(mr_route.calls) == 1
    assert not post_route.called


@respx.mock
async def test_collapsed_patch_stops_all_notes_before_post(gitlab_env):
    """A collapsed patch cannot safely provide a discussion position."""
    # Arrange
    files = [
        diff_file("collapsed.py", "collapsed.py", "@@ -1 +1 @@\n-old\n+new", collapsed=True),
    ]
    add_preflight([(files, None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(
        notes=[review_note("collapsed.py", 1)],
    )

    # Assert
    assert [item["status"] for item in data["results"]] == ["unattempted"]
    assert "collapsed" in data["warning"]
    assert not post_route.called


@respx.mock
async def test_line_on_wrong_diff_side_stops_before_any_post(gitlab_env):
    """An added line has no old-side coordinate, so it cannot take old_line."""
    # Arrange
    add_preflight([([diff_file("added.py", patch="@@ -0,0 +1 @@\n+new", new_file=True)], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[{"file": "added.py", "old_line": 1, "body": "Wrong side."}])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "not a usable diff line" in data["warning"]
    assert not post_route.called


@respx.mock
async def test_http_error_stops_batch_and_preserves_earlier_posted_result(gitlab_env):
    """A rejected post is failed, earlier posts stay reported, and later notes are untouched."""
    # Arrange
    add_preflight([([diff_file("src.py", patch="@@ -1,3 +1,3 @@\n one\n two\n three")], None)])
    post_count = 0

    def reject_second_post(request: httpx.Request) -> httpx.Response:
        """Return one success, then an HTTP rejection for the second note."""
        nonlocal post_count
        post_count += 1
        if post_count == TWO_POSTS:
            return response({"message": "bad position"}, status=422)
        return create_response(request)

    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=reject_second_post)
    notes = [review_note("src.py", line, f"Note {line}") for line in (1, 2, 3)]

    # Act
    data = await call_tool(notes=notes)

    # Assert
    assert [item["status"] for item in data["results"]] == ["posted", "failed", "unattempted"]
    assert data["results"][0]["note_id"] == POSTED_NOTE_ID
    assert "note index 1" in data["warning"]
    assert len(post_route.calls) == TWO_POSTS


@pytest.mark.parametrize("status", [500, 502, 504, 408], ids=["500", "502", "504", "408-timeout"])
@respx.mock
async def test_server_http_errors_have_unknown_outcome_and_stop_batch(gitlab_env, status):
    """Server and timeout errors may follow a committed post, so they are not reported as rejected."""
    # Arrange
    add_preflight([([diff_file("src.py", patch="@@ -1,3 +1,3 @@\n one\n two\n three")], None)])
    post_count = 0

    def fail_second_post(request: httpx.Request) -> httpx.Response:
        """Return one success, then a server or timeout error for the second note."""
        nonlocal post_count
        post_count += 1
        if post_count == TWO_POSTS:
            return response({"message": "upstream response lost"}, status=status)
        return create_response(request)

    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=fail_second_post)
    notes = [review_note("src.py", line, f"Note {line}") for line in (1, 2, 3)]

    # Act
    data = await call_tool(notes=notes)

    # Assert
    assert [item["status"] for item in data["results"]] == ["posted", "unknown", "unattempted"]
    assert data["results"][0]["note_id"] == POSTED_NOTE_ID
    assert "outcome is unknown" in data["warning"]
    assert "rejected" not in data["warning"]
    assert len(post_route.calls) == TWO_POSTS


@respx.mock
async def test_transport_error_is_unknown_and_later_notes_are_not_attempted(gitlab_env):
    """A connection failure cannot prove whether GitLab created the current note."""
    # Arrange
    add_preflight([([diff_file("src.py", patch="@@ -1,3 +1,3 @@\n one\n two\n three")], None)])
    post_count = 0

    def disconnect_on_second_post(request: httpx.Request) -> httpx.Response:
        """Return one success, then raise a transport error for the second note."""
        nonlocal post_count
        post_count += 1
        if post_count == TWO_POSTS:
            raise httpx.ConnectError("connection closed", request=request)  # noqa: TRY003 - simulate network failure.
        return create_response(request)

    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=disconnect_on_second_post)
    notes = [review_note("src.py", line, f"Note {line}") for line in (1, 2, 3)]

    # Act
    data = await call_tool(notes=notes)

    # Assert
    assert [item["status"] for item in data["results"]] == ["posted", "unknown", "unattempted"]
    assert "outcome is unknown" in data["warning"]
    assert len(post_route.calls) == TWO_POSTS


@respx.mock
async def test_mismatched_success_response_is_unverified_with_returned_ids(gitlab_env):
    """A successful response at a different position is unverified and keeps available IDs."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])

    def mismatched_response(request: httpx.Request) -> httpx.Response:
        """Return IDs with a position that differs from the requested line."""
        payload = json.loads(request.content)
        position = {**payload["position"], "new_line": MISMATCHED_LINE}
        return response(
            {"id": "discussion-9", "notes": [{"id": RESPONSE_NOTE_ID, "position": position}]},
            status=201,
        )

    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=mismatched_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    result = data["results"][0]
    assert result["status"] == "unverified"
    assert result["note_id"] == RESPONSE_NOTE_ID
    assert result["discussion_id"] == "discussion-9"
    assert result["position"] == {"path": "src.py", "line": MISMATCHED_LINE}
    assert "did not match" in data["warning"]
    assert len(post_route.calls) == 1


@respx.mock
async def test_ambiguous_path_is_rejected_before_any_post(gitlab_env):
    """Two diff entries matching the same requested path are not guessed between."""
    # Arrange
    add_preflight([([diff_file("src.py"), diff_file("src.py")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "ambiguous" in data["warning"]
    assert not post_route.called


@pytest.mark.parametrize(
    "note",
    [
        {"file": "", "line": 1, "body": "Review."},
        {"file": "   ", "line": 1, "body": "Review."},
        {"file": "src.py", "line": 1, "old_line": 1, "body": "Review."},
        {"file": "src.py", "body": "Review."},
        {"file": "src.py", "line": 0, "body": "Review."},
        {"file": "src.py", "old_line": 0, "body": "Review."},
        {"file": "src.py", "line": 1, "body": "Review.", "body_path": "relative-review.txt"},
        {"file": "src.py", "line": 1},
    ],
    ids=[
        "empty-file",
        "blank-file",
        "both-line-sides",
        "missing-line-side",
        "zero-new-line",
        "zero-old-line",
        "both-body-sources",
        "missing-body",
    ],
)
@respx.mock
async def test_invalid_note_models_are_rejected_before_requests(gitlab_env, note):
    """The input model rejects blank paths and incomplete or conflicting note sources."""
    # Arrange
    mr_route = respx.get(MR_URL).mock(return_value=response(merge_request()))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    result = await call_tool_result(notes=[note])

    # Assert
    assert result.is_error
    assert not mr_route.called
    assert not post_route.called


@pytest.mark.parametrize("head_sha", ["a" * 39, "g" * 40, ""], ids=["short", "not-hex", "blank"])
@respx.mock
async def test_invalid_head_sha_returns_unattempted_without_requests(gitlab_env, head_sha):
    """The required head SHA must contain exactly 40 hexadecimal characters."""
    # Arrange
    mr_route = respx.get(MR_URL).mock(return_value=response(merge_request()))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)], head_sha=head_sha)

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "full 40-character" in data["warning"]
    assert not mr_route.called
    assert not post_route.called


@pytest.mark.parametrize(
    "body_kind",
    ["relative", "directory", "oversize", "invalid-utf8", "blank-file", "blank-inline", "oversize-inline"],
)
@respx.mock
async def test_note_bodies_must_be_regular_utf8_nonblank_and_bounded(gitlab_env, tmp_path, body_kind):
    """Both inline and file-backed bodies obey the same UTF-8 size and nonblank limits."""
    # Arrange
    body_path = tmp_path / "body.txt"
    note: dict[str, Any] = {"file": "src.py", "line": 1}
    if body_kind == "relative":
        note["body_path"] = "relative-body.txt"
    elif body_kind == "directory":
        note["body_path"] = str(tmp_path)
    elif body_kind == "oversize":
        body_path.write_bytes(b"x" * 100001)
        note["body_path"] = str(body_path)
    elif body_kind == "invalid-utf8":
        body_path.write_bytes(b"\xff")
        note["body_path"] = str(body_path)
    elif body_kind == "blank-file":
        body_path.write_text(" \n\t", encoding="utf-8")
        note["body_path"] = str(body_path)
    elif body_kind == "blank-inline":
        note["body"] = " \n\t"
    else:
        note["body"] = "x" * 100001
    mr_route = respx.get(MR_URL).mock(return_value=response(merge_request()))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[note])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "Body preflight failed" in data["warning"]
    assert not mr_route.called
    assert not post_route.called


@pytest.mark.parametrize(
    "payload",
    [
        {"sha": HEAD},
        {"sha": HEAD, "diff_refs": None},
        {"sha": HEAD, "diff_refs": {"base_sha": BASE_SHA, "start_sha": START_SHA}},
        ["unexpected response shape"],
    ],
    ids=["missing-refs", "null-refs", "missing-ref-sha", "malformed-mr"],
)
@respx.mock
async def test_missing_refs_or_malformed_merge_request_stops_preflight(gitlab_env, payload):
    """Preflight needs a valid REST MR object and all four full SHA values."""
    # Arrange
    mr_route = respx.get(MR_URL).mock(return_value=response(payload))
    diff_route = respx.get(DIFF_URL).mock(return_value=response([diff_file("src.py")]))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "preflight failed" in data["warning"]
    assert len(mr_route.calls) == 1
    assert not diff_route.called
    assert not post_route.called


@pytest.mark.parametrize(
    ("file_diff", "expected_warning"),
    [
        (diff_file("src.py", collapsed=True), "collapsed"),
        (diff_file("src.py", too_large=True), "too large"),
        (diff_file("src.py", patch=""), "no usable patch"),
    ],
    ids=["collapsed", "too-large", "empty-patch"],
)
@respx.mock
async def test_unavailable_patch_is_rejected_before_post(gitlab_env, file_diff, expected_warning):
    """Collapsed, oversized, and empty patches cannot provide a safe line position."""
    # Arrange
    add_preflight([([file_diff], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert expected_warning in data["warning"]
    assert not post_route.called


@pytest.mark.parametrize(
    ("patch", "note", "expected_warning"),
    [
        ("@@ malformed\n+line", review_note("src.py", 1), "invalid unified hunk header"),
        ("@@ -1,2 +1,2 @@\n one", review_note("src.py", 1), "incomplete unified hunk"),
        ("@@ -1,2 +1,2 @@\n one\n@@ -3 +3 @@\n next", review_note("src.py", 1), "incomplete unified hunk"),
        ("@@ -1 +1 @@\n one\n extra", review_note("src.py", 1), "more context lines"),
        ("@@ -0,0 +1 @@\n+one\n+extra", review_note("src.py", 1), "more added lines"),
        ("@@ -1 +0,0 @@\n-one\n-extra", {"file": "src.py", "old_line": 1, "body": "Review."}, "more removed lines"),
        ("@@ -1 +1 @@\n?unexpected", review_note("src.py", 1), "invalid line inside"),
        ("plain patch without a hunk", review_note("src.py", 1), "no usable unified patch"),
    ],
    ids=[
        "bad-header",
        "incomplete-at-end",
        "incomplete-before-next-hunk",
        "excess-context",
        "excess-added",
        "excess-removed",
        "invalid-hunk-line",
        "no-hunk",
    ],
)
@respx.mock
async def test_malformed_unified_hunk_is_rejected_before_post(gitlab_env, patch, note, expected_warning):
    """Malformed hunk headers and line counts do not produce guessed coordinates."""
    # Arrange
    add_preflight([([diff_file("src.py", patch=patch)], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[note])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert expected_warning in data["warning"]
    assert not post_route.called


@respx.mock
async def test_old_side_context_position_contains_both_coordinates(gitlab_env):
    """An old-side context line is a valid position and returns both coordinates."""
    # Arrange
    add_preflight([([diff_file("src.py", patch=f"@@ -{OLD_CONTEXT_LINE} +{NEW_CONTEXT_LINE} @@\n unchanged")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[{"file": "src.py", "old_line": OLD_CONTEXT_LINE, "body": "Review context."}])

    # Assert
    position = json.loads(post_route.calls[0].request.content)["position"]
    assert position["old_line"] == OLD_CONTEXT_LINE
    assert position["new_line"] == NEW_CONTEXT_LINE
    assert data["results"][0]["status"] == "posted"
    assert data["results"][0]["position"] == {"path": "src.py", "line": NEW_CONTEXT_LINE}


@pytest.mark.parametrize(
    ("separator", "suffix"),
    [
        ("\r", ""),
        ("\v", ""),
        ("\u2028", ""),
        ("\u2029", ""),
        ("", "\n"),
        ("", "\n\\ No newline at end of file"),
    ],
    ids=[
        "embedded-cr",
        "vertical-tab",
        "unicode-line-separator",
        "unicode-paragraph-separator",
        "trailing-lf",
        "no-newline-marker",
    ],
)
@respx.mock
async def test_added_line_content_delimiters_do_not_change_diff_line_parsing(gitlab_env, separator, suffix):
    """Only LF separates patch lines; file content controls and Git's marker stay in the line."""
    # Arrange
    patch = f"@@ -0,0 +1 @@\n+first{separator}second{suffix}"
    add_preflight([([diff_file("src.py", patch=patch, new_file=True)], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    position = json.loads(post_route.calls[0].request.content)["position"]
    assert position["new_line"] == 1
    assert position["old_line"] is None
    assert data["results"][0]["status"] == "posted"


@respx.mock
async def test_diff_entry_with_invalid_old_or_new_path_is_rejected(gitlab_env):
    """An incomplete diff path pair cannot be sent to GitLab as a discussion position."""
    # Arrange
    invalid_paths = diff_file("src.py")
    invalid_paths["new_path"] = None
    add_preflight([([invalid_paths], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "diff paths" in data["warning"]
    assert not post_route.called


@pytest.mark.parametrize(
    "response_kind",
    ["missing-position", "missing-discussion-id", "empty-notes", "non-object", "invalid-json"],
)
@respx.mock
async def test_malformed_success_response_is_unverified_and_stops_batch(gitlab_env, response_kind):
    """A 2xx response without verifiable position and IDs is never reported as posted."""
    # Arrange
    add_preflight([([diff_file("src.py", patch="@@ -1,2 +1,2 @@\n one\n two")], None)])

    def malformed_success(request: httpx.Request) -> httpx.Response:
        """Return a malformed successful response variant for the first requested note."""
        position = json.loads(request.content)["position"]
        if response_kind == "invalid-json":
            return httpx.Response(201, content=b"not-json")
        if response_kind == "non-object":
            return response(["unexpected response shape"], status=201)
        if response_kind == "empty-notes":
            return response({"id": "discussion-1", "notes": []}, status=201)
        note: dict[str, Any] = {"id": POSTED_NOTE_ID}
        if response_kind == "missing-position":
            return response({"id": "discussion-1", "notes": [note]}, status=201)
        note["position"] = position
        if response_kind == "missing-discussion-id":
            return response({"notes": [note]}, status=201)
        return response({"id": "discussion-1", "notes": [note]}, status=201)

    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=malformed_success)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1), review_note("src.py", 2)])

    # Assert
    assert [item["status"] for item in data["results"]] == ["unverified", "unattempted"]
    assert "note index 0" in data["warning"]
    assert len(post_route.calls) == 1


@respx.mock
async def test_missing_note_id_in_success_response_is_unverified(gitlab_env):
    """A matching position without a note ID is still an unverified success."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])

    def response_without_note_id(request: httpx.Request) -> httpx.Response:
        """Return a discussion ID and position without the created note ID."""
        position = json.loads(request.content)["position"]
        return response({"id": "discussion-1", "notes": [{"position": position}]}, status=201)

    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=response_without_note_id)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    result = data["results"][0]
    assert result["status"] == "unverified"
    assert result["discussion_id"] == "discussion-1"
    assert "note ID" in data["warning"]
    assert len(post_route.calls) == 1


@pytest.mark.parametrize("failed_endpoint", ["merge-request", "diffs"])
@respx.mock
async def test_preflight_http_error_stops_without_posting(gitlab_env, failed_endpoint):
    """An HTTP error during either read-only preflight request produces no writes."""
    # Arrange
    if failed_endpoint == "merge-request":
        mr_route = respx.get(MR_URL).mock(return_value=response({"message": "not found"}, status=404))
        diff_route = respx.get(DIFF_URL).mock(return_value=response([diff_file("src.py")]))
    else:
        mr_route = respx.get(MR_URL).mock(return_value=response(merge_request()))
        diff_route = respx.get(DIFF_URL).mock(return_value=response({"message": "temporarily unavailable"}, status=503))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "preflight failed" in data["warning"]
    assert mr_route.called
    if failed_endpoint == "merge-request":
        assert not diff_route.called
    else:
        assert diff_route.called
    assert not post_route.called


@respx.mock
async def test_missing_gitlab_configuration_stops_before_requests(monkeypatch):
    """A configuration error is returned as an unattempted batch instead of crashing the tool."""
    # Arrange
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    mr_route = respx.get(MR_URL).mock(return_value=response(merge_request()))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "setup failed" in data["warning"]
    assert not mr_route.called
    assert not post_route.called
