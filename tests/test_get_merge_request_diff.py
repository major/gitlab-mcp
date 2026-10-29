"""Tests for the get_merge_request_diff tool, run through a real in-memory MCP client 📄."""

import base64
import json
from typing import Any
lazy from collections.abc import Callable

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from gitlab_mcp.common import MAX_PAGES
from gitlab_mcp.tools.mr_diff import DIFF_BUDGET
from mcp_helpers import call_tool as call_mcp_tool

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 2187
DIFFS_URL = "https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/2187/diffs"
VERSIONS_URL = "https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/2187/versions"
HEAD_SHA = "6096f2d5a1b2c3d4e5f60718293a4b5c6d7e8f90"  # Full SHA. Write tools need it exact, so output keeps all of it.
OLD_SHA = "1096f2d5a1b2c3d4e5f60718293a4b5c6d7e8f90"
UNKNOWN_SHA = "2096f2d5a1b2c3d4e5f60718293a4b5c6d7e8f90"
HEAD_REFS = {"headSha": HEAD_SHA}
BIG = "x" * 15000  # Three of these do not fit in one DIFF_BUDGET, but two do.
DEFAULT_DIFF = "@@ -1 +1 @@\n-old\n+new\n"


def make_stat(path: str, additions: int = 1, deletions: int = 0) -> dict[str, Any]:
    """Build one diffStats entry from the files query."""
    return {"path": path, "additions": additions, "deletions": deletions}


