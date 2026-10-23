"""Tests for the get_merge_request_threads tool, run through a real in-memory MCP client 🧵."""

import base64
import json
from typing import Any
lazy from collections.abc import Callable

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import (
    MAX_BATCH_MERGE_REQUESTS,
    MAX_PAGES,
    NOTE_CAP,
    THREAD_HEADING_BUDGET,
    THREAD_HEADING_CAP,
    mcp,
)

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 2187
HEADING_THREADS_PER_PAGE = 50
HEADING_PAGE_COUNT = 4
HEADING_THREAD_COUNT = HEADING_THREADS_PER_PAGE * HEADING_PAGE_COUNT


def gid(thread_id: str) -> str:
    """Return the full GID that GitLab uses for a discussion."""
    return f"gid://gitlab/Discussion/{thread_id}"


def make_position(old_path: str, new_path: str | None = None) -> dict[str, Any]:
    """Build a diff position on line 212 of the new file."""
    return {"oldPath": old_path, "newPath": new_path or old_path, "oldLine": None, "newLine": 212}


def make_note(
    body: str = "Please rename this.",
    *,
    system: bool = False,
    author: str | None = "mhayden",
    created_at: str = "2026-10-20T19:43:02Z",
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one note. A null position means a general note, not a diff comment."""
    return {
        "system": system,
        "body": body,
        "createdAt": created_at,
        "author": {"username": author} if author else None,
        "position": position,
    }


def make_discussion(
    thread_id: str,
    *notes: dict[str, Any],
    resolvable: bool = True,
    resolved: bool = False,
    notes_have_next: bool = False,
) -> dict[str, Any]:
    """Build one discussion. With no notes given, it gets one human comment."""
    return {
        "id": gid(thread_id),
        "resolvable": resolvable,
        "resolved": resolved,
        "notes": {"pageInfo": {"hasNextPage": notes_have_next}, "nodes": list(notes) or [make_note()]},
    }


def page(*discussions: dict[str, Any], has_next: bool = False, end_cursor: str | None = None) -> dict[str, Any]:
    """Build one GraphQL response body with a page of discussions."""
    connection = {"pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor}, "nodes": list(discussions)}
    return {"data": {"project": {"mergeRequest": {"discussions": connection}}}}


def respond(body: dict[str, Any]) -> httpx.Response:
    """Wrap a GraphQL body in an HTTP 200 response."""
    return httpx.Response(200, json=body)


def serve(pages: dict[str | None, dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer each GraphQL request with the page that matches its `after` cursor."""

    def handler(request: httpx.Request) -> httpx.Response:
        return respond(pages[json.loads(request.content)["variables"]["after"]])

    return handler


def chained_pages(count: int) -> dict[str | None, dict[str, Any]]:
    """Build `count` pages with one thread each. Page n is fetched with cursor c{n}, and page 0 with None."""
    afters: list[str | None] = [None, *(f"c{n}" for n in range(1, count))]
    return {
        after: page(make_discussion(f"t{n}"), has_next=n < count - 1, end_cursor=f"c{n + 1}")
        for n, after in enumerate(afters)
    }


def sent_variables(route: respx.Route) -> list[dict[str, Any]]:
    """Return the GraphQL variables of every request the route received, in order."""
    return [json.loads(call.request.content)["variables"] for call in route.calls]


def requested_afters(route: respx.Route) -> list[str | None]:
    """Return the `after` cursor of every request the route received, in order."""
    return [variables["after"] for variables in sent_variables(route)]


def thread_ids(data: dict[str, Any]) -> list[str]:
    """Return the IDs of the threads in a tool result, in order."""
    return [thread["id"] for thread in data["threads"]]


async def call_tool(**arguments: Any) -> dict[str, Any]:
    """Call get_merge_request_threads through the MCP protocol and return its data."""
    tool_arguments = {"project": PROJECT, **arguments}
    if "iid" not in arguments and "iids" not in arguments:
        tool_arguments["iid"] = IID
    async with Client(mcp) as client:
        result = await client.call_tool("get_merge_request_threads", tool_arguments)
    return result.structured_content


def encode_cursor(raw: str) -> str:
    """Encode raw text the same way the server encodes cursors, so tests can build bad ones."""
    return base64.urlsafe_b64encode(raw.encode()).decode()


@respx.mock
async def test_batch_returns_threads_attributed_to_each_merge_request(gitlab_env):
    """Batch results preserve unique request order, attribute threads, and list missing MRs."""

    # Arrange
    def handler(request: httpx.Request) -> httpx.Response:
        iid = json.loads(request.content)["variables"]["iid"]
        if iid == "42":
            return httpx.Response(200, json={"data": {"project": {"mergeRequest": None}}})
        return respond(page(make_discussion(f"thread-{iid}")))

    route = respx.post(GRAPHQL).mock(side_effect=handler)

    # Act
    data = await call_tool(iids=[2187, 42, 2187, 7])

    # Assert
    assert [result["iid"] for result in data["merge_requests"]] == [2187, 7]
    assert [thread_ids(result) for result in data["merge_requests"]] == [["thread-2187"], ["thread-7"]]
    assert data["missing_iids"] == [42]
    assert [variables["iid"] for variables in sent_variables(route)] == ["2187", "42", "7"]


@pytest.mark.parametrize(
    "iids",
    [[], [0], [-1], list(range(1, 22))],
    ids=["empty", "zero", "negative", "over-20-unique"],
)
@respx.mock
async def test_batch_rejects_invalid_iid_lists_before_network(gitlab_env, iids):
    """Empty, non-positive, and over-limit unique IID lists do not send requests."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(page()))

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(iids=iids)
    assert route.call_count == 0


@respx.mock
async def test_batch_accepts_twenty_unique_iids(gitlab_env):
    """The maximum number of unique batch IIDs is accepted."""
    # Arrange
    iids = list(range(1, MAX_BATCH_MERGE_REQUESTS + 1))
    route = respx.post(GRAPHQL).mock(return_value=respond(page()))

    # Act
    data = await call_tool(iids=iids)

    # Assert
    assert [result["iid"] for result in data["merge_requests"]] == iids
    assert route.call_count == len(iids)


@respx.mock
async def test_batch_accepts_more_than_twenty_raw_duplicate_iids(gitlab_env):
    """Repeated raw IIDs count once toward the unique IID limit."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(page()))

    # Act
    data = await call_tool(iids=[7] * 25)

    # Assert
    assert [result["iid"] for result in data["merge_requests"]] == [7]
    assert route.call_count == 1


@pytest.mark.parametrize(
    "arguments",
    [{"iid": IID, "iids": [42]}],
    ids=["both"],
)
@respx.mock
async def test_exactly_one_iid_argument_is_required(gitlab_env, arguments):
    """The tool rejects calls that pass both IID modes."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(page()))

    # Act / Assert
    with pytest.raises(ToolError, match="exactly one of iid or iids"):
        await call_tool(**arguments)
    assert route.call_count == 0


@respx.mock
async def test_omitting_both_iid_arguments_is_rejected(gitlab_env):
    """The raw MCP call rejects omitted IID arguments instead of using helper defaults."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(page()))

    # Act / Assert
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="exactly one of iid or iids"):
            await client.call_tool("get_merge_request_threads", {"project": PROJECT})
    assert route.call_count == 0


