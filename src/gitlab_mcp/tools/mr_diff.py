"""Merge request diff tools: changed files, patches, and changes since an earlier version 📄."""

import re
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.common import (
    DIFF_PAGE_SIZE,
    MAX_PAGES,
    Iid,
    Project,
    decode_cursor,
    encode_cursor,
    is_int_at_least,
)
from gitlab_mcp.errors import InvalidCursorError
from gitlab_mcp.mr_common import (
    DIFF_HUNK_PATTERN,
    diff_refs_head_sha,
    fetch_diff_page,
    fetch_merge_request,
    full_sha,
    mr_rest_path,
)
lazy from gitlab_mcp.client import GitLabClient

# File list and head SHA for get_merge_request_diff. No patch text, so it stays cheap. 📄
DIFF_FILES_QUERY = """
query($fullPath: ID!, $iid: String!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      diffRefs { headSha }
      diffStats { path additions deletions }
    }
  }
}
"""


# Total patch characters that one diff call may return. A single patch is cut to this too.
DIFF_BUDGET = 40000
DIFF_HUNK_NORMALIZE_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)
FILE_LIST_HINT = "Pass paths=[...] to read patches."
INVALID_SINCE_SHA_MESSAGE = "since_sha must be a full 40-character commit SHA."


class FileStat(BaseModel):
    """A changed file and the new-side ranges found in its patch."""

    path: str
    additions: int
    deletions: int
    new_line_ranges: list[tuple[int, int]] = Field(
        default_factory=list,
        description="Inclusive new-side line ranges for each nonempty hunk, including context lines.",
    )
    ranges_complete: bool = Field(
        default=True,
        description="False when the patch was not scanned or GitLab did not provide usable patch text.",
    )


# What happened to a file. GitLab sets one flag for added, deleted, or renamed files, and none for a plain edit.
DiffStatus = Literal["added", "deleted", "renamed", "modified"]


class FileDiff(BaseModel):
    """One file's patch."""

    path: str  # The new path.
    old_path: str = Field(default="", exclude_if=lambda v: not v)  # Only when it differs from `path`.
    status: DiffStatus
    diff: str = Field(default="", exclude_if=lambda v: not v)  # Empty for a too-large file.
    truncated: bool = Field(default=False, exclude_if=lambda v: not v)  # Cut to the per-call character budget.
    generated: bool = Field(default=False, exclude_if=lambda v: not v)
    collapsed: bool = Field(default=False, exclude_if=lambda v: not v)
    too_large: bool = Field(default=False, exclude_if=lambda v: not v)


class MergeRequestDiff(BaseModel):
    """A file list or the requested patches."""

    head_sha: str = Field(default="", exclude_if=lambda v: not v)
    since_version_found: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    files: list[FileStat] = Field(default_factory=list, exclude_if=lambda v: not v)
    diffs: list[FileDiff] = Field(default_factory=list, exclude_if=lambda v: not v)
    next_cursor: str = Field(default="", exclude_if=lambda v: not v)  # Empty when the scan finished.
    paths_not_found: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    warning: str = Field(default="", exclude_if=lambda value: not value)
    hint: str = Field(default="", exclude_if=lambda value: not value)  # Tells a file-list caller how to get patches.


def _new_line_ranges(diff: str) -> list[tuple[int, int]]:
    """Return inclusive new-side line ranges for each nonempty unified diff hunk."""
    ranges = []
    for line in diff.splitlines():
        match = DIFF_HUNK_PATTERN.match(line)
        if match is None:
            continue
        start = int(match.group(3))
        length = int(match.group(4) or 1)
        if length:
            ranges.append((start, start + length - 1))
    return ranges


def _to_file_stats(
    stats: list[dict[str, Any]] | None,
    patch_ranges: dict[str, tuple[list[tuple[int, int]], bool]],
) -> list[FileStat]:
    """Convert GraphQL stats and attach patch ranges, keeping all original file rows."""
    file_stats = []
    for file_stat in stats or []:
        ranges, complete = patch_ranges.get(file_stat["path"], ([], False))
        file_stats.append(
            FileStat(
                path=file_stat["path"],
                additions=file_stat["additions"],
                deletions=file_stat["deletions"],
                new_line_ranges=ranges,
                ranges_complete=complete,
            ),
        )
    return file_stats


