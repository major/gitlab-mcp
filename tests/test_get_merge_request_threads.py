"""Tests for the get_merge_request_threads tool, run through a real in-memory MCP client 🧵."""

import base64
import hashlib
import json
from typing import Any
lazy from collections.abc import Callable

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp import mcp
from gitlab_mcp.common import MAX_BATCH_MERGE_REQUESTS, MAX_PAGES
from gitlab_mcp.tools.mr_threads import (
    NOTE_CAP,
    THREAD_BODY_BUDGET,
    THREAD_CUT_MARKER,
    THREAD_HEADING_BUDGET,
    THREAD_HEADING_CAP,
)
from mcp_helpers import call_tool as call_mcp_tool

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


def make_note(  # noqa: PLR0913 - optional note fields keep GraphQL fixtures readable.
    body: str = "Please rename this.",
    *,
    system: bool = False,
    note_id: int = 1,
    author: str | None = "mhayden",
    created_at: str = "2026-10-20T19:43:02Z",
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one note. A null position means a general note, not a diff comment."""
    return {
        "id": f"gid://gitlab/Note/{note_id}",
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


def page(  # noqa: PLR0913 - optional MR fields keep GraphQL response fixtures readable.
    *discussions: dict[str, Any],
    has_next: bool = False,
    end_cursor: str | None = None,
    source_branch: str | None = None,
    head_sha: str | None = None,
    merge_status: str | None = None,
    pipeline_status: str | None = None,
) -> dict[str, Any]:
    """Build one GraphQL response body with discussions and optional MR status fields."""
    connection = {"pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor}, "nodes": list(discussions)}
    merge_request = {"discussions": connection}
    if source_branch is not None:
        merge_request["sourceBranch"] = source_branch
    if head_sha is not None:
        merge_request["diffRefs"] = {"headSha": head_sha}
    if merge_status is not None:
        merge_request["detailedMergeStatus"] = merge_status
    if any(value is not None for value in (source_branch, head_sha, merge_status, pipeline_status)):
        merge_request["headPipeline"] = {"status": pipeline_status} if pipeline_status is not None else None
    return {"data": {"project": {"mergeRequest": merge_request}}}


def counts_page(total: int, resolved: int) -> dict[str, Any]:
    """Build a GraphQL response with exact merge request discussion totals."""
    return {
        "data": {
            "project": {
                "mergeRequest": {
                    "resolvableDiscussionsCount": total,
                    "resolvedDiscussionsCount": resolved,
                },
            },
        },
    }


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
    """Call get_merge_request_threads with the default project and IID and return its data."""
    tool_arguments = {"project": PROJECT, **arguments}
    if "iid" not in arguments and "iids" not in arguments:
        tool_arguments["iid"] = IID
    return await call_mcp_tool("get_merge_request_threads", tool_arguments)


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
async def test_batch_rejects_thread_ids_before_network(gitlab_env):
    """Thread IDs are ambiguous across MRs, so the filter is limited to single-IID calls."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(page()))

    # Act / Assert
    with pytest.raises(ToolError, match="ids can only be used with a single iid"):
        await call_tool(iids=[5, 8], ids=["wanted"])
    assert route.call_count == 0


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
                    make_note("Done.", note_id=2, author="bkearney", created_at="2026-10-22T09:30:00Z"),
                ),
            ),
        ),
    )

    # Act
    data = await call_tool()

    # Assert
    assert sent_variables(route) == [{"fullPath": PROJECT, "iid": str(IID), "after": None, "includeMrStatus": True}]
    assert data == {
        "threads": [
            {
                "id": "t1",
                "status": "open",
                "path": "docs/sop.md",
                "line": 212,
                "notes": [
                    {
                        "note_id": 1,
                        "body_hash": hashlib.sha256(b"Rename this.").hexdigest(),
                        "by": "mhayden",
                        "body": "Rename this.",
                        "at": "2026-10-21",
                    },
                    {
                        "note_id": 2,
                        "body_hash": hashlib.sha256(b"Done.").hexdigest(),
                        "by": "bkearney",
                        "body": "Done.",
                        "at": "2026-10-22",
                    },
                ],
            },
        ],
    }