@respx.mock
async def test_batch_rejects_cursor_before_network(gitlab_env):
    """Batch results have independent cursors and cannot be resumed as a batch."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(page()))

    # Act / Assert
    with pytest.raises(ToolError, match="Batch thread calls do not accept cursor"):
        await call_tool(iids=[42], cursor="some-cursor")
    assert route.call_count == 0


@pytest.mark.parametrize("failure", ["missing-project", "network"], ids=["project-error", "network-error"])
@respx.mock
async def test_batch_propagates_project_and_network_errors(gitlab_env, failure):
    """Only missing merge requests are collected; project and connection failures propagate."""
    # Arrange
    if failure == "missing-project":
        route = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"project": None}}))
    else:
        route = respx.post(GRAPHQL).mock(side_effect=httpx.ConnectError("offline"))

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(iids=[42])
    assert route.call_count == 1


@respx.mock
async def test_filters_and_headings_apply_to_every_batch_result(gitlab_env):
    """State, file, and author filters plus compact headings are applied to each MR scan."""

    # Arrange
    def handler(request: httpx.Request) -> httpx.Response:
        iid = json.loads(request.content)["variables"]["iid"]
        discussions = page(
            make_discussion(
                f"selected-{iid}",
                make_note("## Selected heading", author="alice", position=make_position("wanted.md")),
                resolved=True,
            ),
            make_discussion(
                f"wrong-state-{iid}",
                make_note("## Wrong state", author="alice", position=make_position("wanted.md")),
            ),
            make_discussion(
                f"wrong-file-{iid}",
                make_note("## Wrong file", author="alice", position=make_position("other.md")),
                resolved=True,
            ),
            make_discussion(
                f"wrong-author-{iid}",
                make_note("## Wrong author", author="bob", position=make_position("wanted.md")),
                resolved=True,
            ),
        )
        return respond(discussions)

    route = respx.post(GRAPHQL).mock(side_effect=handler)

    # Act
    data = await call_tool(iids=[5, 8], state="resolved", file="wanted.md", author="alice", view="headings")

    # Assert
    assert [result["iid"] for result in data["merge_requests"]] == [5, 8]
    assert [thread_ids(result) for result in data["merge_requests"]] == [["selected-5"], ["selected-8"]]
    assert [result["threads"][0]["heading"] for result in data["merge_requests"]] == [
        "Selected heading",
        "Selected heading",
    ]
    assert all("notes" not in result["threads"][0] for result in data["merge_requests"])
    assert [variables["iid"] for variables in sent_variables(route)] == ["5", "8"]


@respx.mock
async def test_thread_ids_and_missing_ids_are_evaluated_per_merge_request(gitlab_env):
    """The shared thread ID filter selects independently, and each MR reports its own absent IDs."""

    # Arrange
    def handler(request: httpx.Request) -> httpx.Response:
        iid = json.loads(request.content)["variables"]["iid"]
        discussion_id = "wanted" if iid == "5" else "other"
        return respond(page(make_discussion(discussion_id)))

    respx.post(GRAPHQL).mock(side_effect=handler)

    # Act
    data = await call_tool(iids=[5, 8], ids=["wanted"])

    # Assert
    first, second = data["merge_requests"]
    assert [result["iid"] for result in data["merge_requests"]] == [5, 8]
    assert thread_ids(first) == ["wanted"]
    assert "not_found_ids" not in first
    assert second["threads"] == []
    assert second["not_found_ids"] == ["wanted"]


@respx.mock
async def test_each_batch_cursor_resumes_with_a_single_merge_request_call(gitlab_env):
    """Every MR gets its own output cursor, which resumes through the existing single-IID mode."""
    # Arrange
    discussions = [make_discussion(f"t{index}", *(make_note("x" * NOTE_CAP) for _ in range(3))) for index in range(3)]
    route = respx.post(GRAPHQL).mock(return_value=respond(page(*discussions)))

    # Act
    first = await call_tool(iids=[5, 8])
    resumed = [await call_tool(iid=result["iid"], cursor=result["next_cursor"]) for result in first["merge_requests"]]

    # Assert
    assert [thread_ids(result) for result in first["merge_requests"]] == [["t0", "t1"], ["t0", "t1"]]
    assert all(result["next_cursor"] for result in first["merge_requests"])
    assert [thread_ids(result) for result in resumed] == [["t2"], ["t2"]]
    assert [variables["iid"] for variables in sent_variables(route)] == ["5", "8", "5", "8"]


@respx.mock
async def test_sends_variables_and_maps_fields(gitlab_env):
    """Fields map to their output names, the position comes from the first human note, and dates are trimmed."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=respond(
            page(
                make_discussion(
                    "t1",
                    make_note("assigned to @mhayden", system=True, author="bot"),  # Skipped, even though it is first.
                    make_note("Rename this.", created_at="2026-10-21T08:00:00Z", position=make_position("docs/sop.md")),
                    make_note("Done.", author="bkearney", created_at="2026-10-22T09:30:00Z"),
                ),
            ),
        ),
    )

    # Act
    data = await call_tool()

    # Assert
    assert sent_variables(route) == [{"fullPath": PROJECT, "iid": str(IID), "after": None}]
    assert data == {
        "threads": [
            {
                "id": "t1",
                "status": "open",
                "path": "docs/sop.md",
                "line": 212,
                "notes": [
                    {"by": "mhayden", "body": "Rename this.", "at": "2026-10-21"},
                    {"by": "bkearney", "body": "Done.", "at": "2026-10-22"},
                ],
            },
        ],
    }


