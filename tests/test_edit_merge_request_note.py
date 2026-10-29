"""Behavioral tests for guarded merge request note edits 🧪."""

import copy
import hashlib
import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from mcp_helpers import call_tool

API = "https://gitlab.example.com/api/v4"
DISCUSSION_URL = API + "/projects/group%2Fproject/merge_requests/42/discussions/thread-1"
NOTE_URL = DISCUSSION_URL + "/notes/7"
ORIGINAL = "Original café\n<!-- review-bot:finding hidden -->"


def digest(body: str) -> str:
    """Hash the exact UTF-8 body, as returned by GitLab."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def note(note_id: int = 7, body: str = ORIGINAL, **values: Any) -> dict[str, Any]:
    """Build a small realistic REST note with optional overrides."""
    return {
        "id": note_id,
        "type": "DiffNote",
        "system": False,
        "body": body,
        "resolvable": True,
        "resolved": False,
        "position": {
            "base_sha": "base",
            "start_sha": "start",
            "head_sha": "head",
            "old_path": "a.py",
            "new_path": "b.py",
            "old_line": 4,
            "new_line": 5,
            "line_range": {"start": {"line_code": "abc", "type": "new", "new_line": 5}},
        },
        **values,
    }


def discussion(notes: list[dict[str, Any]] | None = None, **values: Any) -> dict[str, Any]:
    """Build one REST discussion, including its complete note list."""
    return {"id": "thread-1", "individual_note": False, "notes": notes if notes is not None else [note()], **values}


async def edit(**arguments: Any) -> dict[str, Any]:
    """Call the editor through the real in-memory MCP client."""
    return await call_tool(
        "edit_merge_request_note",
        {
            "project": "group/project",
            "iid": 42,
            "thread_id": "thread-1",
            "note_id": 7,
            "expected_hash": digest(ORIGINAL),
            "old_text": "Original",
            "new_text": "Edited",
            **arguments,
        },
    )


def responses(before: dict[str, Any], after: dict[str, Any]) -> respx.Route:
    """Mock the two ordered bounded discussion reads."""
    return respx.get(DISCUSSION_URL).mock(
        side_effect=[httpx.Response(200, json=before), httpx.Response(200, json=after)]
    )


@pytest.mark.parametrize(
    "case", ["individual", "root-open", "root-resolved", "reply-open", "reply-resolved", "late-reply"]
)
@respx.mock
async def test_edits_note_without_changing_thread_state(gitlab_env, case):
    """General notes and root or late replies preserve their exact body and discussion invariants."""
    # Arrange
    raw = ORIGINAL + "\n" + "suffix" * 800
    target = note(body=raw)
    is_reply = case.startswith("reply") or case == "late-reply"
    if case == "individual":
        target.update(type=None, resolvable=False, position=None)
        target.pop("resolved")
    if case.endswith("resolved"):
        target["resolved"] = True
    root = note(3, "root", resolved=case.endswith("resolved"))
    entries = [root, target] if is_reply else [target]
    if case == "late-reply":
        entries = [root, *[note(i, "tiny", type=None, resolvable=False, position=None) for i in range(10, 59)], target]
    before = discussion(entries, individual_note=case == "individual")
    saved = raw.replace("Original", "Edited", 1)
    after = copy.deepcopy(before)
    after["notes"][-1]["body"] = saved
    reads = responses(before, after)
    write = respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act
    result = await edit(expected_hash=digest(raw))

    # Assert
    assert result == {
        "status": "applied",
        "note_id": 7,
        "discussion_id": "thread-1",
        "body_verified": True,
        "body_hash": digest(saved),
    }
    assert [call.request.method for call in reads.calls] == ["GET", "GET"]
    assert write.call_count == 1
    assert json.loads(write.calls.last.request.content) == {"body": saved}
    assert [call.request.method for call in respx.calls] == ["GET", "PUT", "GET"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"note_id": True},
        {"note_id": "7"},
        {"note_id": 0},
        {"expected_hash": "A" * 64},
        {"expected_hash": "f" * 63},
        {"thread_id": ""},
        {"thread_id": " "},
        {"thread_id": "gid://gitlab/User/abc"},
        {"thread_id": "gid://gitlab/Discussion/"},
        {"thread_id": "gid://gitlab/Discussion/one/two"},
        {"body": "full"},
        {"old_text": None, "new_text": None},
        {"old_text": "", "new_text": "x"},
        {"new_text": None},
        {"old_text": None},
        {"body": " ", "old_text": None, "new_text": None},
        {"body": "x" * 1_000_001, "old_text": None, "new_text": None},
    ],
)
@respx.mock
async def test_invalid_arguments_are_rejected_before_network(gitlab_env, arguments):
    """Bad IDs, hashes, edit modes, and full bodies fail before any request."""
    # Act / Assert
    with pytest.raises(ToolError):
        await edit(**arguments)
    assert not respx.calls


@pytest.mark.parametrize(
    "variant",
    [
        "wrong-discussion",
        "missing-target",
        "duplicate-target",
        "system",
        "missing-system",
        "missing-body",
        "non-string-body",
        "bool-id",
        "missing-root",
        "missing-notes",
        "bad-notes",
        "bad-individual",
        "bad-root-resolution",
        "bad-resolvable",
        "bad-position",
        "not-object",
        "bad-json",
        "next-page",
        "oversized",
        "stale-hash",
        "read-error",
    ],
)
@respx.mock
async def test_preflight_rejects_unsafe_or_incomplete_discussion(gitlab_env, variant):  # noqa: C901, PLR0912
    """Untrusted discussion responses and stale bodies never result in a write."""
    # Arrange
    data = discussion()
    response = None
    if variant == "wrong-discussion":
        data["id"] = "other"
    elif variant == "missing-target":
        data["notes"][0]["id"] = 8
    elif variant == "duplicate-target":
        data["notes"].append(copy.deepcopy(data["notes"][0]))
    elif variant == "system":
        data["notes"][0]["system"] = True
    elif variant == "missing-system":
        data["notes"][0].pop("system")
    elif variant == "missing-body":
        data["notes"][0].pop("body")
    elif variant == "non-string-body":
        data["notes"][0]["body"] = None
    elif variant == "bool-id":
        data["notes"][0]["id"] = True
    elif variant == "missing-root":
        data["notes"] = []
    elif variant == "missing-notes":
        data.pop("notes")
    elif variant == "bad-notes":
        data["notes"] = ["invalid"]
    elif variant == "bad-individual":
        data["individual_note"] = "false"
    elif variant == "bad-root-resolution":
        data["notes"][0].pop("resolved")
    elif variant == "bad-resolvable":
        data["notes"][0]["resolvable"] = 1
    elif variant == "bad-position":
        data["notes"][0]["position"] = "truncated"
    elif variant == "not-object":
        data = []
    elif variant == "bad-json":
        response = httpx.Response(200, content=b"{")
    elif variant == "next-page":
        response = httpx.Response(200, json=data, headers={"X-Next-Page": "2"})
    elif variant == "oversized":
        response = httpx.Response(200, content=b"x" * 1_048_577)
    elif variant == "read-error":
        response = httpx.Response(403)
    elif variant == "stale-hash":
        data["notes"][0]["body"] = "stale"
    respx.get(DISCUSSION_URL).mock(return_value=response or httpx.Response(200, json=data))

    # Act / Assert
    with pytest.raises(ToolError, match=r"(?i)(no write|not written)"):
        await edit()
    assert [call.request.method for call in respx.calls] == ["GET"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"old_text": "missing", "new_text": "x"},
        {"old_text": "Original", "new_text": "Original"},
        {"old_text": "aa", "new_text": "x", "expected_hash": digest("aaa")},
        {"old_text": ORIGINAL, "new_text": ""},
        {"body": ORIGINAL, "old_text": None, "new_text": None},
    ],
)
@respx.mock
async def test_unsafe_exact_edits_do_not_write(gitlab_env, arguments):
    """Missing, overlapping, unchanged, and blank final edits fail after the read."""
    # Arrange
    original = "aaa" if arguments.get("old_text") == "aa" else ORIGINAL
    respx.get(DISCUSSION_URL).mock(return_value=httpx.Response(200, json=discussion([note(body=original)])))

    # Act / Assert
    with pytest.raises(ToolError, match=r"(?i)(no write|not written)"):
        await edit(**arguments)
    assert [call.request.method for call in respx.calls] == ["GET"]


@pytest.mark.parametrize(
    "final", ["/approve", "text\n /merge now", "```\n/label ~x\n```", "\r/approve", "/\rapprove", "text\u2028/merge"]
)
@respx.mock
async def test_quick_action_lines_rejected_even_inside_fences(gitlab_env, final):
    """The complete final body rejects slash-command lines, including CR-obfuscated examples."""
    # Act / Assert
    with pytest.raises(ToolError, match=r"(?i)(no write|not written)"):
        await edit(body=final, old_text=None, new_text=None)
    assert not respx.calls


@respx.mock
async def test_inherited_quick_action_is_rejected(gitlab_env):
    """Exact edits also scan commands already present in the original body."""
    # Arrange
    raw = "Original\n/approve"
    respx.get(DISCUSSION_URL).mock(return_value=httpx.Response(200, json=discussion([note(body=raw)])))

    # Act / Assert
    with pytest.raises(ToolError, match=r"(?i)(no write|not written)"):
        await edit(expected_hash=digest(raw))
    assert [call.request.method for call in respx.calls] == ["GET"]


@pytest.mark.parametrize(("status", "expected"), [(403, "failed"), (404, "failed"), (408, "unknown"), (500, "unknown")])
@respx.mock
async def test_http_write_outcomes_are_not_retried(gitlab_env, status, expected):
    """Definite client failures and uncertain timeout/server failures have distinct outcomes."""
    # Arrange
    respx.get(DISCUSSION_URL).mock(return_value=httpx.Response(200, json=discussion()))
    write = respx.put(NOTE_URL).mock(return_value=httpx.Response(status))

    # Act
    result = await edit()

    # Assert
    assert result["status"] == expected
    assert result["body_verified"] is False
    assert result["warning"]
    assert write.call_count == 1
    assert json.loads(write.calls.last.request.content) == {"body": ORIGINAL.replace("Original", "Edited")}
    assert [call.request.method for call in respx.calls] == ["GET", "PUT"]


@respx.mock
async def test_timeout_is_unknown_and_never_retried(gitlab_env):
    """A connection timeout after PUT may hide a completed write."""
    # Arrange
    respx.get(DISCUSSION_URL).mock(return_value=httpx.Response(200, json=discussion()))
    write = respx.put(NOTE_URL).mock(side_effect=httpx.ReadTimeout("timed out"))

    # Act
    result = await edit()

    # Assert
    assert result["status"] == "unknown"
    assert write.call_count == 1
    assert [call.request.method for call in respx.calls] == ["GET", "PUT"]


@pytest.mark.parametrize("variant", ["malformed", "missing", "wrong-id", "wrong-body", "bool-id"])
@respx.mock
async def test_untrustworthy_success_response_is_unverified(gitlab_env, variant):
    """An invalid PUT success cannot be reported as failed or silently accepted."""
    # Arrange
    saved = ORIGINAL.replace("Original", "Edited")
    responses(discussion(), discussion([note(body=saved)]))
    reply = {"id": 7, "body": saved}
    if variant == "wrong-id":
        reply["id"] = 8
    elif variant == "wrong-body":
        reply["body"] = "different"
    elif variant == "bool-id":
        reply["id"] = True
    elif variant == "missing":
        reply.pop("body")
    response = httpx.Response(200, content=b"{") if variant == "malformed" else httpx.Response(200, json=reply)
    write = respx.put(NOTE_URL).mock(return_value=response)

    # Act
    result = await edit()

    # Assert
    assert result["status"] == "unverified"
    assert result["body_verified"] is True
    assert result["body_hash"] == digest(saved)
    assert result["warning"]
    assert write.call_count == 1


@pytest.mark.parametrize(
    "variant",
    [
        "normalized",
        "read-error",
        "oversized",
        "wrong-discussion",
        "wrong-note",
        "changed-root-position",
        "changed-target-position",
        "changed-position-type",
        "root-resolution",
        "target-resolution",
        "root-resolvable",
        "target-resolvable",
        "malformed-position",
        "pagination",
        "bad-json",
    ],
)
@respx.mock
async def test_failed_post_verification_is_unverified(gitlab_env, variant):  # noqa: C901, PLR0912
    """A changed body, position, root resolution, identity, or unreadable response is not called applied."""
    # Arrange
    saved = ORIGINAL.replace("Original", "Edited")
    before = discussion([note(3, "root"), note()])
    after = copy.deepcopy(before)
    after["notes"][1]["body"] = saved
    post_response = None
    if variant == "normalized":
        after["notes"][1]["body"] = saved + "\n"
    elif variant == "wrong-discussion":
        after["id"] = "other"
    elif variant == "wrong-note":
        after["notes"][1]["id"] = 8
    elif variant == "changed-root-position":
        after["notes"][0]["position"]["head_sha"] = "new-head"
    elif variant == "changed-target-position":
        after["notes"][1]["position"]["line_range"]["start"]["line_code"] = "new-code"
    elif variant == "changed-position-type":
        after["notes"][1]["position"]["line_range"]["start"]["new_line"] = True
    elif variant == "root-resolution":
        after["notes"][0]["resolved"] = True
    elif variant == "target-resolution":
        after["notes"][1]["resolved"] = True
    elif variant == "root-resolvable":
        after["notes"][0]["resolvable"] = False
    elif variant == "target-resolvable":
        after["notes"][1]["resolvable"] = False
    elif variant == "malformed-position":
        after["notes"][0]["position"] = "invalid"
    elif variant == "read-error":
        post_response = httpx.Response(403)
    elif variant == "oversized":
        post_response = httpx.Response(200, content=b"x" * 1_048_577)
    elif variant == "pagination":
        post_response = httpx.Response(200, json=after, headers={"X-Next-Page": "2"})
    elif variant == "bad-json":
        post_response = httpx.Response(200, content=b"{")
    respx.get(DISCUSSION_URL).mock(
        side_effect=[httpx.Response(200, json=before), post_response or httpx.Response(200, json=after)]
    )
    write = respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act
    result = await edit()

    # Assert
    assert result["status"] == "unverified"
    assert result["warning"]
    assert write.call_count == 1
    if variant == "normalized":
        assert result["body_hash"] == digest(saved + "\n")
        assert result["body_verified"] is False
    if variant in {
        "changed-root-position",
        "changed-target-position",
        "changed-position-type",
        "root-resolution",
        "target-resolution",
        "root-resolvable",
        "target-resolvable",
        "malformed-position",
    }:
        assert result["body_verified"] is True
        assert result["body_hash"] == digest(saved)


@respx.mock
async def test_full_replacement_and_valid_discussion_gid(gitlab_env):
    """An intentional full replacement works with a proper discussion GID."""
    # Arrange
    saved = "New body"
    responses(discussion(), discussion([note(body=saved)]))
    write = respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act
    result = await edit(thread_id="gid://gitlab/Discussion/thread-1", body=saved, old_text=None, new_text=None)

    # Assert
    assert result["status"] == "applied"
    assert json.loads(write.calls.last.request.content) == {"body": saved}


@respx.mock
async def test_gitlab_normalization_returns_observed_hash_without_claiming_exact_match(gitlab_env):
    """GitLab's CR and trailing-whitespace normalization is unverified, not a rejected write."""
    # Arrange
    submitted = "Edited\r\ncafé  \r\n"
    saved = "Edited\ncafé"
    responses(discussion(), discussion([note(body=saved)]))
    write = respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act
    result = await edit(body=submitted, old_text=None, new_text=None)

    # Assert
    assert result["status"] == "unverified"
    assert result["body_verified"] is False
    assert result["body_hash"] == digest(saved)
    assert json.loads(write.calls.last.request.content) == {"body": submitted}
    assert [call.request.method for call in respx.calls] == ["GET", "PUT", "GET"]


