"""Behavioral tests for bounded, scoped work-item note reads 🧪."""

import hashlib
import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from mcp_helpers import call_tool

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 42
NOTE_ID = "gid://gitlab/Note/101"
ITEM_ID = "gid://gitlab/WorkItem/123"
PRIVATE_BODY = "private note body"
PAGE_SIZE = 4000


def note_node(**overrides: Any) -> dict[str, Any]:
    """Build one complete note, allowing individual malformed fields."""
    return {
        "id": NOTE_ID,
        "system": False,
        "body": PRIVATE_BODY,
        "createdAt": "2026-10-21T10:00:00Z",
        "author": {"username": "mhayden"},
        "discussion": {"noteable": {"__typename": "Issue", "id": "gid://gitlab/Issue/123"}},
    } | overrides


def note_response(
    *,
    body: str = PRIVATE_BODY,
    note: Any = ...,
    item: Any = ...,
    scope: str = "project",
    path: str = PROJECT,
) -> httpx.Response:
    """Build one scoped target and note GraphQL response."""
    if item is ...:
        item = {"id": ITEM_ID, "iid": str(IID)}
    if note is ...:
        note = note_node(body=body)
    data = {
        "scopeNamespace": {"__typename": "Project" if scope == "project" else "Group", "fullPath": path},
        "namespace": {"__typename": "Namespace", "fullPath": path, "workItem": item},
        "note": note,
    }
    return httpx.Response(200, json={"data": data})


@pytest.mark.parametrize("scope", ["project", "group"])
@pytest.mark.parametrize("parent_model", ["Issue", "WorkItem"])
@respx.mock
async def test_scoped_read_uses_one_query_and_validates_parent(gitlab_env, scope, parent_model):
    """Both explicit scopes accept either canonical GID for the same Issue row."""
    # Arrange
    node = note_node(discussion={"noteable": {"__typename": "Issue", "id": f"gid://gitlab/{parent_model}/123"}})
    route = respx.post(GRAPHQL).mock(return_value=note_response(note=node, scope=scope))

    # Act
    result = await call_tool("get_work_item_note", {"project": PROJECT, "iid": IID, "note_id": NOTE_ID, "scope": scope})

    # Assert
    request = json.loads(route.calls.last.request.content)
    assert route.call_count == 1
    assert request["variables"] == {"fullPath": PROJECT, "iid": str(IID), "noteId": NOTE_ID}
    assert f"scopeNamespace: {scope}(fullPath: $fullPath)" in request["query"]
    assert "namespace(fullPath: $fullPath)" in request["query"]
    assert "note(id: $noteId)" in request["query"]
    assert "... on Issue { id }" in request["query"]
    assert "noteableId" not in request["query"]
    assert "mutation" not in request["query"]
    assert result == {
        "note_id": NOTE_ID,
        "by": "mhayden",
        "body": PRIVATE_BODY,
        "at": "2026-10-21",
        "body_hash": hashlib.sha256(PRIVATE_BODY.encode()).hexdigest(),
    }


@respx.mock
async def test_note_pages_reconstruct_full_body_and_repeat_full_hash(gitlab_env):
    """Large bodies are returned in bounded chunks with the same full UTF-8 hash."""
    # Arrange
    body = "🦊" * PAGE_SIZE + "b" * PAGE_SIZE + "end"
    route = respx.post(GRAPHQL).mock(return_value=note_response(body=body))
    arguments = {"project": PROJECT, "iid": IID, "note_id": NOTE_ID}

    # Act
    first = await call_tool("get_work_item_note", arguments)
    second = await call_tool("get_work_item_note", arguments | {"body_offset": first["next_body_offset"]})
    third = await call_tool("get_work_item_note", arguments | {"body_offset": second["next_body_offset"]})

    # Assert
    assert first["body"] + second["body"] + third["body"] == body
    assert first["next_body_offset"] == PAGE_SIZE
    assert second["next_body_offset"] == 2 * PAGE_SIZE
    assert "next_body_offset" not in third
    assert [page["body_hash"] for page in (first, second, third)] == [hashlib.sha256(body.encode()).hexdigest()] * 3
    assert route.call_count == len((first, second, third))


