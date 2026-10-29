"""Merge request inline review note tools: validate and post diff comments 📝."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, model_validator
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, get_review_batch_cache, mcp
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
from gitlab_mcp.review_batches import CachedReviewBatch, CachedReviewNote, ReviewBatchUnavailableError

REVIEW_NOTE_REASON_CAP = 200
REVIEW_NOTE_HINT_CAP = 120
REVIEW_NOTE_BLOB_CAP = 131_072
PEOPLE_SKIPPED_WARNING = "Reviewers and assignees were not changed because not every note posted."


class ReviewNote(BaseModel):
    """One general note, file-level note, or inline note on a merge request.

    Omit ``file``, ``line``, and ``old_line`` for a general merge-request discussion. Set ``file`` without either
    line coordinate for a file-level note, or set a file and at most one line coordinate for an inline note. Set
    exactly one of ``body`` or ``body_path``. The body may contain up to 100000 UTF-8 bytes. ``body_path`` must be an
    absolute server-local path to a regular UTF-8 file, or a path relative to the tool's ``body_dir``.
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
        description="Absolute server-local note file, or a path relative to body_dir.",
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


class ReviewNoteBodyOverride(BaseModel):
    """Replace the body source for one cached note index."""

    index: Annotated[int, Field(strict=True, ge=0)] = Field(description="Zero-based index in the cached review batch.")
    body: str | None = Field(default=None, description="Replacement inline note text.")
    body_path: str | None = Field(
        default=None, description="Absolute server-local replacement file, or relative to body_dir."
    )

    @model_validator(mode="after")
    def validate_body_source(self) -> ReviewNoteBodyOverride:
        """Require exactly one replacement body source."""
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


@dataclass(frozen=True)
class _DiffHunk:
    """One validated unified hunk with zero-based, half-open old and new intervals."""

    old_begin: int
    old_end: int
    new_begin: int
    new_end: int


@dataclass(frozen=True)
class _ParsedDiff:
    """Parsed line positions and hunk bounds used to map notes safely."""

    positions: tuple[tuple[str, int | None, int | None], ...]
    hunks: tuple[_DiffHunk, ...]


class PeopleResult(BaseModel):
    """Reviewer and assignee changes made after every note posted. Names match update_merge_request."""

    applied: list[str]  # Steps GitLab confirmed, such as add_reviewers.
    unknown: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    failed: dict[str, str] = Field(default_factory=dict, exclude_if=lambda value: not value)  # Step or item -> reason.


class PostReviewNotesResult(BaseModel):
    """One bounded result per requested note and a reason when the batch stops."""

    results: list[ReviewNotePostResult]
    posted_count: int | None = Field(default=None, exclude_if=lambda value: value is None)
    warning: str = Field(default="", exclude_if=lambda value: not value)
    current_head_sha: str | None = Field(default=None, exclude_if=lambda value: value is None)
    people: PeopleResult | None = Field(default=None, exclude_if=lambda value: value is None)
    batch_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    ttl_seconds: int | None = Field(
        default=None,
        description="Upper bound in seconds until a validated batch expires; use, eviction, or restart may shorten it.",
        exclude_if=lambda value: value is None,
    )
    hint: str = Field(default="", exclude_if=lambda value: not value)  # Next-step guidance after a validated batch.
    batch_error: Literal["unavailable"] | None = Field(default=None, exclude_if=lambda value: value is None)


def _compact_review_result(result: PostReviewNotesResult) -> PostReviewNotesResult:
    """Count verified posts and omit only their rows from the returned batch."""
    posted_count = sum(row.status == "posted" for row in result.results)
    remaining_results = [row for row in result.results if row.status != "posted"]
    return result.model_copy(update={"results": remaining_results, "posted_count": posted_count})


def _claim_review_batch(ctx: Context, batch_id: str, project: str, iid: int, head_sha: str) -> CachedReviewBatch:
    """Claim synchronously, treating an unconfigured cache as unavailable."""
    try:
        cache = get_review_batch_cache(ctx)
    except ToolError as exc:
        raise ReviewBatchUnavailableError from exc
    return cache.claim(batch_id, project, iid, head_sha)