async def _scan_file_stat_ranges(
    client: GitLabClient,
    project: str,
    iid: int,
    stats: list[dict[str, Any]],
) -> dict[str, tuple[list[tuple[int, int]], bool]]:
    """Read at most MAX_PAGES of REST diffs and extract ranges for known GraphQL file paths."""
    known_paths = {stat["path"] for stat in stats}
    patch_ranges: dict[str, tuple[list[tuple[int, int]], bool]] = {}
    page = 1
    for _ in range(MAX_PAGES):
        files, next_page = await fetch_diff_page(client, project, iid, page)
        for raw in files:
            if not isinstance(raw, dict):
                continue
            path = raw.get("new_path")
            if not isinstance(path, str) or path not in known_paths or path in patch_ranges:
                continue
            diff = raw.get("diff")
            available = isinstance(diff, str) and bool(diff) and not raw.get("collapsed") and not raw.get("too_large")
            patch_ranges[path] = (_new_line_ranges(diff) if available else [], available)
        if next_page is None:
            break
        page = next_page
    return patch_ranges


def _diff_status(raw: dict[str, Any]) -> DiffStatus:
    """Name the change kind of one REST diff entry. Checked in this order, and a plain edit sets no flag."""
    if raw["new_file"]:
        return "added"
    if raw["deleted_file"]:
        return "deleted"
    if raw["renamed_file"]:
        return "renamed"
    return "modified"


def _to_file_diff(raw: dict[str, Any]) -> FileDiff:
    """Convert one REST diff entry. A patch over DIFF_BUDGET is cut and flagged."""
    text = raw["diff"]
    old_path = raw["old_path"]
    return FileDiff(
        path=raw["new_path"],
        # GitLab repeats the path in old_path for most files, so only a real difference is kept.
        old_path=old_path if old_path != raw["new_path"] else "",
        status=_diff_status(raw),
        diff=text[:DIFF_BUDGET],
        truncated=len(text) > DIFF_BUDGET,
        # Older GitLab versions may leave these flags out, so a missing flag means false.
        generated=raw.get("generated_file", False),
        collapsed=raw.get("collapsed", False),
        too_large=raw.get("too_large", False),
    )


def _requested_hits(raw: dict[str, Any], wanted: set[str]) -> set[str]:
    """Return the requested paths that this file matches, by its new or old path."""
    return {path for path in (raw["new_path"], raw["old_path"]) if path in wanted}


def _diff_cursor_start(cursor: str) -> tuple[int, int]:
    """Return the REST page and skip count for a diff call. Anything else is an InvalidCursorError."""
    payload = decode_cursor(cursor, "get_merge_request_diff")
    if not is_int_at_least(payload.get("page"), 1) or not is_int_at_least(payload.get("skip"), 0):
        raise InvalidCursorError("get_merge_request_diff")
    return payload["page"], payload["skip"]


async def _scan_diffs(
    client: GitLabClient,
    project: str,
    iid: int,
    wanted: set[str],
    start: tuple[int, int],
) -> tuple[list[FileDiff], str, set[str]]:
    """Read diff pages from `start` and collect the patches of the wanted files.

    The scan stops at the budget, at `MAX_PAGES`, or at the last page. Returns
    the patches, the cursor to resume from (empty when every page was read), and
    the requested paths that matched a file.
    """
    page, skip = start
    diffs: list[FileDiff] = []
    matched: set[str] = set()
    total = 0
    for _ in range(MAX_PAGES):
        files, next_page = await fetch_diff_page(client, project, iid, page)
        for index in range(skip, len(files)):
            hits = _requested_hits(files[index], wanted)
            if not hits:
                continue
            diff = _to_file_diff(files[index])
            # Always return at least one patch, even when that one alone is over budget.
            if diffs and total + len(diff.diff) > DIFF_BUDGET:
                return diffs, encode_cursor({"page": page, "skip": index}), matched
            diffs.append(diff)
            total += len(diff.diff)
            matched |= hits
        if next_page is None:
            return diffs, "", matched
        # Later pages start at their first file.
        page, skip = next_page, 0
    # The page limit was hit. `page` is now the next unread page.
    return diffs, encode_cursor({"page": page, "skip": 0}), matched


