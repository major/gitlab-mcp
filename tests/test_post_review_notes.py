"""Tests for safe, sequential inline review-note posting through the MCP client 📝."""

import json
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import respx
from fastmcp import Client

from gitlab_mcp import mcp
from gitlab_mcp.common import MAX_PAGES
from mcp_helpers import call_tool_result as call_mcp_tool_result

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
FOUR_PREFLIGHT_REQUESTS = 4
THREE_PAGES = 3
REASON_CAP = 200
BLOB_CAP = 131_072
OLD_CONTEXT_LINE = 8
NEW_CONTEXT_LINE = 11
MIXED_INLINE_LINE = 2
MR_URL = f"{BASE}/projects/{ENCODED_PROJECT}/merge_requests/{IID}"
DIFF_URL = f"{MR_URL}/diffs"
DISCUSSIONS_URL = f"{MR_URL}/discussions"
GRAPHQL = "https://gitlab.example.com/api/graphql"
KNOWN_USERS = ("alice", "bob")
# GraphQL operations that change people, plus the username lookup. write_order() looks for these names.
PEOPLE_OPERATIONS = ("users(", "mergeRequestSetReviewers", "mergeRequestSetAssignees")
RAW_FILES_URL = f"{BASE}/projects/{ENCODED_PROJECT}/repository/files"


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
    pages: list[tuple[list[Any], int | None]],
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


def add_blobs(*files: tuple[str, str, bytes], status: int = 200) -> list[respx.Route]:
    """Register immutable raw-file responses and return their routes."""
    return [
        respx.get(f"{RAW_FILES_URL}/{quote(path, safe='')}/raw?ref={sha}").mock(
            return_value=httpx.Response(status, content=content)
        )
        for path, sha, content in files
    ]


def raw_blob_calls() -> list[Any]:
    """Return the raw repository-file requests made by the current test."""
    return [call for call in respx.calls if "/repository/files/" in str(call.request.url)]


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
    *, notes: list[dict[str, Any]], head_sha: str = HEAD, validate_only: bool = False, **arguments: Any
) -> dict[str, Any]:
    """Call post_review_notes through the real in-memory MCP client. Extra arguments are passed through."""
    result = await call_tool_result(notes=notes, head_sha=head_sha, validate_only=validate_only, **arguments)
    return result.structured_content


async def call_tool_result(
    *, notes: list[dict[str, Any]], head_sha: str = HEAD, validate_only: bool = False, **arguments: Any
) -> Any:
    """Call the tool and return its protocol result, including input validation errors."""
    return await call_mcp_tool_result(
        "post_review_notes",
        {
            "project": PROJECT,
            "iid": IID,
            "head_sha": head_sha,
            "notes": notes,
            "validate_only": validate_only,
            **arguments,
        },
        raise_on_error=False,
    )


def serve_people(request: httpx.Request) -> httpx.Response:
    """Answer the username lookup with the known users, and each people mutation with no errors."""
    query = json.loads(request.content)["query"]
    if "users(" in query:
        return response({"data": {"users": {"nodes": [{"username": name} for name in KNOWN_USERS]}}})
    payload = "mergeRequestSetReviewers" if "mergeRequestSetReviewers" in query else "mergeRequestSetAssignees"
    return response({"data": {payload: {"errors": []}}})


def graphql_variables(route: respx.Route, marker: str) -> list[dict[str, Any]]:
    """Return the variables of each GraphQL call whose query contains `marker`, in order."""
    bodies = [json.loads(call.request.content) for call in route.calls]
    return [body["variables"] for body in bodies if marker in body["query"]]


def write_order() -> list[str]:
    """Name each POST in the order respx received it, so tests can check that people changes come last.

    Discussion posts have no GraphQL query, so they are named "discussion".
    """
    names = []
    for call in respx.calls:
        if call.request.method != "POST":
            continue
        query = json.loads(call.request.content).get("query", "")
        names.append(next((name for name in PEOPLE_OPERATIONS if name in query), "discussion"))
    return names


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
    assert not raw_blob_calls()