def graphql_body(diff_refs: dict[str, Any] | None, stats: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Build the files query response for one merge request."""
    return {"data": {"project": {"mergeRequest": {"diffRefs": diff_refs, "diffStats": stats}}}}


def make_file(  # noqa: PLR0913 - each keyword is one REST flag, so the builder mirrors the API.
    new_path: str,
    old_path: str | None = None,
    *,
    diff: str = DEFAULT_DIFF,
    new_file: bool = False,
    deleted_file: bool = False,
    renamed_file: bool = False,
    generated_file: bool = False,
    collapsed: bool = False,
    too_large: bool = False,
) -> dict[str, Any]:
    """Build one REST diff entry. Like GitLab, old_path defaults to new_path."""
    return {
        "diff": diff,
        "collapsed": collapsed,
        "too_large": too_large,
        "new_path": new_path,
        "old_path": new_path if old_path is None else old_path,
        "a_mode": "100644",
        "b_mode": "100644",
        "new_file": new_file,
        "renamed_file": renamed_file,
        "deleted_file": deleted_file,
        "generated_file": generated_file,
    }


def files_response(*files: dict[str, Any], next_page: int | None = None) -> httpx.Response:
    """Wrap diff entries in a REST response. GitLab sends an empty X-Next-Page on the last page."""
    return httpx.Response(200, json=list(files), headers={"X-Next-Page": "" if next_page is None else str(next_page)})


def make_version(version_id: int, sha: str) -> dict[str, Any]:
    """Build one diff version list entry."""
    return {"id": version_id, "head_commit_sha": sha}


def version_detail(sha: str, *files: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Build a collected diff version detail response."""
    return {
        "head_commit_sha": sha,
        "state": "collected",
        "real_size": str(len(files)),
        "diffs": list(files),
        **overrides,
    }


def mock_versions(*versions: dict[str, Any]) -> respx.Route:
    """Return the requested diff versions without relying on API response order."""
    return respx.get(VERSIONS_URL).mock(
        return_value=httpx.Response(200, json=list(versions), headers={"X-Next-Page": ""}),
    )


def serve_pages(pages: dict[int, list[dict[str, Any]]]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer each diffs request with the page it asks for. A page that exists sets X-Next-Page to the next one."""

    def handler(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params["page"])
        next_page = number + 1 if number + 1 in pages else None
        return files_response(*pages[number], next_page=next_page)

    return handler


def requested_pages(route: respx.Route) -> list[int]:
    """Return the `page` query parameter of every REST request the route received, in order."""
    return [int(call.request.url.params["page"]) for call in route.calls]


def sent_variables(route: respx.Route) -> list[dict[str, Any]]:
    """Return the GraphQL variables of every request the route received, in order."""
    return [json.loads(call.request.content)["variables"] for call in route.calls]


def paths_of(data: dict[str, Any]) -> list[str]:
    """Return the new paths of the patches in a tool result, in order."""
    return [diff["path"] for diff in data.get("diffs", [])]


def encode_cursor(raw: str) -> str:
    """Encode raw text the same way the server encodes cursors, so tests can build bad ones."""
    return base64.urlsafe_b64encode(raw.encode()).decode()


async def call_tool(**arguments: Any) -> dict[str, Any]:
    """Call get_merge_request_diff with the default project and IID and return its data."""
    return await call_mcp_tool("get_merge_request_diff", {"project": PROJECT, "iid": IID, **arguments})


# Shared pages for the budget and page-limit scenarios, reused by several tests.
BUDGET_PAGES = {
    1: [
        make_file("a.py", diff=BIG),
        make_file("unrelated.py"),  # Not requested, but it still takes a slot in the page.
        make_file("b.py", diff=BIG),
        make_file("c.py", diff=BIG),
        make_file("d.py"),
    ],
    2: [make_file("e.py")],
}
BUDGET_PATHS = ["a.py", "b.py", "c.py", "d.py", "e.py"]
PAGE_LIMIT_PAGES = {n: [make_file(f"f{n}.py")] for n in range(1, MAX_PAGES + 2)}
PAGE_LIMIT_PATHS = [f"f{n}.py" for n in range(1, MAX_PAGES + 2)]


def files_query() -> httpx.Response:
    """Answer the files query with one file, for tests that only care about the REST diffs."""
    return httpx.Response(200, json=graphql_body(HEAD_REFS, [make_stat("a.py", 3, 1)]))


@pytest.mark.parametrize("arguments", [{}, {"paths": []}], ids=["no-paths", "empty-paths"])
@respx.mock
async def test_file_list_mode_maps_diff_stats_and_head_sha(gitlab_env, arguments):
    """Without paths, GraphQL stats stay intact and REST patches enrich known file anchors."""
    # Arrange
    graphql = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json=graphql_body(HEAD_REFS, [make_stat("a.py", 3, 1), make_stat("docs/b.md", 0, 7)]),
        ),
    )
    rest = respx.get(DIFFS_URL).mock(
        side_effect=serve_pages(
            {1: [make_file("a.py"), make_file("docs/b.md", diff="@@ -1,2 +1,0 @@\n-old\n-old2\n")]},
        ),
    )

    # Act
    data = await call_tool(**arguments)

    # Assert
    assert sent_variables(graphql) == [{"fullPath": PROJECT, "iid": str(IID)}]
    assert requested_pages(rest) == [1]
    assert data == {
        "head_sha": HEAD_SHA,
        "hint": "Pass paths=[...] to read patches.",
        "files": [
            {"path": "a.py", "additions": 3, "deletions": 1, "new_line_ranges": [[1, 1]], "ranges_complete": True},
            {
                "path": "docs/b.md",
                "additions": 0,
                "deletions": 7,
                "new_line_ranges": [],
                "ranges_complete": True,
            },
        ],
    }


@pytest.mark.parametrize(
    ("diff_refs", "stats", "expected"),
    [
        (None, None, {}),  # GitLab has no head SHA and no stats, so both fields are left out.
        (HEAD_REFS, None, {"head_sha": HEAD_SHA}),
    ],
)
@respx.mock
async def test_null_diff_refs_and_stats_are_left_out(gitlab_env, diff_refs, stats, expected):
    """Null diffRefs and diffStats mean unknown, so the fields are left out instead of failing."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=graphql_body(diff_refs, stats)))

    # Act
    data = await call_tool()

    # Assert
    assert data == expected


@respx.mock
async def test_file_list_mode_ignores_cursor(gitlab_env):
    """The cursor only matters for patches, so a bad cursor does not fail the file list."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    rest = respx.get(DIFFS_URL).mock(side_effect=serve_pages({1: [make_file("a.py")]}))

    # Act
    data = await call_tool(cursor="not base64!!")

    # Assert
    assert data["files"] == [
        {"path": "a.py", "additions": 3, "deletions": 1, "new_line_ranges": [[1, 1]], "ranges_complete": True},
    ]
    assert requested_pages(rest) == [1]


@respx.mock
async def test_file_list_ranges_include_context_and_follow_multiple_hunks(gitlab_env):
    """Each nonempty new-side hunk is an inclusive range, including its context lines."""
    # Arrange
    stats = [make_stat("src.py", 2, 2)]
    patch = "@@ -2,3 +2,4 @@\n context\n-old\n+new\n context\n@@ -20 +21 @@\n-old\n+new\n"
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=graphql_body(HEAD_REFS, stats)))
    rest = respx.get(DIFFS_URL).mock(side_effect=serve_pages({1: [make_file("src.py", diff=patch)]}))

    # Act
    data = await call_tool()

    # Assert
    assert requested_pages(rest) == [1]
    assert data["files"] == [
        {
            "path": "src.py",
            "additions": 2,
            "deletions": 2,
            "new_line_ranges": [[2, 5], [21, 21]],
            "ranges_complete": True,
        },
    ]


@respx.mock
async def test_file_list_marks_missing_patch_ranges_incomplete(gitlab_env):
    """A GraphQL file with no returned REST patch cannot be treated as having no anchors."""
    # Arrange
    stats = [make_stat("present.py"), make_stat("missing.py")]
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=graphql_body(HEAD_REFS, stats)))
    rest = respx.get(DIFFS_URL).mock(side_effect=serve_pages({1: [make_file("present.py")]}))

    # Act
    data = await call_tool()

    # Assert
    assert requested_pages(rest) == [1]
    assert data["files"] == [
        {
            "path": "present.py",
            "additions": 1,
            "deletions": 0,
            "new_line_ranges": [[1, 1]],
            "ranges_complete": True,
        },
        {
            "path": "missing.py",
            "additions": 1,
            "deletions": 0,
            "new_line_ranges": [],
            "ranges_complete": False,
        },
    ]


@pytest.mark.parametrize(
    "file_options",
    [{"diff": ""}, {"collapsed": True}, {"too_large": True}],
    ids=["empty", "collapsed", "too-large"],
)
@respx.mock
async def test_file_list_marks_unavailable_patch_ranges_incomplete(gitlab_env, file_options):
    """Empty, collapsed, and too-large patches do not prove that a file has no anchors."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    respx.get(DIFFS_URL).mock(side_effect=serve_pages({1: [make_file("a.py", **file_options)]}))

    # Act
    data = await call_tool()

    # Assert
    assert data["files"] == [
        {"path": "a.py", "additions": 3, "deletions": 1, "new_line_ranges": [], "ranges_complete": False},
    ]


@respx.mock
async def test_file_list_page_limit_marks_unscanned_ranges_incomplete(gitlab_env):
    """Files beyond the bounded REST scan keep their GraphQL stats and unknown range status."""
    # Arrange
    stats = [make_stat("f1.py"), make_stat(f"f{MAX_PAGES + 1}.py")]
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=graphql_body(HEAD_REFS, stats)))
    rest = respx.get(DIFFS_URL).mock(side_effect=serve_pages(PAGE_LIMIT_PAGES))

    # Act
    data = await call_tool()

    # Assert
    assert requested_pages(rest) == list(range(1, MAX_PAGES + 1))
    assert data["files"] == [
        {
            "path": "f1.py",
            "additions": 1,
            "deletions": 0,
            "new_line_ranges": [[1, 1]],
            "ranges_complete": True,
        },
        {
            "path": f"f{MAX_PAGES + 1}.py",
            "additions": 1,
            "deletions": 0,
            "new_line_ranges": [],
            "ranges_complete": False,
        },
    ]


