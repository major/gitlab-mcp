"""Tests for replying to and resolving GitLab discussion threads 🧵."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import mcp

API = "https://gitlab.example.com/api/v4"
GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 2187
NOTE_ID = 789
MR_PATH = f"/projects/group%2Fproject/merge_requests/{IID}"
DISCUSSION_PATH = f"{MR_PATH}/discussions/thread-1"
NOTES_URL = API + DISCUSSION_PATH + "/notes"
DISCUSSION_URL = API + DISCUSSION_PATH


def count_response(resolvable: int, resolved: int) -> httpx.Response:
    """Return the GraphQL thread-count envelope GitLab sends for one merge request."""
    node = {"resolvableDiscussionsCount": resolvable, "resolvedDiscussionsCount": resolved}
    return httpx.Response(200, json={"data": {"project": {"mergeRequest": node}}})


def sent_variables(route: respx.Route) -> dict[str, Any]:
    """Return the GraphQL variables from the last request the route received."""
    return json.loads(route.calls.last.request.content)["variables"]


async def call_reply(**arguments: Any) -> dict[str, Any]:
    """Call reply_to_thread through the in-memory MCP client."""
    async with Client(mcp) as client:
        result = await client.call_tool(
            "reply_to_thread",
            {"project": PROJECT, "iid": IID, "thread_id": "thread-1", **arguments},
        )
    return result.structured_content


async def call_reply_batch(replies: list[dict[str, Any]]) -> dict[str, Any]:
    """Call reply_to_threads through the in-memory MCP client."""
    async with Client(mcp) as client:
        result = await client.call_tool("reply_to_threads", {"project": PROJECT, "replies": replies})
    return result.structured_content


@respx.mock
async def test_posts_reply_and_returns_only_the_note_id(gitlab_env):
    """A reply uses the notes endpoint and does not include the note body in its result."""
    # Arrange
    note = respx.post(NOTES_URL).mock(return_value=httpx.Response(201, json={"id": 321, "body": "secret"}))

    # Act
    result = await call_reply(body="A useful reply")

    # Assert
    assert note.calls.last.request.read() == b'{"body":"A useful reply"}'
    assert result == {"thread_id": "thread-1", "note_id": 321}


@respx.mock
async def test_reply_then_resolve_uses_two_ordered_writes(gitlab_env):
    """A reply and a requested resolution are separate writes in the correct order."""
    # Arrange
    calls: list[str] = []
    note = respx.post(NOTES_URL).mock(
        side_effect=lambda request: (calls.append("reply"), httpx.Response(201, json={"id": 321}))[1]
    )
    resolve = respx.put(DISCUSSION_URL).mock(
        side_effect=lambda request: (calls.append("resolve"), httpx.Response(200, json={"resolved": True}))[1]
    )
    respx.post(GRAPHQL).mock(side_effect=lambda request: (calls.append("count"), count_response(1, 1))[1])

    # Act
    result = await call_reply(body="Done", resolve=True)

    # Assert
    assert calls == ["reply", "resolve", "count"]
    assert note.calls.last.request.read() == b'{"body":"Done"}'
    assert resolve.calls.last.request.read() == b'{"resolved":true}'
    assert result == {"thread_id": "thread-1", "note_id": 321, "resolved": True, "open_threads": 0}


@respx.mock
async def test_resolve_only_sends_no_note(gitlab_env):
    """A missing body allows resolution without creating a reply."""
    # Arrange
    resolve = respx.put(DISCUSSION_URL).mock(return_value=httpx.Response(200, json={"resolved": True}))
    note = respx.post(NOTES_URL).mock(return_value=httpx.Response(201, json={"id": 321}))
    respx.post(GRAPHQL).mock(return_value=count_response(1, 1))

    # Act
    result = await call_reply(body=None, resolve=True)

    # Assert
    assert resolve.calls.call_count == 1
    assert note.calls.call_count == 0
    assert result == {"thread_id": "thread-1", "resolved": True, "open_threads": 0}


@pytest.mark.parametrize(
    ("resolvable", "resolved", "expected_open"),
    [(1, 1, 0), (3, 1, 2)],
    ids=["last-open-thread", "one-of-several"],
)
@respx.mock
async def test_resolve_reports_open_threads_after_the_write(gitlab_env, resolvable, resolved, expected_open):
    """The open count is read after the resolve write and reflects the MR's remaining open threads."""
    # Arrange
    calls: list[str] = []

    def serve_resolve(request: httpx.Request) -> httpx.Response:
        """Record the resolve write and accept it."""
        calls.append("resolve")
        return httpx.Response(200, json={"resolved": True})

    def serve_counts(request: httpx.Request) -> httpx.Response:
        """Record the count read and return the MR's discussion totals."""
        calls.append("count")
        return count_response(resolvable, resolved)

    respx.put(DISCUSSION_URL).mock(side_effect=serve_resolve)
    counts = respx.post(GRAPHQL).mock(side_effect=serve_counts)

    # Act
    result = await call_reply(resolve=True)

    # Assert
    assert calls == ["resolve", "count"]
    assert sent_variables(counts) == {"fullPath": PROJECT, "iid": str(IID)}
    assert result == {"thread_id": "thread-1", "resolved": True, "open_threads": expected_open}