async def _scan_diff_versions(
    client: GitLabClient,
    project: str,
    iid: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Read at most MAX_PAGES of merge request diff versions and report whether the scan finished."""
    versions: list[dict[str, Any]] = []
    page = 1
    for _ in range(MAX_PAGES):
        body, next_page = await client.get_page(
            f"{mr_rest_path(project, iid)}/versions",
            per_page=DIFF_PAGE_SIZE,
            page=page,
        )
        if not isinstance(body, list) or any(not isinstance(version, dict) for version in body):
            return versions, False
        versions.extend(body)
        if next_page is None:
            return versions, True
        page = next_page
    return versions, False


def _version_id(version: dict[str, Any]) -> int | None:
    """Return a usable integer diff version ID, rejecting booleans and other JSON values."""
    value = version.get("id")
    return value if type(value) is int and value >= 0 else None


async def _get_version_detail(
    client: GitLabClient,
    project: str,
    iid: int,
    version_id: int,
) -> Any:
    """Fetch the full file snapshot for one merge request diff version."""
    return await client.get(f"{mr_rest_path(project, iid)}/versions/{version_id}")


def _validate_version_snapshot(  # noqa: C901, PLR0911 - each check prevents an unsafe comparison.
    detail: Any,
    expected_sha: str,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Return validated diff entries, or a warning when GitLab did not collect a complete snapshot."""
    if not isinstance(detail, dict):
        return None, "GitLab returned an unusable diff version payload. No comparison was made."
    if detail.get("overflow"):
        return None, "GitLab marked a diff version as overflowed. No comparison was made."
    if detail.get("state") != "collected":
        return None, "A diff version is not fully collected. No comparison was made."
    if detail.get("head_commit_sha") != expected_sha:
        return None, "A diff version did not match its listed commit SHA. No comparison was made."
    files = detail.get("diffs")
    real_size = detail.get("real_size")
    # GitLab documents real_size as a string. Capped counts such as "1000+" are not complete sizes.
    if isinstance(real_size, str) and real_size.isascii() and real_size.isdecimal():
        real_size = int(real_size)
    if not isinstance(files, list) or type(real_size) is not int or real_size < 0 or real_size > len(files):
        return None, "A diff version has an incomplete file list. No comparison was made."

    validated: list[dict[str, Any]] = []
    for raw in files:
        if not isinstance(raw, dict):
            return None, "A diff version contains an unusable file entry. No comparison was made."
        if raw.get("collapsed") or raw.get("too_large"):
            return None, "A diff version contains a collapsed or too-large file patch. No comparison was made."
        if (
            not isinstance(raw.get("old_path"), str)
            or not isinstance(raw.get("new_path"), str)
            or not isinstance(raw.get("diff"), str)
            or not raw["diff"]
        ):
            return None, "A diff version contains an unusable file patch. No comparison was made."
        file_entry = dict(raw)
        for flag in ("new_file", "deleted_file", "renamed_file", "generated_file", "collapsed", "too_large"):
            file_entry[flag] = bool(raw.get(flag, False))
        validated.append(file_entry)
    return validated, ""


def _normalized_diff_signature(diff: str) -> str:
    """Ignore only numeric hunk ranges when comparing patches from different MR-relative bases."""
    return DIFF_HUNK_NORMALIZE_PATTERN.sub("@@ -0 +0 @@", diff)


def _snapshot_signature(raw: dict[str, Any]) -> tuple[Any, ...]:
    """Return the rename origin, status, and patch signature for one destination path."""
    return (
        raw["old_path"],
        raw["new_file"],
        raw["deleted_file"],
        raw["renamed_file"],
        raw["generated_file"],
        raw["collapsed"],
        raw["too_large"],
        _normalized_diff_signature(raw["diff"]),
    )


def _changed_snapshot_entries(
    previous: list[dict[str, Any]],
    latest: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], bool]], bool]:
    """Select changed latest files and old-only reverted files, keyed by destination path."""
    previous_by_path = {item["new_path"]: item for item in previous}
    latest_by_path = {item["new_path"]: item for item in latest}
    selected = [
        (item, False)
        for key, item in latest_by_path.items()
        if key not in previous_by_path or _snapshot_signature(previous_by_path[key]) != _snapshot_signature(item)
    ]
    reverted = [(item, True) for key, item in previous_by_path.items() if key not in latest_by_path]
    return selected + reverted, bool(reverted)


def _snapshot_file_stat(raw: dict[str, Any], *, reverted: bool = False) -> FileStat:
    """Build file-list counts and latest-snapshot anchors from one selected diff entry."""
    patch = "" if reverted else raw["diff"]
    additions = 0
    deletions = 0
    in_hunk = False
    for line in patch.splitlines():
        if DIFF_HUNK_PATTERN.match(line):
            in_hunk = True
        elif in_hunk:
            # File headers precede hunks; +++ and --- inside a hunk are real content changes.
            additions += line.startswith("+")
            deletions += line.startswith("-")
    return FileStat(
        path=raw["new_path"],
        additions=additions,
        deletions=deletions,
        new_line_ranges=_new_line_ranges(patch),
        ranges_complete=not reverted,
    )


