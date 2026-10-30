"""Behavioral tests for creating work-item notes through MCP. 🧪."""

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

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 42
ITEM_ID = "gid://gitlab/WorkItem/123"
NOTE_ID = "gid://gitlab/Note/456"
DISCUSSION_ID = "gid://gitlab/Discussion/abc"
PRIVATE_BODY = "private note text"
UNKNOWN = {"status": "unknown", "message": "Check GitLab before retrying."}
LOOKUP_AND_MUTATION = 2


def lookup_response(
    item: Any = None, *, scope: str = "project", path: str = PROJECT, scope_namespace: Any = ...
) -> httpx.Response:
    """Build a scoped lookup response with a minimal target item."""
    if item is None:
        item = {"id": ITEM_ID, "iid": str(IID)}
    if scope_namespace is ...:
        scope_namespace = {"__typename": "Project" if scope == "project" else "Group", "fullPath": path}
    return httpx.Response(
        200,
        json={
            "data": {
                "scopeNamespace": scope_namespace,
                "namespace": {"__typename": "Namespace", "fullPath": path, "workItem": item},
            }
        },
    )


def mutation_response(note: Any, errors: Any = None) -> httpx.Response:
    """Build a createNote payload, defaulting to an empty error list."""
    return httpx.Response(
        200, json={"data": {"createNote": {"note": note, "errors": [] if errors is None else errors}}}
    )


async def create_note(**arguments: Any) -> dict[str, Any]:
    """Call the registered production tool through the in-memory MCP client."""
    return await call_tool("create_work_item_note", {"project": PROJECT, "iid": IID, **arguments})


def requests(route: respx.Route) -> list[dict[str, Any]]:
    """Return the GraphQL requests captured by one route."""
    return [json.loads(call.request.content) for call in route.calls]


@pytest.mark.parametrize("source", ["body", "body_file"])
@respx.mock
async def test_truncated_note_is_rejected_before_http(gitlab_env, tmp_path, source):
    """Inline and file-backed previews are rejected before target lookup."""
    # Arrange
    body = "preview (45 more lines → /var/tmp/file)"
    file = tmp_path / "note.md"
    file.write_text(body, encoding="utf-8")

    # Act / Assert
    with pytest.raises(ToolError, match="truncation marker"):
        await create_note(**{source: body if source == "body" else str(file)})
    assert not respx.calls


@pytest.mark.parametrize(("scope", "path"), [("project", PROJECT), ("group", "group")])
@respx.mock
async def test_create_uses_scoped_lookup_and_global_id(gitlab_env, scope, path):
    """Project and group notes use one scoped target lookup and one global-ID mutation."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            lookup_response(scope=scope, path=path),
            mutation_response({"id": NOTE_ID, "discussion": {"id": DISCUSSION_ID}}),
        ]
    )

    # Act
    result = await create_note(project=path, scope=scope, body=PRIVATE_BODY)

    # Assert
    assert result == {"status": "created", "note_id": NOTE_ID, "discussion_id": DISCUSSION_ID}
    assert PRIVATE_BODY not in str(result)
    assert route.call_count == LOOKUP_AND_MUTATION
    lookup, mutation = requests(route)
    assert lookup["variables"] == {"fullPath": path, "iid": str(IID)}
    assert f"scopeNamespace: {scope}(fullPath: $fullPath)" in lookup["query"]
    assert f"scopeNamespace: {'group' if scope == 'project' else 'project'}(" not in lookup["query"]
    assert "namespace(fullPath: $fullPath)" in lookup["query"]
    assert "workItem(iid: $iid) { id iid }" in lookup["query"]
    assert "description" not in lookup["query"]
    assert "widgets" not in lookup["query"]
    assert mutation["variables"] == {"noteableId": ITEM_ID, "body": PRIVATE_BODY}
    assert "$noteableId: NoteableID!" in mutation["query"]
    assert "createNote(input:" in mutation["query"]
    assert "discussion { id }" in mutation["query"]


@pytest.mark.parametrize("source", ["body", "body_file"])
@respx.mock
async def test_exact_body_source_and_optional_discussion(gitlab_env, tmp_path, source):
    """Inline and local-file bodies preserve Unicode, whitespace, and trailing newlines exactly."""
    # Arrange
    body = "  café 🦊\r\n\n"
    file = tmp_path / "note.md"
    file.write_bytes(body.encode("utf-8"))
    route = respx.post(GRAPHQL).mock(side_effect=[lookup_response(), mutation_response({"id": NOTE_ID})])

    # Act
    result = await create_note(**{source: body if source == "body" else str(file)})

    # Assert
    assert result == {"status": "created", "note_id": NOTE_ID}
    assert requests(route)[1]["variables"]["body"] == body
    assert route.call_count == LOOKUP_AND_MUTATION


@pytest.mark.parametrize("discussion", [None, {}, {"id": ""}, {"id": "  "}, {"id": 42}, "bad"])
@respx.mock
async def test_invalid_optional_discussion_is_omitted(gitlab_env, discussion):
    """A valid note ID confirms creation even when the optional discussion ID is unusable."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=[lookup_response(), mutation_response({"id": NOTE_ID, "discussion": discussion})]
    )

    # Act
    result = await create_note(body=PRIVATE_BODY)

    # Assert
    assert result == {"status": "created", "note_id": NOTE_ID}
    assert route.call_count == LOOKUP_AND_MUTATION


