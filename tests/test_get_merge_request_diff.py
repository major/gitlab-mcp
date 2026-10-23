"""Tests for the get_merge_request_diff tool, run through a real in-memory MCP client 📄."""

import base64
import json
from typing import Any
lazy from collections.abc import Callable

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import DIFF_BUDGET, MAX_PAGES, mcp

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 2187
DIFFS_URL = "https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/2187/diffs"
HEAD_SHA = "6096f2d5a1b2c3d4e5f60718293a4b5c6d7e8f90"  # Full SHA from GitLab. Output keeps the first 12 characters.
SHORT_HEAD_SHA = "6096f2d5a1b2"
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
    """Call get_merge_request_diff through the MCP protocol and return its data."""
    async with Client(mcp) as client:
        result = await client.call_tool("get_merge_request_diff", {"project": PROJECT, "iid": IID, **arguments})
    return result.structured_content


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
    """Without paths, the tool returns the diffStats as a file list and never calls the REST diffs endpoint."""
    # Arrange: no REST route is registered, so a REST call would fail the test.
    graphql = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json=graphql_body(HEAD_REFS, [make_stat("a.py", 3, 1), make_stat("docs/b.md", 0, 7)]),
        ),
    )

    # Act
    data = await call_tool(**arguments)

    # Assert
    assert sent_variables(graphql) == [{"fullPath": PROJECT, "iid": str(IID)}]
    assert [call.request.url.path for call in respx.calls] == ["/api/graphql"]
    assert data == {
        "head_sha": SHORT_HEAD_SHA,
        "files": [
            {"path": "a.py", "additions": 3, "deletions": 1},
            {"path": "docs/b.md", "additions": 0, "deletions": 7},
        ],
    }


@pytest.mark.parametrize(
    ("diff_refs", "stats", "expected"),
    [
        (None, None, {}),  # GitLab has no head SHA and no stats, so both fields are left out.
        (HEAD_REFS, None, {"head_sha": SHORT_HEAD_SHA}),
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

    # Act
    data = await call_tool(cursor="not base64!!")

    # Assert
    assert data["files"] == [{"path": "a.py", "additions": 3, "deletions": 1}]
    assert [call.request.url.path for call in respx.calls] == ["/api/graphql"]


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
    assert data["head_sha"] == SHORT_HEAD_SHA


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