@respx.mock
async def test_headings_view_uses_root_markdown_heading_and_location(gitlab_env):
    """Headings view returns the root note summary without bodies or replies."""
    # Arrange
    root = make_note(
        "Introduction\n\n## Replace parser ###\nDetails\n<!-- review-bot:finding hidden detail -->",
        author="root-author",
        position={"oldPath": "src/parser.py", "newPath": "src/parser.py", "oldLine": 18, "newLine": None},
    )
    reply = make_note("## Reply heading\nReply body", author="reply-author", created_at="2026-10-22T09:30:00Z")
    system_note = make_note("updated the title", system=True, author="bot", created_at="2026-10-23T09:30:00Z")
    respx.post(GRAPHQL).mock(
        return_value=respond(page(make_discussion("t1", root, reply, system_note, notes_have_next=True))),
    )

    # Act
    data = await call_tool(view="headings")

    # Assert
    assert data["threads"] == [
        {
            "id": "t1",
            "status": "open",
            "path": "src/parser.py",
            "old_line": 18,
            "by": "root-author",
            "heading": "Replace parser",
            "last_by": "reply-author",
            "last_at": "2026-10-22",
            "note_count": 2,
            "notes_truncated": True,
        },
    ]


@respx.mock
async def test_headings_view_uses_newest_human_note_with_deleted_author(gitlab_env):
    """A deleted latest author is empty, while trailing system notes are not counted as the latest note."""
    # Arrange
    root = make_note("## Root", author="root", created_at="2026-10-20T12:00:00Z")
    newest = make_note("Reply", author=None, created_at="2026-10-22T12:00:00Z")
    older_reply = make_note("Older reply", author="reply", created_at="2026-10-21T12:00:00Z")
    system_note = make_note("assigned", system=True, author="bot", created_at="2026-10-23T12:00:00Z")
    respx.post(GRAPHQL).mock(
        return_value=respond(
            page(make_discussion("t1", root, newest, older_reply, system_note, notes_have_next=True)),
        ),
    )

    # Act
    data = await call_tool(view="headings")

    # Assert
    assert data["threads"][0] == {
        "id": "t1",
        "status": "open",
        "by": "root",
        "heading": "Root",
        "last_by": "",
        "last_at": "2026-10-22",
        "note_count": 3,
        "notes_truncated": True,
    }


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("  \n First useful line \nAnother line", "First useful line"),
        ("# " + "x" * 180, "x" * 160),
        ("<!-- review-bot:finding only marker -->", ""),
    ],
    ids=["first-nonempty-line", "heading-cap", "stripped-review-marker"],
)
@respx.mock
async def test_headings_view_falls_back_and_caps_heading(gitlab_env, body, expected):
    """Without a Markdown heading, headings view uses the first nonempty line and caps it."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1", make_note(body)))))

    # Act
    data = await call_tool(view="headings")

    # Assert
    assert data["threads"][0]["heading"] == expected


@respx.mock
async def test_headings_view_keeps_default_open_state_filter(gitlab_env):
    """Open state still excludes resolved, plain-comment, and system-only discussions in headings view."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=respond(
            page(
                make_discussion("open"),
                make_discussion("resolved", resolved=True),
                make_discussion("comment", resolvable=False),
                make_discussion("system", make_note(system=True)),
            ),
        ),
    )

    # Act
    data = await call_tool(state="open", view="headings")

    # Assert
    assert thread_ids(data) == ["open"]


