"""Merge request inline review note tools: validate and post diff comments 📝."""

from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, model_validator
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError
from gitlab_mcp.common import (
    MAX_PAGES,
    REVIEW_NOTE_BODY_CAP,
    Iid,
    Project,
    read_server_local_utf8_file,
)
from gitlab_mcp.errors import raise_review_preflight_error
from gitlab_mcp.mr_common import (
    DIFF_HUNK_PATTERN,
    fetch_diff_page,
    full_sha,
    mr_rest_path,
)


class ReviewNote(BaseModel):
    """One inline note with exactly one positive line side and one nonblank body source.

    Set exactly one of ``line`` or ``old_line``, and exactly one of ``body`` or ``body_path``. The body may contain
    up to 100000 UTF-8 bytes. ``body_path`` must be an absolute server-local path to a regular UTF-8 file.
    """

    file: str = Field(min_length=1, description="Diff path.")
    line: Annotated[int, Field(strict=True, ge=1)] | None = Field(
        default=None,
        description="New-side line.",
    )
    old_line: Annotated[int, Field(strict=True, ge=1)] | None = Field(
        default=None,
        description="Old-side line.",
    )
    body: str | None = Field(
        default=None,
        description="Inline note text.",
    )
    body_path: str | None = Field(
        default=None,
        description="Absolute server-local note file.",
    )

    @model_validator(mode="after")
    def validate_sources_and_location(self) -> ReviewNote:
        """Require exactly one line side and exactly one body source."""
        if not self.file.strip():
            raise_review_preflight_error("file must not be blank")
        if (self.line is None) == (self.old_line is None):
            raise_review_preflight_error("Set exactly one of line or old_line")
        if (self.body is None) == (self.body_path is None):
            raise_review_preflight_error("Set exactly one of body or body_path")
        return self


ReviewNoteStatus = Literal["validated", "posted", "failed", "unknown", "unverified", "unattempted"]


class ReviewNotePostResult(BaseModel):
    """The outcome for one note in a sequential review-note batch."""

    index: int
    status: ReviewNoteStatus
    note_id: int | None = Field(default=None, exclude_if=lambda value: value is None)
    discussion_id: str = Field(default="", exclude_if=lambda value: not value)
    position: dict[str, str | int] = Field(default_factory=dict, exclude_if=lambda value: not value)


class PostReviewNotesResult(BaseModel):
    """One bounded result per requested note and a reason when the batch stops."""

    results: list[ReviewNotePostResult]
    warning: str = Field(default="", exclude_if=lambda value: not value)
    current_head_sha: str | None = Field(default=None, exclude_if=lambda value: value is None)


def _unattempted_review_notes(
    count: int,
    warning: str,
    *,
    current_head_sha: str | None = None,
) -> PostReviewNotesResult:
    """Build a result row for every note when preflight stops before posting."""
    return PostReviewNotesResult(
        results=[ReviewNotePostResult(index=index, status="unattempted") for index in range(count)],
        warning=warning[:500],
        current_head_sha=current_head_sha,
    )


def _read_review_note_bodies(notes: list[ReviewNote]) -> list[str]:
    """Read and validate every note body before the tool sends any GitLab request."""
    bodies: list[str] = []
    for index, note in enumerate(notes):
        if note.body is not None:
            body = note.body
            size = len(body.encode("utf-8"))
        else:
            body = read_server_local_utf8_file(
                note.body_path or "",
                path_field=f"note {index} body_path",
                size_field=f"note {index} body",
                byte_limit=REVIEW_NOTE_BODY_CAP,
            )
            size = len(body.encode("utf-8"))
        if not body.strip():
            raise_review_preflight_error(f"note {index} body must not be blank")
        if size > REVIEW_NOTE_BODY_CAP:
            raise_review_preflight_error(f"note {index} body exceeds {REVIEW_NOTE_BODY_CAP} UTF-8 bytes")
        bodies.append(body)
    return bodies


async def _get_review_diff_refs(client: GitLabClient, project: str, iid: int) -> dict[str, str]:
    """Read the REST merge request SHA and the three diff reference SHAs."""
    merge_request = await client.get(mr_rest_path(project, iid))
    if not isinstance(merge_request, dict):
        raise_review_preflight_error("GitLab returned an invalid merge request response")
    refs = merge_request.get("diff_refs")
    if not isinstance(refs, dict):
        raise_review_preflight_error("GitLab did not return merge request diff_refs")
    state = {
        "sha": merge_request.get("sha"),
        "base_sha": refs.get("base_sha"),
        "start_sha": refs.get("start_sha"),
        "head_sha": refs.get("head_sha"),
    }
    if not all(full_sha(value) for value in state.values()):
        raise_review_preflight_error("GitLab did not return full SHA values for the merge request and diff_refs")
    return {key: str(value).lower() for key, value in state.items()}