@respx.mock
async def test_validate_only_checks_target_without_mutation(gitlab_env):
    """Validation confirms the target but sends no mutation and does not claim write permission."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=lookup_response())

    # Act
    result = await create_note(body=PRIVATE_BODY, validate_only=True)

    # Assert
    assert result["status"] == "validated"
    assert "permission" in result.get("message", "").lower()
    assert PRIVATE_BODY not in str(result)
    assert route.call_count == 1
    assert "mutation" not in requests(route)[0]["query"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "exactly one"),
        ({"body": "a", "body_file": "/unused/file"}, "exactly one"),
        ({"body": ""}, "blank"),
        ({"body": " \n\t"}, "blank"),
        ({"body": "x", "project": " "}, "project"),
        ({"body": "x", "iid": 0}, "iid"),
        ({"body": "x", "scope": "other"}, "project.*group"),
    ],
)
@respx.mock
async def test_invalid_arguments_fail_before_http(gitlab_env, arguments, message):
    """Invalid source choices, bodies, targets, and scopes are rejected before any request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=lookup_response())

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await create_note(**arguments)
    assert route.call_count == 0


@pytest.mark.parametrize("source", ["body", "body_file"])
@pytest.mark.parametrize("extra", [0, 1])
@respx.mock
async def test_utf8_byte_limit(gitlab_env, tmp_path, source, extra):
    """The cap counts UTF-8 bytes, with exactly the cap accepted and one extra byte rejected."""
    # Arrange
    body = "é" * (REVIEW_NOTE_BODY_CAP // 2) + "x" * extra
    file = tmp_path / "note.md"
    file.write_text(body, encoding="utf-8")
    route = respx.post(GRAPHQL).mock(return_value=lookup_response())
    arguments = {source: body if source == "body" else str(file), "validate_only": True}

    # Act / Assert
    if extra:
        with pytest.raises(ToolError, match="100000 UTF-8 bytes"):
            await create_note(**arguments)
        assert route.call_count == 0
    else:
        assert (await create_note(**arguments))["status"] == "validated"
        assert route.call_count == 1


@pytest.mark.parametrize("kind", ["relative", "empty", "missing", "directory", "fifo", "utf8", "blank"])
@respx.mock
async def test_invalid_local_file_fails_before_http(gitlab_env, tmp_path, kind):
    """Only absolute regular UTF-8 files with nonblank content are accepted."""
    # Arrange
    file = tmp_path / "note"
    if kind == "relative":
        path = "relative-note"
    elif kind == "empty":
        path = ""
    elif kind == "directory":
        path = str(tmp_path)
    else:
        path = str(file)
        if kind == "fifo":
            os.mkfifo(file)
        elif kind == "utf8":
            file.write_bytes(b"\xff")
        elif kind == "blank":
            file.write_text(" \n", encoding="utf-8")
    route = respx.post(GRAPHQL).mock(return_value=lookup_response())

    # Act / Assert
    with pytest.raises(ToolError):
        await create_note(body_file=path)
    assert route.call_count == 0


async def test_tool_schema_describes_optional_body_sources(gitlab_env):
    """The MCP catalog describes both optional, mutually exclusive body sources."""
    # Arrange / Act
    async with Client(mcp) as client:
        tool = next(tool for tool in await client.list_tools() if tool.name == "create_work_item_note")

    # Assert
    schema = tool.input_schema
    assert "body" not in schema.get("required", [])
    assert "body_file" not in schema.get("required", [])
    assert "body_file" in schema["properties"]["body"]["description"]
    assert "server" in schema["properties"]["body_file"]["description"]
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is True


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (lookup_response(scope_namespace=None), "not found"),
        (lookup_response(item={"id": ITEM_ID, "iid": "43"}), "different work item IID"),
        (lookup_response(item={"id": ITEM_ID, "iid": 42.5}), "different work item IID"),
        (lookup_response(item={"id": ITEM_ID, "iid": "invalid"}), "valid IID"),
        (lookup_response(scope_namespace={"__typename": "Group", "fullPath": PROJECT}), "not the requested"),
        (lookup_response(scope_namespace={"__typename": "Project", "fullPath": "other"}), "different full path"),
        (lookup_response(item={"id": "gid://gitlab/Issue/123", "iid": "42"}), "work item.*ID"),
        (lookup_response(item={"id": "gid://gitlab/WorkItem/0", "iid": "42"}), "work item.*ID"),
        (lookup_response(item={"id": "gid://gitlab/WorkItem/-1", "iid": "42"}), "work item.*ID"),
        (lookup_response(item={"id": None, "iid": "42"}), "work item.*ID"),
        (
            httpx.Response(
                200,
                json={
                    "data": {
                        "scopeNamespace": {"__typename": "Project", "fullPath": PROJECT},
                        "namespace": {"workItem": None},
                    }
                },
            ),
            "not found",
        ),
    ],
)
@respx.mock
async def test_invalid_target_never_mutates(gitlab_env, target, message):
    """Missing, mismatched, wrong-scope, and invalid-ID targets cannot reach the mutation."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=target)

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await create_note(body=PRIVATE_BODY)
    assert route.call_count == 1


@respx.mock
async def test_missing_group_target_never_falls_back_to_project(gitlab_env):
    """A missing group target is not retried under project scope."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=lookup_response(scope_namespace=None))

    # Act / Assert
    with pytest.raises(ToolError, match="not found"):
        await create_note(project="group", scope="group", body=PRIVATE_BODY)
    assert route.call_count == 1
    assert "scopeNamespace: group(" in requests(route)[0]["query"]