@respx.mock
async def test_exact_deletion_may_leave_nonblank_text(gitlab_env):
    """Deleting a unique substring is valid when the final body is not blank."""
    # Arrange
    saved = ORIGINAL.replace("Original", "")
    responses(discussion(), discussion([note(body=saved)]))
    respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act
    result = await edit(new_text="")

    # Assert
    assert result["status"] == "applied"
    assert result["body_hash"] == digest(saved)


@respx.mock
async def test_changed_author_and_other_replies_do_not_change_verification(gitlab_env):
    """Only the stable target and root invariants matter, not authors, timestamps, or other replies."""
    # Arrange
    before = discussion([note(3, "root"), note(), note(9, "other")])
    after = copy.deepcopy(before)
    saved = ORIGINAL.replace("Original", "Edited")
    after["notes"][1].update(body=saved, author={"username": "new"}, updated_at="later")
    after["notes"][0].update(author=None, updated_at="later")
    after["notes"][2]["body"] = "a changed reply"
    responses(before, after)
    respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act
    result = await edit()

    # Assert
    assert result["status"] == "applied"


@respx.mock
async def test_discussion_and_project_path_segments_are_quoted(gitlab_env):
    """Bare discussion IDs and nested project paths cannot escape their REST path segments."""
    # Arrange
    discussion_id = "thread/a?b"
    url = API + "/projects/group%2Fnested%20project/merge_requests/42/discussions/thread%2Fa%3Fb"
    saved = ORIGINAL.replace("Original", "Edited")
    respx.get(url).mock(
        side_effect=[
            httpx.Response(200, json=discussion(id=discussion_id)),
            httpx.Response(200, json=discussion([note(body=saved)], id=discussion_id)),
        ]
    )
    write = respx.put(url + "/notes/7").mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act
    result = await edit(project="group/nested project", thread_id=discussion_id)

    # Assert
    assert result["status"] == "applied"
    assert write.calls.last.request.url.raw_path.endswith(b"/discussions/thread%2Fa%3Fb/notes/7")
    assert json.loads(write.calls.last.request.content) == {"body": saved}