def _diff_line_positions(diff: str) -> list[tuple[str, int | None, int | None]]:  # noqa: C901, PLR0912 - unified diff parsing has distinct line cases.
    """Parse unified diff hunks into line kinds and their old/new coordinates."""
    positions: list[tuple[str, int | None, int | None]] = []
    old_line = new_line = old_remaining = new_remaining = 0
    in_hunk = False
    saw_hunk = False

    for content in diff.removesuffix("\n").split("\n"):
        if content.startswith("@@"):
            if in_hunk and (old_remaining or new_remaining):
                raise_review_preflight_error("diff has an incomplete unified hunk")
            match = DIFF_HUNK_PATTERN.match(content)
            if match is None:
                raise_review_preflight_error("diff contains an invalid unified hunk header")
            old_line = int(match.group(1))
            old_remaining = int(match.group(2) or 1)
            new_line = int(match.group(3))
            new_remaining = int(match.group(4) or 1)
            in_hunk = saw_hunk = True
            continue
        if not in_hunk or content.startswith("\\"):
            continue
        if content.startswith(" "):
            if old_remaining < 1 or new_remaining < 1:
                raise_review_preflight_error("diff contains more context lines than its hunk header")
            positions.append(("context", old_line, new_line))
            old_line += 1
            new_line += 1
            old_remaining -= 1
            new_remaining -= 1
        elif content.startswith("+"):
            if new_remaining < 1:
                raise_review_preflight_error("diff contains more added lines than its hunk header")
            positions.append(("added", None, new_line))
            new_line += 1
            new_remaining -= 1
        elif content.startswith("-"):
            if old_remaining < 1:
                raise_review_preflight_error("diff contains more removed lines than its hunk header")
            positions.append(("removed", old_line, None))
            old_line += 1
            old_remaining -= 1
        else:
            raise_review_preflight_error("diff contains an invalid line inside a unified hunk")

    if in_hunk and (old_remaining or new_remaining):
        raise_review_preflight_error("diff has an incomplete unified hunk")
    if not saw_hunk or not positions:
        raise_review_preflight_error("diff has no usable unified patch")
    return positions


def _review_position(raw: dict[str, Any], note: ReviewNote) -> dict[str, str | int | None]:
    """Find the requested old- or new-side line in a file's unified patch."""
    if raw.get("collapsed") or raw.get("too_large"):
        raise_review_preflight_error(f"diff for {note.file!r} is collapsed or too large to position safely")
    diff = raw.get("diff")
    if not isinstance(diff, str) or not diff:
        raise_review_preflight_error(f"diff for {note.file!r} has no usable patch")
    locations = _diff_line_positions(diff)
    matches = []
    for kind, old_line, new_line in locations:
        if note.line is not None and new_line == note.line and kind in ("added", "context"):
            matches.append((old_line, new_line))
        if note.old_line is not None and old_line == note.old_line and kind in ("removed", "context"):
            matches.append((old_line, new_line))
    if len(matches) != 1:
        side = "new" if note.line is not None else "old"
        line = note.line if note.line is not None else note.old_line
        if matches:
            raise_review_preflight_error(f"line {line} on the {side} side is ambiguous in {note.file!r}")
        raise_review_preflight_error(f"line {line} on the {side} side is not a usable diff line in {note.file!r}")
    old_path, new_path = raw.get("old_path"), raw.get("new_path")
    if not isinstance(old_path, str) or not isinstance(new_path, str):
        raise_review_preflight_error(f"diff paths for {note.file!r} are invalid")
    old_line, new_line = matches[0]
    return {
        "position_type": "text",
        "old_path": old_path,
        "new_path": new_path,
        "old_line": old_line,
        "new_line": new_line,
    }