@respx.mock
async def test_patch_mode_requests_encoded_project_path_and_page_size(gitlab_env):
    """The project path is URL-encoded and the REST page holds 100 files."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    rest = respx.get(DIFFS_URL).mock(side_effect=serve_pages({1: [make_file("a.py")]}))

    # Act
    await call_tool(paths=["a.py"])

    # Assert
    assert str(rest.calls.last.request.url) == f"{DIFFS_URL}?per_page=100&page=1"


@pytest.mark.parametrize(
    ("requested", "expected_paths", "expected_not_found"),
    [
        ("new.py", ["new.py"], []),  # Matches the new path.
        ("old.py", ["new.py"], []),  # Matches the old path of a renamed file.
        ("other.py", [], ["other.py"]),  # Matches nothing, so it is reported after the full scan.
    ],
)
@respx.mock
async def test_path_matches_new_or_old_path(gitlab_env, requested, expected_paths, expected_not_found):
    """A requested path matches a file when it equals the file's new or old path."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    respx.get(DIFFS_URL).mock(
        side_effect=serve_pages({1: [make_file("new.py", "old.py", renamed_file=True), make_file("keep.py")]}),
    )

    # Act
    data = await call_tool(paths=[requested])

    # Assert
    assert paths_of(data) == expected_paths
    assert data.get("paths_not_found", []) == expected_not_found
    assert data["head_sha"] == HEAD_SHA


@pytest.mark.parametrize(
    ("flags", "old_path", "expected_status", "expected_old_path"),
    [
        ({"new_file": True}, None, "added", None),
        ({"deleted_file": True}, None, "deleted", None),
        ({"renamed_file": True}, "old.py", "renamed", "old.py"),  # Only a rename has a different old path.
        ({}, None, "modified", None),
    ],
    ids=["added", "deleted", "renamed", "modified"],
)
@respx.mock
async def test_status_and_old_path_follow_the_file_change(
    gitlab_env, flags, old_path, expected_status, expected_old_path
):
    """Each change kind maps to one status. old_path is left out unless it differs from the new path."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    respx.get(DIFFS_URL).mock(side_effect=serve_pages({1: [make_file("new.py", old_path, **flags)]}))

    # Act
    data = await call_tool(paths=["new.py"])

    # Assert
    diff = data["diffs"][0]
    assert diff["status"] == expected_status
    assert diff.get("old_path") == expected_old_path


@pytest.mark.parametrize(
    ("raw_flag", "output_key"),
    [("generated_file", "generated"), ("collapsed", "collapsed"), ("too_large", "too_large")],
)
@respx.mock
async def test_flags_pass_through_and_are_left_out_when_false(gitlab_env, raw_flag, output_key):
    """A true flag is kept, and a false flag is left out to save tokens."""
    # Arrange
    flag: dict[str, Any] = {raw_flag: True}
    respx.post(GRAPHQL).mock(return_value=files_query())
    respx.get(DIFFS_URL).mock(
        side_effect=serve_pages({1: [make_file("set.py", **flag), make_file("plain.py")]}),
    )

    # Act
    data = await call_tool(paths=["set.py", "plain.py"])

    # Assert
    assert data["diffs"][0][output_key] is True
    assert output_key not in data["diffs"][1]


@respx.mock
async def test_empty_patch_text_is_left_out(gitlab_env):
    """GitLab sends an empty diff for a too-large file, so the diff field is left out."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    respx.get(DIFFS_URL).mock(side_effect=serve_pages({1: [make_file("huge.bin", diff="", too_large=True)]}))

    # Act
    data = await call_tool(paths=["huge.bin"])

    # Assert
    assert data["diffs"] == [{"path": "huge.bin", "status": "modified", "too_large": True}]