async def _unavailable_review_batch(ctx: Context, project: str, iid: int, head_sha: str) -> PostReviewNotesResult:
    """Diagnose the live head without exposing read errors or assuming earlier writes failed."""
    current_head_sha = None
    if not full_sha(head_sha):
        head_warning = "Head status is unknown: head_sha must be a full 40-character hexadecimal SHA."
    else:
        try:
            # Only the MR source SHA is needed; diff refs can be pending after a push.
            merge_request = await get_client(ctx).get(mr_rest_path(project, iid))
            raw_sha = merge_request.get("sha") if isinstance(merge_request, dict) else None
            if full_sha(raw_sha):
                current_head_sha = str(raw_sha).lower()
        except ToolError, OSError, ValueError, KeyError, TypeError, RecursionError:
            pass  # Diagnostic failures must not leak API errors or prevent recovery guidance.
        if current_head_sha is None:
            head_warning = "Head status is unknown."
        elif current_head_sha == head_sha.lower():
            head_warning = "The supplied head is current, but cached notes and positions remain unavailable."
        else:
            head_warning = "The supplied head is stale."
    warning = (
        "Review note batch is unavailable; it may have expired, been evicted, used, or lost on restart. "
        "This call attempted no writes; prior attempts may have posted. "
        "Wait for any in-flight attempt to finish. "
        "Check prior results and GitLab before resubmitting if a previous attempt may have written. "
        "Check the head and note positions before resubmitting, and get explicit APPROVE for any changed final text. "
        f"{head_warning}"
    )
    return PostReviewNotesResult(
        results=[], batch_error="unavailable", warning=warning, current_head_sha=current_head_sha
    )


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


def _resolve_body_path(body_path: str, body_dir: str | None) -> str:
    """Return the path to read: absolute paths as given, relative paths joined to `body_dir`.

    The joined path is resolved first, so `..` parts and symlinks that leave `body_dir` are rejected. 🛡️
    """
    if Path(body_path).is_absolute():
        return body_path
    if body_dir is None:
        raise_review_preflight_error("body_path must be absolute unless body_dir is set")
    base = Path(body_dir).resolve()
    resolved = (base / body_path).resolve()
    if not resolved.is_relative_to(base):
        raise_review_preflight_error("body_path must stay inside body_dir")
    return str(resolved)


def _read_review_note_body(note: ReviewNote, body_dir: str | None) -> str:
    """Return one usable note body, or raise a ValueError or OSError that says why it is not usable."""
    if note.body is not None:
        body = note.body
    else:
        body = read_server_local_utf8_file(
            _resolve_body_path(note.body_path or "", body_dir),
            path_field="body_path",
            size_field="body",
            byte_limit=REVIEW_NOTE_BODY_CAP,
        )
    if not body.strip():
        raise_review_preflight_error("body must not be blank")
    if len(body.encode("utf-8")) > REVIEW_NOTE_BODY_CAP:
        raise_review_preflight_error(f"body exceeds {REVIEW_NOTE_BODY_CAP} UTF-8 bytes")
    return body


def _read_review_note_bodies(notes: list[ReviewNote], body_dir: str | None) -> list[_CheckedNote]:
    """Read every note body before any GitLab request, recording a reason for each unusable one."""
    checks: list[_CheckedNote] = []
    for note in notes:
        try:
            checks.append(_CheckedNote(body=_read_review_note_body(note, body_dir)))
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


def _parse_unified_diff(diff: str) -> _ParsedDiff:  # noqa: C901, PLR0912, PLR0915 - distinct hunk cases stay explicit.
    """Parse unified hunks, their positions, and unchanged gap bounds."""
    positions: list[tuple[str, int | None, int | None]] = []
    hunks: list[_DiffHunk] = []
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
            old_start = int(match.group(1))
            old_remaining = int(match.group(2) or 1)
            new_start = int(match.group(3))
            new_remaining = int(match.group(4) or 1)
            if (old_remaining and old_start < 1) or (not old_remaining and old_start < 0):
                raise_review_preflight_error("diff contains an invalid old unified hunk range")
            if (new_remaining and new_start < 1) or (not new_remaining and new_start < 0):
                raise_review_preflight_error("diff contains an invalid new unified hunk range")
            if not old_remaining and not new_remaining:
                raise_review_preflight_error("diff contains an empty unified hunk")
            old_begin = old_start - 1 if old_remaining else old_start
            new_begin = new_start - 1 if new_remaining else new_start
            hunk = _DiffHunk(
                old_begin=old_begin,
                old_end=old_begin + old_remaining,
                new_begin=new_begin,
                new_end=new_begin + new_remaining,
            )
            previous_old_end = hunks[-1].old_end if hunks else 0
            previous_new_end = hunks[-1].new_end if hunks else 0
            if hunk.old_begin < previous_old_end or hunk.new_begin < previous_new_end:
                raise_review_preflight_error("diff hunks overlap or are out of order")
            if hunk.old_begin - previous_old_end != hunk.new_begin - previous_new_end:
                raise_review_preflight_error("diff hunks have unequal unchanged gaps")
            hunks.append(hunk)
            old_line = old_start
            new_line = new_start
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
    return _ParsedDiff(positions=tuple(positions), hunks=tuple(hunks))