@respx.mock
async def test_note_ids_and_raw_hashes_cover_root_and_reply(gitlab_env):
    """Note and DiffNote GIDs become distinct REST IDs with hashes of the untouched UTF-8 bodies."""
    # Arrange
    root_body = "Résumé 🧪\n\n<!-- review-bot:finding hidden -->\n"
    reply_body = "Reply with trailing space  "
    root = make_note(root_body, author="alice") | {"id": "gid://gitlab/DiffNote/123"}
    reply = make_note(reply_body, author="bob") | {"id": "gid://gitlab/Note/456"}
    route = respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1", root, reply))))

    # Act
    results = [
        await call_tool(view="full"),
        await call_tool(view="finding"),
        await call_tool(view="full", max_body_chars=3),
        await call_tool(view="finding", max_body_chars=0),
    ]
    headings = await call_tool(view="headings")

    # Assert
    expected_hashes = [hashlib.sha256(body.encode("utf-8")).hexdigest() for body in (root_body, reply_body)]
    for result in results:
        notes = result["threads"][0]["notes"]
        assert [note["note_id"] for note in notes] == [123, 456]
        assert [note["body_hash"] for note in notes] == expected_hashes
        assert [note["by"] for note in notes] == ["alice", "bob"]
        assert [note["at"] for note in notes] == ["2026-10-20", "2026-10-20"]
    assert results[0]["threads"][0]["notes"][0]["body"] == "Résumé 🧪"
    assert results[2]["threads"][0]["notes"][0]["body"] == "Rés [truncated]"
    assert all(note["body"] == "" for note in results[3]["threads"][0]["notes"])
    assert "notes" not in headings["threads"][0]
    assert route.call_count == len(results) + 1
    assert "id system body createdAt" in json.loads(route.calls[0].request.content)["query"]


@pytest.mark.parametrize(
    "bad_id",
    [
        "missing",
        None,
        True,
        123,
        "123",
        "gid://gitlab/User/123",
        "gid://other/Note/123",
        "gid://gitlab/Note/0",
        "gid://gitlab/Note/-1",
        "gid://gitlab/Note/1/2",
        "gid://gitlab/Note/\uff11\uff12\uff13",
        "gid://gitlab/Note/" + "9" * 5000,
    ],
    ids=[
        "missing",
        "null",
        "bool",
        "integer",
        "bare",
        "wrong-type",
        "wrong-host",
        "zero",
        "negative",
        "extra-path",
        "unicode-digits",
        "too-large",
    ],
)
@respx.mock
async def test_malformed_graphql_note_ids_are_rejected(gitlab_env, bad_id):
    """Invalid note identities never turn into plausible but wrong REST note IDs."""
    # Arrange
    note = make_note() | {"id": bad_id}
    if bad_id == "missing":
        del note["id"]
    route = respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1", note))))

    # Act / Assert
    with pytest.raises(ToolError, match="invalid note ID"):
        await call_tool()
    assert route.call_count == 1


@pytest.mark.parametrize("view", ["full", "finding"])
@respx.mock
async def test_zero_body_output_budget_accounts_for_note_metadata(gitlab_env, view):
    """Large metadata-only pages remain bounded and resume without missing or repeating threads."""
    # Arrange
    discussions = [make_discussion(f"t{index}", *(make_note("content") for _ in range(25))) for index in range(12)]
    route = respx.post(GRAPHQL).mock(return_value=respond(page(*discussions)))

    # Act
    first = await call_tool(view=view, max_body_chars=0)
    second = await call_tool(view=view, max_body_chars=0, cursor=first["next_cursor"])

    # Assert
    assert len(json.dumps(first["threads"], ensure_ascii=False, separators=(",", ":"))) <= THREAD_BODY_BUDGET
    assert len(json.dumps(second["threads"], ensure_ascii=False, separators=(",", ":"))) <= THREAD_BODY_BUDGET
    assert thread_ids(first) + thread_ids(second) == [f"t{index}" for index in range(len(discussions))]
    assert "next_cursor" not in second
    assert route.call_count == len((first, second))


@pytest.mark.parametrize("view", ["full", "headings"])
@respx.mock
async def test_bot_author_display_is_compact_and_filter_uses_username(gitlab_env, view):
    """Bot labels stay compact in every view while author filters use their original usernames."""
    # Arrange
    root = make_note(
        "## Root bot note",
        created_at="2026-10-20T12:00:00Z",
    ) | {"author": {"username": "review-bot", "bot": True, "name": "****", "id": "gid://gitlab/User/123"}}
    latest = make_note("Bot reply", created_at="2026-10-21T12:00:00Z") | {
        "author": {
            "username": "review-bot-reply",
            "bot": True,
            "name": "Review Reply Bot",
            "id": "gid://gitlab/User/987",
        },
    }
    route = respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("bot-thread", root, latest))))

    # Act
    data = await call_tool(author="review-bot", view=view)
    short_name_filter = await call_tool(author="bot:123", view=view)

    # Assert
    assert thread_ids(data) == ["bot-thread"]
    if view == "full":
        assert [note["by"] for note in data["threads"][0]["notes"]] == ["bot:123", "Review Reply Bot"]
    else:
        assert data["threads"][0]["by"] == "bot:123"
        assert data["threads"][0]["last_by"] == "Review Reply Bot"
    assert short_name_filter["threads"] == []
    query = json.loads(route.calls[0].request.content)["query"]
    assert "author { username bot name id }" in query