@respx.mock
async def test_follows_next_page_and_keeps_api_order(gitlab_env):
    """Pages are read in order until X-Next-Page is empty, and matches keep the API order, not the request order."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    rest = respx.get(DIFFS_URL).mock(
        side_effect=serve_pages(
            {
                1: [make_file("a.py"), make_file("skipped.py")],
                2: [make_file("b.py")],
                3: [make_file("c.py")],
            },
        ),
    )

    # Act
    data = await call_tool(paths=["c.py", "a.py", "b.py"])

    # Assert
    assert requested_pages(rest) == [1, 2, 3]
    assert paths_of(data) == ["a.py", "b.py", "c.py"]
    assert "next_cursor" not in data


@pytest.mark.parametrize("headers", [{"X-Next-Page": ""}, {}], ids=["empty-header", "missing-header"])
@respx.mock
async def test_empty_or_missing_next_page_ends_the_scan(gitlab_env, headers):
    """Both an empty and a missing X-Next-Page mean there is no next page."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    rest = respx.get(DIFFS_URL).mock(
        return_value=httpx.Response(200, json=[make_file("a.py")], headers=headers),
    )

    # Act
    data = await call_tool(paths=["a.py"])

    # Assert
    assert rest.call_count == 1
    assert "next_cursor" not in data


@respx.mock
async def test_budget_stops_with_cursor_and_resume_has_no_gaps_or_repeats(gitlab_env):
    """A call stops before the file that would pass the budget, and the next call starts exactly there."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    rest = respx.get(DIFFS_URL).mock(side_effect=serve_pages(BUDGET_PAGES))

    # Act
    first = await call_tool(paths=BUDGET_PATHS)
    second = await call_tool(paths=BUDGET_PATHS, cursor=first["next_cursor"])

    # Assert
    assert paths_of(first) == ["a.py", "b.py"]
    assert paths_of(second) == ["c.py", "d.py", "e.py"]
    assert "next_cursor" not in second
    assert requested_pages(rest) == [1, 1, 2]  # The first call stopped before page 2.


@respx.mock
async def test_single_oversized_diff_is_capped_and_flagged(gitlab_env):
    """One patch over the budget is still returned, cut to the budget and marked truncated."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    huge = "y" * (DIFF_BUDGET + 5000)
    respx.get(DIFFS_URL).mock(
        side_effect=serve_pages({1: [make_file("big.py", diff=huge), make_file("small.py")]}),
    )

    # Act
    data = await call_tool(paths=["big.py", "small.py"])

    # Assert
    assert paths_of(data) == ["big.py"]
    assert data["diffs"][0]["diff"] == "y" * DIFF_BUDGET
    assert data["diffs"][0]["truncated"] is True
    assert "next_cursor" in data


@respx.mock
async def test_page_limit_stops_with_a_cursor_at_the_next_page(gitlab_env):
    """After MAX_PAGES pages the call stops at the next page, and the next call reads from there."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    rest = respx.get(DIFFS_URL).mock(side_effect=serve_pages(PAGE_LIMIT_PAGES))

    # Act
    first = await call_tool(paths=PAGE_LIMIT_PATHS)
    second = await call_tool(paths=PAGE_LIMIT_PATHS, cursor=first["next_cursor"])

    # Assert
    assert paths_of(first) == PAGE_LIMIT_PATHS[:MAX_PAGES]
    assert paths_of(second) == PAGE_LIMIT_PATHS[MAX_PAGES:]
    assert "next_cursor" not in second
    assert requested_pages(rest) == list(range(1, MAX_PAGES + 2))


@respx.mock
async def test_paths_not_found_is_reported_after_a_full_scan(gitlab_env):
    """A path that no file matches is listed once every page was read."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    respx.get(DIFFS_URL).mock(side_effect=serve_pages({1: [make_file("a.py")]}))

    # Act
    data = await call_tool(paths=["a.py", "missing.py"])

    # Assert
    assert paths_of(data) == ["a.py"]
    assert data["paths_not_found"] == ["missing.py"]