def _diff_paths(raw: dict[str, Any], file: str) -> tuple[str, str]:
    """Return the old and new paths of one REST diff entry, or reject malformed path data."""
    old_path, new_path = raw.get("old_path"), raw.get("new_path")
    if not isinstance(old_path, str) or not isinstance(new_path, str):
        raise_review_preflight_error(f"diff paths for {file!r} are invalid")
    return old_path, new_path


def _parsed_diff_for_note(
    raw: dict[str, Any],
    note: ReviewNote,
    cache: dict[int, tuple[_ParsedDiff | None, str]],
) -> _ParsedDiff:
    """Return one cached patch parse, or preserve the established unusable-patch errors."""
    if raw.get("collapsed") or raw.get("too_large"):
        raise_review_preflight_error(f"diff for {note.file!r} is collapsed or too large to position safely")
    if raw.get("binary"):
        raise_review_preflight_error(f"diff for {note.file!r} is binary and cannot be positioned safely")
    patch = raw.get("diff")
    if not isinstance(patch, str) or not patch:
        raise_review_preflight_error(f"diff for {note.file!r} has no usable patch")

    key = id(raw)
    if key not in cache:
        try:
            cache[key] = (_parse_unified_diff(patch), "")
        except ValueError as exc:
            cache[key] = (None, str(exc))
    parsed, error = cache[key]
    if parsed is None:
        raise_review_preflight_error(error)
    return parsed


def _matching_lines(parsed: _ParsedDiff, note: ReviewNote) -> list[tuple[int | None, int | None]]:
    """Return the in-hunk diff positions matching the requested side and line number."""
    matches = []
    for kind, old_line, new_line in parsed.positions:
        if note.line is not None and new_line == note.line and kind in ("added", "context"):
            matches.append((old_line, new_line))
        if note.old_line is not None and old_line == note.old_line and kind in ("removed", "context"):
            matches.append((old_line, new_line))
    return matches


def _line_in_hunk(parsed: _ParsedDiff, side: str, line: int) -> bool:
    """Check whether a one-based line coordinate falls inside a hunk on the requested side."""
    coordinate = line - 1
    return any(
        begin <= coordinate < end
        for begin, end in (
            (hunk.new_begin, hunk.new_end) if side == "new" else (hunk.old_begin, hunk.old_end) for hunk in parsed.hunks
        )
    )


def _nearest_line_hint(parsed: _ParsedDiff, side: str, target: int) -> str:
    """Format up to five nearby validated hunk line numbers for the requested side."""
    if side == "new":
        coordinates = {new_line for _, _, new_line in parsed.positions if new_line is not None}
    else:
        coordinates = {old_line for _, old_line, _ in parsed.positions if old_line is not None}
    if not coordinates:
        return "Nearest line hints are unavailable; use a file-level note."
    closest = sorted(coordinates, key=lambda line: (abs(line - target), line))[:5]
    ordered = sorted(closest)
    ranges: list[str] = []
    start = previous = ordered[0]
    for line in ordered[1:]:
        if line == previous + 1:
            previous = line
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = line
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    hint = f"Nearest {side}-side diff lines: {', '.join(ranges)}."
    if len(hint) > REVIEW_NOTE_HINT_CAP:
        return "Nearest line hints are unavailable; use a file-level note."
    return hint


def _line_failure_reason(message: str, hint: str) -> str:
    """Cap a line-note reason while preserving the nearest-line hint."""
    if len(hint) > REVIEW_NOTE_HINT_CAP:
        hint = "Nearest line hints are unavailable; use a file-level note."
    suffix = f". {hint}"
    available = max(0, REVIEW_NOTE_REASON_CAP - len(suffix))
    return f"{message[:available]}{suffix}"


def _git_blob_lines(content: bytes) -> list[bytes]:
    """Split a Git blob on LF only, retaining each line terminator and a final unterminated line."""
    if not content:
        return []
    pieces = content.split(b"\n")
    lines = [piece + b"\n" for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])
    return lines