@respx.mock
async def test_reply_without_resolve_keeps_shape_and_skips_the_count(gitlab_env):
    """A reply that does not resolve has no open_threads field and makes no count read."""
    # Arrange
    respx.post(NOTES_URL).mock(return_value=httpx.Response(201, json={"id": NOTE_ID}))
    counts = respx.post(GRAPHQL).mock(return_value=count_response(1, 0))

    # Act
    result = await call_reply(body="Reply only")

    # Assert
    assert result == {"thread_id": "thread-1", "note_id": NOTE_ID}
    assert counts.call_count == 0


@pytest.mark.parametrize(
    "failure",
    [
        {"return_value": httpx.Response(403, json={"message": "Forbidden"})},
        {"side_effect": httpx.ConnectError("connection reset")},
    ],
    ids=["api-error", "connection-error"],
)
@respx.mock
async def test_failed_count_read_omits_open_threads_without_failing_the_write(gitlab_env, failure):
    """A resolved thread still returns success when the follow-up count cannot be read."""
    # Arrange
    resolve = respx.put(DISCUSSION_URL).mock(return_value=httpx.Response(200, json={"resolved": True}))
    respx.post(GRAPHQL).mock(**failure)

    # Act
    result = await call_reply(resolve=True)

    # Assert
    assert resolve.calls.call_count == 1
    assert result == {"thread_id": "thread-1", "resolved": True}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"body": "   "}, "body"),
        ({"body": None}, "reply body"),
        ({"thread_id": ""}, "thread_id"),
        ({"thread_id": "   "}, "thread_id"),
    ],
)
@respx.mock
async def test_invalid_requests_fail_without_network(gitlab_env, arguments, message):
    """Empty replies, no-op requests, and empty thread IDs are rejected before network access."""
    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_reply(**arguments)
    assert not respx.calls


@respx.mock
async def test_full_gid_and_unsafe_components_are_encoded(gitlab_env):
    """A full discussion GID is normalized, and project and thread path segments are escaped."""
    # Arrange
    project = "group/nested project"
    thread_id = "gid://gitlab/Discussion/id/with?query"
    expected_url = (
        API + "/projects/group%2Fnested%20project/merge_requests/2187/discussions/" + "id%2Fwith%3Fquery/notes"
    )
    route = respx.post(expected_url).mock(return_value=httpx.Response(201, json={"id": 456}))

    # Act
    async with Client(mcp) as client:
        result = await client.call_tool(
            "reply_to_thread",
            {"project": project, "iid": IID, "thread_id": thread_id, "body": "Reply"},
        )

    # Assert
    assert route.called
    assert result.structured_content == {"thread_id": "id/with?query", "note_id": 456}
    assert route.calls.last.request.url.raw_path.endswith(b"/discussions/id%2Fwith%3Fquery/notes")


@pytest.mark.parametrize("status", [403, 404])
@respx.mock
async def test_post_api_errors_propagate_without_resolving(gitlab_env, status):
    """A refused reply keeps the API error and prevents the resolution write."""
    # Arrange
    note = respx.post(NOTES_URL).mock(return_value=httpx.Response(status, json={"message": "No permission"}))
    resolve = respx.put(DISCUSSION_URL).mock(return_value=httpx.Response(200, json={}))

    # Act / Assert
    with pytest.raises(ToolError, match=f"HTTP {status}"):
        await call_reply(body="Reply", resolve=True)
    assert note.called
    assert not resolve.called