@pytest.mark.parametrize(
    ("pages", "matching"),
    [(BUDGET_PAGES, BUDGET_PATHS), (PAGE_LIMIT_PAGES, PAGE_LIMIT_PATHS)],
    ids=["budget", "page-limit"],
)
@respx.mock
async def test_paths_not_found_is_left_out_of_partial_scans(gitlab_env, pages, matching):
    """A partial scan cannot prove a path is missing, so the list is left out, even on the resumed call."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    respx.get(DIFFS_URL).mock(side_effect=serve_pages(pages))
    requested = [*matching, "missing.py"]

    # Act
    first = await call_tool(paths=requested)
    second = await call_tool(paths=requested, cursor=first["next_cursor"])

    # Assert
    assert "next_cursor" in first
    assert "paths_not_found" not in first
    assert "paths_not_found" not in second


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64!!",
        encode_cursor("not json"),
        encode_cursor('["a", "list"]'),
        encode_cursor('{"page": 0, "skip": 0}'),
        encode_cursor('{"page": true, "skip": 0}'),
        encode_cursor('{"page": "2", "skip": 0}'),
        encode_cursor('{"page": 1, "skip": -1}'),
        encode_cursor('{"page": 1, "skip": false}'),
        encode_cursor('{"page": 1}'),
    ],
)
@respx.mock
async def test_invalid_cursor_is_rejected_before_any_request(gitlab_env, cursor):
    """A cursor that is not one we issued is rejected before any request is sent."""
    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool(paths=["a.py"], cursor=cursor)
    assert respx.calls.call_count == 0


@pytest.mark.parametrize("paths", [None, ["a.py"]], ids=["file-list", "patches"])
@respx.mock
async def test_missing_project_is_reported(gitlab_env, paths):
    """GitLab returns project null for unknown or invisible projects, in both modes."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"project": None}}))

    # Act / Assert
    with pytest.raises(ToolError, match="not found, or the token cannot see it"):
        await call_tool(paths=paths)


@respx.mock
async def test_missing_merge_request_is_reported(gitlab_env):
    """A null mergeRequest names the IID and the project."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"project": {"mergeRequest": None}}}))

    # Act / Assert
    with pytest.raises(ToolError, match=r"Merge request !2187 not found in 'group/project'"):
        await call_tool(paths=["a.py"])


@respx.mock
async def test_rest_http_error_gets_a_hint(gitlab_env):
    """A 403 from the diffs endpoint becomes a ToolError with the scope hint."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    respx.get(DIFFS_URL).mock(return_value=httpx.Response(403))

    # Act / Assert
    with pytest.raises(ToolError, match="does not have the scope"):
        await call_tool(paths=["a.py"])


@respx.mock
async def test_missing_flags_default_to_false(gitlab_env):
    """Older GitLab versions may leave out collapsed, too_large, and generated_file. A missing flag means false."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=files_query())
    raw = make_file("a.py")
    for key in ("collapsed", "too_large", "generated_file"):
        del raw[key]
    respx.get(DIFFS_URL).mock(side_effect=serve_pages({1: [raw]}))

    # Act
    data = await call_tool(paths=["a.py"])

    # Assert
    assert data["diffs"] == [{"path": "a.py", "status": "modified", "diff": DEFAULT_DIFF}]


@pytest.mark.parametrize("real_size", [1, "1"])
@respx.mock
async def test_since_accepts_string_and_integer_file_counts(gitlab_env, real_size):
    """Collected snapshots accept GitLab's string count and equivalent integer counts."""
    # Arrange
    mock_versions(make_version(1, OLD_SHA), make_version(2, HEAD_SHA))
    respx.get(f"{VERSIONS_URL}/1").mock(return_value=httpx.Response(200, json=version_detail(OLD_SHA)))
    respx.get(f"{VERSIONS_URL}/2").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(HEAD_SHA, make_file("new.py"), real_size=real_size),
        ),
    )

    # Act
    data = await call_tool(since_sha=OLD_SHA)

    # Assert
    assert data["since_version_found"] is True
    assert [row["path"] for row in data["files"]] == ["new.py"]
    assert "warning" not in data


@pytest.mark.parametrize("headers", ["", "--- a/new.py\n+++ b/new.py\n"])
@respx.mock
async def test_since_counts_header_like_content_inside_hunks(gitlab_env, headers):
    """Triple-sign content lines count as changes, unlike optional file headers outside hunks."""
    # Arrange
    mock_versions(make_version(1, OLD_SHA), make_version(2, HEAD_SHA))
    respx.get(f"{VERSIONS_URL}/1").mock(return_value=httpx.Response(200, json=version_detail(OLD_SHA)))
    respx.get(f"{VERSIONS_URL}/2").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(HEAD_SHA, make_file("new.py", diff=f"{headers}@@ -1 +1 @@\n---old\n+++new\n")),
        ),
    )

    # Act
    data = await call_tool(since_sha=OLD_SHA)

    # Assert
    assert data["files"][0]["additions"] == 1
    assert data["files"][0]["deletions"] == 1