@pytest.mark.parametrize("kind", [None, "Note", "DiscussionNote"])
@respx.mock
async def test_nonresolvable_note_does_not_need_resolved_field(gitlab_env, kind):
    """Nonresolvable notes need no resolved field and ignore any nonapplicable value."""
    # Arrange
    target = note(type=kind, resolvable=False, position=None)
    target.pop("resolved")
    saved = ORIGINAL.replace("Original", "Edited")
    changed = {**target, "body": saved, "resolved": False}
    responses(discussion([target], individual_note=True), discussion([changed], individual_note=True))
    respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act
    result = await edit()

    # Assert
    assert result["status"] == "applied"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("third-reopened", "unverified"),
        ("last-resolvable-removed", "unverified"),
        ("another-open", "applied"),
        ("new-nonresolvable", "applied"),
        ("top-level-changed", "unverified"),
    ],
)
@respx.mock
async def test_complete_discussion_resolution_is_checked(gitlab_env, case, expected):
    """Verify aggregate resolution without requiring unrelated replies to remain unchanged."""
    # Arrange
    before = discussion([note(3, "root", resolved=True), note(resolved=True), note(9, "other", resolved=True)])
    if case == "last-resolvable-removed":
        before["notes"][0]["resolvable"] = False
        before["notes"][1]["resolvable"] = False
    elif case == "another-open":
        before["notes"][0]["resolved"] = False
    elif case == "top-level-changed":
        before.update(resolvable=True, resolved=True)
    saved = ORIGINAL.replace("Original", "Edited")
    after = copy.deepcopy(before)
    after["notes"][1]["body"] = saved
    if case in {"third-reopened", "another-open"}:
        after["notes"][2]["resolved"] = False
    elif case == "last-resolvable-removed":
        after["notes"][2]["resolvable"] = False
    elif case == "new-nonresolvable":
        added = note(10, "new reply", type="DiscussionNote", position=None, resolvable=False, resolved="ignored")
        after["notes"].append(added)
    elif case == "top-level-changed":
        after["resolved"] = False
    responses(before, after)
    write = respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act
    result = await edit()

    # Assert
    assert result["status"] == expected
    assert result["body_verified"] is True
    assert result["body_hash"] == digest(saved)
    assert write.call_count == 1
    assert json.loads(write.calls.last.request.content) == {"body": saved}


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize(
    "invalid", ["missing-resolvable", "invalid-resolvable", "missing-resolved", "invalid-resolved"]
)
@respx.mock
async def test_unrelated_reply_requires_valid_resolution_state(gitlab_env, phase, invalid):
    """Malformed resolution on another reply blocks the write or makes verification unverified."""
    # Arrange
    before = discussion([note(3, "root"), note(), note(9, "other")])
    saved = ORIGINAL.replace("Original", "Edited")
    after = copy.deepcopy(before)
    after["notes"][1]["body"] = saved
    other = (before if phase == "before" else after)["notes"][2]
    field = "resolvable" if invalid.endswith("resolvable") else "resolved"
    if invalid.startswith("missing"):
        other.pop(field)
    else:
        other[field] = 1
    reads = responses(before, after)
    write = respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act / Assert
    if phase == "before":
        with pytest.raises(ToolError, match=r"(?i)no write"):
            await edit()
        assert write.call_count == 0
        assert reads.call_count == 1
    else:
        result = await edit()
        assert result["status"] == "unverified"
        assert result["body_verified"] is True
        assert result["body_hash"] == digest(saved)
        assert result["warning"]
        assert write.call_count == 1


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("selected", ["root", "target"])
@pytest.mark.parametrize("invalid", ["missing", "malformed", "unknown"])
@respx.mock
async def test_root_and_target_require_explicit_valid_note_kind(gitlab_env, phase, selected, invalid):
    """Missing and unrecognized kinds cannot make an absent position look like a general note."""
    # Arrange
    before = discussion(
        [note(3, "root", type="DiscussionNote", position=None), note(type="DiscussionNote", position=None)]
    )
    saved = ORIGINAL.replace("Original", "Edited")
    after = copy.deepcopy(before)
    after["notes"][1]["body"] = saved
    selected_note = (before if phase == "before" else after)["notes"][0 if selected == "root" else 1]
    selected_note.pop("position")
    if invalid == "missing":
        selected_note.pop("type")
    else:
        selected_note["type"] = 4 if invalid == "malformed" else "UnknownNote"
    reads = responses(before, after)
    write = respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act / Assert
    if phase == "before":
        with pytest.raises(ToolError, match=r"(?i)no write"):
            await edit()
        assert write.call_count == 0
        assert reads.call_count == 1
    else:
        result = await edit()
        assert result["status"] == "unverified"
        assert result["body_verified"] is True
        assert result["body_hash"] == digest(saved)
        assert result["warning"]
        assert write.call_count == 1