@respx.mock
async def test_returns_single_merge_request_triage_headers_with_headings(gitlab_env):
    """The thread call returns compact MR status with its existing heading metadata in one query."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=respond(
            page(
                make_discussion("t1", make_note("## Fix the parser", author="alice")),
                source_branch="feature/parser",
                head_sha="a" * 40,
                merge_status="CLEAN",
                pipeline_status="SUCCESS",
            ),
        ),
    )

    # Act
    data = await call_tool(view="headings")

    # Assert
    assert {key: data[key] for key in ("head_sha", "source_branch", "merge_status", "pipeline_status")} == {
        "head_sha": "a" * 40,
        "source_branch": "feature/parser",
        "merge_status": "clean",
        "pipeline_status": "success",
    }
    assert data["threads"][0]["heading"] == "Fix the parser"
    assert route.call_count == 1
    query = json.loads(route.calls[0].request.content)["query"]
    assert "sourceBranch @include(if: $includeMrStatus)" in query
    assert "diffRefs @include(if: $includeMrStatus) { headSha }" in query
    assert "detailedMergeStatus @include(if: $includeMrStatus)" in query
    assert "headPipeline @include(if: $includeMrStatus) { status }" in query
    assert "@include(if: $includeMrStatus)" in query


@pytest.mark.parametrize(
    ("discussions", "expected_ids"),
    [((), []), ((make_discussion("t1"),), ["t1"])],
    ids=["zero-threads", "one-thread"],
)
@respx.mock
async def test_status_can_be_omitted_for_empty_or_single_thread_results(gitlab_env, discussions, expected_ids):
    """Disabling MR status keeps empty and nonempty thread results free of status fields."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(page(*discussions)))

    # Act
    data = await call_tool(include_mr_status=False)

    # Assert
    assert thread_ids(data) == expected_ids
    assert not {"head_sha", "source_branch", "merge_status", "pipeline_status"} & data.keys()
    assert sent_variables(route) == [{"fullPath": PROJECT, "iid": str(IID), "after": None, "includeMrStatus": False}]
    query = json.loads(route.calls[0].request.content)["query"]
    conditional_status_fields = (
        "sourceBranch @include(if: $includeMrStatus)",
        "diffRefs @include(if: $includeMrStatus) { headSha }",
        "detailedMergeStatus @include(if: $includeMrStatus)",
        "headPipeline @include(if: $includeMrStatus) { status }",
    )
    for field_selection in conditional_status_fields:
        assert field_selection in query


@respx.mock
async def test_status_remains_omitted_when_resuming_across_discussion_pages(gitlab_env):
    """A resumed call keeps status disabled on each requested discussion page."""
    # Arrange
    large_threads = [make_discussion(f"t{index}", *(make_note("x" * NOTE_CAP) for _ in range(3))) for index in range(3)]
    route = respx.post(GRAPHQL).mock(
        side_effect=serve(
            {
                None: page(*large_threads, has_next=True, end_cursor="c1"),
                "c1": page(make_discussion("t3")),
            },
        ),
    )

    # Act
    first = await call_tool(include_mr_status=False)
    second = await call_tool(include_mr_status=False, cursor=first["next_cursor"])

    # Assert
    assert thread_ids(first) == ["t0", "t1"]
    assert thread_ids(second) == ["t2", "t3"]
    assert all(
        not {"head_sha", "source_branch", "merge_status", "pipeline_status"} & result.keys()
        for result in (first, second)
    )
    assert requested_afters(route) == [None, None, "c1"]
    assert [variables["includeMrStatus"] for variables in sent_variables(route)] == [False, False, False]


@respx.mock
async def test_status_can_be_omitted_for_every_batch_merge_request(gitlab_env):
    """Batch scans pass the status choice to each request and omit status from every entry."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("t1"))))

    # Act
    data = await call_tool(iids=[5, 8], include_mr_status=False)

    # Assert
    assert [thread_ids(result) for result in data["merge_requests"]] == [["t1"], ["t1"]]
    assert all(
        not {"head_sha", "source_branch", "merge_status", "pipeline_status"} & result.keys()
        for result in data["merge_requests"]
    )
    assert [variables["includeMrStatus"] for variables in sent_variables(route)] == [False, False]


@respx.mock
async def test_returns_triage_headers_for_each_batch_entry_and_omits_absent_pipeline(gitlab_env):
    """Batch entries carry their own compact MR status, and a missing pipeline stays omitted."""

    # Arrange
    def handler(request: httpx.Request) -> httpx.Response:
        iid = json.loads(request.content)["variables"]["iid"]
        if iid == "5":
            return respond(
                page(
                    make_discussion("thread-5"),
                    source_branch="feature/five",
                    head_sha="5" * 40,
                    merge_status="CLEAN",
                    pipeline_status="SUCCESS",
                ),
            )
        return respond(
            page(
                make_discussion("thread-8"),
                source_branch="feature/eight",
                head_sha="8" * 40,
                merge_status="BLOCKED_STATUS",
            ),
        )

    route = respx.post(GRAPHQL).mock(side_effect=handler)

    # Act
    data = await call_tool(iids=[5, 8])

    # Assert
    first, second = data["merge_requests"]
    assert {key: first[key] for key in ("head_sha", "source_branch", "merge_status", "pipeline_status")} == {
        "head_sha": "5" * 40,
        "source_branch": "feature/five",
        "merge_status": "clean",
        "pipeline_status": "success",
    }
    assert {key: second[key] for key in ("head_sha", "source_branch", "merge_status")} == {
        "head_sha": "8" * 40,
        "source_branch": "feature/eight",
        "merge_status": "blocked_status",
    }
    assert "pipeline_status" not in second
    assert route.call_count == len(data["merge_requests"])
    assert [variables["iid"] for variables in sent_variables(route)] == ["5", "8"]


@pytest.mark.parametrize(
    ("total", "resolved", "state", "expected"),
    [(0, 0, "open", {"open": 0, "resolved": 0}), (9, 4, "resolved", {"open": 5, "resolved": 4})],
    ids=["zero", "counts-both-states"],
)
@respx.mock
async def test_count_only_returns_exact_single_mr_counts_without_notes(gitlab_env, total, resolved, state, expected):
    """Count-only returns exact aggregate totals and does not request discussion pages or note bodies."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(counts_page(total, resolved)))

    # Act
    data = await call_tool(count_only=True, state=state)

    # Assert
    assert data == expected
    assert sent_variables(route) == [{"fullPath": PROJECT, "iid": str(IID)}]
    query = json.loads(route.calls[0].request.content)["query"]
    assert "resolvableDiscussionsCount" in query
    assert "resolvedDiscussionsCount" in query
    assert "discussions(" not in query.lower()
    assert "notes" not in query.lower()
    assert "body" not in query.lower()


