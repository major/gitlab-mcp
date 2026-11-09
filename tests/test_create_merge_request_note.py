"""Behavioral tests for top-level merge request comments through MCP. 📝."""

import json
import os
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp import mcp
from gitlab_mcp.common import REVIEW_NOTE_BODY_CAP
from mcp_helpers import call_tool

API = "https://gitlab.example.com/api/v4"
MR_PATH = "/projects/group%2Fproject/merge_requests/42"
MR_URL = "https://gitlab.example.com/group/project/-/merge_requests/42"
PRIVATE_BODY = "private coordination comment"
UNKNOWN = {"status": "unknown", "message": "Creation may have completed. Check GitLab before retrying."}


async def create_note(**arguments: Any) -> dict[str, Any]:
    """Call the registered tool with a default project and MR number."""
    return await call_tool("create_merge_request_note", {"project": "group/project", "iid": 42, **arguments})


@pytest.mark.parametrize("source", ["body", "body_file"])
@pytest.mark.parametrize(
    ("project", "path", "web_url"),
    [
        ("group/project", MR_PATH, MR_URL),
        (
            "team/sub/project",
            "/projects/team%2Fsub%2Fproject/merge_requests/42",
            "https://code.example.com/gitlab/team/sub/project/-/merge_requests/42",
        ),
        ("123", "/projects/123/merge_requests/42", "http://code.example.com/gitlab/team/repo/-/merge_requests/42"),
    ],
)
@respx.mock
async def test_creates_one_top_level_note_with_exact_body(  # noqa: PLR0913, PLR0917 - fixtures and URL cases.
    gitlab_env, tmp_path, source, project, path, web_url
):
    """Both sources preserve text, omit inline positions, and link to the canonical MR URL."""
    # Arrange
    body = "  café 🦊\r\n\n"
    file = tmp_path / "note.md"
    file.write_bytes(body.encode("utf-8"))
    lookup = respx.get(f"{API}{path}").respond(200, json={"web_url": web_url})
    post = respx.post(f"{API}{path}/notes").respond(201, json={"id": 456, "body": body})

    # Act
    result = await create_note(project=project, **{source: body if source == "body" else str(file)})

    # Assert
    assert result == {"status": "created", "note_id": 456, "web_url": f"{web_url}#note_456"}
    assert lookup.call_count == post.call_count == 1
    assert json.loads(post.calls[0].request.content) == {"body": body}
    assert body not in str(result)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "exactly one"),
        ({"body": "x", "body_file": "/unused"}, "exactly one"),
        ({"body": "", "body_file": "/unused"}, "exactly one"),
        ({"body": ""}, "blank"),
        ({"body": " \n\t"}, "blank"),
        ({"body": "x", "project": " "}, "project"),
        ({"body": "x", "iid": 0}, "iid"),
    ],
)
@respx.mock
async def test_invalid_input_makes_no_request(gitlab_env, arguments, message):
    """Missing, conflicting, blank, and invalid target inputs fail before HTTP."""
    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await create_note(**arguments)
    assert not respx.calls


@pytest.mark.parametrize("source", ["body", "body_file"])
@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("preview [truncated]", "truncation marker"),
        ("preview (45 more lines → /var/tmp/file)", "truncation marker"),
        ("comment\n  /merge", "quick action"),
        ("```\n/close\n```", "quick action"),
    ],
)
@respx.mock
async def test_unsafe_body_makes_no_request(gitlab_env, tmp_path, source, body, message):
    """Full bodies reject previews and possible quick actions even inside code fences."""
    # Arrange
    file = tmp_path / "note.md"
    file.write_text(body, encoding="utf-8")

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await create_note(**{source: body if source == "body" else str(file)})
    assert not respx.calls