def _since_cursor_start(cursor: str, since_sha: str, wanted: list[str]) -> dict[str, Any]:
    """Validate the since cursor inputs before requests and return its decoded payload."""
    payload = decode_cursor(cursor, "get_merge_request_diff")
    if (
        payload.get("mode") != "since"
        or payload.get("since_sha") != since_sha
        or payload.get("paths") != wanted
        or not is_int_at_least(payload.get("index"), 0)
        or not is_int_at_least(payload.get("version_id"), 0)
        or not full_sha(payload.get("head_sha"))
    ):
        raise InvalidCursorError("get_merge_request_diff")
    return payload


def _since_cursor(index: int, since_sha: str, wanted: list[str], head_sha: str, version_id: int) -> str:
    """Encode a bounded-scan cursor tied to its inputs and the current diff version."""
    return encode_cursor(
        {
            "mode": "since",
            "index": index,
            "since_sha": since_sha,
            "paths": wanted,
            "head_sha": head_sha,
            "version_id": version_id,
        },
    )


def _scan_since_entries(  # noqa: PLR0913, PLR0917 - all values bind the resumable cursor.
    entries: list[tuple[dict[str, Any], bool]],
    wanted: set[str],
    start: int,
    since_sha: str,
    wanted_order: list[str],
    head_sha: str,
    version_id: int,
) -> tuple[list[FileDiff], str, set[str]]:
    """Return one budgeted snapshot page and a cursor index for any remaining entries."""
    diffs: list[FileDiff] = []
    matched: set[str] = set()
    total = 0
    stop = min(len(entries), start + MAX_PAGES * DIFF_PAGE_SIZE)
    index = start
    while index < stop:
        raw, reverted = entries[index]
        index += 1
        hits = _requested_hits(raw, wanted)
        if not hits:
            continue
        file_entry = {**raw, "diff": ""} if reverted else raw
        diff = _to_file_diff(file_entry)
        if diffs and total + len(diff.diff) > DIFF_BUDGET:
            index -= 1
            return diffs, _since_cursor(index, since_sha, wanted_order, head_sha, version_id), matched
        diffs.append(diff)
        total += len(diff.diff)
        matched |= hits
    if index < len(entries):
        return diffs, _since_cursor(index, since_sha, wanted_order, head_sha, version_id), matched
    return diffs, "", matched