@respx.mock
async def test_count_only_batches_counts_and_reports_missing_merge_requests(gitlab_env):
    """Batch count mode keeps request order, deduplicates IIDs, and reports missing MRs as usual."""

    # Arrange
    def handler(request: httpx.Request) -> httpx.Response:
        iid = json.loads(request.content)["variables"]["iid"]
        if iid == "7":
            return httpx.Response(200, json={"data": {"project": {"mergeRequest": None}}})
        return respond(counts_page(total=6, resolved=2) if iid == "5" else counts_page(total=0, resolved=0))

    route = respx.post(GRAPHQL).mock(side_effect=handler)

    # Act
    data = await call_tool(iids=[5, 7, 8, 5], count_only=True)

    # Assert
    assert data == {
        "merge_requests": [
            {"iid": 5, "open": 4, "resolved": 2},
            {"iid": 8, "open": 0, "resolved": 0},
        ],
        "missing_iids": [7],
    }
    assert [variables["iid"] for variables in sent_variables(route)] == ["5", "7", "8"]
    assert all("notes" not in json.loads(call.request.content)["query"].lower() for call in route.calls)


@pytest.mark.parametrize(
    "arguments",
    [
        {"cursor": "some-cursor"},
        {"ids": []},
        {"ids": ["thread-id"]},
        {"file": "src/parser.py"},
        {"author": "alice"},
    ],
    ids=["cursor", "empty-ids", "ids", "file", "author"],
)
@respx.mock
async def test_count_only_rejects_thread_filters_before_network(gitlab_env, arguments):
    """Count mode rejects page and thread filters because it does not read discussion records."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(counts_page(total=0, resolved=0)))

    # Act / Assert
    with pytest.raises(ToolError, match="count_only cannot be combined with cursor, ids, file, or author"):
        await call_tool(count_only=True, **arguments)
    assert route.call_count == 0


@respx.mock
async def test_count_only_propagates_unsupported_graphql_fields_as_tool_error(gitlab_env):
    """GraphQL field errors are reported instead of returning fabricated zero counts."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json={"errors": [{"message": "Field 'resolvableDiscussionsCount' doesn't exist on MergeRequest"}]},
        ),
    )

    # Act / Assert
    with pytest.raises(ToolError, match="GitLab GraphQL error: Field 'resolvableDiscussionsCount' doesn't exist"):
        await call_tool(count_only=True)
    assert route.call_count == 1


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
        ("# " + "x" * 240, "x" * 200),
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
    assert thread["notes"] == [
        {
            "note_id": 1,
            "body_hash": hashlib.sha256(b"Please rename this.").hexdigest(),
            "body": "Please rename this.",
            "at": "2026-10-20",
        }
    ]


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