@respx.mock
async def test_since_ignores_only_rebased_hunk_ranges_and_skips_graphql(gitlab_env):
    """A target-only rebase changes numeric hunk offsets, not the patch content."""
    # Arrange
    mock_versions(make_version(9, HEAD_SHA), make_version(3, OLD_SHA))
    respx.get(f"{VERSIONS_URL}/3").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(OLD_SHA, make_file("same.py", diff="@@ -4,2 +8,2 @@ fn\n-old\n+new\n")),
        ),
    )
    respx.get(f"{VERSIONS_URL}/9").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(HEAD_SHA, make_file("same.py", diff="@@ -104,2 +208,2 @@ fn\n-old\n+new\n")),
        ),
    )

    # Act
    data = await call_tool(since_sha=OLD_SHA)

    # Assert
    assert data == {"head_sha": HEAD_SHA, "since_version_found": True}
    assert {call.request.method for call in respx.calls} == {"GET"}


@respx.mock
async def test_since_file_list_reports_changed_added_deleted_and_renamed_files(gitlab_env):
    """The file list contains only changed snapshot entries with patch-derived counts and current anchors."""
    # Arrange
    mock_versions(make_version(1, OLD_SHA), make_version(7, HEAD_SHA))
    respx.get(f"{VERSIONS_URL}/1").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(
                OLD_SHA,
                make_file("edit.py", diff="@@ -1 +1 @@\n-old\n+same\n"),
                make_file("gone.py", diff="@@ -1 +0,0 @@\n-old\n", deleted_file=True),
                make_file("new.py", old_path="old.py", renamed_file=True, diff="@@ -1 +1 @@\n-old\n+new\n"),
            ),
        ),
    )
    respx.get(f"{VERSIONS_URL}/7").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(
                HEAD_SHA,
                make_file("edit.py", diff="@@ -20 +40 @@\n-old\n+changed\n"),
                make_file("gone.py", diff="@@ -1 +0,0 @@\n-old\n", deleted_file=True),
                make_file("added.py", diff="@@ -0,0 +1 @@\n+new\n", new_file=True),
                make_file("new.py", old_path="old.py", diff="@@ -1 +1 @@\n-old\n+newer\n", renamed_file=True),
            ),
        ),
    )

    # Act
    data = await call_tool(since_sha=OLD_SHA)

    # Assert
    assert data["head_sha"] == HEAD_SHA
    assert data["since_version_found"] is True
    assert [row["path"] for row in data["files"]] == ["edit.py", "added.py", "new.py"]
    assert data["files"] == [
        {"path": "edit.py", "additions": 1, "deletions": 1, "new_line_ranges": [[40, 40]], "ranges_complete": True},
        {"path": "added.py", "additions": 1, "deletions": 0, "new_line_ranges": [[1, 1]], "ranges_complete": True},
        {"path": "new.py", "additions": 1, "deletions": 1, "new_line_ranges": [[1, 1]], "ranges_complete": True},
    ]


@respx.mock
async def test_since_paths_return_latest_added_deleted_and_renamed_patches(gitlab_env):
    """Path mode keeps the latest MR patch shape and matches either side of a rename."""
    # Arrange
    mock_versions(make_version(1, OLD_SHA), make_version(2, HEAD_SHA))
    respx.get(f"{VERSIONS_URL}/1").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(
                OLD_SHA,
                make_file("gone.py", diff="@@ -1 +1 @@\n-old\n+old\n"),
                make_file("new.py", old_path="old.py", renamed_file=True),
            ),
        ),
    )
    respx.get(f"{VERSIONS_URL}/2").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(
                HEAD_SHA,
                make_file("gone.py", diff="@@ -1 +0,0 @@\n-old\n", deleted_file=True),
                make_file("added.py", diff="@@ -0,0 +1 @@\n+new\n", new_file=True),
                make_file(
                    "new.py",
                    old_path="old.py",
                    renamed_file=True,
                    diff="@@ -4 +6 @@\n-old\n+updated\n",
                ),
            ),
        ),
    )

    # Act
    data = await call_tool(since_sha=OLD_SHA, paths=["gone.py", "added.py", "old.py"])

    # Assert
    assert data["diffs"] == [
        {"path": "gone.py", "status": "deleted", "diff": "@@ -1 +0,0 @@\n-old\n"},
        {"path": "added.py", "status": "added", "diff": "@@ -0,0 +1 @@\n+new\n"},
        {
            "path": "new.py",
            "old_path": "old.py",
            "status": "renamed",
            "diff": "@@ -4 +6 @@\n-old\n+updated\n",
        },
    ]
    assert "paths_not_found" not in data