@respx.mock
async def test_note_offsets_are_unicode_codepoints_and_edits_change_full_hash(gitlab_env):
    """Offsets may split combining characters; an edit outside the slice changes the hash."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=[note_response(body="🦊e\u0301漢"), note_response(body="🦝e\u0301漢")],
    )
    arguments = {"project": PROJECT, "iid": IID, "note_id": NOTE_ID, "body_limit": 2}

    # Act
    first = await call_tool("get_work_item_note", arguments)
    second = await call_tool("get_work_item_note", arguments | {"body_offset": first["next_body_offset"]})

    # Assert
    assert first["body"] == "🦊e"
    assert first["next_body_offset"] == len("🦊e")
    assert second["body"] == "\u0301漢"
    assert "next_body_offset" not in second
    assert first["body_hash"] == hashlib.sha256("🦊e\u0301漢".encode()).hexdigest()
    assert second["body_hash"] == hashlib.sha256("🦝e\u0301漢".encode()).hexdigest()
    assert first["body_hash"] != second["body_hash"]
    assert route.call_count == len((first, second))


@pytest.mark.parametrize(("body", "offset"), [("", 0), ("", 1), ("abc", 3), ("abc", 9)])
@respx.mock
async def test_empty_or_exhausted_note_pages_have_no_continuation(gitlab_env, body, offset):
    """Empty bodies and offsets at or beyond the end return empty text."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=note_response(body=body))

    # Act
    result = await call_tool(
        "get_work_item_note", {"project": PROJECT, "iid": IID, "note_id": NOTE_ID, "body_offset": offset}
    )

    # Assert
    assert result["body"] == ""
    assert result["body_hash"] == hashlib.sha256(body.encode()).hexdigest()
    assert "next_body_offset" not in result
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"project": " "}, "project must not be blank"),
        ({"iid": 0}, "iid must be a positive integer"),
        ({"scope": "namespace"}, "Input should be"),
        ({"body_offset": -1}, "greater than or equal to 0"),
        ({"body_limit": 0}, "greater than or equal to 1"),
        ({"body_limit": 4001}, "less than or equal to 4000"),
        ({"note_id": ""}, "note_id"),
        ({"note_id": "101"}, "note_id"),
        ({"note_id": "gid://gitlab/Issue/101"}, "note_id"),
        ({"note_id": "gid://gitlab/Note/0"}, "note_id"),
        ({"note_id": "gid://gitlab/Note/0101"}, "note_id"),
    ],
)
@respx.mock
async def test_invalid_arguments_fail_before_requests(gitlab_env, override, message):
    """Invalid bounds and noncanonical note IDs are rejected without a network request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=note_response())

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_work_item_note", {"project": PROJECT, "iid": IID, "note_id": NOTE_ID} | override)
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("node", "message"),
    [
        (None, "not found"),
        ("not a note", "invalid work-item note"),
        (note_node(id=None), "note global ID"),
        (note_node(id="gid://gitlab/Issue/101"), "note global ID"),
        (note_node(id="gid://gitlab/Note/0101"), "note global ID"),
        (note_node(id="gid://gitlab/Note/102"), "different.*note"),
        (note_node(system=True), "system note"),
        (note_node(system=None), "invalid.*system"),
        (note_node(body=None), "note body"),
        (note_node(body=7), "note body"),
        (note_node(discussion=None), "note parent"),
        (note_node(discussion={}), "note parent"),
        (note_node(discussion={"noteable": None}), "note parent"),
        (note_node(discussion={"noteable": {"__typename": "Epic", "id": "gid://gitlab/Issue/123"}}), "note parent"),
        (note_node(discussion={"noteable": {"__typename": "Issue", "id": None}}), "note parent"),
        (note_node(discussion={"noteable": {"__typename": "Issue", "id": "gid://gitlab/Epic/123"}}), "note parent"),
        (note_node(discussion={"noteable": {"__typename": "Issue", "id": "gid://gitlab/Issue/0123"}}), "note parent"),
        (note_node(discussion={"noteable": {"__typename": "Issue", "id": "gid://gitlab/Issue/124"}}), "note parent"),
    ],
)
@respx.mock
async def test_bad_note_or_parent_is_rejected_without_body_output(gitlab_env, node, message):
    """Missing, system, mismatched, or malformed notes never reveal their body."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=note_response(note=node))

    # Act / Assert
    with pytest.raises(ToolError, match=message) as error:
        await call_tool("get_work_item_note", {"project": PROJECT, "iid": IID, "note_id": NOTE_ID})
    assert PRIVATE_BODY not in str(error.value)
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (None, "not found"),
        ({"id": ITEM_ID, "iid": "43"}, "different work item IID"),
        ({"id": ITEM_ID, "iid": None}, "IID"),
        ({"id": "gid://gitlab/Issue/123", "iid": "42"}, "work item global ID"),
        ({"id": "gid://gitlab/WorkItem/0123", "iid": "42"}, "work item global ID"),
        ({"id": None, "iid": "42"}, "work item global ID"),
    ],
)
@respx.mock
async def test_bad_target_is_rejected_without_body_output(gitlab_env, item, message):
    """An inaccessible, mismatched, or invalid scoped target cannot expose a note."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=note_response(item=item))

    # Act / Assert
    with pytest.raises(ToolError, match=message) as error:
        await call_tool("get_work_item_note", {"project": PROJECT, "iid": IID, "note_id": NOTE_ID})
    assert PRIVATE_BODY not in str(error.value)
    assert route.call_count == 1


@respx.mock
async def test_wrong_scope_does_not_fallback_or_leak_body(gitlab_env):
    """The returned namespace must match the requested scope, with no fallback."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=note_response(scope="group"))

    # Act / Assert
    with pytest.raises(ToolError, match="not the requested project scope") as error:
        await call_tool("get_work_item_note", {"project": PROJECT, "iid": IID, "note_id": NOTE_ID})
    assert PRIVATE_BODY not in str(error.value)
    assert route.call_count == 1