def _verified_unchanged_gaps(
    parsed: _ParsedDiff,
    old_lines: list[bytes],
    new_lines: list[bytes],
) -> tuple[list[tuple[int, int, int, int]] | None, str]:
    """Check hunk bounds and return the byte-identical unchanged line intervals."""
    gaps = []
    old_cursor = new_cursor = 0
    for hunk in parsed.hunks:
        if hunk.old_end > len(old_lines) or hunk.new_end > len(new_lines):
            return None, "hunk bounds exceed the file content"
        old_gap = old_lines[old_cursor : hunk.old_begin]
        new_gap = new_lines[new_cursor : hunk.new_begin]
        if len(old_gap) != len(new_gap) or old_gap != new_gap:
            return None, "unchanged file content differs outside the reported hunks"
        gaps.append((old_cursor, hunk.old_begin, new_cursor, hunk.new_begin))
        old_cursor, new_cursor = hunk.old_end, hunk.new_end

    old_tail = old_lines[old_cursor:]
    new_tail = new_lines[new_cursor:]
    if len(old_tail) != len(new_tail) or old_tail != new_tail:
        return None, "unchanged file content differs outside the reported hunks"
    gaps.append((old_cursor, len(old_lines), new_cursor, len(new_lines)))
    return gaps, ""


async def _read_review_blob(
    client: GitLabClient,
    project: str,
    path: str,
    sha: str,
    cache: dict[tuple[str, str], tuple[bytes | None, str]],
) -> tuple[bytes | None, str]:
    """Read and cache one bounded immutable Git blob, including failed reads."""
    key = (path, sha)
    if key in cache:
        return cache[key]
    encoded_project = quote(project, safe="")
    encoded_path = quote(path, safe="")
    endpoint = f"/projects/{encoded_project}/repository/files/{encoded_path}/raw?ref={sha}"
    try:
        content, truncated = await client.get_trace_prefix(endpoint, max_bytes=REVIEW_NOTE_BLOB_CAP)
        if truncated or len(content) > REVIEW_NOTE_BLOB_CAP:
            result = (None, f"file content exceeds the {REVIEW_NOTE_BLOB_CAP}-byte limit")
        elif b"\x00" in content:
            result = (None, "file content is binary")
        else:
            result = (content, "")
    except GitLabAPIError as exc:
        result = (None, f"GitLab returned HTTP {exc.status}")
    except GitLabConnectionError:
        result = (None, "GitLab connection failed")
    except ToolError:
        result = (None, "GitLab could not read raw file content")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = (None, str(exc))
    cache[key] = result
    return result


async def _unchanged_line_position(  # noqa: PLR0911, PLR0913, PLR0917 - every value defines the mapping proof.
    client: GitLabClient,
    project: str,
    raw: dict[str, Any],
    old_path: str,
    new_path: str,
    note: ReviewNote,
    parsed: _ParsedDiff,
    refs: dict[str, str],
    blob_cache: dict[tuple[str, str], tuple[bytes | None, str]],
) -> tuple[int | None, int | None, str]:
    """Map an out-of-hunk coordinate only when both immutable blobs prove it is unchanged."""
    if raw.get("new_file") or raw.get("deleted_file"):
        return None, None, "unchanged lines cannot be verified for a new or deleted file"

    old_content, error = await _read_review_blob(client, project, old_path, refs["base_sha"], blob_cache)
    if old_content is None:
        return None, None, f"old file content could not be read safely: {error}"
    new_content, error = await _read_review_blob(client, project, new_path, refs["head_sha"], blob_cache)
    if new_content is None:
        return None, None, f"new file content could not be read safely: {error}"

    gaps, error = _verified_unchanged_gaps(parsed, _git_blob_lines(old_content), _git_blob_lines(new_content))
    if gaps is None:
        return None, None, error

    target = note.line if note.line is not None else note.old_line
    if target is None:
        return None, None, "line coordinate is missing"
    coordinate = target - 1
    for old_begin, old_end, new_begin, new_end in gaps:
        if note.line is not None and new_begin <= coordinate < new_end:
            mapped = old_begin + coordinate - new_begin
            return mapped + 1, target, ""
        if note.old_line is not None and old_begin <= coordinate < old_end:
            mapped = new_begin + coordinate - old_begin
            return target, mapped + 1, ""
    return None, None, "line coordinate is outside the file or is not in a verified unchanged gap"


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