@pytest.mark.parametrize(
    ("latest_old_path", "renamed", "patch"),
    [
        ("new.py", False, "@@ -1 +1 @@\n-old\n+updated\n"),
        ("new.py", False, DEFAULT_DIFF),
        ("other.py", True, DEFAULT_DIFF),
    ],
)
@respx.mock
async def test_since_changed_rename_origin_returns_one_latest_entry(gitlab_env, latest_old_path, renamed, patch):
    """Destination paths identify files, and only the latest rename origin matches requested paths."""
    # Arrange
    mock_versions(make_version(1, OLD_SHA), make_version(2, HEAD_SHA))
    respx.get(f"{VERSIONS_URL}/1").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(OLD_SHA, make_file("new.py", old_path="old.py", renamed_file=True)),
        ),
    )
    respx.get(f"{VERSIONS_URL}/2").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(
                HEAD_SHA,
                make_file("new.py", old_path=latest_old_path, renamed_file=renamed, diff=patch),
            ),
        ),
    )

    # Act
    listing = await call_tool(since_sha=OLD_SHA)
    patches = await call_tool(since_sha=OLD_SHA, paths=["new.py", "old.py", latest_old_path])

    # Assert
    assert listing["files"] == [
        {"path": "new.py", "additions": 1, "deletions": 1, "new_line_ranges": [[1, 1]], "ranges_complete": True},
    ]
    expected_diff = {"path": "new.py", "status": "renamed" if renamed else "modified", "diff": patch}
    if renamed:
        expected_diff["old_path"] = latest_old_path
    assert patches["diffs"] == [expected_diff]
    assert patches["paths_not_found"] == ["old.py"]
    assert "warning" not in listing
    assert "warning" not in patches


@respx.mock
async def test_since_returns_reverted_files_with_empty_patch_and_warning(gitlab_env):
    """A file present only in the old snapshot is visible but has no current patch anchors."""
    # Arrange
    mock_versions(make_version(1, OLD_SHA), make_version(2, HEAD_SHA))
    respx.get(f"{VERSIONS_URL}/1").mock(
        return_value=httpx.Response(200, json=version_detail(OLD_SHA, make_file("reverted.py"))),
    )
    respx.get(f"{VERSIONS_URL}/2").mock(return_value=httpx.Response(200, json=version_detail(HEAD_SHA)))

    # Act
    data = await call_tool(since_sha=OLD_SHA, paths=["reverted.py"])

    # Assert
    assert data["diffs"] == [{"path": "reverted.py", "status": "modified"}]
    assert "paths_not_found" not in data
    assert "empty patches and no valid anchors" in data["warning"]


@respx.mock
async def test_since_unknown_sha_warns_without_falling_back_to_full_diff(gitlab_env):
    """An unknown SHA is explicit and does not trigger a current-diff or GraphQL fallback."""
    # Arrange
    versions = mock_versions(make_version(2, HEAD_SHA), make_version(1, OLD_SHA))
    diff_route = respx.get(DIFFS_URL).mock(return_value=files_response(make_file("current.py")))

    # Act
    data = await call_tool(since_sha=UNKNOWN_SHA)

    # Assert
    assert data == {
        "head_sha": HEAD_SHA,
        "since_version_found": False,
        "warning": f"No diff version has head commit {UNKNOWN_SHA}; no full diff was returned.",
    }
    assert versions.call_count == 1
    assert diff_route.call_count == 0
    assert not any(call.request.method == "POST" for call in respx.calls)


@respx.mock
async def test_since_scans_paginated_versions_and_selects_highest_version_id(gitlab_env):
    """Version pages are bounded and response order does not determine the latest snapshot."""
    # Arrange
    page_calls: list[int] = []

    def versions_handler(request: httpx.Request) -> httpx.Response:
        """Return two unordered version pages."""
        page = int(request.url.params["page"])
        page_calls.append(page)
        entries = [make_version(5, HEAD_SHA)] if page == 1 else [make_version(2, OLD_SHA)]
        return httpx.Response(200, json=entries, headers={"X-Next-Page": "2" if page == 1 else ""})

    respx.get(VERSIONS_URL).mock(side_effect=versions_handler)
    respx.get(f"{VERSIONS_URL}/2").mock(return_value=httpx.Response(200, json=version_detail(OLD_SHA)))
    respx.get(f"{VERSIONS_URL}/5").mock(return_value=httpx.Response(200, json=version_detail(HEAD_SHA)))

    # Act
    data = await call_tool(since_sha=OLD_SHA)

    # Assert
    assert page_calls == [1, 2]
    assert data["head_sha"] == HEAD_SHA
    assert data["since_version_found"] is True