async def _get_since_diff(  # noqa: C901, PLR0911, PLR0913, PLR0917 - keep comparison flow in one place.
    client: GitLabClient,
    project: str,
    iid: int,
    since_sha: str,
    wanted: list[str],
    cursor: str | None,
) -> MergeRequestDiff:
    """Compare two complete diff-version snapshots and return changed MR-relative files."""
    if not full_sha(since_sha):
        raise ToolError(INVALID_SINCE_SHA_MESSAGE)
    cursor_payload = _since_cursor_start(cursor, since_sha, wanted) if cursor is not None else None
    if cursor is not None and not wanted:
        raise InvalidCursorError("get_merge_request_diff")

    versions, complete = await _scan_diff_versions(client, project, iid)
    if not complete:
        return MergeRequestDiff(
            since_version_found=None,
            warning=f"Diff version scan stopped after {MAX_PAGES} pages or returned an unusable page.",
        )
    if any(_version_id(version) is None or not full_sha(version.get("head_commit_sha")) for version in versions):
        return MergeRequestDiff(
            since_version_found=None,
            warning="GitLab returned diff versions without usable IDs or commit SHAs.",
        )
    latest_version = max(versions, key=lambda version: version["id"], default=None)
    if latest_version is None:
        return MergeRequestDiff(
            since_version_found=False,
            warning="GitLab returned no diff versions, so the requested commit could not be compared.",
        )

    latest_id = latest_version["id"]
    latest_sha = latest_version["head_commit_sha"]
    if cursor_payload is not None and (
        cursor_payload["head_sha"] != latest_sha or cursor_payload["version_id"] != latest_id
    ):
        raise InvalidCursorError("get_merge_request_diff")

    matching_versions = [version for version in versions if version["head_commit_sha"] == since_sha]
    if not matching_versions:
        return MergeRequestDiff(
            head_sha=latest_sha,
            since_version_found=False,
            warning=f"No diff version has head commit {since_sha}; no full diff was returned.",
        )
    previous_version = max(matching_versions, key=lambda version: version["id"])
    previous_id = previous_version["id"]

    previous_detail = await _get_version_detail(client, project, iid, previous_id)
    if previous_id == latest_id:
        latest_detail = previous_detail
    else:
        latest_detail = await _get_version_detail(client, project, iid, latest_id)
    previous_files, warning = _validate_version_snapshot(previous_detail, since_sha)
    if previous_files is None:
        return MergeRequestDiff(head_sha=latest_sha, since_version_found=True, warning=warning)
    latest_files, warning = _validate_version_snapshot(latest_detail, latest_sha)
    if latest_files is None:
        return MergeRequestDiff(head_sha=latest_sha, since_version_found=True, warning=warning)

    entries, has_reverted = _changed_snapshot_entries(previous_files, latest_files)
    warning = (
        "Files absent from the current MR diff are returned with empty patches and no valid anchors."
        if has_reverted
        else ""
    )
    if not wanted:
        return MergeRequestDiff(
            head_sha=latest_sha,
            since_version_found=True,
            files=[_snapshot_file_stat(raw, reverted=reverted) for raw, reverted in entries],
            warning=warning,
        )

    start = cursor_payload["index"] if cursor_payload is not None else 0
    diffs, next_cursor, matched = _scan_since_entries(
        entries,
        set(wanted),
        start,
        since_sha,
        wanted,
        latest_sha,
        latest_id,
    )
    not_found = [path for path in wanted if path not in matched] if cursor is None and not next_cursor else []
    return MergeRequestDiff(
        head_sha=latest_sha,
        since_version_found=True,
        diffs=diffs,
        next_cursor=next_cursor,
        paths_not_found=not_found,
        warning=warning,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_merge_request_diff(  # noqa: PLR0913, PLR0917 - these are the public MCP tool arguments.
    ctx: Context,
    project: Project,
    iid: Iid,
    paths: Annotated[
        list[str] | None,
        Field(description="Paths to read patches for, old or new. Omit for the file list."),
    ] = None,
    cursor: Annotated[str | None, Field(description="next_cursor from an earlier call, with the same paths.")] = None,
    since_sha: Annotated[
        str | None,
        Field(description="Full commit SHA of an earlier merge request diff version to compare against."),
    ] = None,
) -> MergeRequestDiff:
    """Review inline comment anchors and patches.

    Like `glab mr diff`, this lists changed files or reads patches of chosen files.
    - Call without `paths` for a file list with +/- counts and `new_line_ranges`, then with `paths` for patches.
    - Ranges include context lines and are candidate new-side anchors. `ranges_complete` is false if a patch was not
      scanned or GitLab did not provide usable patch text. A complete empty range list means no new-side hunk lines.
    - Ranges are not proof that a line is a valid comment position. Use `post_review_notes(validate_only=true)` to
      validate exact patch positions; its result is the final authority before posting.
    - `head_sha` changes when new commits are pushed, so older patches may be stale.
    - `status`: added, deleted, renamed, or modified. `old_path` is only set when it differs.
    - `collapsed`: hidden as big, the web UI may show it. `too_large`: never shown. `generated`: usually skippable.
    - Up to 40000 patch characters and 5 pages per call. Cut patches have `truncated`. Next: `next_cursor`, same paths.
    - `paths_not_found` is only set after a full scan from the first page. GitLab limits can still hide a file.
    - With `since_sha`, compare complete GitLab diff-version snapshots. Patches still use the latest MR target-side
      base, not a commit-to-commit diff. Numeric hunk ranges are normalized only for deciding whether a patch changed.
      `since_version_found` is a boolean when known; it is omitted with a warning if the version scan is incomplete.
      Incomplete snapshots return a warning instead of claiming no changes. Reverted files use empty patches and no
      valid anchors. Files are identified by destination path; current entries match only their latest old/new paths,
      not historical rename origins. Resume with the same `since_sha` and paths; cursors are rejected when the latest
      head changes.
    """
    client = get_client(ctx)
    wanted = list(dict.fromkeys(paths)) if paths else []
    if since_sha is not None:
        return await _get_since_diff(client, project, iid, since_sha, wanted, cursor)
    # Decode the cursor before any request, so a bad one fails fast. The file list ignores it.
    start = _diff_cursor_start(cursor) if wanted and cursor is not None else (1, 0)
    node = await fetch_merge_request(client, DIFF_FILES_QUERY, project, iid)
    head_sha = diff_refs_head_sha(node["diffRefs"])

    if not wanted:
        stats = node["diffStats"]
        patch_ranges = await _scan_file_stat_ranges(client, project, iid, stats) if stats else {}
        files = _to_file_stats(stats, patch_ranges)
        return MergeRequestDiff(head_sha=head_sha, files=files, hint=FILE_LIST_HINT if files else "")

    diffs, next_cursor, matched = await _scan_diffs(client, project, iid, set(wanted), start)
    # A resumed call did not read the first page, so it cannot prove a path is missing.
    not_found = [path for path in wanted if path not in matched] if cursor is None and not next_cursor else []
    return MergeRequestDiff(head_sha=head_sha, diffs=diffs, next_cursor=next_cursor, paths_not_found=not_found)
