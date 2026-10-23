"""Tests for replying to and resolving GitLab discussion threads 🧵."""

from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import mcp

API = "https://gitlab.example.com/api/v4"
PROJECT = "group/project"
IID = 2187
NOTE_ID = 789
MR_PATH = f"/projects/group%2Fproject/merge_requests/{IID}"
DISCUSSION_PATH = f"{MR_PATH}/discussions/thread-1"
NOTES_URL = API + DISCUSSION_PATH + "/notes"
DISCUSSION_URL = API + DISCUSSION_PATH


async def call_reply(**arguments: Any) -> dict[str, Any]:
    """Call reply_to_thread through the in-memory MCP client."""
    async with Client(mcp) as client:
        result = await client.call_tool(
            "reply_to_thread",
            {"project": PROJECT, "iid": IID, "thread_id": "thread-1", **arguments},
        )
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

    # Act
    result = await call_reply(body="Done", resolve=True)

    # Assert
    assert calls == ["reply", "resolve"]
    assert note.calls.last.request.read() == b'{"body":"Done"}'
    assert resolve.calls.last.request.read() == b'{"resolved":true}'
    assert result == {"thread_id": "thread-1", "note_id": 321, "resolved": True}


@respx.mock
async def test_resolve_only_sends_no_note(gitlab_env):
    """A missing body allows resolution without creating a reply."""
    # Arrange
    resolve = respx.put(DISCUSSION_URL).mock(return_value=httpx.Response(200, json={"resolved": True}))
    note = respx.post(NOTES_URL).mock(return_value=httpx.Response(201, json={"id": 321}))

    # Act
    result = await call_reply(body=None, resolve=True)

    # Assert
    assert resolve.calls.call_count == 1
    assert note.calls.call_count == 0
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