def state_mix_page() -> dict[str, Any]:
    """Build one page with an open, a resolved, a plain-comment, and a system-only discussion, in that order."""
    return page(
        make_discussion("open"),
        make_discussion("resolved", resolved=True),
        make_discussion("comment", resolvable=False),
        make_discussion("system", make_note(system=True)),
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (["open"], ["open"]),  # A one-item list matches the scalar form.
        (["open", "comments"], ["open", "comment"]),  # Resolved threads are left out.
        (["resolved", "comments"], ["resolved", "comment"]),
        (["open", "open", "open"], ["open"]),  # Duplicate states never repeat a thread.
        (["comments", "open", "comments"], ["open", "comment"]),  # List order does not change result order.
        (["open", "all"], ["open", "resolved", "comment"]),  # all wins a mixed set.
        (["all", "comments", "resolved"], ["open", "resolved", "comment"]),
    ],
    ids=["one-item", "open-comments", "resolved-comments", "duplicates", "order", "all-mixed", "all-three"],
)
@respx.mock
async def test_state_list_keeps_the_union_in_scan_order(gitlab_env, state, expected):
    """A state list keeps the union of its states, in GitLab's order, with one scan and no repeats."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(state_mix_page()))

    # Act
    data = await call_tool(state=state)

    # Assert
    assert thread_ids(data) == expected
    assert route.call_count == 1


@pytest.mark.parametrize(
    "arguments",
    [{"state": []}, {"state": [], "count_only": True}, {"state": [], "ids": ["open"]}],
    ids=["threads", "count-only", "ids"],
)
@respx.mock
async def test_empty_state_list_is_rejected_before_network(gitlab_env, arguments):
    """An empty state list is an error in every mode, so it never silently means "all" or "open"."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(state_mix_page()))

    # Act / Assert
    with pytest.raises(ToolError, match="Pass at least one state"):
        await call_tool(**arguments)
    assert route.call_count == 0


@pytest.mark.parametrize("state", ["bogus", ["open", "bogus"], ["open", None]], ids=["scalar", "mixed", "null"])
@respx.mock
async def test_unknown_state_values_are_rejected_before_network(gitlab_env, state):
    """Only the four state names are accepted, alone or inside a list."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(state_mix_page()))

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(state=state)
    assert route.call_count == 0


@respx.mock
async def test_state_list_budget_cursor_resumes_with_the_same_states(gitlab_env):
    """A state list shares one output budget, and the resumed call returns each matching thread once."""
    # Arrange: each thread holds three full-size notes, 12000 characters. Resolved threads never match.
    big_notes = [make_note("x" * NOTE_CAP) for _ in range(3)]
    route = respx.post(GRAPHQL).mock(
        side_effect=serve(
            {
                None: page(
                    make_discussion("t1", *big_notes),  # open, matches
                    make_discussion("t2", *big_notes, resolved=True),  # resolved, skipped
                    make_discussion("t3", *big_notes, resolvable=False),  # comment, matches
                    make_discussion("t4", *big_notes),  # open, would pass the budget
                    make_discussion("t5", *big_notes, resolved=True),
                    has_next=True,
                    end_cursor="c1",
                ),
                "c1": page(
                    make_discussion("t6", *big_notes, resolvable=False),
                    make_discussion("t7", *big_notes, resolved=True),
                    make_discussion("t8"),
                ),
            },
        ),
    )

    # Act
    first = await call_tool(state=["open", "comments"])
    second = await call_tool(state=["open", "comments"], cursor=first["next_cursor"])

    # Assert
    assert thread_ids(first) == ["t1", "t3"]
    assert thread_ids(second) == ["t4", "t6", "t8"]
    assert "next_cursor" not in second
    assert requested_afters(route) == [None, None, "c1"]


@respx.mock
async def test_state_list_applies_to_every_batch_result(gitlab_env):
    """Each batch merge request gets the same state union, and each keeps its own result."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(state_mix_page()))

    # Act
    iids = [5, 8]
    data = await call_tool(iids=iids, state=["resolved", "comments"])

    # Assert
    assert [result["iid"] for result in data["merge_requests"]] == [5, 8]
    assert [thread_ids(result) for result in data["merge_requests"]] == [["resolved", "comment"]] * len(iids)
    assert route.call_count == len(iids)


@respx.mock
async def test_ids_ignore_a_state_list(gitlab_env):
    """Explicit IDs still win over a state list, so a resolved thread is returned."""
    # Arrange
    respx.post(GRAPHQL).mock(
        return_value=respond(page(make_discussion("resolved", resolved=True), make_discussion("open"))),
    )

    # Act
    data = await call_tool(ids=["resolved"], state=["open", "comments"])

    # Assert
    assert thread_ids(data) == ["resolved"]