async def _scan_review_paths(
    client: GitLabClient,
    project: str,
    iid: int,
    requested_paths: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Collect requested old/new paths from at most MAX_PAGES REST diff pages."""
    path_matches: dict[str, list[dict[str, Any]]] = {path: [] for path in requested_paths}
    page = 1
    for _ in range(MAX_PAGES):
        files, next_page = await fetch_diff_page(client, project, iid, page)
        for raw in files:
            if not isinstance(raw, dict):
                continue
            for path in requested_paths:
                if path in (raw.get("old_path"), raw.get("new_path")):
                    path_matches[path].append(raw)
        if next_page is None:
            break
        page = next_page

    if next_page is not None:
        raise_review_preflight_error(f"Diff path scan is incomplete after {MAX_PAGES} pages. No notes were posted.")

    missing = sorted(path for path, matches in path_matches.items() if not matches)
    if missing:
        raise_review_preflight_error(
            f"Diff preflight could not find requested path(s) within {MAX_PAGES} pages: {', '.join(missing)}"
        )
    ambiguous = sorted(path for path, matches in path_matches.items() if len(matches) != 1)
    if ambiguous:
        raise_review_preflight_error(f"Diff preflight found ambiguous requested path(s): {', '.join(ambiguous)}")
    return path_matches


async def _prepare_review_notes(  # noqa: PLR0913, PLR0917 - each argument is part of the preflight contract.
    client: GitLabClient,
    project: str,
    iid: int,
    head_sha: str,
    notes: list[ReviewNote],
    bodies: list[str],
) -> tuple[list[tuple[str, dict[str, str | int | None]]], dict[str, str], str]:
    """Validate every requested path and coordinate against a stable preflight snapshot."""
    try:
        refs = await _get_review_diff_refs(client, project, iid)
        if refs["sha"] != head_sha.lower() or refs["head_sha"] != head_sha.lower():
            return [], refs, "The supplied head_sha is stale. No notes were posted."
        paths = await _scan_review_paths(client, project, iid, {note.file for note in notes})
        prepared = [
            (body, _review_position(paths[note.file][0], note)) for note, body in zip(notes, bodies, strict=True)
        ]
        current_refs = await _get_review_diff_refs(client, project, iid)
        if current_refs != refs:
            return [], {}, "The merge request or its diff refs changed during preflight. No notes were posted."
    except (ToolError, OSError, ValueError, KeyError, TypeError) as exc:
        return [], {}, f"Review-note preflight failed: {exc}. No notes were posted."
    return prepared, refs, ""


def _review_response_ids(response: Any) -> tuple[str, int | None, dict[str, Any] | None]:
    """Extract IDs and the created root note position when the response has them."""
    if not isinstance(response, dict):
        return "", None, None
    raw_discussion_id = response.get("id")
    discussion_id = (
        str(raw_discussion_id) if type(raw_discussion_id) is int or isinstance(raw_discussion_id, str) else ""
    )
    notes = response.get("notes")
    if not isinstance(notes, list) or not notes or not isinstance(notes[0], dict):
        return discussion_id, None, None
    root = notes[0]
    raw_note_id = root.get("id")
    note_id = raw_note_id if type(raw_note_id) is int else None
    position = root.get("position")
    return discussion_id, note_id, position if isinstance(position, dict) else None


def _review_response_position(
    actual: dict[str, Any] | None,
) -> dict[str, str | int]:
    """Return one path and line coordinate, plus the old path for a rename."""
    if actual is None:
        return {}
    old_path = actual.get("old_path")
    path = actual.get("path") or actual.get("new_path") or old_path
    compact: dict[str, str | int] = {}
    if isinstance(path, str):
        compact["path"] = path
    if isinstance(old_path, str) and old_path != path:
        compact["old_path"] = old_path
    new_line = actual.get("line", actual.get("new_line"))
    old_line = actual.get("old_line")
    if type(new_line) is int:
        compact["line"] = new_line
    elif type(old_line) is int:
        compact["old_line"] = old_line
    return compact


def _review_response_matches(
    actual: dict[str, Any] | None,
    expected: dict[str, str | int | None],
) -> bool:
    """Check that GitLab placed the note at the requested refs, paths, and coordinates."""
    if actual is None:
        return False
    keys = ("position_type", "base_sha", "start_sha", "head_sha", "old_path", "new_path", "old_line", "new_line")
    return all(
        actual.get(key) is None
        if expected.get(key) is None
        else type(actual.get(key)) is type(expected[key]) and actual.get(key) == expected[key]
        for key in keys
    )


async def _post_one_review_note(  # noqa: PLR0913, PLR0917 - each argument identifies one requested write.
    client: GitLabClient,
    project: str,
    iid: int,
    index: int,
    body: str,
    position: dict[str, str | int | None],
) -> tuple[ReviewNotePostResult, str]:
    """Post one note and verify the position returned for its created discussion."""
    try:
        response = await client.request(
            "POST",
            f"{mr_rest_path(project, iid)}/discussions",
            json={"body": body, "position": position},
        )
    except GitLabAPIError as exc:
        if exc.status >= 500 or exc.status == 408:  # noqa: PLR2004 - these statuses can hide a committed write.
            return (
                ReviewNotePostResult(index=index, status="unknown"),
                f"The post outcome is unknown: {exc} Check GitLab before retrying.",
            )
        return (
            ReviewNotePostResult(index=index, status="failed"),
            f"GitLab rejected the post: {exc} Check the item before retrying.",
        )
    except GitLabConnectionError as exc:
        return (
            ReviewNotePostResult(index=index, status="unknown"),
            f"The post outcome is unknown: {exc} Check GitLab before retrying.",
        )
    except (TypeError, ValueError) as exc:
        return (
            ReviewNotePostResult(index=index, status="unverified"),
            (
                f"GitLab may have created the note, but its response could not be verified: {exc} Check GitLab "
                "before retrying."
            ),
        )

    discussion_id, note_id, actual_position = _review_response_ids(response)
    compact_position = _review_response_position(actual_position)
    if not discussion_id or note_id is None:
        reason = "GitLab's successful response was missing the discussion or note ID."
    elif not _review_response_matches(actual_position, position):
        reason = "GitLab's returned position did not match the request."
    else:
        reason = ""
    return (
        ReviewNotePostResult(
            index=index,
            status="unverified" if reason else "posted",
            note_id=note_id,
            discussion_id=discussion_id,
            position=compact_position,
        ),
        f"The post may have succeeded. {reason} Check GitLab before retrying." if reason else "",
    )


async def _post_review_note_batch(
    client: GitLabClient,
    project: str,
    iid: int,
    prepared: list[tuple[str, dict[str, str | int | None]]],
    refs: dict[str, str],
) -> PostReviewNotesResult:
    """Post prepared notes one at a time and stop at the first unverified outcome."""
    results: list[ReviewNotePostResult] = []
    warning = ""
    for index, (body, location) in enumerate(prepared):
        position: dict[str, str | int | None] = {
            **location,
            "base_sha": refs["base_sha"],
            "start_sha": refs["start_sha"],
            "head_sha": refs["head_sha"],
        }
        result, reason = await _post_one_review_note(client, project, iid, index, body, position)
        results.append(result)
        if reason:
            warning = f"Stopped at note index {index} ({result.status}): {reason} Later notes were not attempted."
            if result.status in ("unknown", "unverified"):
                warning += " Do not retry this note until its outcome is checked."
            results.extend(
                ReviewNotePostResult(index=later_index, status="unattempted")
                for later_index in range(index + 1, len(prepared))
            )
            break
    return PostReviewNotesResult(results=results, warning=warning[:500])


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def post_review_notes(  # noqa: PLR0913, PLR0917 - the user-facing arguments describe the requested batch.
    ctx: Context,
    project: Project,
    iid: Iid,
    head_sha: Annotated[
        str,
        Field(description="Full 40-character head SHA from get_merge_request or get_merge_request_diff."),
    ],
    notes: Annotated[list[ReviewNote], Field(min_length=1, max_length=20)],
    validate_only: Annotated[
        bool,
        Field(description="Run all preflight checks and return positions without posting any notes."),
    ] = False,
) -> PostReviewNotesResult:
    """Review inline comments: validate or post a sequential, bounded batch.

    Like `glab mr note create`, but creates diff discussions. Set `validate_only` to true to run all body, path, line,
    and SHA checks without writing; validation does not need the user's `APPROVE`. Actual posting still requires the
    user's explicit `APPROVE`. After APPROVE, call this tool directly; it reruns the same checks before the first
    write. Do not call `validate_only` again. `head_sha` must be the full SHA from `get_merge_request` or
    `get_merge_request_diff`.
    A `body_path` is an absolute path on this server, which is useful with stdio but is not a path on the agent's
    computer. All bodies, diff lines, and paths are checked before the first post. A second head check is preflight
    only, not an atomic lock, because GitLab has no expected-head guard for discussion creation. Writes are sequential
    and never retried. Check every result before deciding what to do next.
    """
    if not full_sha(head_sha):
        return _unattempted_review_notes(
            len(notes), "head_sha must be a full 40-character hexadecimal SHA. No notes were posted."
        )
    try:
        bodies = _read_review_note_bodies(notes)
    except (OSError, ValueError) as exc:
        return _unattempted_review_notes(len(notes), f"Body preflight failed: {exc}. No notes were posted.")

    try:
        client = get_client(ctx)
    except ToolError as exc:
        return _unattempted_review_notes(len(notes), f"Review-note setup failed: {exc}. No notes were posted.")
    prepared, refs, warning = await _prepare_review_notes(client, project, iid, head_sha, notes, bodies)
    if warning:
        current_head_sha = refs.get("sha") if warning.startswith("The supplied head_sha is stale.") else None
        return _unattempted_review_notes(len(notes), warning, current_head_sha=current_head_sha)
    if validate_only:
        return PostReviewNotesResult(
            results=[
                ReviewNotePostResult(
                    index=index,
                    status="validated",
                    position=_review_response_position(location),
                )
                for index, (_, location) in enumerate(prepared)
            ],
        )
    return await _post_review_note_batch(client, project, iid, prepared, refs)