@respx.mock
async def test_list_identity_discovery_allows_reading_notes_after_body_budget(gitlab_env):
    """A zero-body list reread exposes IDs after the aggregate budget, then reads one note."""
    # Arrange
    notes = [note_node(id=f"gid://gitlab/Note/{number}", body="x" * 4000) for number in range(101, 110)]
    discussions = [{"notes": {"nodes": notes, "pageInfo": {"hasNextPage": True}}}]
    list_response = note_response().json()
    list_response["data"]["namespace"]["workItem"]["widgets"] = [
        {
            "__typename": "WorkItemWidgetNotes",
            "discussions": {"nodes": discussions, "pageInfo": {"hasNextPage": False}},
        },
    ]
    last_id = notes[-1]["id"]
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            httpx.Response(200, json=list_response),
            httpx.Response(200, json=list_response),
            note_response(note=notes[-1]),
        ],
    )
    arguments = {"project": PROJECT, "iid": IID}

    # Act
    bounded = await call_tool("list_work_item_notes", arguments)
    identities = await call_tool("list_work_item_notes", arguments | {"max_body_chars": 0})
    result = await call_tool("get_work_item_note", arguments | {"note_id": identities["notes"][-1]["note_id"]})

    # Assert
    assert len(bounded["notes"]) == len(notes) - 1
    assert len(identities["notes"]) == len(notes)
    assert all(entry["body"] == "" for entry in identities["notes"])
    assert identities["notes_truncated"] is True
    assert result["note_id"] == last_id
    assert result["body"] == "x" * 4000
    assert route.call_count == len((bounded, identities, result))
    assert all("mutation" not in json.loads(call.request.content)["query"] for call in route.calls)