@pytest.mark.parametrize("status", [403, 404])
@respx.mock
async def test_resolve_only_api_errors_propagate(gitlab_env, status):
    """A resolution refusal with no reply passes the API error through."""
    # Arrange
    route = respx.put(DISCUSSION_URL).mock(return_value=httpx.Response(status, json={"message": "Cannot resolve"}))

    # Act / Assert
    with pytest.raises(ToolError, match=f"HTTP {status}"):
        await call_reply(body=None, resolve=True)
    assert route.called


@respx.mock
async def test_post_connection_error_reports_unknown_and_prevents_retry(gitlab_env):
    """A lost connection while posting says the reply outcome is unknown and advises a thread check."""
    # Arrange
    note = respx.post(NOTES_URL).mock(side_effect=httpx.ConnectError("connection reset"))
    resolve = respx.put(DISCUSSION_URL).mock(return_value=httpx.Response(200, json={}))

    # Act / Assert
    with pytest.raises(ToolError, match="outcome is unknown") as exc_info:
        await call_reply(body="Reply", resolve=True)
    assert "get_merge_request_threads" in str(exc_info.value)
    assert "before retrying" in str(exc_info.value)
    assert note.calls.call_count == 1
    assert not resolve.called


@respx.mock
async def test_resolution_failure_after_reply_returns_partial_success_without_reposting(gitlab_env):
    """A resolution connection failure returns the posted note and a warning, without retrying the reply."""
    # Arrange
    note = respx.post(NOTES_URL).mock(return_value=httpx.Response(201, json={"id": NOTE_ID}))
    resolve = respx.put(DISCUSSION_URL).mock(side_effect=httpx.ConnectError("connection reset"))

    # Act
    result = await call_reply(body="Reply", resolve=True)

    # Assert
    assert note.calls.call_count == 1
    assert resolve.calls.call_count == 1
    assert result["thread_id"] == "thread-1"
    assert result["note_id"] == NOTE_ID
    warning = result["warning"].lower()
    assert "reply was posted" in warning
    assert "resolution outcome is unknown" in warning
    assert "get_merge_request_threads" in warning


@respx.mock
async def test_resolution_api_error_after_reply_returns_partial_success(gitlab_env):
    """A resolution API refusal reports that the reply was posted and resolution failed."""
    # Arrange
    note = respx.post(NOTES_URL).mock(return_value=httpx.Response(201, json={"id": NOTE_ID}))
    resolve = respx.put(DISCUSSION_URL).mock(return_value=httpx.Response(403, json={"message": "Cannot resolve"}))

    # Act
    result = await call_reply(body="Reply", resolve=True)

    # Assert
    assert note.calls.call_count == 1
    assert resolve.calls.call_count == 1
    assert result["note_id"] == NOTE_ID
    warning = result["warning"].lower()
    assert "reply was posted" in warning
    assert "resolution failed" in warning


@respx.mock
async def test_resolve_only_connection_error_reports_unknown(gitlab_env):
    """A lost connection during resolve-only reports an unknown outcome without retrying."""
    # Arrange
    route = respx.put(DISCUSSION_URL).mock(side_effect=httpx.ConnectError("connection reset"))

    # Act / Assert
    with pytest.raises(ToolError, match="outcome is unknown") as exc_info:
        await call_reply(body=None, resolve=True)
    assert "get_merge_request_threads" in str(exc_info.value)
    assert route.calls.call_count == 1


async def test_tool_is_a_non_idempotent_write_and_has_user_approval_guidance(gitlab_env):
    """Tool metadata and docs describe the write and ask for approval before posting."""
    # Arrange
    async with Client(mcp) as client:
        tools = await client.list_tools()

    # Act
    tool = next(tool for tool in tools if tool.name == "reply_to_thread")

    # Assert
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is True
    assert "glab mr note create --reply" in tool.description
    assert "glab mr note resolve" in tool.description
    assert "approval" in tool.description.lower()