@respx.mock
async def test_headings_view_reduces_serialized_size_by_at_least_seventy_percent(gitlab_env):
    """Compact root headings omit large reply bodies and substantially reduce output size."""
    # Arrange
    notes = [make_note(f"## Root summary\n{'x' * 5000}", author="root")]
    notes.extend(make_note(f"Reply {'y' * 5000}", author="reply") for _ in range(11))
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("large", *notes))))

    # Act
    full = await call_tool()
    headings = await call_tool(view="headings")

    # Assert
    full_size = len(json.dumps(full["threads"], separators=(",", ":")))
    headings_size = len(json.dumps(headings["threads"], separators=(",", ":")))
    assert headings_size * 10 <= full_size * 3
    assert headings["threads"][0]["heading"] == "Root summary"
    assert "notes" not in headings["threads"][0]


@respx.mock
async def test_headings_view_budget_cursor_resumes_without_gaps(gitlab_env):
    """The compact output budget returns a cursor and resumes at the exact next thread."""
    # Arrange
    discussions = [
        make_discussion(f"t{index}", make_note("# " + str(index).ljust(180, "x")))
        for index in range(HEADING_THREAD_COUNT)
    ]
    pages = {
        None if page_index == 0 else f"c{page_index}": page(
            *discussions[page_index * HEADING_THREADS_PER_PAGE : (page_index + 1) * HEADING_THREADS_PER_PAGE],
            has_next=page_index < HEADING_PAGE_COUNT - 1,
            end_cursor=f"c{page_index + 1}",
        )
        for page_index in range(HEADING_PAGE_COUNT)
    }
    route = respx.post(GRAPHQL).mock(side_effect=serve(pages))

    # Act
    gathered: list[str] = []
    cursor = None
    calls = 0
    while True:
        data = await call_tool(view="headings", cursor=cursor)
        calls += 1
        assert len(json.dumps(data["threads"], separators=(",", ":"))) <= THREAD_HEADING_BUDGET
        gathered.extend(thread_ids(data))
        if not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]

    # Assert
    assert calls > 1
    assert len(requested_afters(route)) > HEADING_PAGE_COUNT
    assert gathered == [f"t{index}" for index in range(HEADING_THREAD_COUNT)]
    assert all(len(thread["heading"]) <= THREAD_HEADING_CAP for thread in data["threads"])