async def _check_note_position(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0917 - preflight outcomes stay explicit.
    client: GitLabClient,
    project: str,
    refs: dict[str, str],
    matches: list[dict[str, Any]],
    note: ReviewNote,
    check: _CheckedNote,
    parsed_cache: dict[int, tuple[_ParsedDiff | None, str]],
    blob_cache: dict[tuple[str, str], tuple[bytes | None, str]],
) -> _CheckedNote:
    """Add a GitLab position to a note whose body is usable, or record why its diff position fails."""
    if note.file is None:
        return check if check.reason else _CheckedNote(reason=_note_reason("file is required for a diff position"))
    file = note.file
    line_note = note.line is not None or note.old_line is not None
    try:
        if not matches:
            message = f"Diff preflight could not find {file!r} within {MAX_PAGES} pages"
            if line_note:
                message = _line_failure_reason(message, "Nearest line hints are unavailable; use a file-level note.")
            return _CheckedNote(position=check.position, reason=check.reason or _note_reason(message))
        if len(matches) != 1:
            message = f"Diff preflight found ambiguous path {file!r}"
            if line_note:
                message = _line_failure_reason(message, "Nearest line hints are unavailable; use a file-level note.")
            return _CheckedNote(position=check.position, reason=check.reason or _note_reason(message))

        raw = matches[0]
        old_path, new_path = _diff_paths(raw, file)
        if not line_note:
            # Omit line keys entirely: response verification accepts omitted and null line keys.
            position: dict[str, str | int | None] = {
                "position_type": "file",
                "old_path": old_path,
                "new_path": new_path,
            }
            return _CheckedNote(body=check.body, position=position, reason=check.reason)

        parsed = _parsed_diff_for_note(raw, note, parsed_cache)
        matched = _matching_lines(parsed, note)
        if len(matched) == 1:
            old_line, new_line = matched[0]
            position = {
                "position_type": "text",
                "old_path": old_path,
                "new_path": new_path,
                "old_line": old_line,
                "new_line": new_line,
            }
            return _CheckedNote(body=check.body, position=position, reason=check.reason)

        side = "new" if note.line is not None else "old"
        target = note.line if note.line is not None else note.old_line
        if target is None:
            reason = _line_failure_reason(
                "line coordinate is missing", "Nearest line hints are unavailable; use a file-level note."
            )
            return _CheckedNote(position=check.position, reason=check.reason or _note_reason(reason))
        if matched:
            message = f"line {target} on the {side} side is ambiguous in {file!r}"
            reason = _line_failure_reason(message, _nearest_line_hint(parsed, side, target))
            return _CheckedNote(position=check.position, reason=check.reason or reason)

        base_reason = f"line {target} on the {side} side is not a usable diff line in {file!r}"
        if _line_in_hunk(parsed, side, target):
            reason = _line_failure_reason(base_reason, _nearest_line_hint(parsed, side, target))
            return _CheckedNote(position=check.position, reason=check.reason or reason)

        hint = _nearest_line_hint(parsed, side, target)
        old_line, new_line, fallback_error = await _unchanged_line_position(
            client,
            project,
            raw,
            old_path,
            new_path,
            note,
            parsed,
            refs,
            blob_cache,
        )
        if fallback_error:
            message = f"line {target} on the {side} side in {file!r}; fallback rejected: {fallback_error}"
            reason = _line_failure_reason(message, hint)
            return _CheckedNote(position=check.position, reason=check.reason or reason)
        position = {
            "position_type": "text",
            "old_path": old_path,
            "new_path": new_path,
            "old_line": old_line,
            "new_line": new_line,
        }
        return _CheckedNote(body=check.body, position=position, reason=check.reason)
    except ValueError as exc:
        message = str(exc)
        if line_note:
            message = _line_failure_reason(message, "Nearest line hints are unavailable; use a file-level note.")
        return _CheckedNote(position=check.position, reason=check.reason or _note_reason(message))


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
        parsed_cache: dict[int, tuple[_ParsedDiff | None, str]] = {}
        blob_cache: dict[tuple[str, str], tuple[bytes | None, str]] = {}
        for note, check in zip(notes, checks, strict=True):
            if note.file is None:
                checked.append(check)
            else:
                checked.append(
                    await _check_note_position(
                        client,
                        project,
                        refs,
                        paths[note.file],
                        note,
                        check,
                        parsed_cache,
                        blob_cache,
                    )
                )
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
    requested_position = _review_response_position(position)
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
        detail = f": {exc.detail}" if exc.detail else ""
        if exc.status >= 500 or exc.status == 408:  # noqa: PLR2004 - these statuses can hide a committed write.
            reason = f"The post outcome is unknown after HTTP {exc.status}{detail}. Check GitLab before retrying."
            return (
                ReviewNotePostResult(
                    index=index,
                    status="unknown",
                    position=requested_position,
                    reason=_note_reason(reason),
                ),
                reason,
            )
        reason = f"GitLab rejected the post with HTTP {exc.status}{detail}. Check the item before retrying."
        return (
            ReviewNotePostResult(
                index=index,
                status="failed",
                position=requested_position,
                reason=_note_reason(reason),
            ),
            reason,
        )
    except GitLabConnectionError as exc:
        reason = f"The post outcome is unknown: {exc} Check GitLab before retrying."
        return (
            ReviewNotePostResult(
                index=index,
                status="unknown",
                position=requested_position,
                reason=_note_reason(reason),
            ),
            reason,
        )
    except (TypeError, ValueError) as exc:
        reason = (
            f"GitLab may have created the note, but its response could not be verified: {exc} Check GitLab "
            "before retrying."
        )
        return (
            ReviewNotePostResult(
                index=index,
                status="unverified",
                position=requested_position,
                reason=_note_reason(reason),
            ),
            reason,
        )

    discussion_id, note_id, actual_position = _review_response_ids(response)
    compact_position = _review_response_position(actual_position)
    if not discussion_id or note_id is None:
        reason = "GitLab's successful response was missing the discussion or note ID."
    elif not _review_response_matches(actual_position, position):
        reason = "GitLab's returned position did not match the request."
    else:
        reason = ""
    if reason:
        return (
            ReviewNotePostResult(
                index=index,
                status="unverified",
                note_id=note_id,
                discussion_id=discussion_id,
                position=compact_position or requested_position,
                reason=_note_reason(reason),
            ),
            f"The post may have succeeded. {reason} Check GitLab before retrying.",
        )
    return ReviewNotePostResult(index=index, status="posted", note_id=note_id, discussion_id=discussion_id), ""