@pytest.mark.parametrize("source", ["body", "body_file"])
@pytest.mark.parametrize("extra", [0, 1])
@respx.mock
async def test_utf8_byte_cap(gitlab_env, tmp_path, source, extra):
    """Exactly 100000 UTF-8 bytes pass while one extra byte fails before HTTP."""
    # Arrange
    body = "é" * (REVIEW_NOTE_BODY_CAP // 2) + "x" * extra
    file = tmp_path / "note.md"
    file.write_text(body, encoding="utf-8")
    respx.get(f"{API}{MR_PATH}").respond(200, json={"web_url": MR_URL})
    post = respx.post(f"{API}{MR_PATH}/notes").respond(201, json={"id": 456})

    # Act / Assert
    if extra:
        with pytest.raises(ToolError, match="100000 UTF-8 bytes"):
            await create_note(**{source: body if source == "body" else str(file)})
        assert not respx.calls
    else:
        assert (await create_note(**{source: body if source == "body" else str(file)}))["status"] == "created"
        assert post.call_count == 1


@pytest.mark.parametrize("kind", ["relative", "empty", "missing", "directory", "fifo", "utf8", "blank"])
@respx.mock
async def test_invalid_local_file_makes_no_request(gitlab_env, tmp_path, kind):
    """Only absolute regular UTF-8 files containing nonblank text can be posted."""
    # Arrange
    file = tmp_path / "note.md"
    path = str(file)
    if kind == "relative":
        path = "relative.md"
    elif kind == "empty":
        path = ""
    elif kind == "directory":
        path = str(tmp_path)
    elif kind == "fifo":
        os.mkfifo(file)
    elif kind == "utf8":
        file.write_bytes(b"\xff")
    elif kind == "blank":
        file.write_text(" \n", encoding="utf-8")

    # Act / Assert
    with pytest.raises(ToolError, match=r"body|regular file|UTF-8|server-local"):
        await create_note(body_file=path)
    assert not respx.calls


@pytest.mark.parametrize(
    "lookup_response",
    [
        httpx.Response(404),
        httpx.Response(503),
        httpx.Response(200, json={}),
        httpx.Response(200, json=[]),
        *[
            httpx.Response(200, json={"web_url": value})
            for value in (None, "", " ", "/relative", "https:///missing-host", "ftp://host/mr", f"{MR_URL}#old")
        ],
    ],
)
@respx.mock
async def test_lookup_failure_never_posts(gitlab_env, lookup_response):
    """Failed lookup and invalid canonical links cannot cause a write."""
    # Arrange
    lookup = respx.get(f"{API}{MR_PATH}").mock(return_value=lookup_response)

    # Act / Assert
    with pytest.raises(ToolError):
        await create_note(body=PRIVATE_BODY)
    assert lookup.call_count == 1
    assert len(respx.calls) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
@respx.mock
async def test_definite_rejection_is_private_and_not_retried(gitlab_env, status):
    """A definite HTTP rejection raises a safe error without GitLab's echoed note body."""
    # Arrange
    respx.get(f"{API}{MR_PATH}").respond(200, json={"web_url": MR_URL})
    post = respx.post(f"{API}{MR_PATH}/notes").respond(status, json={"message": PRIVATE_BODY})

    # Act / Assert
    with pytest.raises(ToolError, match=str(status)) as exc:
        await create_note(body=PRIVATE_BODY)
    assert PRIVATE_BODY not in str(exc.value)
    assert post.call_count == 1


@pytest.mark.parametrize(
    "write_response",
    [
        httpx.ConnectError("connection lost"),
        *[httpx.Response(status, json={"id": 456}) for status in (200, 202, 204, 302, 408, 500, 503)],
        httpx.Response(201, text="not json"),
        httpx.Response(201, json=[]),
        httpx.Response(201, json={}),
        *[httpx.Response(201, json={"id": value}) for value in (None, True, 0, -1, "456")],
    ],
)
@respx.mock
async def test_uncertain_write_is_not_retried(gitlab_env, write_response):
    """Uncertain statuses, transport failures, and malformed success payloads never trigger another POST."""
    # Arrange
    respx.get(f"{API}{MR_PATH}").respond(200, json={"web_url": MR_URL})
    post = respx.post(f"{API}{MR_PATH}/notes").mock(side_effect=[write_response])

    # Act
    result = await create_note(body=PRIVATE_BODY)

    # Assert
    assert result == UNKNOWN
    assert PRIVATE_BODY not in str(result)
    assert post.call_count == 1


async def test_schema_and_workflow_describe_general_comments(gitlab_env):
    """The catalog distinguishes general comments and documents approval and body sources."""
    # Arrange / Act
    async with Client(mcp) as client:
        tool = next(tool for tool in await client.list_tools() if tool.name == "create_merge_request_note")
        instructions = client.instructions or ""

    # Assert
    schema = tool.input_schema
    assert "body" not in schema.get("required", [])
    assert "body_file" not in schema.get("required", [])
    assert "body_file" in schema["properties"]["body"]["description"]
    assert "server" in schema["properties"]["body_file"]["description"]
    assert "glab mr note" in tool.description
    assert "APPROVE" in tool.description
    assert "create_merge_request_note(project, iid, body, body_file)" in instructions
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is True
