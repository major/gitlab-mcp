"""Behavioral tests for paged merge request note reads 📖."""

import hashlib
from typing import Any

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from mcp_helpers import call_tool

NOTE_URL = "https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/42/notes/7"
LONG_BODY = "é" * 4000 + "x" * 1001  # 🧮 Code points, not bytes, so the page ends on a whole character.
PAGE_CAP = 4000


def rest_note(body: str, *, author: dict[str, Any] | None) -> dict[str, Any]:
    """Build a small REST note like GitLab returns it."""
    return {"id": 7, "body": body, "author": author, "created_at": "2026-10-29T17:21:43.000Z", "system": False}


async def read(**arguments: Any) -> dict[str, Any]:
    """Call get_merge_request_note through the real in-memory MCP client."""
    return await call_tool("get_merge_request_note", {"project": "group/project", "iid": 42, "note_id": 7, **arguments})


@pytest.mark.parametrize(
    ("body", "author", "offset", "expected"),
    [
        pytest.param("short", {"username": "alice"}, 0, {"body": "short", "by": "alice"}, id="short"),
        pytest.param(
            LONG_BODY,
            {"username": "alice"},
            0,
            {"body": LONG_BODY[:PAGE_CAP], "by": "alice", "next_body_offset": PAGE_CAP},
            id="page-1",
        ),
        pytest.param(LONG_BODY, None, PAGE_CAP, {"body": LONG_BODY[PAGE_CAP:]}, id="last-page-deleted-author"),
        pytest.param("short", {"username": "alice"}, 99, {"body": "", "by": "alice"}, id="past-end"),
    ],
)
@respx.mock
async def test_note_pages_cover_the_whole_body(gitlab_env, body, author, offset, expected):
    """Each page carries the full-body hash, and next_body_offset appears only while text remains."""
    # Arrange
    route = respx.get(NOTE_URL).mock(return_value=httpx.Response(200, json=rest_note(body, author=author)))

    # Act
    data = await read(body_offset=offset)

    # Assert
    assert data == {
        "note_id": 7,
        "at": "2026-10-29",
        "body_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        **expected,
    }
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        pytest.param(
            httpx.Response(404, json={"message": "404 Not found"}),
            "Note 7, merge request !42, or the project was not found",
            id="404",
        ),
        pytest.param(httpx.Response(500, json={"message": "boom"}), "HTTP 500", id="server-error"),
        pytest.param(httpx.Response(200, json={"id": 7, "body": None}), "invalid note body", id="bad-body"),
    ],
)
@respx.mock
async def test_unknown_or_invalid_notes_raise_clear_errors(gitlab_env, reply, message):
    """A note on another MR comes back as a 404 from the scoped route and gets a clear message."""
    # Arrange
    respx.get(NOTE_URL).mock(return_value=reply)

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await read()