@respx.mock
async def test_batch_continues_after_success_failure_and_unknown_write(gitlab_env):
    """Every item is reported and later items run after definite or uncertain errors."""
    # Arrange
    calls: list[int] = []
    routes = [
        respx.post(API + f"/projects/group%2Fproject/merge_requests/{iid}/discussions/thread/notes").mock(
            side_effect=lambda request, requested_iid=iid, response_status=status, response_body=response: (
                calls.append(requested_iid),
                httpx.Response(response_status, json=response_body),
            )[1]
        )
        for iid, status, response in [
            (2187, 201, {"id": 11}),
            (2188, 403, {"message": "Not allowed"}),
            (2189, 503, {"message": "Server error"}),
            (2190, 201, {"id": 44}),
        ]
    ]
    replies = [{"iid": iid, "thread_id": "thread", "body": f"reply {iid}"} for iid in (2187, 2188, 2189, 2190)]

    # Act
    result = await call_reply_batch(replies)

    # Assert
    assert [route.calls.call_count for route in routes] == [1, 1, 1, 1]
    assert calls == [2187, 2188, 2189, 2190]
    assert [(row["iid"], row["thread_id"], row["status"], row["resolved"]) for row in result["results"]] == [
        (2187, "thread", "applied", None),
        (2188, "thread", "failed", None),
        (2189, "thread", "unknown", None),
        (2190, "thread", "applied", None),
    ]
    assert "unknown" not in result["results"][1]["warning"].lower()
    assert "outcome is unknown" in result["results"][2]["warning"].lower()


@respx.mock
async def test_batch_keeps_cross_merge_request_writes_in_input_order(gitlab_env):
    """Each MR reply and resolve pair runs before the next input item."""
    # Arrange
    calls: list[tuple[str, int]] = []
    replies = [
        {"iid": 2187, "thread_id": "first", "body": "first", "resolve": True},
        {"iid": 2188, "thread_id": "second", "body": "second", "resolve": True},
    ]
    for iid, thread_id in ((2187, "first"), (2188, "second")):
        path = API + f"/projects/group%2Fproject/merge_requests/{iid}/discussions/{thread_id}"
        respx.post(path + "/notes").mock(
            side_effect=lambda request, requested_iid=iid: (
                calls.append(("reply", requested_iid)),
                httpx.Response(201, json={"id": requested_iid}),
            )[1]
        )
        respx.put(path).mock(
            side_effect=lambda request, requested_iid=iid: (
                calls.append(("resolve", requested_iid)),
                httpx.Response(200, json={"resolved": True}),
            )[1]
        )

    respx.post(GRAPHQL).mock(return_value=count_response(1, 1))

    # Act
    result = await call_reply_batch(replies)

    # Assert
    assert calls == [("reply", 2187), ("resolve", 2187), ("reply", 2188), ("resolve", 2188)]
    assert [row["status"] for row in result["results"]] == ["applied", "applied"]
    assert [row["resolved"] for row in result["results"]] == [True, True]
    assert [row["open_threads"] for row in result["results"]] == [0, 0]


@pytest.mark.parametrize("count", [0, 21])
@respx.mock
async def test_batch_size_bounds_reject_before_writes(gitlab_env, count):
    """The batch requires 1 to 20 entries and validates bounds before network access."""
    # Act / Assert
    replies = [{"iid": IID, "thread_id": "thread", "body": "reply"}] * count
    with pytest.raises(ToolError):
        await call_reply_batch(replies)
    assert not respx.calls


@pytest.mark.parametrize(
    "invalid_reply",
    [
        {"iid": 2188, "thread_id": " ", "body": "reply"},
        {"iid": 2188, "thread_id": "thread", "body": "  "},
        {"iid": 2188, "thread_id": "thread"},
    ],
)
@respx.mock
async def test_batch_validates_every_item_before_any_write(gitlab_env, invalid_reply):
    """An invalid later input prevents earlier batch entries from being written."""
    # Act / Assert
    with pytest.raises(ToolError):
        await call_reply_batch(
            [
                {"iid": 2187, "thread_id": "thread", "body": "valid"},
                invalid_reply,
            ]
        )
    assert not respx.calls