@respx.mock
async def test_null_author_and_general_note_leave_out_fields(gitlab_env):
    """A deleted author and a general note (no position) leave out their fields."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1", make_note(author=None)))))

    # Act
    data = await call_tool()

    # Assert
    thread = data["threads"][0]
    assert set(thread) == {"id", "status", "notes"}
    assert thread["notes"] == [{"body": "Please rename this.", "at": "2026-10-20"}]


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        (make_position("docs/sop.md"), {"path": "docs/sop.md", "line": 212}),  # Same old and new path: no old_path.
        (make_position("old.md", "new.md"), {"path": "new.md", "old_path": "old.md", "line": 212}),  # A rename.
        (
            {"oldPath": "docs/sop.md", "newPath": "docs/sop.md", "oldLine": 40, "newLine": None},
            {"path": "docs/sop.md", "old_line": 40},  # A comment on a deleted line: old_line, not line.
        ),
        (None, {}),  # A general thread has no location fields at all.
    ],
    ids=["same-path", "renamed", "deleted-line", "general"],
)
@respx.mock
async def test_location_fields_are_deduplicated(gitlab_env, position, expected):
    """A thread names its file once, and shows old_path or old_line only when they add information."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1", make_note(position=position)))))

    # Act
    data = await call_tool()

    # Assert
    location = {key: value for key, value in data["threads"][0].items() if key not in {"id", "status", "notes"}}
    assert location == expected


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, [("open", "open")]),  # open is the default state.
        ({"state": "open"}, [("open", "open")]),
        ({"state": "resolved"}, [("resolved", "resolved")]),
        ({"state": "comments"}, [("comment", "comment")]),
        ({"state": "all"}, [("open", "open"), ("resolved", "resolved"), ("comment", "comment")]),
    ],
)
@respx.mock
async def test_state_filters_threads(gitlab_env, arguments, expected):
    """Each state picks its own threads. System-only discussions are never listed."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=respond(
            page(
                make_discussion("open"),
                make_discussion("resolved", resolved=True),
                make_discussion("comment", resolvable=False),
                make_discussion("system", make_note(system=True)),  # Would be open, but has no human note.
            ),
        ),
    )

    # Act
    data = await call_tool(**arguments)

    # Assert
    assert [(thread["id"], thread["status"]) for thread in data["threads"]] == [
        (thread_id, status) for thread_id, status in expected
    ]


@pytest.mark.parametrize("requested", [gid("resolved"), "resolved"], ids=["full-gid", "bare-sha"])
@respx.mock
async def test_ids_bypass_state_and_accept_bare_shas(gitlab_env, requested):
    """Explicit IDs return a thread whatever its state, and a bare SHA gets the GID prefix."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=respond(page(make_discussion("resolved", resolved=True), make_discussion("open"))),
    )

    # Act
    data = await call_tool(ids=[requested])  # The default state is open, but ids win.

    # Assert
    assert thread_ids(data) == ["resolved"]
    assert data["threads"][0]["status"] == "resolved"