@pytest.mark.parametrize("response", [httpx.Response(503), httpx.Response(200, json={"errors": [{"message": "no"}]})])
@respx.mock
async def test_lookup_failure_is_an_error_not_an_unknown_write(gitlab_env, response):
    """Failures during target lookup are surfaced without attempting a mutation."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response)

    # Act / Assert
    with pytest.raises(ToolError):
        await create_note(body=PRIVATE_BODY)
    assert route.call_count == 1
    assert "createNote" not in requests(route)[0]["query"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
@respx.mock
async def test_http_rejection_does_not_echo_body(gitlab_env, status):
    """Definite HTTP rejections raise a safe error without retrying or returning GitLab's echoed body."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=[lookup_response(), httpx.Response(status, json={"message": PRIVATE_BODY})]
    )

    # Act / Assert
    with pytest.raises(ToolError, match=str(status)) as exc:
        await create_note(body=PRIVATE_BODY)
    assert PRIVATE_BODY not in str(exc.value)
    assert route.call_count == LOOKUP_AND_MUTATION


@respx.mock
async def test_payload_rejection_does_not_echo_body(gitlab_env):
    """A nonempty list of mutation errors with no note is a definite rejection."""
    # Arrange
    route = respx.post(GRAPHQL).mock(side_effect=[lookup_response(), mutation_response(None, [PRIVATE_BODY])])

    # Act / Assert
    with pytest.raises(ToolError, match="rejected") as exc:
        await create_note(body=PRIVATE_BODY)
    assert PRIVATE_BODY not in str(exc.value)
    assert route.call_count == LOOKUP_AND_MUTATION


@pytest.mark.parametrize(
    "write_response",
    [
        httpx.ConnectError("connection lost"),
        httpx.Response(408),
        httpx.Response(500),
        httpx.Response(503),
        httpx.Response(
            200, json={"errors": [{"message": PRIVATE_BODY}], "data": {"createNote": {"note": {"id": NOTE_ID}}}}
        ),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"data": None}),
        httpx.Response(200, json={"data": {"createNote": None}}),
        httpx.Response(200, json={"data": {"createNote": {"note": {"id": NOTE_ID}}}}),
        httpx.Response(200, json={"data": {"createNote": {"errors": ["error"]}}}),
        httpx.Response(200, json={"data": {"createNote": {"errors": None, "note": {"id": NOTE_ID}}}}),
        mutation_response(None),
        mutation_response({"id": NOTE_ID}, ["error"]),
        mutation_response({"id": NOTE_ID}, "error"),
        mutation_response({"id": NOTE_ID}, [3]),
        mutation_response({"id": "gid://gitlab/Note/0"}),
        mutation_response({"id": "gid://gitlab/WorkItem/1"}),
        mutation_response({"id": "gid://gitlab/Note/-1"}),
        mutation_response({"id": ""}),
        mutation_response({}),
    ],
)
@respx.mock
async def test_uncertain_mutation_is_never_retried(gitlab_env, write_response):
    """Transport, GraphQL, malformed, and null-note results have unknown outcomes, including quick actions."""
    # Arrange
    route = respx.post(GRAPHQL).mock(side_effect=[lookup_response(), write_response])

    # Act
    result = await create_note(body=PRIVATE_BODY)

    # Assert
    assert result == UNKNOWN
    assert PRIVATE_BODY not in str(result)
    assert route.call_count == LOOKUP_AND_MUTATION
    assert "createNote" in requests(route)[1]["query"]