@respx.mock
async def test_validate_only_stale_head_sha_remains_unattempted_without_posting(gitlab_env):
    """A stale head SHA is a batch-wide problem, so validation returns unattempted rows and no discussions."""
    # Arrange
    mr_route, diff_route = add_preflight([([diff_file("src.py")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)], head_sha="d" * 40, validate_only=True)

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert "stale" in data["warning"]
    assert len(mr_route.calls) == 1
    assert not diff_route.called
    assert not post_route.called


@pytest.mark.parametrize("validate_only", [True, False], ids=["validate-only", "post-mode"])
@respx.mock
async def test_every_bad_note_is_reported_and_no_note_is_posted(gitlab_env, validate_only):
    """A bad line and a missing path each fail their own row, and the valid note is not posted either."""
    # Arrange
    mr_route, diff_route = add_preflight([([diff_file("src.py")], None)])
    add_blobs(("src.py", BASE_SHA, b"old\n"), ("src.py", HEAD, b"new\n"), status=404)
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [
        review_note("src.py", 1, "Good note."),
        review_note("src.py", 99, "Bad line."),
        review_note("absent.py", 1, "Missing path."),
    ]

    # Act
    data = await call_tool(notes=notes, validate_only=validate_only)

    # Assert
    assert data["results"] == [
        {"index": 0, "status": "validated", "position": {"path": "src.py", "line": 1}},
        {
            "index": 1,
            "status": "failed",
            "reason": (
                "line 99 on the new side in 'src.py'; fallback rejected: old file content could not be read safely: "
                "GitLab returned HTTP 404. Nearest new-side diff lines: 1."
            ),
        },
        {
            "index": 2,
            "status": "failed",
            "reason": (
                f"Diff preflight could not find 'absent.py' within {MAX_PAGES} pages. "
                "Nearest line hints are unavailable; use a file-level note."
            ),
        },
    ]
    assert data["warning"] == "2 of 3 notes failed preflight. No notes were posted."
    assert len(mr_route.calls) == TWO_POSTS
    assert len(diff_route.calls) == 1
    assert not post_route.called


@respx.mock
async def test_body_failure_and_bad_line_are_both_reported(gitlab_env):
    """A bad body does not hide a bad diff line on another note, and each failed row explains itself."""
    # Arrange
    mr_route, diff_route = add_preflight([([diff_file("src.py")], None)])
    add_blobs(("src.py", BASE_SHA, b"old\n"), ("src.py", HEAD, b"new\n"), status=404)
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [
        {"file": "src.py", "line": 1, "body_path": "relative-body.txt"},
        review_note("src.py", 99),
        review_note("src.py", 1, "Good note."),
    ]

    # Act
    data = await call_tool(notes=notes)

    # Assert
    assert data["results"] == [
        {
            "index": 0,
            "status": "failed",
            "reason": "body_path must be an absolute server-local path",
            "position": {"path": "src.py", "line": 1},
        },
        {
            "index": 1,
            "status": "failed",
            "reason": (
                "line 99 on the new side in 'src.py'; fallback rejected: old file content could not be read safely: "
                "GitLab returned HTTP 404. Nearest new-side diff lines: 1."
            ),
        },
        {"index": 2, "status": "validated", "position": {"path": "src.py", "line": 1}},
    ]
    assert data["warning"] == "2 of 3 notes failed preflight. No notes were posted."
    assert len(mr_route.calls) == TWO_POSTS
    assert len(diff_route.calls) == 1
    assert not post_route.called


@respx.mock
async def test_file_level_note_is_validated_from_its_path_alone(gitlab_env):
    """A file-level note needs only its path in the diff, so validation returns just the path."""
    # Arrange
    _, diff_route = add_preflight([([diff_file("src.py", patch="")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(
        notes=[{"file": "src.py", "body": "Finding outside the hunks."}],
        validate_only=True,
    )

    # Assert
    assert data["results"] == [{"index": 0, "status": "validated", "position": {"path": "src.py"}}]
    assert "warning" not in data
    assert len(diff_route.calls) == 1
    assert not post_route.called


@pytest.mark.parametrize(
    "file_diff",
    [
        diff_file("src.py", patch="@@ -1 +1 @@\n-old\n+new"),
        diff_file("src.py", patch="", collapsed=True),
        diff_file("src.py", patch="", too_large=True),
    ],
    ids=["usable-patch", "collapsed", "too-large"],
)
@respx.mock
async def test_file_level_note_ignores_patch_usability(gitlab_env, file_diff):
    """Collapsed and too-large patches fail line notes only; a file-level note still validates."""
    # Arrange
    add_preflight([([file_diff], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[{"file": "src.py", "body": "File finding."}], validate_only=True)

    # Assert
    assert [item["status"] for item in data["results"]] == ["validated"]
    assert not post_route.called


@pytest.mark.parametrize("null_line_keys", [False, True], ids=["omitted-line-keys", "null-line-keys"])
@respx.mock
async def test_file_level_post_sends_file_position_and_verifies_echo(gitlab_env, null_line_keys):
    """The request has no line keys, and GitLab's echo verifies whether it omits or nulls them."""
    # Arrange
    add_preflight([([diff_file("old.py", "new.py", patch="")], None)])

    def echo_position(request: httpx.Request) -> httpx.Response:
        """Echo the requested file position, optionally with null line keys as GitLab may return them."""
        position = json.loads(request.content)["position"]
        if null_line_keys:
            position = {**position, "old_line": None, "new_line": None}
        return response(
            {"id": "discussion-1", "notes": [{"id": POSTED_NOTE_ID, "position": position}]},
            status=201,
        )

    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=echo_position)

    # Act
    data = await call_tool(notes=[{"file": "new.py", "body": "File-level finding."}])

    # Assert
    sent = json.loads(post_route.calls[0].request.content)["position"]
    assert sent == {
        "position_type": "file",
        "old_path": "old.py",
        "new_path": "new.py",
        "base_sha": BASE_SHA,
        "start_sha": START_SHA,
        "head_sha": HEAD,
    }
    assert data["results"] == [
        {
            "index": 0,
            "status": "posted",
            "discussion_id": "discussion-1",
        },
    ]
    assert "posted_count" not in data
    assert len(post_route.calls) == 1


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
    assert data["results"] == [
        {"index": index, "status": "posted", "discussion_id": "discussion-1"} for index in range(5)
    ]
    assert not raw_blob_calls()


@respx.mock
async def test_missing_body_file_fails_only_its_note_and_posts_nothing(gitlab_env, tmp_path):
    """A missing body file fails its own note; the valid note is validated but still not posted."""
    # Arrange
    first_body = tmp_path / "first.txt"
    first_body.write_text("valid body", encoding="utf-8")
    mr_route, diff_route = add_preflight([([diff_file("src.py")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [
        {"file": "src.py", "line": 1, "body_path": str(first_body)},
        {"file": "src.py", "line": 1, "body_path": str(tmp_path / "missing.txt")},
    ]

    # Act
    data = await call_tool(notes=notes)

    # Assert
    assert [item["status"] for item in data["results"]] == ["validated", "failed"]
    assert data["results"][1]["reason"].startswith("body_path could not be read: ")
    assert data["warning"] == "1 of 2 notes failed preflight. No notes were posted."
    assert len(mr_route.calls) == TWO_POSTS
    assert len(diff_route.calls) == 1
    assert not post_route.called


@respx.mock
async def test_long_body_read_reason_is_capped_at_200_characters(gitlab_env, tmp_path):
    """An OS error that quotes a long path keeps only the first 200 characters of its reason."""
    # Arrange
    long_path = tmp_path / ("a" * 190 + ".txt")
    note = {"file": "src.py", "line": 1, "body_path": str(long_path)}

    # Act
    data = await call_tool(notes=[note])

    # Assert
    reason = data["results"][0]["reason"]
    assert reason.startswith("body_path could not be read: ")
    assert len(reason) == REASON_CAP


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
async def test_missing_path_fails_its_note_after_a_complete_scan(gitlab_env):
    """A path absent from a completed scan fails only its note, and nothing is posted."""
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
    assert [item["status"] for item in missing["results"]] == ["failed"]
    assert "absent.py" in missing["results"][0]["reason"]
    assert missing["warning"] == "1 of 1 notes failed preflight. No notes were posted."
    assert len(missing_diff.calls) == TWO_POSTS
    assert len(missing_mr.calls) == TWO_POSTS
    assert not missing_post.called


@respx.mock
async def test_same_path_on_later_page_fails_its_note_as_ambiguous(gitlab_env):
    """A second old or new path match on a later page makes the note ambiguous, so nothing is posted."""
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
    assert data["results"] == [
        {
            "index": 0,
            "status": "failed",
            "reason": (
                "Diff preflight found ambiguous path 'wanted.py'. "
                "Nearest line hints are unavailable; use a file-level note."
            ),
        },
    ]
    assert data["warning"] == "1 of 1 notes failed preflight. No notes were posted."
    assert len(diff_route.calls) == TWO_POSTS
    assert len(mr_route.calls) == TWO_POSTS
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
async def test_line_on_wrong_diff_side_fails_its_note_before_any_post(gitlab_env):
    """An added line has no old-side coordinate, so it cannot take old_line."""
    # Arrange
    add_preflight([([diff_file("added.py", patch="@@ -0,0 +1 @@\n+new", new_file=True)], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[{"file": "added.py", "old_line": 1, "body": "Wrong side."}])

    # Assert
    assert data["results"] == [
        {
            "index": 0,
            "status": "failed",
            "reason": (
                "line 1 on the old side in 'added.py'; fallback rejected: unchanged lines cannot be verified for a "
                "new or deleted file. Nearest line hints are unavailable; use a file-level note."
            ),
        },
    ]
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
    assert set(data["results"][0]) == {"index", "status", "discussion_id"}
    assert data["results"][1]["position"] == {"path": "src.py", "line": 2}
    assert "GitLab rejected the post" in data["results"][1]["reason"]
    assert "bad position" in data["results"][1]["reason"]
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
    assert set(data["results"][0]) == {"index", "status", "discussion_id"}
    assert data["results"][1]["position"] == {"path": "src.py", "line": 2}
    assert "outcome is unknown" in data["results"][1]["reason"]
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
    assert data["results"][1]["position"] == {"path": "src.py", "line": 2}
    assert "outcome is unknown" in data["results"][1]["reason"]
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
    assert "did not match" in result["reason"]
    assert "did not match" in data["warning"]
    assert len(post_route.calls) == 1


@respx.mock
async def test_ambiguous_path_fails_its_note_before_any_post(gitlab_env):
    """Two diff entries matching the same requested path are not guessed between."""
    # Arrange
    add_preflight([([diff_file("src.py"), diff_file("src.py")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    assert data["results"] == [
        {
            "index": 0,
            "status": "failed",
            "reason": (
                "Diff preflight found ambiguous path 'src.py'. "
                "Nearest line hints are unavailable; use a file-level note."
            ),
        },
    ]
    assert not post_route.called


@pytest.mark.parametrize(
    "note",
    [
        {"file": "", "line": 1, "body": "Review."},
        {"file": "   ", "line": 1, "body": "Review."},
        {"file": "src.py", "line": 1, "old_line": 1, "body": "Review."},
        {"file": "src.py", "line": 0, "body": "Review."},
        {"file": "src.py", "old_line": 0, "body": "Review."},
        {"file": "src.py", "line": 1, "body": "Review.", "body_path": "relative-review.txt"},
        {"file": "src.py", "line": 1},
    ],
    ids=[
        "empty-file",
        "blank-file",
        "both-line-sides",
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
    ("body_kind", "expected_reason"),
    [
        ("relative", "body_path must be an absolute server-local path"),
        ("directory", "body_path must point to a regular file"),
        ("oversize", "body exceeds 100000 UTF-8 bytes"),
        ("invalid-utf8", "body_path is not valid UTF-8"),
        ("blank-file", "body must not be blank"),
        ("blank-inline", "body must not be blank"),
        ("oversize-inline", "body exceeds 100000 UTF-8 bytes"),
    ],
    ids=["relative", "directory", "oversize", "invalid-utf8", "blank-file", "blank-inline", "oversize-inline"],
)
@respx.mock
async def test_unusable_note_body_fails_its_note_without_any_gitlab_request(
    gitlab_env,
    tmp_path,
    body_kind,
    expected_reason,
):
    """Inline and file-backed bodies share the size and nonblank limits, and bad bodies send no GitLab request."""
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
    assert data["results"] == [{"index": 0, "status": "failed", "reason": expected_reason}]
    assert data["warning"] == "1 of 1 notes failed preflight. No notes were posted."
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


@respx.mock
async def test_line_repeated_in_two_hunks_fails_its_note_as_ambiguous(gitlab_env):
    """Overlapping hunks are malformed, so the note is not guessed."""
    # Arrange
    patch = "@@ -0,0 +1 @@\n+first\n@@ -0,0 +1 @@\n+second"
    add_preflight([([diff_file("src.py", patch=patch)], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    assert data["results"] == [
        {
            "index": 0,
            "status": "failed",
            "reason": (
                "diff hunks overlap or are out of order. Nearest line hints are unavailable; use a file-level note."
            ),
        },
    ]
    assert not post_route.called


@pytest.mark.parametrize(
    ("file_diff", "expected_reason"),
    [
        (diff_file("src.py", patch="@@ -2 +2 @@\n-old\n+new", collapsed=True), "collapsed"),
        (diff_file("src.py", patch="@@ -2 +2 @@\n-old\n+new", too_large=True), "too large"),
        (diff_file("src.py", patch=""), "no usable patch"),
    ],
    ids=["collapsed", "too-large", "empty-patch"],
)
@respx.mock
async def test_unavailable_patch_fails_its_line_note_before_post(gitlab_env, file_diff, expected_reason):
    """Collapsed, oversized, and empty patches cannot provide a safe line position."""
    # Arrange
    add_preflight([([file_diff], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)])

    # Assert
    assert data["results"][0]["status"] == "failed"
    assert expected_reason in data["results"][0]["reason"]
    assert data["warning"] == "1 of 1 notes failed preflight. No notes were posted."
    assert not post_route.called


@pytest.mark.parametrize(
    ("patch", "note", "expected_reason"),
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
async def test_malformed_unified_hunk_fails_its_note_before_post(gitlab_env, patch, note, expected_reason):
    """Malformed hunk headers and line counts do not produce guessed coordinates."""
    # Arrange
    add_preflight([([diff_file("src.py", patch=patch)], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[note])

    # Assert
    assert data["results"][0]["status"] == "failed"
    assert expected_reason in data["results"][0]["reason"]
    assert not post_route.called


@respx.mock
async def test_old_side_context_position_contains_both_coordinates(gitlab_env):
    """An old-side context line is a valid position and returns both coordinates."""
    # Arrange
    patch = (
        "@@ -0,0 +1,3 @@\n+added one\n+added two\n+added three\n"
        f"@@ -{OLD_CONTEXT_LINE} +{NEW_CONTEXT_LINE} @@\n unchanged"
    )
    add_preflight([([diff_file("src.py", patch=patch)], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[{"file": "src.py", "old_line": OLD_CONTEXT_LINE, "body": "Review context."}])

    # Assert
    position = json.loads(post_route.calls[0].request.content)["position"]
    assert position["old_line"] == OLD_CONTEXT_LINE
    assert position["new_line"] == NEW_CONTEXT_LINE
    assert data["results"][0] == {"index": 0, "status": "posted", "discussion_id": "discussion-1"}


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
    assert data["results"][0]["status"] == "failed"
    assert "diff paths" in data["results"][0]["reason"]
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
    assert result["position"] == {"path": "src.py", "line": 1}
    assert "note ID" in result["reason"]
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


@respx.mock
async def test_people_change_after_every_note_posts_in_gitlab_spelling(gitlab_env):
    """Once all notes post, reviewers and assignees get one APPEND call each, after the discussion posts."""
    # Arrange
    add_preflight([([diff_file("src.py", patch="@@ -1,2 +1,2 @@\n one\n two")], None)])
    respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    graphql_route = respx.post(GRAPHQL).mock(side_effect=serve_people)
    notes = [review_note("src.py", 1), review_note("src.py", 2)]

    # Act
    data = await call_tool(notes=notes, add_reviewers=["Alice"], add_assignees=["bob"])

    # Assert
    assert [item["status"] for item in data["results"]] == ["posted", "posted"]
    assert data["people"] == {"applied": ["add_reviewers", "add_assignees"]}
    assert "warning" not in data
    assert graphql_variables(graphql_route, "mergeRequestSetReviewers") == [
        {"projectPath": PROJECT, "iid": str(IID), "usernames": ["alice"], "mode": "APPEND"}
    ]
    assert graphql_variables(graphql_route, "mergeRequestSetAssignees") == [
        {"projectPath": PROJECT, "iid": str(IID), "usernames": ["bob"], "mode": "APPEND"}
    ]
    assert write_order() == [
        "discussion",
        "discussion",
        "users(",
        "mergeRequestSetReviewers",
        "mergeRequestSetAssignees",
    ]


@respx.mock
async def test_unknown_reviewer_is_reported_and_known_assignee_is_still_added(gitlab_env):
    """An unknown name fails only its own item, and the known assignee is still added."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])
    respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    graphql_route = respx.post(GRAPHQL).mock(side_effect=serve_people)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)], add_reviewers=["ghost"], add_assignees=["bob"])

    # Assert
    assert data["people"] == {"applied": ["add_assignees"], "failed": {"reviewers:ghost": "unknown user"}}
    assert graphql_variables(graphql_route, "mergeRequestSetReviewers") == []


def reject_post(request: httpx.Request) -> httpx.Response:
    """Refuse the post with a client error, as GitLab does for a position it cannot use."""
    return response({"message": "bad position"}, status=422)


@pytest.mark.parametrize(
    ("notes", "post_reply"),
    [
        pytest.param(
            [review_note("src.py", 1), review_note("src.py", 99)],
            create_response,
            id="preflight-failure",
        ),
        pytest.param([review_note("src.py", 1), review_note("src.py", 2)], reject_post, id="rejected-post"),
    ],
)
@respx.mock
async def test_people_are_skipped_when_a_note_does_not_post(gitlab_env, notes, post_reply):
    """If any note fails to post, nobody is added, and the warning says why."""
    # Arrange
    add_preflight([([diff_file("src.py", patch="@@ -1,2 +1,2 @@\n one\n two")], None)])
    if any(note.get("line") == MISMATCHED_LINE for note in notes):
        add_blobs(("src.py", BASE_SHA, b"old\n"), ("src.py", HEAD, b"new\n"), status=404)
    respx.post(DISCUSSIONS_URL).mock(side_effect=post_reply)
    graphql_route = respx.post(GRAPHQL).mock(side_effect=serve_people)

    # Act
    data = await call_tool(notes=notes, add_reviewers=["alice"], add_assignees=["bob"])

    # Assert
    assert "people" not in data
    assert data["warning"].endswith("Reviewers and assignees were not changed because not every note posted.")
    assert not graphql_route.called


@respx.mock
async def test_connection_error_on_a_people_write_is_unknown(gitlab_env):
    """A connection error on a people write may have reached GitLab, so it is unknown and not failed."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])
    respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    respx.post(GRAPHQL).mock(
        side_effect=[
            response({"data": {"users": {"nodes": [{"username": "alice"}]}}}),
            httpx.ConnectError("connection closed"),
        ]
    )

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)], add_reviewers=["alice"])

    # Assert
    assert data["results"][0]["status"] == "posted"
    assert data["people"] == {"applied": [], "unknown": ["add_reviewers"]}


@respx.mock
async def test_gitlab_error_on_a_people_write_is_failed_with_its_reason(gitlab_env):
    """A GitLab refusal of a people write is a failed item with GitLab's reason, and the notes stay posted."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])
    respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    respx.post(GRAPHQL).mock(
        side_effect=[
            response({"data": {"users": {"nodes": [{"username": "alice"}]}}}),
            response({"message": "Boom"}, status=500),
        ]
    )

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)], add_reviewers=["alice"])

    # Assert
    assert data["results"][0]["status"] == "posted"
    assert data["people"]["applied"] == []
    assert "HTTP 500" in data["people"]["failed"]["add_reviewers"]


@respx.mock
async def test_validate_only_ignores_people_and_sends_no_people_requests(gitlab_env):
    """Validation returns positions only. People are applied by the real post, not by validation."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    graphql_route = respx.post(GRAPHQL).mock(side_effect=serve_people)

    # Act
    data = await call_tool(
        notes=[review_note("src.py", 1)],
        validate_only=True,
        add_reviewers=["alice"],
        add_assignees=["bob"],
    )

    # Assert
    assert data["results"] == [{"index": 0, "status": "validated", "position": {"path": "src.py", "line": 1}}]
    assert "people" not in data
    assert "warning" not in data
    assert not graphql_route.called
    assert not post_route.called


@respx.mock
async def test_general_note_validation_skips_diff_scan(gitlab_env):
    """A general note needs both MR SHA checks but does not read diff pages."""
    # Arrange
    mr_route, diff_route = add_preflight([([diff_file("unused.py")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[{"body": "A general review comment."}], validate_only=True)

    # Assert
    assert data["results"] == [{"index": 0, "status": "validated"}]
    assert len(mr_route.calls) == TWO_POSTS
    assert not diff_route.called
    assert not post_route.called


@respx.mock
async def test_compact_success_counts_posted_notes_after_people_changes(gitlab_env):
    """Compact output removes posted rows only after reviewer changes use the full result."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    graphql_route = respx.post(GRAPHQL).mock(side_effect=serve_people)

    # Act
    data = await call_tool(
        notes=[{"file": "src.py", "body": "Review this file."}],
        compact=True,
        add_reviewers=["Alice"],
    )

    # Assert
    assert data == {"results": [], "posted_count": 1, "people": {"applied": ["add_reviewers"]}}
    assert len(post_route.calls) == 1
    assert json.loads(post_route.calls[0].request.content) == {
        "body": "Review this file.",
        "position": {
            "position_type": "file",
            "old_path": "src.py",
            "new_path": "src.py",
            "base_sha": BASE_SHA,
            "start_sha": START_SHA,
            "head_sha": HEAD,
        },
    }
    assert write_order() == ["discussion", "users(", "mergeRequestSetReviewers"]
    assert graphql_variables(graphql_route, "mergeRequestSetReviewers") == [
        {"projectPath": PROJECT, "iid": str(IID), "usernames": ["alice"], "mode": "APPEND"}
    ]


@respx.mock
async def test_compact_mixed_failure_keeps_failure_and_unattempted_rows(gitlab_env):
    """A rejected note keeps failure details and later unattempted rows, while posted rows are counted."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])
    post_count = 0

    def reject_second_post(request: httpx.Request) -> httpx.Response:
        """Create the first note, then reject the second request."""
        nonlocal post_count
        post_count += 1
        if post_count == TWO_POSTS:
            return response({"message": "bad position"}, status=422)
        return create_response(request)

    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=reject_second_post)
    graphql_route = respx.post(GRAPHQL).mock(side_effect=serve_people)
    notes = [{"file": "src.py", "body": f"Review {index}."} for index in range(3)]

    # Act
    data = await call_tool(notes=notes, compact=True, add_reviewers=["alice"])

    # Assert
    assert [item["status"] for item in data["results"]] == ["failed", "unattempted"]
    assert [item["index"] for item in data["results"]] == [1, 2]
    assert data["posted_count"] == 1
    assert "note index 1" in data["warning"]
    assert data["warning"].endswith("Reviewers and assignees were not changed because not every note posted.")
    assert len(post_route.calls) == TWO_POSTS
    assert not graphql_route.called


@respx.mock
async def test_compact_preflight_keeps_validated_and_failed_details(gitlab_env):
    """A preflight failure preserves both its reason and the validated row, with no posts counted."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    notes = [review_note("src.py", 1), review_note("missing.py", 1)]

    # Act
    data = await call_tool(notes=notes, compact=True)

    # Assert
    assert data["results"] == [
        {"index": 0, "status": "validated", "position": {"path": "src.py", "line": 1}},
        {
            "index": 1,
            "status": "failed",
            "reason": (
                f"Diff preflight could not find 'missing.py' within {MAX_PAGES} pages. "
                "Nearest line hints are unavailable; use a file-level note."
            ),
        },
    ]
    assert data["posted_count"] == 0
    assert data["warning"] == "1 of 2 notes failed preflight. No notes were posted."
    assert not post_route.called


@pytest.mark.parametrize("outcome", ["unknown", "unverified"], ids=["unknown", "unverified"])
@respx.mock
async def test_compact_preserves_unknown_or_unverified_and_unattempted_rows(gitlab_env, outcome):
    """Uncertain outcomes and later unattempted rows remain visible in compact mode."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])

    def uncertain_response(request: httpx.Request) -> httpx.Response:
        """Return either an ambiguous server error or a successful response with the wrong position."""
        if outcome == "unknown":
            return response({"message": "upstream response lost"}, status=503)
        payload = json.loads(request.content)
        position = {**payload["position"], "new_line": MISMATCHED_LINE}
        return response({"id": "discussion-9", "notes": [{"id": RESPONSE_NOTE_ID, "position": position}]}, status=201)

    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=uncertain_response)
    notes = [{"file": "src.py", "body": f"Review {index}."} for index in range(2)]

    # Act
    data = await call_tool(notes=notes, compact=True)

    # Assert
    assert [item["status"] for item in data["results"]] == [outcome, "unattempted"]
    uncertain = data["results"][0]
    assert uncertain["position"] == (
        {"path": "src.py"} if outcome == "unknown" else {"path": "src.py", "line": MISMATCHED_LINE}
    )
    assert uncertain["reason"]
    assert data["posted_count"] == 0
    assert "note index 0" in data["warning"]
    assert len(post_route.calls) == 1


@respx.mock
async def test_compact_stale_sha_keeps_current_head_and_unattempted_results(gitlab_env):
    """Compaction does not remove stale-SHA details or unattempted note rows."""
    # Arrange
    mr_route, diff_route = add_preflight(
        [([diff_file("src.py")], None)],
        merge_requests=[merge_request()],
    )
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)], head_sha="d" * 40, compact=True)

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert data["posted_count"] == 0
    assert data["current_head_sha"] == HEAD
    assert "stale" in data["warning"]
    assert len(mr_route.calls) == 1
    assert not diff_route.called
    assert not post_route.called


@respx.mock
async def test_compact_validation_keeps_batch_id_and_reposting_counts_success(gitlab_env):
    """Validation keeps all rows and its batch handle, then compact posting returns the count."""
    # Arrange
    mr_route = respx.get(MR_URL).mock(return_value=response(merge_request()))
    diff_route = respx.get(DIFF_URL).mock(return_value=response([diff_file("src.py")]))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    note = review_note("src.py", 1)

    # Act
    async with Client(mcp) as client:
        validated_result = await client.call_tool(
            "post_review_notes",
            {
                "project": PROJECT,
                "iid": IID,
                "head_sha": HEAD,
                "notes": [note],
                "validate_only": True,
                "compact": True,
            },
            raise_on_error=False,
        )
        validated = validated_result.structured_content
        posted = await client.call_tool(
            "post_review_notes",
            {"project": PROJECT, "iid": IID, "head_sha": HEAD, "batch_id": validated["batch_id"], "compact": True},
            raise_on_error=False,
        )
        posted_data = posted.structured_content

    # Assert
    assert validated["results"] == [{"index": 0, "status": "validated", "position": {"path": "src.py", "line": 1}}]
    assert validated["posted_count"] == 0
    assert validated["batch_id"]
    assert posted_data == {"results": [], "posted_count": 1}
    assert len(mr_route.calls) == FOUR_PREFLIGHT_REQUESTS
    assert len(diff_route.calls) == TWO_POSTS
    assert len(post_route.calls) == 1
    assert json.loads(post_route.calls[0].request.content)["body"] == "Please check this."


@respx.mock
async def test_compact_preserves_people_write_failure(gitlab_env):
    """A failed people update remains visible after successful note rows are compacted."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    graphql_route = respx.post(GRAPHQL).mock(
        side_effect=[
            response({"data": {"users": {"nodes": [{"username": "alice"}]}}}),
            response({"message": "Boom"}, status=500),
        ]
    )

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)], compact=True, add_reviewers=["alice"])

    # Assert
    assert data["results"] == []
    assert data["posted_count"] == 1
    assert data["people"]["applied"] == []
    assert "HTTP 500" in data["people"]["failed"]["add_reviewers"]
    assert len(post_route.calls) == 1
    assert len(graphql_route.calls) == TWO_POSTS


@respx.mock
async def test_general_note_posts_body_without_position_and_verifies_ids(gitlab_env):
    """A general discussion verifies both response IDs but returns only the discussion ID."""
    # Arrange
    mr_route, diff_route = add_preflight([([], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(
        return_value=response({"id": "general-discussion", "notes": [{"id": POSTED_NOTE_ID}]}, status=201)
    )

    # Act
    data = await call_tool(notes=[{"body": "A general review comment."}])

    # Assert
    assert json.loads(post_route.calls[0].request.content) == {"body": "A general review comment."}
    assert data["results"] == [
        {
            "index": 0,
            "status": "posted",
            "discussion_id": "general-discussion",
        }
    ]
    assert len(mr_route.calls) == TWO_POSTS
    assert not diff_route.called


@respx.mock
async def test_general_note_with_malformed_returned_position_is_unverified_and_stops(gitlab_env):
    """A malformed non-null returned position is not mistaken for an absent general-note position."""
    # Arrange
    mr_route, diff_route = add_preflight([([], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(
        return_value=response(
            {"id": "general-discussion", "notes": [{"id": POSTED_NOTE_ID, "position": "invalid"}]},
            status=201,
        )
    )
    graphql_route = respx.post(GRAPHQL).mock(side_effect=serve_people)

    # Act
    data = await call_tool(
        notes=[{"body": "First general comment."}, {"body": "Second general comment."}],
        add_reviewers=["alice"],
    )

    # Assert
    assert [item["status"] for item in data["results"]] == ["unverified", "unattempted"]
    assert data["results"][0]["note_id"] == POSTED_NOTE_ID
    assert data["results"][0]["discussion_id"] == "general-discussion"
    assert "position did not match" in data["results"][0]["reason"]
    assert "position" not in data["results"][0]
    assert "position did not match" in data["warning"]
    assert len(post_route.calls) == 1
    assert len(mr_route.calls) == TWO_POSTS
    assert not diff_route.called
    assert not graphql_route.called


@respx.mock
async def test_general_note_with_unknown_post_does_not_report_a_position(gitlab_env):
    """An unknown general-note outcome has no requested location to report."""
    # Arrange
    add_preflight([([], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(return_value=response({"message": "upstream lost"}, status=503))

    # Act
    data = await call_tool(notes=[{"body": "A general review comment."}])

    # Assert
    result = data["results"][0]
    assert result["status"] == "unknown"
    assert "position" not in result
    assert "reason" in result
    assert "outcome is unknown" in result["reason"]
    assert len(post_route.calls) == 1


@respx.mock
async def test_mixed_general_file_and_line_notes_keep_order_and_scan_only_file_paths(gitlab_env):
    """General, file, and line notes post in request order with positions only for diff notes."""
    # Arrange
    _, diff_route = add_preflight([([diff_file("src.py", patch="@@ -1,2 +1,2 @@\n one\n two")], None)])

    def echo_general_or_position(request: httpx.Request) -> httpx.Response:
        """Return matching IDs and include a position only when the request supplied one."""
        payload = json.loads(request.content)
        note: dict[str, Any] = {"id": POSTED_NOTE_ID}
        if "position" in payload:
            note["position"] = payload["position"]
        return response({"id": f"discussion-{len(post_route.calls) + 1}", "notes": [note]}, status=201)

    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=echo_general_or_position)
    notes = [
        {"body": "General comment."},
        {"file": "src.py", "body": "File comment."},
        review_note("src.py", 2, "Line comment."),
    ]

    # Act
    data = await call_tool(notes=notes)

    # Assert
    payloads = [json.loads(call.request.content) for call in post_route.calls]
    assert [item["body"] for item in payloads] == ["General comment.", "File comment.", "Line comment."]
    assert payloads[0] == {"body": "General comment."}
    assert payloads[1]["position"] == {
        "position_type": "file",
        "old_path": "src.py",
        "new_path": "src.py",
        "base_sha": BASE_SHA,
        "start_sha": START_SHA,
        "head_sha": HEAD,
    }
    assert payloads[2]["position"]["new_line"] == MIXED_INLINE_LINE
    assert [item["index"] for item in data["results"]] == [0, 1, 2]
    assert [item["status"] for item in data["results"]] == ["posted"] * 3
    assert data["results"] == [
        {"index": index, "status": "posted", "discussion_id": f"discussion-{index + 1}"} for index in range(3)
    ]
    assert len(diff_route.calls) == 1


@pytest.mark.parametrize(
    ("merge_requests", "request_count", "warning_part"),
    [
        pytest.param([merge_request(sha="d" * 40, head_sha="d" * 40)], 1, "stale", id="stale-sha"),
        pytest.param(
            [merge_request(), merge_request(sha="d" * 40, head_sha="d" * 40)],
            TWO_POSTS,
            "changed during preflight",
            id="changed-sha",
        ),
    ],
)
@respx.mock
async def test_general_note_sha_checks_stop_before_post(gitlab_env, merge_requests, request_count, warning_part):
    """Stale and changed MR SHA values stop general-note batches before any write."""
    # Arrange
    mr_route, diff_route = add_preflight([([], None)], merge_requests=merge_requests)
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[{"body": "General comment."}])

    # Assert
    assert data["results"] == [{"index": 0, "status": "unattempted"}]
    assert warning_part in data["warning"]
    assert len(mr_route.calls) == request_count
    assert not diff_route.called
    assert not post_route.called


@respx.mock
async def test_invalid_file_note_in_mixed_batch_prevents_general_note_post(gitlab_env):
    """An invalid diff note makes the whole mixed batch atomic and skips people changes."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])
    add_blobs(("src.py", BASE_SHA, b"old\n"), ("src.py", HEAD, b"new\n"), status=404)
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    graphql_route = respx.post(GRAPHQL).mock(side_effect=serve_people)

    # Act
    data = await call_tool(
        notes=[{"body": "General comment."}, review_note("src.py", 99)],
        add_reviewers=["alice"],
    )

    # Assert
    assert [item["status"] for item in data["results"]] == ["validated", "failed"]
    assert "No notes were posted" in data["warning"]
    assert not post_route.called
    assert not graphql_route.called


@respx.mock
async def test_line_without_file_is_rejected_before_requests(gitlab_env):
    """A line coordinate cannot identify a discussion position without a file path."""
    # Arrange
    mr_route = respx.get(MR_URL).mock(return_value=response(merge_request()))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    result = await call_tool_result(notes=[{"line": 1, "body": "Missing path."}])

    # Assert
    assert result.is_error
    assert not mr_route.called
    assert not post_route.called


EXPANSION_PATCH = "@@ -2 +2,2 @@\n-old\n+new one\n+new two\n@@ -5 +6 @@\n context"
OLD_SNAPSHOT = b"before\nold\nmiddle one\nmiddle two\ncontext\ntrailing\n"
NEW_SNAPSHOT = b"before\nnew one\nnew two\nmiddle one\nmiddle two\ncontext\ntrailing\n"


@pytest.mark.parametrize(
    ("side", "target", "expected_old", "expected_new"),
    [
        ("new", 1, 1, 1),
        ("new", 4, 3, 4),
        ("new", 7, 6, 7),
        ("old", 1, 1, 1),
        ("old", 3, 3, 4),
        ("old", 6, 6, 7),
    ],
    ids=["new-before", "new-between", "new-after", "old-before", "old-between", "old-after"],
)
@respx.mock
async def test_unchanged_lines_map_before_between_and_after_hunks_on_both_sides(
    gitlab_env,
    side,
    target,
    expected_old,
    expected_new,
):
    """Verified gaps map by offset despite the earlier replacement's line-count change."""
    # Arrange
    add_preflight([([diff_file("src.py", patch=EXPANSION_PATCH)], None)])
    old_route, new_route = add_blobs(
        ("src.py", BASE_SHA, OLD_SNAPSHOT),
        ("src.py", HEAD, NEW_SNAPSHOT),
    )
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)
    note = {"file": "src.py", "body": "Review unchanged code."}
    note["line" if side == "new" else "old_line"] = target

    # Act
    data = await call_tool(notes=[note], validate_only=True)

    # Assert
    assert data["results"] == [
        {"index": 0, "status": "validated", "position": {"path": "src.py", "line": expected_new}}
    ]
    assert len(old_route.calls) == 1
    assert len(new_route.calls) == 1
    assert old_route.calls[0].request.url.params["ref"] == BASE_SHA
    assert new_route.calls[0].request.url.params["ref"] == HEAD
    assert not post_route.called


@respx.mock
async def test_posted_unchanged_line_uses_resolved_coordinates_and_immutable_refs(gitlab_env):
    """A real note request uses the verified old and new positions with base and head SHAs."""
    # Arrange
    add_preflight([([diff_file("src.py", patch=EXPANSION_PATCH)], None)])
    add_blobs(("src.py", BASE_SHA, OLD_SNAPSHOT), ("src.py", HEAD, NEW_SNAPSHOT))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 4)])

    # Assert
    position = json.loads(post_route.calls[0].request.content)["position"]
    assert position == {
        "position_type": "text",
        "old_path": "src.py",
        "new_path": "src.py",
        "old_line": 3,
        "new_line": 4,
        "base_sha": BASE_SHA,
        "start_sha": START_SHA,
        "head_sha": HEAD,
    }
    assert data["results"][0] == {"index": 0, "status": "posted", "discussion_id": "discussion-1"}


@pytest.mark.parametrize(
    ("side", "target", "expected_old", "expected_new"),
    [("new", 4, 3, 4), ("old", 4, 4, 5), ("new", 6, 6, 6)],
    ids=["after-insertion", "before-deletion", "after-deletion"],
)
@respx.mock
async def test_zero_count_hunks_preserve_insertion_and_deletion_boundaries(
    gitlab_env,
    side,
    target,
    expected_old,
    expected_new,
):
    """Zero-count hunk ranges create boundaries without consuming a line on that side."""
    # Arrange
    patch = "@@ -2,0 +3 @@\n+inserted\n@@ -5 +5,0 @@\n-removed"
    old_blob = b"one\ntwo\nthree\nfour\nremoved\nsix\n"
    new_blob = b"one\ntwo\ninserted\nthree\nfour\nsix\n"
    add_preflight([([diff_file("src.py", patch=patch)], None)])
    add_blobs(("src.py", BASE_SHA, old_blob), ("src.py", HEAD, new_blob))
    note = {"file": "src.py", "body": "Review unchanged code."}
    note["line" if side == "new" else "old_line"] = target

    # Act
    data = await call_tool(notes=[note], validate_only=True)

    # Assert
    assert data["results"] == [
        {"index": 0, "status": "validated", "position": {"path": "src.py", "line": expected_new}}
    ]
    assert len(raw_blob_calls()) == TWO_POSTS


@pytest.mark.parametrize(
    ("old_blob", "new_blob", "hunk_line", "target"),
    [
        (b"first\r\nbefore\r\nold\r\nlast\r\n", b"first\r\nbefore\r\nnew\r\nlast\r\n", 3, 4),
        (b"first\rbefore\nold\nlast\r", b"first\rbefore\nnew\nlast\r", 2, 3),
        (b"first\nold\nlast", b"first\nnew\nlast", 2, 3),
        (b"first\nold\nlast\n\n", b"first\nnew\nlast\n\n", 2, 4),
    ],
    ids=["crlf", "bare-cr-is-not-a-separator", "no-final-lf", "final-blank-line"],
)
@respx.mock
async def test_unchanged_gap_comparison_preserves_git_blob_line_terminators(
    gitlab_env,
    old_blob,
    new_blob,
    hunk_line,
    target,
):
    """LF alone separates blob lines, and complete terminators participate in byte equality."""
    # Arrange
    patch = f"@@ -{hunk_line} +{hunk_line} @@\n-old\n+new"
    add_preflight([([diff_file("src.py", patch=patch)], None)])
    add_blobs(("src.py", BASE_SHA, old_blob), ("src.py", HEAD, new_blob))

    # Act
    data = await call_tool(notes=[review_note("src.py", target)], validate_only=True)

    # Assert
    assert data["results"] == [{"index": 0, "status": "validated", "position": {"path": "src.py", "line": target}}]


@pytest.mark.parametrize(
    ("old_blob", "new_blob"),
    [
        (b"first\nold\nlast", b"first\nnew\nlast\n"),
        (b"repeat\nold\nrepeat\n", b"changed\nnew\nrepeat\n"),
    ],
    ids=["final-newline-differs", "omitted-balanced-edit-and-repeated-text"],
)
@respx.mock
async def test_unchanged_gap_must_match_exact_bytes_even_with_balanced_changes(gitlab_env, old_blob, new_blob):
    """A balanced edit omitted from a patch or a newline-only change blocks mapping."""
    # Arrange
    patch = "@@ -2 +2 @@\n-old\n+new"
    add_preflight([([diff_file("src.py", patch=patch)], None)])
    add_blobs(("src.py", BASE_SHA, old_blob), ("src.py", HEAD, new_blob))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 3)], validate_only=True)

    # Assert
    assert data["results"][0]["status"] == "failed"
    assert "unchanged file content differs" in data["results"][0]["reason"]
    assert "Nearest new-side diff lines: 2." in data["results"][0]["reason"]
    assert not post_route.called


@pytest.mark.parametrize(
    "raw",
    [
        diff_file("src.py", patch="@@ -2 +2 @@\n-old\n+new", new_file=True),
        diff_file("src.py", patch="@@ -2 +2 @@\n-old\n+new", deleted_file=True),
    ],
    ids=["new-file", "deleted-file"],
)
@respx.mock
async def test_new_or_deleted_files_reject_only_out_of_hunk_fallback(gitlab_env, raw):
    """New and deleted files reject blob expansion, while in-hunk support remains available."""
    # Arrange
    add_preflight([([raw], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)], validate_only=True)

    # Assert
    assert data["results"][0]["status"] == "failed"
    assert "new or deleted file" in data["results"][0]["reason"]
    assert "Nearest new-side diff lines: 2." in data["results"][0]["reason"]
    assert not raw_blob_calls()
    assert not post_route.called


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (diff_file("src.py", patch="@@ -2 +2 @@\n-old\n+new", collapsed=True), "collapsed"),
        (diff_file("src.py", patch="@@ -2 +2 @@\n-old\n+new", too_large=True), "too large"),
        (diff_file("src.py", patch="@@ -2 +2 @@\n-old\n+new", binary=True), "binary"),
    ],
    ids=["collapsed", "too-large", "binary-flag"],
)
@respx.mock
async def test_unsafe_diff_flags_reject_expansion_with_nearest_hints(gitlab_env, raw, reason):
    """Collapsed, large, and flagged binary diffs do not offer hints from unsafe patches."""
    # Arrange
    add_preflight([([raw], None)])

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)], validate_only=True)

    # Assert
    assert data["results"][0]["status"] == "failed"
    assert reason in data["results"][0]["reason"]
    assert "Nearest line hints are unavailable" in data["results"][0]["reason"]
    assert not raw_blob_calls()


@pytest.mark.parametrize(
    "raw",
    [
        diff_file("src.py", patch="@@ -2 +2 @@\n-old\n+new", collapsed=True),
        diff_file("src.py", patch="@@ -2 +2 @@\n-old\n+new", too_large=True),
        diff_file("src.py", patch="@@ -2 +2 @@\n-old\n+new", binary=True),
    ],
    ids=["collapsed-in-hunk", "too-large-in-hunk", "binary-in-hunk"],
)
@respx.mock
async def test_unsafe_diff_flags_reject_in_hunk_notes_before_blob_read_or_post(gitlab_env, raw):
    """Even a matching patch line is rejected when GitLab marks the inline diff unsafe."""
    # Arrange
    add_preflight([([raw], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 2)], validate_only=True)

    # Assert
    assert data["results"][0]["status"] == "failed"
    assert any(flag in data["results"][0]["reason"] for flag in ("collapsed", "too large", "binary"))
    assert "Nearest line hints are unavailable" in data["results"][0]["reason"]
    assert not raw_blob_calls()
    assert not post_route.called


@respx.mock
async def test_binary_blob_and_hunk_bounds_reject_expansion(gitlab_env):
    """A NUL byte or a hunk endpoint beyond either snapshot makes expansion unsafe."""
    # Arrange binary snapshot
    add_preflight([([diff_file("src.py", patch="@@ -2 +2 @@\n-old\n+new")], None)])
    old_route, new_route = add_blobs(
        ("src.py", BASE_SHA, b"first\n\x00old\nlast"), ("src.py", HEAD, b"first\nnew\nlast")
    )

    # Act binary snapshot
    binary = await call_tool(notes=[review_note("src.py", 3)], validate_only=True)

    # Assert binary snapshot
    assert binary["results"][0]["status"] == "failed"
    assert "file content is binary" in binary["results"][0]["reason"]
    assert len(old_route.calls) == 1
    assert not new_route.called

    # Arrange hunk beyond snapshot
    add_preflight([([diff_file("src.py", patch="@@ -5 +5 @@\n-old\n+new")], None)])
    add_blobs(("src.py", BASE_SHA, b"first\n"), ("src.py", HEAD, b"first\n"))

    # Act hunk beyond snapshot
    bounds = await call_tool(notes=[review_note("src.py", 1)], validate_only=True)

    # Assert hunk beyond snapshot
    assert bounds["results"][0]["status"] == "failed"
    assert "hunk bounds exceed the file content" in bounds["results"][0]["reason"]


@respx.mock
async def test_rename_uses_old_and_new_paths_and_base_head_refs(gitlab_env):
    """A rename reads each path at its immutable blob SHA, not at start_sha."""
    # Arrange
    old_path, new_path = "old dir/src.py", "new dir/src.py"
    patch = "@@ -2 +2 @@\n-old\n+new"
    add_preflight([([diff_file(old_path, new_path, patch)], None)])
    old_route, new_route = add_blobs(
        (old_path, BASE_SHA, b"before\nold\nafter\n"),
        (new_path, HEAD, b"before\nnew\nafter\n"),
    )

    # Act
    data = await call_tool(notes=[review_note(new_path, 3)], validate_only=True)

    # Assert
    assert data["results"][0]["position"] == {"path": new_path, "old_path": old_path, "line": 3}
    assert str(old_route.calls[0].request.url).endswith("/old%20dir%2Fsrc.py/raw?ref=" + BASE_SHA)
    assert str(new_route.calls[0].request.url).endswith("/new%20dir%2Fsrc.py/raw?ref=" + HEAD)
    assert old_route.calls[0].request.url.params["ref"] == BASE_SHA
    assert new_route.calls[0].request.url.params["ref"] == HEAD


@respx.mock
async def test_raw_blobs_are_cached_for_successful_and_failed_fallbacks(gitlab_env):
    """Each path and SHA pair is fetched once, including when its first read fails."""
    # Arrange successful reads
    add_preflight([([diff_file("src.py", patch=EXPANSION_PATCH)], None)])
    old_route, new_route = add_blobs(
        ("src.py", BASE_SHA, OLD_SNAPSHOT),
        ("src.py", HEAD, NEW_SNAPSHOT),
    )

    # Act successful reads
    valid = await call_tool(
        notes=[review_note("src.py", 1), {"file": "src.py", "old_line": 6, "body": "Another note."}],
        validate_only=True,
    )

    # Assert successful reads
    assert [item["status"] for item in valid["results"]] == ["validated", "validated"]
    assert len(old_route.calls) == 1
    assert len(new_route.calls) == 1

    # Arrange failed reads
    add_preflight([([diff_file("src.py")], None)])
    calls_before = len(raw_blob_calls())
    _, failed_new = add_blobs(
        ("src.py", BASE_SHA, b"unavailable"),
        ("src.py", HEAD, b"unavailable"),
        status=404,
    )
    new_calls_before = len(failed_new.calls)

    # Act failed reads
    invalid = await call_tool(notes=[review_note("src.py", 99), review_note("src.py", 100)], validate_only=True)

    # Assert failed reads
    assert [item["status"] for item in invalid["results"]] == ["failed", "failed"]
    assert len(raw_blob_calls()) - calls_before == 1
    assert len(failed_new.calls) == new_calls_before


@respx.mock
async def test_truncated_blob_is_rejected_and_final_ref_change_stops_batch(gitlab_env):
    """Oversized snapshots fail locally, and refs are rechecked after blob reads before any write."""
    # Arrange truncated snapshot
    add_preflight([([diff_file("src.py", patch=EXPANSION_PATCH)], None)])
    truncated, _ = add_blobs(
        ("src.py", BASE_SHA, b"x" * (BLOB_CAP + 1)),
        ("src.py", HEAD, NEW_SNAPSHOT),
    )

    # Act truncated snapshot
    too_large = await call_tool(notes=[review_note("src.py", 1)], validate_only=True)

    # Assert truncated snapshot
    assert too_large["results"][0]["status"] == "failed"
    assert "exceeds the 131072-byte limit" in too_large["results"][0]["reason"]
    assert len(truncated.calls) == 1

    # Arrange changed refs after blob reads
    changed = merge_request(sha="d" * 40, head_sha="d" * 40)
    mr_route, _ = add_preflight(
        [([diff_file("src.py", patch=EXPANSION_PATCH)], None)],
        merge_requests=[merge_request(), changed],
    )
    calls_before = len(mr_route.calls)
    add_blobs(("src.py", BASE_SHA, OLD_SNAPSHOT), ("src.py", HEAD, NEW_SNAPSHOT))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act changed refs after blob reads
    changed_refs = await call_tool(notes=[review_note("src.py", 1)])

    # Assert changed refs after blob reads
    assert changed_refs["results"] == [{"index": 0, "status": "unattempted"}]
    assert "changed during preflight" in changed_refs["warning"]
    assert len(mr_route.calls) - calls_before == TWO_POSTS
    assert not post_route.called


@respx.mock
async def test_nearest_hint_selection_ranges_and_reason_cap(gitlab_env):
    """Hints choose the five nearest side-specific lines, compress ranges, and survive the reason cap."""
    # Arrange
    hunks = [f"@@ -{line} +{line} @@\n context {line}" for line in (1, 2, 3, 6, 10, 11, 12)]
    patch = "\n".join(hunks)
    raw = diff_file("src.py", patch=patch, new_file=True)
    add_preflight([([raw], None)])

    # Act nearest hint
    nearest = await call_tool(notes=[review_note("src.py", 8)], validate_only=True)

    # Assert nearest hint
    assert "Nearest new-side diff lines: 3, 6, 10-12." in nearest["results"][0]["reason"]
    assert len(nearest["results"][0]["reason"]) <= REASON_CAP

    # Arrange capped hint
    long_file = "x" * 180 + ".py"
    long_raw = diff_file(long_file, patch="@@ -1,5 +1,5 @@\n a\n b\n c\n d\n e", new_file=True)
    add_preflight([([long_raw], None)])

    # Act capped hint
    capped = await call_tool(notes=[review_note(long_file, 99)], validate_only=True)

    # Assert capped hint
    reason = capped["results"][0]["reason"]
    assert len(reason) == REASON_CAP
    assert reason.endswith("Nearest new-side diff lines: 1-5.")

    # Arrange oversized coordinates
    huge_line = 10**200
    huge_patch = f"@@ -{huge_line} +{huge_line} @@\n unchanged"
    huge_raw = diff_file("src.py", patch=huge_patch, new_file=True)
    add_preflight([([huge_raw], None)])

    # Act oversized coordinates
    huge_hint = await call_tool(notes=[review_note("src.py", 1)], validate_only=True)

    # Assert oversized coordinates
    huge_reason = huge_hint["results"][0]["reason"]
    assert len(huge_reason) <= REASON_CAP
    assert "Nearest line hints are unavailable" in huge_reason


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ("@@ -0 +1 @@\n-old\n+new", "invalid old unified hunk range"),
        ("@@ -1 +0 @@\n-old\n+new", "invalid new unified hunk range"),
        ("@@ -1,0 +1,0 @@", "empty unified hunk"),
        ("@@ -2 +3 @@\n context", "unequal unchanged gaps"),
        ("@@ -1 +1 @@\n first\n@@ -3 +4 @@\n second", "unequal unchanged gaps"),
        ("@@ -1 +1 @@\n first\n@@ -1 +1 @@\n second", "overlap or are out of order"),
    ],
    ids=["old-start-zero", "new-start-zero", "empty-range", "unequal-leading-gap", "unequal-middle-gap", "overlap"],
)
@respx.mock
async def test_invalid_hunk_ranges_and_gaps_are_rejected_without_blob_reads(gitlab_env, patch, reason):
    """Invalid interval bounds are not treated as candidates for unchanged-line mapping."""
    # Arrange
    add_preflight([([diff_file("src.py", patch=patch)], None)])

    # Act
    data = await call_tool(notes=[review_note("src.py", 1)], validate_only=True)

    # Assert
    assert data["results"][0]["status"] == "failed"
    assert reason in data["results"][0]["reason"]
    assert "Nearest line hints are unavailable" in data["results"][0]["reason"]
    assert not raw_blob_calls()


@respx.mock
async def test_empty_old_blob_and_out_of_file_line_are_rejected(gitlab_env):
    """An empty old snapshot has zero lines and cannot map a coordinate beyond the new EOF."""
    # Arrange
    patch = "@@ -0,0 +1 @@\n+added"
    add_preflight([([diff_file("src.py", patch=patch)], None)])
    old_route, new_route = add_blobs(("src.py", BASE_SHA, b""), ("src.py", HEAD, b"added\n"))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(notes=[review_note("src.py", 2)], validate_only=True)

    # Assert
    assert data["results"][0]["status"] == "failed"
    assert "outside the file" in data["results"][0]["reason"]
    assert len(old_route.calls) == 1
    assert len(new_route.calls) == 1
    assert not post_route.called


@pytest.mark.parametrize("failure", ["connection", "redirect"], ids=["connection-error", "redirect"])
@respx.mock
async def test_raw_blob_read_failures_are_cached_per_note(gitlab_env, failure):
    """A connection or redirect failure stays on affected notes and does not repeat the same blob read."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])
    old_url = f"{RAW_FILES_URL}/src.py/raw?ref={BASE_SHA}"
    new_url = f"{RAW_FILES_URL}/src.py/raw?ref={HEAD}"
    if failure == "connection":

        def disconnect(request: httpx.Request) -> httpx.Response:
            """Raise a transport error while opening the immutable old blob."""
            raise httpx.ConnectError("connection closed", request=request)  # noqa: TRY003 - simulate a transport failure.

        old_route = respx.get(old_url).mock(side_effect=disconnect)
    else:
        old_route = respx.get(old_url).mock(
            return_value=httpx.Response(302, headers={"Location": "https://elsewhere.example/raw"})
        )
    new_route = respx.get(new_url).mock(return_value=httpx.Response(200, content=b"new\n"))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(
        notes=[review_note("src.py", 99), review_note("src.py", 100), review_note("src.py", 1)],
        validate_only=True,
    )

    # Assert
    assert [item["status"] for item in data["results"]] == ["failed", "failed", "validated"]
    assert len(old_route.calls) == 1
    assert not new_route.called
    assert "could not be read safely" in data["results"][0]["reason"]
    assert not post_route.called


@respx.mock
async def test_new_blob_http_error_is_cached_without_hiding_in_hunk_note(gitlab_env):
    """A new-snapshot error rejects outside lines but leaves a matching in-hunk note validated."""
    # Arrange
    add_preflight([([diff_file("src.py")], None)])
    old_route = add_blobs(("src.py", BASE_SHA, b"old\n"))[0]
    new_url = f"{RAW_FILES_URL}/src.py/raw?ref={HEAD}"
    new_route = respx.get(new_url).mock(return_value=response({"message": "not found"}, status=404))
    post_route = respx.post(DISCUSSIONS_URL).mock(side_effect=create_response)

    # Act
    data = await call_tool(
        notes=[review_note("src.py", 99), review_note("src.py", 100), review_note("src.py", 1)],
        validate_only=True,
    )

    # Assert
    assert [item["status"] for item in data["results"]] == ["failed", "failed", "validated"]
    assert len(old_route.calls) == 1
    assert len(new_route.calls) == 1
    assert "new file content could not be read safely" in data["results"][0]["reason"]
    assert "HTTP 404" in data["results"][0]["reason"]
    assert not post_route.called


@respx.mock
async def test_invalid_diff_records_do_not_make_a_valid_expansion_ambiguous(gitlab_env):
    """Malformed diff records are ignored while a valid entry still maps its verified gap."""
    # Arrange
    raw = diff_file("src.py")
    add_preflight([([None, "not a diff record", raw], None)])
    old_route, new_route = add_blobs(
        ("src.py", BASE_SHA, b"old\nsame\n"),
        ("src.py", HEAD, b"new\nsame\n"),
    )

    # Act
    data = await call_tool(notes=[review_note("src.py", 2)], validate_only=True)

    # Assert
    assert data["results"] == [{"index": 0, "status": "validated", "position": {"path": "src.py", "line": 2}}]
    assert len(old_route.calls) == 1
    assert len(new_route.calls) == 1


@respx.mock
async def test_unknown_general_note_outcome_stops_later_notes_and_people(gitlab_env):
    """An unknown general-note outcome stops the batch and prevents people changes."""
    # Arrange
    mr_route, diff_route = add_preflight([([], None)])
    post_route = respx.post(DISCUSSIONS_URL).mock(return_value=response({"message": "unknown"}, status=503))
    graphql_route = respx.post(GRAPHQL).mock(side_effect=serve_people)

    # Act
    data = await call_tool(
        notes=[{"body": "First general comment."}, {"body": "Second general comment."}],
        add_reviewers=["alice"],
    )

    # Assert
    assert [item["status"] for item in data["results"]] == ["unknown", "unattempted"]
    assert "outcome is unknown" in data["warning"]
    assert len(post_route.calls) == 1
    assert len(mr_route.calls) == TWO_POSTS
    assert not diff_route.called
    assert not graphql_route.called