@respx.mock
async def test_not_found_ids_are_reported_after_a_full_scan(gitlab_env):
    """An ID that was never seen is listed, but an ID on a system-only discussion counts as seen."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=respond(
            page(
                make_discussion("t1"),
                make_discussion("system", make_note(system=True)),
            ),
        ),
    )

    # Act
    data = await call_tool(ids=["t1", "system", "missing"])

    # Assert
    assert thread_ids(data) == ["t1"]
    assert data["not_found_ids"] == ["missing"]


@respx.mock
async def test_not_found_ids_absent_when_scan_stops_early(gitlab_env):
    """A partial scan cannot prove an ID is missing, so the list is left out."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1"), has_next=True, end_cursor="c1")))

    # Act
    data = await call_tool(ids=["missing"])

    # Assert
    assert "next_cursor" in data
    assert "not_found_ids" not in data


@respx.mock
async def test_not_found_ids_absent_on_a_resumed_call(gitlab_env):
    """A resumed call did not read the first page, so it cannot prove an ID is missing either."""
    # Arrange
    respx.post(GRAPHQL).mock(side_effect=serve(chained_pages(MAX_PAGES + 1)))
    first = await call_tool(ids=["missing"])

    # Act
    second = await call_tool(ids=["missing"], cursor=first["next_cursor"])

    # Assert
    assert "next_cursor" not in second
    assert "not_found_ids" not in second


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        ("old.md", ["renamed"]),  # Matches the old path of a renamed file.
        ("new.md", ["renamed"]),  # Matches the new path of a renamed file.
        ("other.md", ["other"]),
        ("missing.md", []),
    ],
)
@respx.mock
async def test_file_matches_old_or_new_path(gitlab_env, file, expected):
    """The file filter is an exact match on the old or the new path."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=respond(
            page(
                make_discussion("renamed", make_note(position=make_position("old.md", "new.md"))),
                make_discussion("other", make_note(position=make_position("other.md"))),
            ),
        ),
    )

    # Act
    data = await call_tool(file=file)

    # Assert
    assert thread_ids(data) == expected


@pytest.mark.parametrize(
    ("author", "expected"),
    [
        ("mhayden", ["t1"]),  # The thread starter.
        ("bkearney", ["t1"]),  # A reply in the same thread.
        ("carol", ["t2"]),
        ("bot", []),  # Only a system note, which does not count.
        ("nobody", []),
    ],
)
@respx.mock
async def test_author_matches_any_human_note(gitlab_env, author, expected):
    """The author filter matches the starter or any reply, but never a system note."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=respond(
            page(
                make_discussion("t1", make_note(author="mhayden"), make_note("Thanks.", author="bkearney")),
                make_discussion("t2", make_note(system=True, author="bot"), make_note(author="carol")),
            ),
        ),
    )

    # Act
    data = await call_tool(author=author)

    # Assert
    assert thread_ids(data) == expected


