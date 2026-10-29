"""Guarded editing of existing merge request discussion notes 📝."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError
from gitlab_mcp.common import (
    DISCUSSION_PREFIX,
    HTTP_REQUEST_TIMEOUT,
    HTTP_SERVER_ERROR,
    Iid,
    Project,
    has_quick_action,
    is_int_at_least,
)
from gitlab_mcp.mr_common import mr_rest_path

DISCUSSION_BYTE_LIMIT = 1_048_576
NOTE_BODY_LIMIT = 1_000_000
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


class NoteEditResult(BaseModel):
    """Compact outcome of one guarded note edit."""

    status: Literal["applied", "failed", "unknown", "unverified"]
    note_id: int
    discussion_id: str
    body_verified: bool = False
    body_hash: str | None = Field(default=None, exclude_if=lambda value: value is None)
    warning: str = Field(default="", exclude_if=lambda value: not value)


@dataclass(frozen=True)
class _NoteState:
    """Stable identity, full position, and applicable resolution state for a note."""

    note_id: int
    kind: str | None
    position: str | None
    resolvable: bool
    resolved: bool | None


@dataclass(frozen=True)
class _DiscussionState:
    """The target body and stable root and target discussion invariants."""

    body: str
    individual_note: bool
    root: _NoteState
    target: _NoteState
    resolution: tuple[bool, bool | None] | None
    aggregate_resolution: tuple[bool, bool | None]


class _InvalidInvariantError(ToolError):
    """A known target body was read, but its discussion invariants were invalid."""

    def __init__(self, body: str, cause: ToolError) -> None:
        """Keep the valid target body so a post-write result can still report its hash."""
        super().__init__(str(cause))
        self.body = body


def _discussion_id(thread_id: str) -> str:
    """Accept a bare ID or a single-segment GitLab Discussion GID, never another GID type."""
    if thread_id.startswith(DISCUSSION_PREFIX):
        value = thread_id.removeprefix(DISCUSSION_PREFIX)
        if not value or re.search(r"[/\s?#]", value):
            raise ToolError("Invalid discussion GID. No write was made.")  # noqa: TRY003
    else:
        value = thread_id
        if value.startswith("gid:") or "://" in value:
            raise ToolError("Invalid discussion GID. No write was made.")  # noqa: TRY003
    if not value.strip() or value in {".", ".."}:
        raise ToolError("thread_id must be a valid nonempty discussion ID. No write was made.")  # noqa: TRY003
    return value


def _body_hash(body: str) -> str:
    """Hash the exact API body as UTF-8 without changing whitespace or line endings."""
    try:
        return hashlib.sha256(body.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise ToolError("The note body is not valid UTF-8.") from exc  # noqa: TRY003


def _resolution(node: dict[str, Any], *, required: bool) -> tuple[bool, bool | None] | None:
    """Read resolution state, requiring resolved only when the note is resolvable."""
    if not required and "resolvable" not in node:
        if "resolved" in node:
            raise ToolError("GitLab returned incomplete discussion resolution state.")  # noqa: TRY003
        return None
    resolvable = node.get("resolvable")
    if not isinstance(resolvable, bool):
        raise ToolError("GitLab returned invalid resolvable state.")  # noqa: TRY003
    resolved = node.get("resolved") if resolvable else None
    if resolvable and not isinstance(resolved, bool):
        raise ToolError("GitLab returned invalid resolved state.")  # noqa: TRY003
    return resolvable, resolved


def _note_state(node: dict[str, Any]) -> _NoteState:
    """Keep the entire REST position and resolution state, not a truncated display location."""
    kind = node.get("type")
    if "type" not in node or (
        kind is not None and (not isinstance(kind, str) or kind not in {"Note", "DiscussionNote", "DiffNote"})
    ):
        raise ToolError("GitLab returned an invalid note type.")  # noqa: TRY003
    position = node.get("position")
    if (position is not None and not isinstance(position, dict)) or (
        kind == "DiffNote" and not isinstance(position, dict)
    ):
        raise ToolError("GitLab returned an invalid note position.")  # noqa: TRY003
    try:
        # Canonical JSON preserves the full position and distinguishes booleans from numeric line values.
        position_json = (
            json.dumps(position, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if position is not None
            else None
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ToolError("GitLab returned an invalid note position.") from exc  # noqa: TRY003
    resolution = _resolution(node, required=True)
    if resolution is None:
        raise ToolError("GitLab returned incomplete note resolution state.")  # noqa: TRY003
    return _NoteState(
        note_id=node["id"], kind=kind, position=position_json, resolvable=resolution[0], resolved=resolution[1]
    )


def _aggregate_resolution(notes: list[dict[str, Any]]) -> tuple[bool, bool | None]:
    """Validate every note and derive whether any are resolvable and all such notes are resolved."""
    has_resolvable = False
    all_resolved = True
    for note in notes:
        state = _resolution(note, required=True)
        if state is None:
            raise ToolError("GitLab returned incomplete note resolution state.")  # noqa: TRY003
        if state[0]:
            has_resolvable = True
            all_resolved = all_resolved and state[1] is True
    return has_resolvable, all_resolved if has_resolvable else None


async def _read_discussion(client: GitLabClient, path: str, discussion_id: str, note_id: int) -> _DiscussionState:
    """Fetch one bounded, complete discussion and establish target membership."""
    data, next_page = await client.get_bounded_json(path, max_bytes=DISCUSSION_BYTE_LIMIT)
    if next_page is not None:
        raise ToolError("GitLab unexpectedly paginated the discussion.")  # noqa: TRY003
    if (
        not isinstance(data, dict)
        or data.get("id") != discussion_id
        or not isinstance(data.get("individual_note"), bool)
    ):
        raise ToolError("GitLab returned an invalid or mismatched discussion.")  # noqa: TRY003
    notes = data.get("notes")
    if (
        not isinstance(notes, list)
        or not notes
        or any(not isinstance(item, dict) or not is_int_at_least(item.get("id"), 1) for item in notes)
    ):
        raise ToolError("GitLab returned an incomplete discussion note list.")  # noqa: TRY003
    matches = [item for item in notes if item["id"] == note_id]
    if len(matches) != 1:
        raise ToolError("The discussion does not contain exactly one matching note.")  # noqa: TRY003
    target = matches[0]
    if target.get("system") is not False or not isinstance(target.get("body"), str):
        raise ToolError("The target is a system note or has an invalid body.")  # noqa: TRY003
    body = target["body"]
    _body_hash(body)  # Reject invalid UTF-8 even when the note is only used for verification.
    try:
        return _DiscussionState(
            body=body,
            individual_note=data["individual_note"],
            root=_note_state(notes[0]),
            target=_note_state(target),
            resolution=_resolution(data, required=False),
            aggregate_resolution=_aggregate_resolution(notes),
        )
    except ToolError as exc:
        raise _InvalidInvariantError(body, exc) from exc


def _validate_body(body: str) -> None:
    """Reject an unsafe complete body before it can be sent to GitLab."""
    if not body.strip() or len(body) > NOTE_BODY_LIMIT:
        raise ToolError("The final body must be nonblank and at most 1000000 characters. No write was made.")  # noqa: TRY003
    if has_quick_action(body):
        raise ToolError("The final body may contain a GitLab quick action. No write was made.")  # noqa: TRY003
    try:
        _body_hash(body)
    except ToolError as exc:
        raise ToolError(f"{exc} No write was made.") from exc  # noqa: TRY003


def _final_body(before: str, body: str | None, old_text: str | None, new_text: str | None) -> str:
    """Apply one exact edit, then reject no-ops, blank text, and possible quick actions."""
    if body is None:
        if old_text is None or new_text is None:
            raise ToolError("Both exact texts are required. No write was made.")  # noqa: TRY003
        first = before.find(old_text)
        # Searching from the next character catches overlapping matches, unlike str.count.
        if first < 0 or before.find(old_text, first + 1) >= 0:
            raise ToolError("old_text must occur exactly once, including overlaps. No write was made.")  # noqa: TRY003
        after = before[:first] + new_text + before[first + len(old_text) :]
    else:
        after = body
    if after == before:
        raise ToolError("The edit would not change the body. No write was made.")  # noqa: TRY003
    _validate_body(after)
    return after


def _valid_write_response(data: Any, discussion_id: str, note_id: int, iid: int, body: str) -> bool:
    """Check only identity and body in the write response, never trusting it as final verification."""
    return (
        isinstance(data, dict)
        and is_int_at_least(data.get("id"), 1)
        and data["id"] == note_id
        and data.get("body") == body
        and ("discussion_id" not in data or data["discussion_id"] == discussion_id)
        and ("noteable_iid" not in data or (type(data["noteable_iid"]) is int and data["noteable_iid"] == iid))
        and ("noteable_type" not in data or data["noteable_type"] == "MergeRequest")
    )


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
async def edit_merge_request_note(  # noqa: C901, PLR0912, PLR0913, PLR0917 - inputs and distinct write outcomes.
    ctx: Context,
    project: Project,
    iid: Iid,
    thread_id: Annotated[str, Field(description="Bare discussion ID or GitLab Discussion GID.")],
    note_id: Annotated[
        int, Field(strict=True, gt=0, description="Positive integer ID of the note in this discussion.")
    ],
    expected_hash: Annotated[
        str,
        Field(
            pattern=r"^[0-9a-f]{64}$",
            min_length=64,
            max_length=64,
            description="Required lowercase SHA-256 of the exact UTF-8 note body.",
        ),
    ],
    body: Annotated[
        str | None, Field(description="Intentional complete replacement; never use a transformed preview.")
    ] = None,
    old_text: Annotated[str | None, Field(description="Exact nonempty text occurring once in the raw body.")] = None,
    new_text: Annotated[str | None, Field(description="Replacement for old_text; may be empty.")] = None,
) -> NoteEditResult:
    r"""Edit an existing MR discussion note with `glab api --method PUT` behavior.

    Equivalent CLI endpoint (without this tool's checks):

    ```sh
    glab api --method PUT \
      "projects/$PROJECT_ID/merge_requests/$IID/discussions/$THREAD_ID/notes/$NOTE_ID" \
      -f "body=$BODY"
    ```

    Use a numeric project ID or URL-encoded project path, and a URL-encoded bare discussion ID.

    Show the user the final text and require explicit APPROVE before editing. Obtain the note ID and exact raw body
    hash from thread discovery. Prefer old_text/new_text for formatting edits: they replace exactly one occurrence in
    the full raw body, preserving unseen suffixes and metadata. Use body only for intentional whole replacement;
    never submit a truncated or transformed preview as body. The final body limit is 1000000 characters.

    The hash check is not atomic: a concurrent edit can happen between GET and PUT. This tool never rolls back,
    automatically retries, or creates a replacement note. Each discussion read is bounded to 1 MiB. For safety any
    slash-command-looking line is rejected in the complete final body, including inside code fences; harmless examples
    can be rejected. GitLab line-ending or trailing-whitespace normalization may make verification unverified.
    `applied` confirms the write response, saved body, anchors, and resolution state. `failed` means GitLab rejected
    the PUT; `unknown` means the PUT may have completed; `unverified` means its successful outcome could not be fully
    verified. `body_verified` reports only the saved body match. `body_hash` hashes that observed body when available.
    Check unknown or unverified outcomes before taking further action.
    """
    discussion_id = _discussion_id(thread_id)
    if _HASH_PATTERN.fullmatch(expected_hash) is None:
        raise ToolError("expected_hash must be lowercase SHA-256. No write was made.")  # noqa: TRY003
    if body is not None:
        if old_text is not None or new_text is not None:
            raise ToolError("Provide a nonblank full body or both exact texts, not both modes. No write was made.")  # noqa: TRY003
        _validate_body(body)
    elif old_text is None or not old_text or new_text is None:
        raise ToolError("Provide a full body or nonempty old_text and new_text. No write was made.")  # noqa: TRY003

    client = get_client(ctx)
    path = f"{mr_rest_path(project, iid)}/discussions/{quote(discussion_id, safe='')}"
    try:
        before = await _read_discussion(client, path, discussion_id, note_id)
    except ToolError as exc:
        raise ToolError(f"{exc} No write was made.") from exc  # noqa: TRY003
    if _body_hash(before.body) != expected_hash:
        raise ToolError("The note body hash changed. No write was made.")  # noqa: TRY003
    after = _final_body(before.body, body, old_text, new_text)
    result = NoteEditResult(status="unverified", note_id=note_id, discussion_id=discussion_id)
    response_valid = False
    try:
        response = await client.request("PUT", f"{path}/notes/{note_id}", json={"body": after})
        response_valid = _valid_write_response(response, discussion_id, note_id, iid, after)
    except GitLabConnectionError as exc:
        result.status = "unknown"
        result.warning = f"Write outcome is unknown: {exc} Check the note before taking further action."
        return result
    except GitLabAPIError as exc:
        uncertain = exc.status == HTTP_REQUEST_TIMEOUT or exc.status >= HTTP_SERVER_ERROR
        result.status = "unknown" if uncertain else "failed"
        result.warning = f"Write outcome is {result.status}: {exc} Do not retry automatically."
        return result
    except ValueError, RecursionError:
        # A successful HTTP write with malformed JSON may already have changed the note.
        pass

    try:
        observed = await _read_discussion(client, path, discussion_id, note_id)
        result.body_hash = _body_hash(observed.body)
        result.body_verified = observed.body == after
        same_state = (
            before.individual_note == observed.individual_note
            and before.root == observed.root
            and before.target == observed.target
            and before.resolution == observed.resolution
            and before.aggregate_resolution == observed.aggregate_resolution
        )
        if response_valid and result.body_verified and same_state:
            result.status = "applied"
        else:
            result.warning = (
                "Write response, saved body, or discussion invariants could not be verified. "
                "Do not retry automatically."
            )
    except _InvalidInvariantError as exc:
        result.body_hash = _body_hash(exc.body)
        result.body_verified = exc.body == after
        result.warning = f"Could not verify discussion invariants: {exc} Do not retry automatically."
    except ToolError as exc:
        result.warning = f"Could not verify the saved note: {exc} Do not retry automatically."
    return result
