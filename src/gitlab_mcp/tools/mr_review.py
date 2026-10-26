"""Merge request inline review note tools: validate and post diff comments 📝."""

from dataclasses import dataclass, field
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
    set_people,
)

REVIEW_NOTE_REASON_CAP = 200
PEOPLE_SKIPPED_WARNING = "Reviewers and assignees were not changed because not every note posted."


class ReviewNote(BaseModel):
    """One general note, file-level note, or inline note on a merge request.

    Omit ``file``, ``line``, and ``old_line`` for a general merge-request discussion. Set ``file`` without either
    line coordinate for a file-level note, or set a file and at most one line coordinate for an inline note. Set
    exactly one of ``body`` or ``body_path``. The body may contain up to 100000 UTF-8 bytes. ``body_path`` must be an
    absolute server-local path to a regular UTF-8 file.
    """

    file: str | None = Field(default=None, min_length=1, description="Diff path. Omit for a general MR note.")
    line: Annotated[int, Field(strict=True, ge=1)] | None = Field(
        default=None,
        description="New-side line. Requires file. Omit both coordinates for a file-level or general note.",
    )
    old_line: Annotated[int, Field(strict=True, ge=1)] | None = Field(
        default=None,
        description="Old-side line. Requires file. Omit both coordinates for a file-level or general note.",
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
        if self.file is not None and not self.file.strip():
            raise_review_preflight_error("file must not be blank")
        if self.line is not None and self.old_line is not None:
            raise_review_preflight_error("Set at most one of line or old_line")
        if self.file is None and (self.line is not None or self.old_line is not None):
            raise_review_preflight_error("file is required when line or old_line is set")
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
    reason: str = Field(default="", exclude_if=lambda value: not value)


@dataclass(frozen=True)
class _CheckedNote:
    """One requested note after preflight: its body and GitLab position, or the reason it cannot be posted."""

    body: str = ""
    position: dict[str, str | int | None] = field(default_factory=dict)
    reason: str = ""


class PeopleResult(BaseModel):
    """Reviewer and assignee changes made after every note posted. Names match update_merge_request."""

    applied: list[str]  # Steps GitLab confirmed, such as add_reviewers.
    unknown: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    failed: dict[str, str] = Field(default_factory=dict, exclude_if=lambda value: not value)  # Step or item -> reason.


class PostReviewNotesResult(BaseModel):
    """One bounded result per requested note and a reason when the batch stops."""

    results: list[ReviewNotePostResult]
    warning: str = Field(default="", exclude_if=lambda value: not value)
    current_head_sha: str | None = Field(default=None, exclude_if=lambda value: value is None)
    people: PeopleResult | None = Field(default=None, exclude_if=lambda value: value is None)


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


def _note_reason(message: str) -> str:
    """Cap one note's reason so a long OS error message cannot flood the result."""
    return message[:REVIEW_NOTE_REASON_CAP]


def _read_review_note_body(note: ReviewNote) -> str:
    """Return one usable note body, or raise a ValueError or OSError that says why it is not usable."""
    if note.body is not None:
        body = note.body
    else:
        body = read_server_local_utf8_file(
            note.body_path or "",
            path_field="body_path",
            size_field="body",
            byte_limit=REVIEW_NOTE_BODY_CAP,
        )
    if not body.strip():
        raise_review_preflight_error("body must not be blank")
    if len(body.encode("utf-8")) > REVIEW_NOTE_BODY_CAP:
        raise_review_preflight_error(f"body exceeds {REVIEW_NOTE_BODY_CAP} UTF-8 bytes")
    return body


def _read_review_note_bodies(notes: list[ReviewNote]) -> list[_CheckedNote]:
    """Read every note body before any GitLab request, recording a reason for each unusable one."""
    checks: list[_CheckedNote] = []
    for note in notes:
        try:
            checks.append(_CheckedNote(body=_read_review_note_body(note)))
        except OSError as exc:
            checks.append(_CheckedNote(reason=_note_reason(f"body_path could not be read: {exc}")))
        except ValueError as exc:
            checks.append(_CheckedNote(reason=_note_reason(str(exc))))
    return checks


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


def _diff_paths(raw: dict[str, Any], file: str) -> tuple[str, str]:
    """Return the old and new paths of one REST diff entry, or reject malformed path data."""
    old_path, new_path = raw.get("old_path"), raw.get("new_path")
    if not isinstance(old_path, str) or not isinstance(new_path, str):
        raise_review_preflight_error(f"diff paths for {file!r} are invalid")
    return old_path, new_path


def _matching_line(raw: dict[str, Any], note: ReviewNote) -> tuple[int | None, int | None]:
    """Find the one diff line that matches a line note's side and number, or say why it cannot be used."""
    if raw.get("collapsed") or raw.get("too_large"):
        raise_review_preflight_error(f"diff for {note.file!r} is collapsed or too large to position safely")
    diff = raw.get("diff")
    if not isinstance(diff, str) or not diff:
        raise_review_preflight_error(f"diff for {note.file!r} has no usable patch")
    matches: list[tuple[int | None, int | None]] = []
    for kind, old_line, new_line in _diff_line_positions(diff):
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
    return matches[0]


def _note_position(matches: list[dict[str, Any]], note: ReviewNote) -> dict[str, str | int | None]:
    """Return the GitLab position for one note, or raise why GitLab would not accept it.

    A file-level note (no line) needs only its path to appear once in the diff. A line note also needs a usable
    hunk, so collapsed or too-large patches fail only that note.
    """
    if note.file is None:
        raise_review_preflight_error("file is required for a diff position")
    file = note.file
    if not matches:
        raise_review_preflight_error(f"Diff preflight could not find {file!r} within {MAX_PAGES} pages")
    if len(matches) != 1:
        raise_review_preflight_error(f"Diff preflight found ambiguous path {file!r}")
    raw = matches[0]
    old_path, new_path = _diff_paths(raw, file)
    if note.line is None and note.old_line is None:
        # Omit the line keys entirely: _review_response_matches treats a missing key and a null key the same way.
        return {"position_type": "file", "old_path": old_path, "new_path": new_path}
    old_line, new_line = _matching_line(raw, note)
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
    """Collect every diff entry that matches each requested path across at most MAX_PAGES REST pages.

    Missing and ambiguous paths are returned as they are, so each note can report its own reason. An incomplete
    scan still fails the whole batch, because a later page could hold a second match.
    """
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
    return path_matches


def _check_note_position(
    matches: list[dict[str, Any]],
    note: ReviewNote,
    check: _CheckedNote,
) -> _CheckedNote:
    """Add a GitLab position to a note whose body is usable, or record why its diff position fails."""
    if check.reason:
        return check
    try:
        position = _note_position(matches, note)
    except ValueError as exc:
        return _CheckedNote(reason=_note_reason(str(exc)))
    return _CheckedNote(body=check.body, position=position)


async def _prepare_review_notes(  # noqa: PLR0913, PLR0917 - each argument is part of the preflight contract.
    client: GitLabClient,
    project: str,
    iid: int,
    head_sha: str,
    notes: list[ReviewNote],
    checks: list[_CheckedNote],
) -> tuple[list[_CheckedNote], dict[str, str], str]:
    """Check every note's path and coordinate against a stable preflight snapshot.

    Batch-wide problems return a warning and no per-note results. Per-note problems stay on each returned check.
    """
    try:
        refs = await _get_review_diff_refs(client, project, iid)
        if refs["sha"] != head_sha.lower() or refs["head_sha"] != head_sha.lower():
            return [], refs, "The supplied head_sha is stale. No notes were posted."
        requested_paths = {note.file for note in notes if note.file is not None}
        paths = await _scan_review_paths(client, project, iid, requested_paths) if requested_paths else {}
        checked = []
        for note, check in zip(notes, checks, strict=True):
            if note.file is None:
                checked.append(check)
            else:
                checked.append(_check_note_position(paths[note.file], note, check))
        current_refs = await _get_review_diff_refs(client, project, iid)
        if current_refs != refs:
            return [], {}, "The merge request or its diff refs changed during preflight. No notes were posted."
    except (ToolError, OSError, ValueError, KeyError, TypeError) as exc:
        return [], {}, f"Review-note preflight failed: {exc}. No notes were posted."
    return checked, refs, ""


def _review_response_ids(response: Any) -> tuple[str, int | None, Any]:
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
    return discussion_id, note_id, position


def _review_response_position(
    actual: Any,
) -> dict[str, str | int]:
    """Return one path and line coordinate, plus the old path for a rename."""
    if not isinstance(actual, dict):
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
    actual: Any,
    expected: dict[str, str | int | None],
) -> bool:
    """Check that GitLab placed the note at the requested refs, paths, and coordinates."""
    if not expected:
        return actual is None or actual == {}
    if not isinstance(actual, dict):
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
    """Post one note and verify its position when it is a diff discussion."""
    try:
        payload: dict[str, Any] = {"body": body}
        if position:
            payload["position"] = position
        response = await client.request(
            "POST",
            f"{mr_rest_path(project, iid)}/discussions",
            json=payload,
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


def _review_preflight_result(checks: list[_CheckedNote]) -> PostReviewNotesResult:
    """Return one row per note: validated when it can be posted, failed with a reason when it cannot.

    The warning appears only when at least one note failed, and it says that nothing was posted.
    """
    results = [
        ReviewNotePostResult(index=index, status="failed", reason=check.reason)
        if check.reason
        else ReviewNotePostResult(index=index, status="validated", position=_review_response_position(check.position))
        for index, check in enumerate(checks)
    ]
    failed = sum(1 for check in checks if check.reason)
    warning = f"{failed} of {len(checks)} notes failed preflight. No notes were posted." if failed else ""
    return PostReviewNotesResult(results=results, warning=warning[:500])


async def _post_review_note_batch(
    client: GitLabClient,
    project: str,
    iid: int,
    checks: list[_CheckedNote],
    refs: dict[str, str],
) -> PostReviewNotesResult:
    """Post checked notes one at a time and stop at the first unverified outcome."""
    results: list[ReviewNotePostResult] = []
    warning = ""
    for index, check in enumerate(checks):
        position: dict[str, str | int | None] = {}
        if check.position:
            position = {
                **check.position,
                "base_sha": refs["base_sha"],
                "start_sha": refs["start_sha"],
                "head_sha": refs["head_sha"],
            }
        result, reason = await _post_one_review_note(client, project, iid, index, check.body, position)
        results.append(result)
        if reason:
            warning = f"Stopped at note index {index} ({result.status}): {reason} Later notes were not attempted."
            if result.status in ("unknown", "unverified"):
                warning += " Do not retry this note until its outcome is checked."
            results.extend(
                ReviewNotePostResult(index=later_index, status="unattempted")
                for later_index in range(index + 1, len(checks))
            )
            break
    return PostReviewNotesResult(results=results, warning=warning[:500])


async def _post_review_notes(  # noqa: PLR0913, PLR0917 - each argument is part of the batch request.
    ctx: Context,
    project: str,
    iid: int,
    head_sha: str,
    notes: list[ReviewNote],
    validate_only: bool,
) -> PostReviewNotesResult:
    """Run preflight, then validate or post the notes. The public tool adds people changes after this returns."""
    if not full_sha(head_sha):
        return _unattempted_review_notes(
            len(notes), "head_sha must be a full 40-character hexadecimal SHA. No notes were posted."
        )
    checks = _read_review_note_bodies(notes)
    if all(check.reason for check in checks):
        return _review_preflight_result(checks)

    try:
        client = get_client(ctx)
    except ToolError as exc:
        return _unattempted_review_notes(len(notes), f"Review-note setup failed: {exc}. No notes were posted.")
    checks, refs, warning = await _prepare_review_notes(client, project, iid, head_sha, notes, checks)
    if warning:
        current_head_sha = refs.get("sha") if warning.startswith("The supplied head_sha is stale.") else None
        return _unattempted_review_notes(len(notes), warning, current_head_sha=current_head_sha)
    if validate_only or any(check.reason for check in checks):
        return _review_preflight_result(checks)
    return await _post_review_note_batch(client, project, iid, checks, refs)


async def _add_people_after_posts(  # noqa: PLR0913, PLR0917 - each argument is one part of the people request.
    ctx: Context,
    project: str,
    iid: int,
    result: PostReviewNotesResult,
    add_reviewers: list[str] | None,
    add_assignees: list[str] | None,
) -> PostReviewNotesResult:
    """Add the requested reviewers and assignees, but only when every note posted.

    When a note did not post, people stay as they are and the warning says so. The rule keeps a partly
    posted review from also changing who is assigned to it.
    """
    if not (add_reviewers or add_assignees):
        return result
    if not all(row.status == "posted" for row in result.results):
        warning = " ".join(filter(None, (result.warning, PEOPLE_SKIPPED_WARNING)))
        return result.model_copy(update={"warning": warning[:500]})
    jobs = [
        ("reviewers", "add_reviewers", "APPEND", add_reviewers or []),
        ("assignees", "add_assignees", "APPEND", add_assignees or []),
    ]
    outcome = await set_people(get_client(ctx), project, iid, jobs)
    people = PeopleResult(applied=outcome.applied, unknown=outcome.unknown, failed=outcome.failed)
    return result.model_copy(update={"people": people})


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
    add_reviewers: Annotated[
        list[str] | None,
        Field(description="Reviewer usernames to add after every note posts."),
    ] = None,
    add_assignees: Annotated[
        list[str] | None,
        Field(description="Assignee usernames to add after every note posts."),
    ] = None,
    validate_only: Annotated[
        bool,
        Field(description="Run all preflight checks and return positions without posting any notes."),
    ] = False,
) -> PostReviewNotesResult:
    """Validate or post inline comments and general review notes in a sequential, bounded batch.

    Like `glab mr note create`. Omit `file`, `line`, and `old_line` for a general merge-request discussion. Set only
    `file` for a file-level diff discussion, or set `file` and one line coordinate for an inline diff discussion. Set
    `validate_only` to true to run all body, path, line, and SHA checks without writing; validation does not need the
    user's `APPROVE`. Actual posting still requires the user's explicit `APPROVE`. After APPROVE, call this tool
    directly; it reruns the same checks before the first write. Do not call `validate_only` again. `head_sha` must be
    the full SHA from `get_merge_request` or `get_merge_request_diff`.
    Each note is checked on its own. If any note fails, nothing is posted: every note returns a row, with `validated`
    for notes that passed and `failed` plus a `reason` for notes that did not. A batch-wide problem, such as a stale
    `head_sha`, returns `unattempted` for every note and a `warning`.
    A file-level note needs only the file's path in the diff, so it works for findings outside diff hunks and for
    collapsed or too-large diffs. General notes do not read diff pages, but still check the merge request head twice.
    A `body_path` is an absolute path on this server, which is useful with stdio but is not a path on the agent's
    computer. All bodies, diff lines, and paths are checked before the first post. A second head check is preflight
    only, not an atomic lock, because GitLab has no expected-head guard for discussion creation. Writes are sequential
    and never retried. Check every result before deciding what to do next.
    `add_reviewers` and `add_assignees` add people only after every note posts, in the same call. They are ignored
    when `validate_only` is true.
    """
    result = await _post_review_notes(ctx, project, iid, head_sha, notes, validate_only)
    if validate_only:
        return result
    return await _add_people_after_posts(ctx, project, iid, result, add_reviewers, add_assignees)