@pytest.mark.parametrize(
    ("length", "expected_suffix"),
    [(NOTE_CAP, ""), (NOTE_CAP + 1, " [truncated]")],
)
@respx.mock
async def test_note_bodies_are_capped_with_a_marker(gitlab_env, length, expected_suffix):
    """A body longer than the cap is cut and marked. A body at the cap is kept whole."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1", make_note("x" * length)))))

    # Act
    data = await call_tool()

    # Assert
    assert data["threads"][0]["notes"][0]["body"] == "x" * NOTE_CAP + expected_suffix


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Review this. \n<!-- review-bot:finding detail -->  \n", "Review this."),
        ("<!-- review-bot:finding detail -->", ""),
        ("Before <!-- review-bot:finding detail -->\nAfter", "Before <!-- review-bot:finding detail -->\nAfter"),
        ("Before <!-- another comment -->", "Before <!-- another comment -->"),
        (
            "Before <!-- review-bot:finding detail --> <!-- another comment -->",
            "Before <!-- review-bot:finding detail --> <!-- another comment -->",
        ),
        ("Before <!-- review-bot:finding detail", "Before <!-- review-bot:finding detail"),
        (
            "Before <!-- review-bot:finding malformed <!-- another comment -->",
            "Before <!-- review-bot:finding malformed <!-- another comment -->",
        ),
        ("Before <!-- review-bot:findingXYZ detail -->", "Before <!-- review-bot:findingXYZ detail -->"),
        ("Before <!-- review-bot:finding--> detail -->", "Before <!-- review-bot:finding--> detail -->"),
        ("Body with ordinary whitespace \n", "Body with ordinary whitespace \n"),
    ],
    ids=[
        "trailing",
        "only-marker",
        "embedded",
        "other-comment",
        "other-trailing-comment",
        "unclosed",
        "malformed-before-comment",
        "no-boundary",
        "empty-boundary",
        "whitespace",
    ],
)
@respx.mock
async def test_only_trailing_finding_comments_are_removed(gitlab_env, body, expected):
    """Only a well-formed trailing review-bot finding comment and its preceding whitespace are removed."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1", make_note(body)))))

    # Act
    data = await call_tool()

    # Assert
    assert data["threads"][0]["notes"][0]["body"] == expected


@pytest.mark.parametrize(
    ("max_body_chars", "body", "expected"),
    [
        (0, "abcdef", ""),
        (3, "abcdef", "abc [truncated]"),
        (3, "abc", "abc"),
        (NOTE_CAP, "x" * (NOTE_CAP + 1), "x" * NOTE_CAP + " [truncated]"),
    ],
    ids=["omit", "short-cap", "at-cap", "maximum-cap"],
)
@respx.mock
async def test_max_body_chars_controls_truncation(gitlab_env, max_body_chars, body, expected):
    """The requested body cap truncates after marker removal, while zero omits all body text."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1", make_note(body)))))

    # Act
    data = await call_tool(max_body_chars=max_body_chars)

    # Assert
    assert data["threads"][0]["notes"][0]["body"] == expected


@pytest.mark.parametrize("max_body_chars", [-1, NOTE_CAP + 1])
@respx.mock
async def test_max_body_chars_rejects_values_outside_bounds(gitlab_env, max_body_chars):
    """The requested body cap must be between zero and NOTE_CAP, inclusive."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1"))))

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(max_body_chars=max_body_chars)


@respx.mock
async def test_marker_is_removed_before_body_budget_and_cursor_accounting(gitlab_env):
    """Stripped finding comments do not consume the thread body budget or cause an early cursor."""
    # Arrange
    body = "x" * 3500 + " <!-- review-bot:finding " + "d" * 500 + " -->"
    notes = [make_note(body) for _ in range(4)]
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1", *notes), make_discussion("t2", *notes))))

    # Act
    data = await call_tool()

    # Assert
    assert thread_ids(data) == ["t1", "t2"]
    assert not data.get("next_cursor")
    assert all(note["body"] == "x" * 3500 for thread in data["threads"] for note in thread["notes"])


@respx.mock
async def test_custom_body_cap_is_used_when_resuming_budget_cursor(gitlab_env):
    """A resumed scan uses the caller's cap to budget bodies and return the remaining threads."""
    # Arrange: each of 50 threads has 50 notes, so a cap of three allows exactly 40 threads in the first result.
    discussions = [make_discussion(f"t{index}", *(make_note("xxxx") for _ in range(50))) for index in range(50)]
    route = respx.post(GRAPHQL).mock(return_value=respond(page(*discussions)))

    # Act
    first = await call_tool(max_body_chars=3)
    second = await call_tool(max_body_chars=3, cursor=first["next_cursor"])

    # Assert
    assert thread_ids(first) == [f"t{index}" for index in range(40)]
    assert thread_ids(second) == [f"t{index}" for index in range(40, 50)]
    assert all(note["body"] == "xxx [truncated]" for thread in second["threads"] for note in thread["notes"])
    assert not second.get("next_cursor")
    assert requested_afters(route) == [None, None]