@pytest.mark.parametrize(("change_kind", "selected"), [(False, "root"), (True, "root"), (True, "target")])
@respx.mock
async def test_general_threaded_discussion_notes_have_stable_kind(gitlab_env, change_kind, selected):
    """Valid general discussion notes work, but a changed root or target kind is not ignored."""
    # Arrange
    root = note(3, "root", type="DiscussionNote", position=None, resolvable=False)
    target = note(type="DiscussionNote", position=None, resolvable=False)
    root.pop("resolved")
    target.pop("resolved")
    before = discussion([root, target])
    saved = ORIGINAL.replace("Original", "Edited")
    after = copy.deepcopy(before)
    after["notes"][1]["body"] = saved
    if change_kind:
        after["notes"][0 if selected == "root" else 1]["type"] = None
    responses(before, after)
    write = respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act
    result = await edit()

    # Assert
    assert result["status"] == ("unverified" if change_kind else "applied")
    assert result["body_verified"] is True
    assert result["body_hash"] == digest(saved)
    assert write.call_count == 1


@pytest.mark.parametrize("note_type", ["Note", "DiffNote", "DiscussionNote"])
@respx.mock
async def test_read_then_edit_uses_hash_of_raw_body_not_transformed_preview(gitlab_env, note_type):
    """Discovery provides the raw hash, so an exact diagram edit preserves hidden suffix and marker."""
    # Arrange
    expected_note_id = 7
    raw = "Input -> Output\nUnseen suffix stays here\n<!-- review-bot:finding hidden -->"
    saved = raw.replace("Input -> Output", "Input → Output")
    graphql_note = {
        "id": f"gid://gitlab/{note_type}/7",
        "system": False,
        "body": raw,
        "createdAt": "2026-10-20T19:43:02Z",
        "author": {"username": "author"},
        "position": None,
    }
    graphql_thread = {
        "id": "gid://gitlab/Discussion/thread-1",
        "resolvable": True,
        "resolved": False,
        "notes": {"pageInfo": {"hasNextPage": False}, "nodes": [graphql_note]},
    }
    connection = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [graphql_thread]}
    graphql_result = {"data": {"project": {"mergeRequest": {"discussions": connection}}}}
    graphql = respx.post("https://gitlab.example.com/api/graphql").mock(
        return_value=httpx.Response(200, json=graphql_result)
    )
    responses(discussion([note(body=raw)]), discussion([note(body=saved)]))
    write = respx.put(NOTE_URL).mock(return_value=httpx.Response(200, json={"id": 7, "body": saved}))

    # Act
    threads = await call_tool(
        "get_merge_request_threads",
        {
            "project": "group/project",
            "iid": 42,
            "view": "full",
            "include_mr_status": False,
            "max_body_chars": 16,
            "include_hashes": True,
        },
    )
    thread = threads["threads"][0]
    discovered = thread["notes"][0]
    result = await edit(
        thread_id=thread["id"],
        note_id=discovered["note_id"],
        expected_hash=discovered["body_hash"],
        old_text="Input -> Output",
        new_text="Input → Output",
    )

    # Assert
    assert graphql.call_count == 1
    assert discovered["note_id"] == expected_note_id
    assert discovered["body_hash"] == digest(raw)
    assert "Unseen suffix" not in discovered["body"]
    assert "review-bot:finding" not in discovered["body"]
    assert result["status"] == "applied"
    assert result["body_verified"] is True
    assert result["body_hash"] == digest(saved)
    assert write.call_count == 1
    assert json.loads(write.calls.last.request.content) == {"body": saved}