def _review_preflight_result(checks: list[_CheckedNote]) -> PostReviewNotesResult:
    """Return one row per note: validated when it can be posted, failed with a reason when it cannot.

    The warning appears only when at least one note failed, and it says that nothing was posted.
    """
    results = [
        ReviewNotePostResult(
            index=index,
            status="failed",
            reason=check.reason,
            position=_review_response_position(check.position),
        )
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


def _cached_review_notes_with_overrides(
    cached_notes: tuple[CachedReviewNote, ...],
    body_overrides: list[ReviewNoteBodyOverride] | None,
) -> list[ReviewNote]:
    """Validate sparse override indices and rebuild notes from cached coordinates and final body sources."""
    overrides = body_overrides or []
    override_indices = [override.index for override in overrides]
    if len(set(override_indices)) != len(override_indices):
        raise ToolError("body_overrides indices must be unique.")  # noqa: TRY003
    if any(index < 0 or index >= len(cached_notes) for index in override_indices):
        raise ToolError("body_overrides index is outside the cached batch.")  # noqa: TRY003

    overrides_by_index = {override.index: override for override in overrides}
    notes = []
    for index, cached_note in enumerate(cached_notes):
        override = overrides_by_index.get(index)
        notes.append(
            ReviewNote(
                body=override.body if override is not None else cached_note.body,
                body_path=override.body_path if override is not None else None,
                file=cached_note.file,
                line=cached_note.line,
                old_line=cached_note.old_line,
            )
        )
    return notes


def _select_review_notes(notes: list[ReviewNote], indices: list[int] | None) -> tuple[list[ReviewNote], list[int]]:
    """Select notes in caller order while keeping their original batch indices."""
    if indices is None:
        return notes.copy(), list(range(len(notes)))
    if not indices:
        raise ToolError("indices must contain at least one batch index.")  # noqa: TRY003
    if len(set(indices)) != len(indices):
        raise ToolError("indices must be unique.")  # noqa: TRY003
    if any(index < 0 or index >= len(notes) for index in indices):
        raise ToolError("indices must be within the cached batch.")  # noqa: TRY003
    return [notes[index] for index in indices], indices


async def _post_review_notes(  # noqa: PLR0913, PLR0917 - each argument is part of the batch request.
    ctx: Context,
    project: str,
    iid: int,
    head_sha: str,
    notes: list[ReviewNote],
    validate_only: bool,
    body_dir: str | None,
) -> PostReviewNotesResult:
    """Run preflight, then validate or post the notes. The public tool adds people changes after this returns."""
    if not full_sha(head_sha):
        return _unattempted_review_notes(
            len(notes), "head_sha must be a full 40-character hexadecimal SHA. No notes were posted."
        )
    checks = _read_review_note_bodies(notes, body_dir)
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
    has_failed_checks = any(check.reason for check in checks)
    if validate_only or has_failed_checks:
        result = _review_preflight_result(checks)
        if validate_only and not has_failed_checks:
            cached_notes = tuple(
                CachedReviewNote(
                    file=note.file,
                    line=note.line,
                    old_line=note.old_line,
                    body=check.body,
                )
                for note, check in zip(notes, checks, strict=True)
            )
            cache = get_review_batch_cache(ctx)
            batch_id = cache.put(project, iid, refs["head_sha"], cached_notes)
            result = result.model_copy(
                update={
                    "batch_id": batch_id,
                    "ttl_seconds": cache.TTL_SECONDS,
                    "hint": (
                        "To change note text before posting, pass body_overrides with this batch_id. "
                        "No new validation is needed."
                    ),
                    "results": [row.model_copy(update={"position": {}}) for row in result.results],
                }
            )
        return result
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
    if not result.results or not all(row.status == "posted" for row in result.results):
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
async def post_review_notes(  # noqa: C901, PLR0913, PLR0917 - the public batch contract keeps its checks explicit.
    ctx: Context,
    project: Project,
    iid: Iid,
    head_sha: Annotated[
        str,
        Field(description="Full 40-character head SHA from get_merge_request or get_merge_request_diff."),
    ],
    indices: Annotated[
        list[int] | None,
        Field(
            min_length=1,
            description="Unique zero-based indices to post from a cached batch, in posting order. Requires batch_id.",
        ),
    ] = None,
    notes: Annotated[
        list[ReviewNote] | None,
        Field(min_length=1, max_length=20, description="Notes to validate or post. Required unless batch_id is set."),
    ] = None,
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
    compact: Annotated[
        bool,
        Field(description="Omit verified posted rows and return their count, while keeping all other outcomes."),
    ] = False,
    batch_id: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=200,
            description=(
                "Single-use handle from a successful validate_only call. Provide this instead of notes. It is claimed "
                "before body_overrides indices are checked, so an invalid index consumes the handle."
            ),
        ),
    ] = None,
    body_overrides: Annotated[
        list[ReviewNoteBodyOverride] | None,
        Field(
            min_length=1,
            max_length=20,
            description=(
                "Sparse replacements for a claimed batch, using zero-based unique in-range indices and exactly one "
                "of body or body_path per entry. Requires batch_id; omitted indices keep their cached body snapshots. "
                "Show the final replacement text to the user and get approval before posting."
            ),
        ),
    ] = None,
    body_dir: Annotated[
        str | None,
        Field(
            min_length=1,
            description=(
                "Absolute server-local directory. Relative body_path values in notes and body_overrides resolve "
                "inside it, so each note can send only a short file name."
            ),
        ),
    ] = None,
) -> PostReviewNotesResult:
    """Validate or post inline comments and general review notes in a sequential, bounded batch.

    Like `glab mr note create`. Omit `file`, `line`, and `old_line` for a general merge-request discussion. Set only
    `file` for a file-level diff discussion, or set `file` and one line coordinate for an inline diff discussion.
    Provide exactly one of `notes` or `batch_id`. `validate_only` cannot be combined with `batch_id`. Set
    `validate_only` to true to run all body, path, line, and SHA checks without writing; validation does not need the
    user's `APPROVE`. A successful validation returns a single-use `batch_id` with the resolved note bodies and
    locations, and `ttl_seconds`, an upper bound until expiration. Show the final note text to the user and wait for
    explicit `APPROVE`; approval of placeholder drafts
    alone is not enough. Then call this tool with that `batch_id` instead of `notes`. Optional `body_overrides` entries
    use `{index, body}` or `{index, body_path}` to replace cached bodies at zero-based indices. Each index must be
    unique and in range, each entry must set exactly one body source, and omitted indices keep their cached snapshots.
    Overrides require `batch_id` and always use indices from the original full batch. Optional `indices` selects a
    nonempty, unique, in-range subset to post, in the supplied order. It also requires `batch_id`; omitted `indices`
    posts the full batch in its original order. Returned row indices always refer to the original batch. The tool
    checks every selected final body and reruns fresh SHA, path, and position checks
    before the first write. The handle is opaque, is not approval, expires after 30 minutes,
    may be evicted earlier when the bounded cache is full, and is invalidated by a server restart. It is consumed on
    any posting attempt, including failed preflight or writes. Invalid override and selection indices are checked
    after the handle is claimed and therefore consume it. A mismatched project, MR, or head consumes the handle and
    returns an error without GitLab requests. An unavailable handle returns `batch_error="unavailable"` and no rows;
    this call attempted no writes, but prior attempts may have posted. Check prior results and GitLab before
    resubmitting if a previous attempt may have written, and wait for any in-flight attempt to finish. Check the head
    and note positions before resubmitting; show
    changed final text to the user and obtain explicit `APPROVE` before posting. The warning states whether the
    supplied head is current, stale, or unknown; even a current head cannot recover the cached notes or positions.
    `head_sha` must be the full SHA from `get_merge_request` or `get_merge_request_diff`.
    Each note is checked on its own. If any note fails, nothing is posted: every note returns a row, with `validated`
    for notes that passed and `failed` plus a `reason` for notes that did not. A batch-wide problem, such as a stale
    `head_sha`, returns `unattempted` for every note and a `warning`.
    A file-level note needs only the file's path in the diff, so it works for findings outside diff hunks and for
    collapsed or too-large diffs. General notes do not read diff pages, but still check the merge request head twice.
    A `body_path` is an absolute path on this server, which is useful with stdio but is not a path on the agent's
    computer. Set `body_dir` to send relative `body_path` values, such as `01.md`, that must stay inside it; absolute
    paths still work. All bodies, diff lines, and paths are checked before the first post. A second head check is
    preflight only, not an atomic lock, because GitLab has no expected-head guard for discussion creation. Writes are
    sequential and never retried. If a write result is unknown or unverified, check GitLab before trying again. Check
    every result before deciding what to do next.
    Normal posted rows contain only `index`, `status`, `note_id`, and `discussion_id`. Failed, unknown, and unverified
    rows include a `reason` and any available `position`. `add_reviewers` and `add_assignees` add people only after
    every note posts, in the same call. They are ignored when `validate_only` is true.
    Set `compact` to true to omit verified posted rows and return their count. All other result rows and batch details
    remain unchanged.
    """
    if (notes is None) == (batch_id is None):
        raise ToolError("Provide exactly one of notes or batch_id.")  # noqa: TRY003
    if batch_id is not None and not batch_id.strip():
        raise ToolError("batch_id must not be blank.")  # noqa: TRY003
    if validate_only and batch_id is not None:
        raise ToolError("validate_only cannot be combined with batch_id.")  # noqa: TRY003
    if body_overrides is not None and batch_id is None:
        raise ToolError("body_overrides can only be used with batch_id.")  # noqa: TRY003
    if indices is not None and batch_id is None:
        raise ToolError("indices can only be used with batch_id.")  # noqa: TRY003
    if body_dir is not None and not Path(body_dir).is_absolute():
        raise ToolError("body_dir must be an absolute server-local directory.")  # noqa: TRY003

    selected_indices = list(range(len(notes or [])))
    if batch_id is not None:
        try:
            cached_batch = _claim_review_batch(ctx, batch_id, project, iid, head_sha)
        except ReviewBatchUnavailableError:
            result = await _unavailable_review_batch(ctx, project, iid, head_sha)
            return _compact_review_result(result) if compact else result
        cached_notes = _cached_review_notes_with_overrides(cached_batch.notes, body_overrides)
        notes, selected_indices = _select_review_notes(cached_notes, indices)
    if notes is None:
        raise ToolError("Provide exactly one of notes or batch_id.")  # noqa: TRY003

    result = await _post_review_notes(ctx, project, iid, head_sha, notes, validate_only, body_dir)
    result = result.model_copy(
        update={
            "results": [row.model_copy(update={"index": selected_indices[row.index]}) for row in result.results],
        }
    )
    if not validate_only:
        result = await _add_people_after_posts(ctx, project, iid, result, add_reviewers, add_assignees)
    if compact:
        result = _compact_review_result(result)
    return result