@pytest.mark.parametrize(("notes_have_next", "expected"), [(True, True), (False, None)])
@respx.mock
async def test_notes_truncated_flags_more_than_fifty_notes(gitlab_env, notes_have_next, expected):
    """The flag is set only when GitLab has more notes than the first page holds, and is left out otherwise."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=respond(page(make_discussion("t1", notes_have_next=notes_have_next))),
    )

    # Act
    data = await call_tool()

    # Assert
    assert data["threads"][0].get("notes_truncated") is expected


@respx.mock
async def test_budget_stops_and_cursor_resumes_without_gaps_or_repeats(gitlab_env):
    """A call stops before the thread that would pass the budget, and the next call starts exactly there."""
    # Arrange: t3, t4, and t5 hold three full-size notes each, 12000 characters, so only two fit in the budget.
    big_notes = [make_note("x" * NOTE_CAP) for _ in range(3)]
    route = respx.post(GRAPHQL).mock(
        side_effect=serve(
            {
                None: page(make_discussion("t1"), make_discussion("t2"), has_next=True, end_cursor="c1"),
                "c1": page(
                    *(make_discussion(f"t{n}", *big_notes) for n in (3, 4, 5)),
                    has_next=True,
                    end_cursor="c2",
                ),
                "c2": page(make_discussion("t6")),
            },
        ),
    )

    # Act
    first = await call_tool()
    second = await call_tool(cursor=first["next_cursor"])

    # Assert
    assert thread_ids(first) == [f"t{n}" for n in (1, 2, 3, 4)]
    assert thread_ids(second) == ["t5", "t6"]
    assert "next_cursor" not in second
    assert requested_afters(route) == [None, "c1", "c1", "c2"]


@respx.mock
async def test_first_thread_is_returned_even_when_it_alone_is_over_budget(gitlab_env):
    """The first matching thread is always returned, so a big thread cannot block the list forever."""
    # Arrange: eight notes of 5000 characters are capped to 4012 each, about 32000 in total.
    notes = [make_note("y" * 5000) for _ in range(8)]
    huge = make_discussion("huge", *notes)
    respx.post(GRAPHQL).mock(return_value=respond(page(huge, make_discussion("small"))))

    # Act
    data = await call_tool()

    # Assert
    assert thread_ids(data) == ["huge"]
    assert len(data["threads"][0]["notes"]) == len(notes)
    assert "next_cursor" in data


@respx.mock
async def test_page_limit_stops_with_a_cursor_at_the_next_page(gitlab_env):
    """After MAX_PAGES pages the call stops at the next page's start, and the next call reads from there."""
    # Arrange: one thread per page, and more pages than one call may scan.
    route = respx.post(GRAPHQL).mock(side_effect=serve(chained_pages(MAX_PAGES + 1)))

    # Act
    first = await call_tool()
    second = await call_tool(cursor=first["next_cursor"])

    # Assert
    assert thread_ids(first) == [f"t{n}" for n in range(MAX_PAGES)]
    assert thread_ids(second) == [f"t{MAX_PAGES}"]  # Nothing skipped at the page start.
    assert "next_cursor" not in second
    assert requested_afters(route) == [None, *(f"c{n}" for n in range(1, MAX_PAGES + 1))]


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64!!",
        encode_cursor("not json"),
        encode_cursor('["a", "list"]'),
        encode_cursor('{"after": 5, "skip": 0}'),
        encode_cursor('{"after": null, "skip": -1}'),
        encode_cursor('{"after": null}'),
    ],
)
@respx.mock
async def test_invalid_cursor_is_rejected(gitlab_env, cursor):
    """A cursor that is not one we issued is rejected before any request is sent."""
    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool(cursor=cursor)


@respx.mock
async def test_missing_project_is_reported(gitlab_env):
    """GitLab returns project null for unknown or invisible projects."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"project": None}}))

    # Act / Assert
    with pytest.raises(ToolError, match="not found, or the token cannot see it"):
        await call_tool()


@respx.mock
async def test_missing_merge_request_is_reported(gitlab_env):
    """A null mergeRequest names the IID and the project."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"project": {"mergeRequest": None}}}))

    # Act / Assert
    with pytest.raises(ToolError, match=r"Merge request !2187 not found in 'group/project'"):
        await call_tool()