@pytest.mark.parametrize(
    "state",
    [["open", "comments"], ["all", "resolved"], "resolved"],
    ids=["list", "all", "scalar"],
)
@respx.mock
async def test_count_only_ignores_a_state_list(gitlab_env, state):
    """Count-only still counts both states and sends no discussion query, whatever the state."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=respond(counts_page(9, 4)))

    # Act
    data = await call_tool(count_only=True, state=state)

    # Assert
    assert data == {"open": 5, "resolved": 4}
    assert sent_variables(route) == [{"fullPath": PROJECT, "iid": str(IID)}]


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


@respx.mock
async def test_finding_view_compacts_bot_greeting_and_signoff_preserving_findings(gitlab_env):
    """Finding view removes separate bot greetings and signoffs, but keeps the full finding content."""
    # Arrange
    body = (
        "👋 Hi @maintainer!\n\n"
        "## Review findings\n\n"
        "**File:** `src/parser.py`\n\n"
        "1. **[P1] Keep the original parse error.**\n\n"
        "The current code loses the original error details when parsing an invalid file.\n\n"
        "**Fix:** Preserve `error.message` and include the source path.\n\n"
        "```python\n"
        "raise ParseError(error.message)\n\n"
        'print("Thanks!")\n'
        "```\n\n"
        "Thanks!"
    )
    expected = (
        "## Review findings\n\n"
        "**File:** `src/parser.py`\n\n"
        "1. **[P1] Keep the original parse error.**\n\n"
        "The current code loses the original error details when parsing an invalid file.\n\n"
        "**Fix:** Preserve `error.message` and include the source path.\n\n"
        "```python\n"
        "raise ParseError(error.message)\n\n"
        'print("Thanks!")\n'
        "```"
    )
    note = make_note(body, author="review-bot") | {
        "author": {"username": "review-bot", "bot": True, "name": "Review Bot", "id": "gid://gitlab/User/123"},
    }
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("finding", note))))

    # Act
    full = await call_tool()
    finding = await call_tool(view="finding")

    # Assert
    assert full["threads"][0]["notes"][0]["body"] == body
    assert finding["threads"][0]["notes"][0]["body"] == expected
    expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert full["threads"][0]["notes"][0]["body_hash"] == expected_hash
    assert finding["threads"][0]["notes"][0]["body_hash"] == expected_hash


@respx.mock
async def test_finding_view_keeps_selected_bot_sections_and_their_code(gitlab_env):
    """Section findings keep the bold title and approved sections, including code and diagrams."""
    # Arrange
    body = (
        "👋 Hello team!\n\n"
        "**[P1] Preserve the original parse error**\n\n"
        "The parser currently loses `error.message`.\n\n"
        "### 🧭 What needs to be improved\n\n"
        "The error context is missing.\n\n"
        "### 💥 FAILURE SCENARIO\n\n"
        "1. Invalid input reaches the parser.\n"
        "2. The original error details are lost.\n\n"
        "```python\n"
        "raise ParseError(error.message)\n"
        "```\n\n"
        "```text\n"
        "Input -> Parser -> Error\n"
        "1. Keep this numbered example\n"
        "### Keep this heading example\n"
        "Fix: This is an example line\n"
        "```\n\n"
        "    keep_context(error.message)\n\n"
        "### 🌟 What was done well\n\n"
        "The tests are clear.\n\n"
        "**🛠️ HOW TO FIX**\n"
        "- Keep the original error details.\n\n"
        "Looks like we're ready to leave the station! 🛤️"
    )
    expected = (
        "**[P1] Preserve the original parse error**\n\n"
        "### 🧭 What needs to be improved\n\n"
        "The error context is missing.\n\n"
        "### 💥 FAILURE SCENARIO\n\n"
        "1. Invalid input reaches the parser.\n"
        "2. The original error details are lost.\n\n"
        "```python\n"
        "raise ParseError(error.message)\n"
        "```\n\n"
        "```text\n"
        "Input -> Parser -> Error\n"
        "1. Keep this numbered example\n"
        "### Keep this heading example\n"
        "Fix: This is an example line\n"
        "```\n\n"
        "    keep_context(error.message)\n\n"
        "**🛠️ HOW TO FIX**\n"
        "- Keep the original error details."
    )
    note = make_note(body, author="review-bot") | {
        "author": {"username": "review-bot", "bot": True, "name": "Review Bot", "id": "gid://gitlab/User/123"},
    }
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("finding", note))))

    # Act
    full = await call_tool()
    finding = await call_tool(view="finding")

    # Assert
    assert full["threads"][0]["notes"][0]["body"] == body
    assert finding["threads"][0]["notes"][0]["body"] == expected


@respx.mock
async def test_finding_view_preserves_signoff_text_when_meaningful_prose_follows(gitlab_env):
    """A section signoff candidate stays when a meaningful paragraph follows it."""
    # Arrange
    body = (
        "**[P1] Keep error context**\n\n"
        "### Failure scenario\n\n"
        "Invalid input loses its source details.\n\n"
        "### How to fix\n"
        "- Keep the original error.\n\n"
        "Looks like we're ready to leave the station! 🛤️\n\n"
        "The retry path still needs review."
    )
    note = make_note(body, author="review-bot") | {
        "author": {"username": "review-bot", "bot": True, "name": "Review Bot", "id": "gid://gitlab/User/123"},
    }
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("finding", note))))

    # Act
    finding = await call_tool(view="finding")

    # Assert
    assert finding["threads"][0]["notes"][0]["body"] == body


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            (
                "**🟡 LOW: Pin the reason in the neutral baseline rejection test** 🔧\n\n"
                "All aboard! 🎫 Nice move to the shared `registered_neutral_ecosystem` fixture instead of "
                "patching the registry.\n\n"
                "- **Location:** `tests/test_config.py:42`\n"
                "- **Fix:** Pin the rejection reason."
            ),
            (
                "**🟡 LOW: Pin the reason in the neutral baseline rejection test** 🔧\n\n"
                "- **Location:** `tests/test_config.py:42`\n"
                "- **Fix:** Pin the rejection reason."
            ),
        ),
        (
            (
                "👋 Hi!\n\n**[P2] Keep the source path**\n\n"
                "This breaks when `config.yaml` is invalid.\n\n"
                "- **Fix:** Keep the original path.\n"
                "- **Location:** `src/parser.py:42`\n\nThanks!"
            ),
            ("**[P2] Keep the source path**\n\n- **Fix:** Keep the original path.\n- **Location:** `src/parser.py:42`"),
        ),
        (
            (
                "**🟡 LOW: Pin the reason in the neutral baseline rejection test** 🔧 trailing prose\n\n"
                "This paragraph has `inline code`, but no action marker.\n\n"
                "- Fix: This must remain unchanged."
            ),
            (
                "**🟡 LOW: Pin the reason in the neutral baseline rejection test** 🔧 trailing prose\n\n"
                "This paragraph has `inline code`, but no action marker.\n\n"
                "- Fix: This must remain unchanged."
            ),
        ),
        (
            (
                "**[P2] Keep the source path**\n\n"
                "This paragraph has `inline code`, but no action marker.\n\n"
                "The prose remains ambiguous."
            ),
            (
                "**[P2] Keep the source path**\n\n"
                "This paragraph has `inline code`, but no action marker.\n\n"
                "The prose remains ambiguous."
            ),
        ),
        (
            (
                "**[P2] Keep the source path**\n\n"
                "```text\n- Fix: This is fenced example text.\n```\n\n"
                "The prose remains ambiguous."
            ),
            (
                "**[P2] Keep the source path**\n\n"
                "```text\n- Fix: This is fenced example text.\n```\n\n"
                "The prose remains ambiguous."
            ),
        ),
        (
            (
                "**[P2] Keep the source path**\n\n"
                "### Root cause\n\n"
                "The source path is lost.\n\n"
                "### Suggested change\n\n"
                "Preserve the original path."
            ),
            (
                "**[P2] Keep the source path**\n\n"
                "### Root cause\n\n"
                "The source path is lost.\n\n"
                "### Suggested change\n\n"
                "Preserve the original path."
            ),
        ),
    ],
    ids=[
        "trailing-emoji-heading-and-exact-intro",
        "remove-intro-before-action-bullets",
        "trailing-prose-is-not-heading-decoration",
        "keep-unknown-prose",
        "keep-fenced-example",
        "keep-unknown-sections",
    ],
)
@respx.mock
async def test_finding_view_only_removes_intro_in_recognized_bullet_layout(gitlab_env, body, expected):
    """Only a bold-heading layout with a real action marker loses its introductory paragraph."""
    # Arrange
    note = make_note(body, author="review-bot") | {
        "author": {"username": "review-bot", "bot": True, "name": "Review Bot", "id": "gid://gitlab/User/123"},
    }
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("finding", note))))

    # Act
    finding = await call_tool(view="finding")

    # Assert
    assert finding["threads"][0]["notes"][0]["body"] == expected


@pytest.mark.parametrize(
    ("indent", "placement"),
    [
        ("    ", "greeting"),
        ("\t", "greeting"),
        ("    ", "signoff"),
        ("\t", "signoff"),
    ],
    ids=["four-space-greeting", "tab-greeting", "four-space-signoff", "tab-signoff"],
)
@respx.mock
async def test_finding_view_preserves_ambiguous_indented_paragraphs(gitlab_env, indent, placement):
    """Indented greeting and signoff paragraphs are preserved as possibly meaningful code."""
    # Arrange
    finding = "1. [P1] Preserve this output example."
    indented_line = f"{indent}{'Hi!' if placement == 'greeting' else 'Thanks!'}"
    body = f"{indented_line}\n\n{finding}" if placement == "greeting" else f"{finding}\n\n{indented_line}"
    note = make_note(body, author="review-bot") | {
        "author": {"username": "review-bot", "bot": True, "name": "Review Bot", "id": "gid://gitlab/User/123"},
    }
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("finding", note))))

    # Act
    data = await call_tool(view="finding")

    # Assert
    assert data["threads"][0]["notes"][0]["body"] == body


@pytest.mark.parametrize(
    ("greeting", "keep_greeting"),
    [
        ("Peep peep! 🚂 ... Really useful work! ⭐", False),
        ("Peep peep!", False),
        ("Peep peep! 🚂", False),
        ("Peep peep! 🚂 Preserve error context before merging. Really useful work! ⭐", True),
        ("Peep peep! 🚂 This change is a joy to review. Really useful work! ⭐", True),
        ("Peep peep! 🚂 Update src/parser.py before merging. Really useful work! ⭐", True),
        ("Peep peep! 🚂 Keep `error.message` here. Really useful work! ⭐", True),
        ("Peep peep! 🚂 Fix: preserve the original error. Really useful work! ⭐", True),
        ("Peep peep! 🚂 **[P1] Preserve the original error. Really useful work! ⭐", True),
    ],
    ids=[
        "issue-greeting",
        "peep-only",
        "train-only",
        "useful-prose",
        "unknown-prose",
        "path",
        "code-span",
        "fix-text",
        "finding-content",
    ],
)
@respx.mock
async def test_finding_view_only_compacts_known_standalone_bot_greetings(gitlab_env, greeting, keep_greeting):
    """The known Peep greeting is compacted unless its paragraph includes review details."""
    # Arrange
    finding = "1. [P1] Keep the original error context."
    body = f"{greeting}\n\n{finding}\n\nFull steam ahead! 💨"
    expected = f"{greeting}\n\n{finding}" if keep_greeting else finding
    note = make_note(body, author="review-bot") | {
        "author": {"username": "review-bot", "bot": True, "name": "Review Bot", "id": "gid://gitlab/User/123"},
    }
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("finding", note))))

    # Act
    data = await call_tool(view="finding")

    # Assert
    assert data["threads"][0]["notes"][0]["body"] == expected


@respx.mock
async def test_finding_view_keeps_prose_after_multiline_signoff(gitlab_env):
    """A second line after a signoff phrase is prose, not a removable signature."""
    # Arrange
    body = "1. [P1] Keep the original error context.\n\nThanks,\nPreserve error context"
    note = make_note(body, author="review-bot") | {
        "author": {"username": "review-bot", "bot": True, "name": "Review Bot", "id": "gid://gitlab/User/123"},
    }
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("finding", note))))

    # Act
    data = await call_tool(view="finding")

    # Assert
    assert data["threads"][0]["notes"][0]["body"] == body


@pytest.mark.parametrize(
    ("author", "body"),
    [
        ({"username": "review-bot", "bot": True}, "Thanks!"),
        ({"username": "review-bot", "bot": True}, "Full steam ahead! 💨"),
        (
            {"username": "review-bot", "bot": True},
            "Peep peep! 🚂 This review has no numbered finding. Really useful work! ⭐\n\nFull steam ahead! 💨",
        ),
        (
            {"username": "alice", "bot": False},
            "👋 Hi!\n\n1. [P1] Keep the human note unchanged.\n\nThanks!",
        ),
        (
            {"username": "alice", "bot": False},
            (
                "👋 Hi!\n\n**[P1] Keep this human finding unchanged**\n\n"
                "### Needs to be improved\n\nThe section stays as written.\n\nThanks!"
            ),
        ),
    ],
    ids=[
        "signoff-without-finding",
        "known-signoff-without-finding",
        "no-numbered-items",
        "human-numbered-note",
        "human-section-note",
    ],
)
@respx.mock
async def test_finding_view_keeps_signoff_only_unstructured_and_human_notes(gitlab_env, author, body):
    """Compaction is skipped without numbered findings and for every human-authored note."""
    # Arrange
    note = make_note(body, author=author["username"]) | {"author": author}
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("finding", note))))

    # Act
    data = await call_tool(view="finding")

    # Assert
    assert data["threads"][0]["notes"][0]["body"] == body


@respx.mock
async def test_finding_view_compacts_before_applying_body_cap(gitlab_env):
    """The body cap applies after the removable bot greeting and signoff are compacted."""
    # Arrange
    finding = "1. [P1] " + "x" * 100
    body = f"👋 Hi!\n\n{finding}\n\nThanks!"
    note = make_note(body, author="review-bot") | {
        "author": {"username": "review-bot", "bot": True, "name": "Review Bot", "id": "gid://gitlab/User/123"},
    }
    respx.post(GRAPHQL).mock(return_value=respond(page(make_discussion("finding", note))))

    # Act
    data = await call_tool(view="finding", max_body_chars=30)

    # Assert
    assert data["threads"][0]["notes"][0]["body"] == finding[:30] + THREAD_CUT_MARKER


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
    # Arrange: note metadata and the capped bodies together exceed the serialized output budget.
    discussions = [make_discussion(f"t{index}", *(make_note("xxxx") for _ in range(25))) for index in range(12)]
    route = respx.post(GRAPHQL).mock(return_value=respond(page(*discussions)))

    # Act
    first = await call_tool(max_body_chars=3)
    second = await call_tool(max_body_chars=3, cursor=first["next_cursor"])

    # Assert
    assert thread_ids(first) + thread_ids(second) == [f"t{index}" for index in range(len(discussions))]
    assert 0 < len(first["threads"]) < len(discussions)
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


@pytest.mark.parametrize("view", ["full", "finding"])
@respx.mock
async def test_budget_stops_and_cursor_resumes_without_gaps_or_repeats(gitlab_env, view):
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
    first = await call_tool(view=view)
    second = await call_tool(view=view, cursor=first["next_cursor"])

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