@respx.mock
async def test_batch_connection_error_is_unknown_and_later_items_continue(gitlab_env):
    """A lost connection marks only that item unknown and does not stop later writes."""
    # Arrange
    unknown = respx.post(API + "/projects/group%2Fproject/merge_requests/2187/discussions/thread/notes").mock(
        side_effect=httpx.ConnectError("connection reset")
    )
    success = respx.post(API + "/projects/group%2Fproject/merge_requests/2188/discussions/thread/notes").mock(
        return_value=httpx.Response(201, json={"id": 12})
    )

    # Act
    result = await call_reply_batch(
        [
            {"iid": 2187, "thread_id": "thread", "body": "possibly posted"},
            {"iid": 2188, "thread_id": "thread", "body": "continue"},
        ]
    )

    # Assert
    assert unknown.calls.call_count == success.calls.call_count == 1
    assert [row["status"] for row in result["results"]] == ["unknown", "applied"]
    assert "outcome is unknown" in result["results"][0]["warning"].lower()


@respx.mock
async def test_batch_resolution_5xx_is_unknown_not_failed(gitlab_env):
    """A swallowed single-tool resolution warning is corrected to an uncertain batch outcome."""
    # Arrange
    note = respx.post(NOTES_URL).mock(return_value=httpx.Response(201, json={"id": NOTE_ID}))
    resolve = respx.put(DISCUSSION_URL).mock(return_value=httpx.Response(503, json={"message": "Server error"}))

    # Act
    result = await call_reply_batch([{"iid": IID, "thread_id": "thread-1", "body": "Reply", "resolve": True}])

    # Assert
    assert note.calls.call_count == resolve.calls.call_count == 1
    assert result["results"][0]["status"] == "unknown"
    assert "resolution outcome is unknown" in result["results"][0]["warning"].lower()


@respx.mock
async def test_batch_reads_open_threads_once_per_merge_request_after_its_last_resolve(gitlab_env):
    """Each MR gets one count, read after its last resolve item, and each count stays with its own MR."""
    # Arrange
    open_counts = {"2187": (2, 2), "2188": (3, 1)}  # (resolvable, resolved): 0 open for 2187, 2 open for 2188.
    for iid, thread_id in ((2187, "a"), (2188, "b"), (2187, "c")):
        path = API + f"/projects/group%2Fproject/merge_requests/{iid}/discussions/{thread_id}"
        respx.post(path + "/notes").mock(return_value=httpx.Response(201, json={"id": 1}))
        respx.put(path).mock(return_value=httpx.Response(200, json={"resolved": True}))

    def serve_counts(request: httpx.Request) -> httpx.Response:
        """Return the totals for the merge request named in the GraphQL variables."""
        iid = json.loads(request.content)["variables"]["iid"]
        return count_response(*open_counts[iid])

    counts = respx.post(GRAPHQL).mock(side_effect=serve_counts)

    # Act
    result = await call_reply_batch(
        [
            {"iid": 2187, "thread_id": "a", "body": "Done", "resolve": True},
            {"iid": 2188, "thread_id": "b", "body": "Done", "resolve": True},
            {"iid": 2187, "thread_id": "c", "body": "Done", "resolve": True},
        ]
    )

    # Assert
    assert [json.loads(call.request.content)["variables"]["iid"] for call in counts.calls] == ["2188", "2187"]
    assert "open_threads" not in result["results"][0]
    assert [row["open_threads"] for row in result["results"][1:]] == [2, 0]


@respx.mock
async def test_batch_omits_open_threads_when_the_last_resolution_fails(gitlab_env):
    """An earlier successful resolve gets no count when the MR's last resolve item failed."""
    # Arrange
    path = API + "/projects/group%2Fproject/merge_requests/2187/discussions/"
    respx.post(path + "a/notes").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.put(path + "a").mock(return_value=httpx.Response(200, json={"resolved": True}))
    respx.post(path + "b/notes").mock(return_value=httpx.Response(201, json={"id": 2}))
    respx.put(path + "b").mock(return_value=httpx.Response(403, json={"message": "Cannot resolve"}))
    counts = respx.post(GRAPHQL).mock(return_value=count_response(1, 1))

    # Act
    result = await call_reply_batch(
        [
            {"iid": IID, "thread_id": "a", "body": "Done", "resolve": True},
            {"iid": IID, "thread_id": "b", "body": "Done", "resolve": True},
        ]
    )

    # Assert
    assert [row["status"] for row in result["results"]] == ["applied", "partial"]
    assert all("open_threads" not in row for row in result["results"])
    assert counts.call_count == 0