@respx.mock
async def test_since_version_scan_exhaustion_is_explicit(gitlab_env):
    """A still-paginated version list after MAX_PAGES does not claim a complete comparison."""

    # Arrange
    def versions_handler(request: httpx.Request) -> httpx.Response:
        """Keep reporting another page until the scan budget is exhausted."""
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[make_version(page, OLD_SHA if page == 1 else HEAD_SHA)],
            headers={"X-Next-Page": str(page + 1)},
        )

    route = respx.get(VERSIONS_URL).mock(side_effect=versions_handler)

    # Act
    data = await call_tool(since_sha=OLD_SHA)

    # Assert
    assert route.call_count == MAX_PAGES
    assert "stopped after" in data["warning"]
    assert "since_version_found" not in data
    assert "files" not in data


@pytest.mark.parametrize(
    ("detail_options", "file_options", "expected_warning"),
    [
        ({"overflow": True}, {}, "overflowed"),
        ({"state": "collecting"}, {}, "not fully collected"),
        ({"real_size": 2}, {}, "incomplete file list"),
        ({"real_size": "2"}, {}, "incomplete file list"),
        ({"real_size": "1000+"}, {}, "incomplete file list"),
        ({"real_size": None}, {}, "incomplete file list"),
        ({"real_size": True}, {}, "incomplete file list"),
        ({}, {"collapsed": True}, "collapsed or too-large"),
        ({}, {"diff": ""}, "unusable file patch"),
    ],
)
@respx.mock
async def test_since_incomplete_snapshots_warn_without_claiming_no_changes(
    gitlab_env, detail_options, file_options, expected_warning
):
    """Incomplete GitLab version data stops comparison instead of producing a false empty result."""
    # Arrange
    mock_versions(make_version(1, OLD_SHA), make_version(2, HEAD_SHA))
    respx.get(f"{VERSIONS_URL}/1").mock(
        return_value=httpx.Response(200, json=version_detail(OLD_SHA, make_file("old.py"))),
    )
    respx.get(f"{VERSIONS_URL}/2").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(HEAD_SHA, make_file("new.py", **file_options), **detail_options),
        ),
    )

    # Act
    data = await call_tool(since_sha=OLD_SHA)

    # Assert
    assert data["since_version_found"] is True
    assert "files" not in data
    assert expected_warning in data["warning"]


@respx.mock
async def test_since_path_cursor_resumes_with_same_inputs_and_rejects_changed_paths(gitlab_env):
    """Snapshot cursors resume by file index and remain bound to since SHA and requested paths."""
    # Arrange
    mock_versions(make_version(1, OLD_SHA), make_version(2, HEAD_SHA))
    respx.get(f"{VERSIONS_URL}/1").mock(return_value=httpx.Response(200, json=version_detail(OLD_SHA)))
    respx.get(f"{VERSIONS_URL}/2").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(HEAD_SHA, *(make_file(f"f{i}.py", diff=BIG) for i in range(3))),
        ),
    )

    # Act
    first = await call_tool(since_sha=OLD_SHA, paths=["f0.py", "f1.py", "f2.py"])
    before_changed_input = respx.calls.call_count
    second = await call_tool(since_sha=OLD_SHA, paths=["f0.py", "f1.py", "f2.py"], cursor=first["next_cursor"])

    # Assert
    assert paths_of(first) == ["f0.py", "f1.py"]
    assert paths_of(second) == ["f2.py"]
    assert "next_cursor" not in second
    assert respx.calls.call_count > before_changed_input
    request_count = respx.calls.call_count
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool(since_sha=OLD_SHA, paths=["f0.py"], cursor=first["next_cursor"])
    assert respx.calls.call_count == request_count


@respx.mock
async def test_since_cursor_rejects_a_new_latest_head(gitlab_env):
    """A continuation cursor cannot be reused after the latest diff version changes."""
    # Arrange
    newest_sha = "3096f2d5a1b2c3d4e5f60718293a4b5c6d7e8f90"
    versions_calls = 0

    def versions_handler(request: httpx.Request) -> httpx.Response:
        """Advance the latest version after the first list request."""
        nonlocal versions_calls
        versions_calls += 1
        latest_sha = HEAD_SHA if versions_calls == 1 else newest_sha
        return httpx.Response(
            200,
            json=[make_version(1, OLD_SHA), make_version(2 if versions_calls == 1 else 3, latest_sha)],
            headers={"X-Next-Page": ""},
        )

    respx.get(VERSIONS_URL).mock(side_effect=versions_handler)
    respx.get(f"{VERSIONS_URL}/1").mock(return_value=httpx.Response(200, json=version_detail(OLD_SHA)))
    respx.get(f"{VERSIONS_URL}/2").mock(
        return_value=httpx.Response(
            200,
            json=version_detail(
                HEAD_SHA,
                make_file("big.py", diff=BIG),
                make_file("next.py", diff=BIG),
                make_file("last.py", diff=BIG),
            ),
        ),
    )

    # Act
    first = await call_tool(since_sha=OLD_SHA, paths=["big.py", "next.py", "last.py"])

    # Assert
    assert "next_cursor" in first
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool(since_sha=OLD_SHA, paths=["big.py", "next.py", "last.py"], cursor=first["next_cursor"])
