"""Merge request review thread tools: read, count, and reply to threads 🧵."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, model_validator
lazy from fastmcp import Context

from gitlab_mcp import findings as _findings
from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError, GitLabGraphQLError
from gitlab_mcp.common import (
    HTTP_REQUEST_TIMEOUT,
    HTTP_SERVER_ERROR,
    MAX_BATCH_MERGE_REQUESTS,
    MAX_PAGES,
    Iid,
    PositiveIid,
    Project,
    bare_id,
    decode_cursor,
    encode_cursor,
    is_int_at_least,
    lower_or_empty,
)
from gitlab_mcp.errors import InvalidCursorError, MergeRequestNotFoundError
from gitlab_mcp.mr_common import (
    ThreadLocation,
    author_display,
    diff_refs_head_sha,
    fetch_merge_request,
    mr_rest_path,
    position_fields,
)

# One page of discussions with their notes, for get_merge_request_threads. 🧵
THREADS_QUERY = """
query($fullPath: ID!, $iid: String!, $after: String, $includeMrStatus: Boolean!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      sourceBranch @include(if: $includeMrStatus)
      diffRefs @include(if: $includeMrStatus) { headSha }
      detailedMergeStatus @include(if: $includeMrStatus)
      headPipeline @include(if: $includeMrStatus) { status }
      discussions(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id resolvable resolved
          notes(first: 50) {
            pageInfo { hasNextPage }
            nodes {
              id system body createdAt
              author { username bot name id }
              position { oldPath newPath oldLine newLine }
            }
          }
        }
      }
    }
  }
}
"""

# Discussion IDs and resolution flags only, for resolve_open_threads. 🔓
OPEN_THREAD_IDS_QUERY = """
query($fullPath: ID!, $iid: String!, $after: String) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      discussions(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id resolvable resolved }
      }
    }
  }
}
"""

# Exact thread totals without fetching discussion pages or note bodies. 🧮
THREAD_COUNTS_QUERY = """
query($fullPath: ID!, $iid: String!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      resolvableDiscussionsCount
      resolvedDiscussionsCount
    }
  }
}
"""

# Compact approval, auto-merge, and discussion status for a reply batch. 📊
THREAD_REPLY_STATUS_QUERY = """
query($fullPath: ID!, $iid: String!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      approved
      approvalsLeft
      autoMergeStrategy
      resolvableDiscussionsCount
      resolvedDiscussionsCount
    }
  }
}
"""

# CE does not expose approvalsLeft on MergeRequest. Use only after its explicit unknown-field error. 🧩
THREAD_REPLY_STATUS_WITHOUT_APPROVALS_QUERY = """
query($fullPath: ID!, $iid: String!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      approved
      autoMergeStrategy
      resolvableDiscussionsCount
      resolvedDiscussionsCount
    }
  }
}
"""


# Long note bodies are cut to this many characters, then marked with THREAD_CUT_MARKER.
NOTE_CAP = 4000
THREAD_CUT_MARKER = " [truncated]"
# Do not let a malformed marker consume a later HTML comment.
REVIEW_BOT_FINDING_PATTERN = re.compile(r"<!-- review-bot:finding\s(?:(?!-->|<!--).)*-->\s*$", re.DOTALL)
# Note, DiffNote, and DiscussionNote GIDs. Group 1 is the REST note ID.
NOTE_GID_PATTERN = re.compile(r"gid://gitlab/(?:Note|DiffNote|DiscussionNote)/([0-9]+)")
# Serialized character budget for full and finding thread results.
THREAD_BODY_BUDGET = 30000
# Serialized character budget for compact heading results.
THREAD_HEADING_BUDGET = 30000
# Headings are capped so one unusually long note cannot dominate a compact result.
THREAD_HEADING_CAP = 200


# "comment" means the discussion cannot be resolved, like a plain MR comment.
ThreadStatus = Literal["open", "resolved", "comment"]
ThreadState = Literal["open", "resolved", "comments", "all"]
ThreadView = Literal["full", "headings", "finding"]
# Maps each state filter to the thread statuses it keeps. "all" keeps every status.
STATE_STATUSES: dict[ThreadState, frozenset[ThreadStatus]] = {
    "open": frozenset({"open"}),
    "resolved": frozenset({"resolved"}),
    "comments": frozenset({"comment"}),
    "all": frozenset({"open", "resolved", "comment"}),
}


class Note(BaseModel):
    """One human note."""

    note_id: int  # REST-compatible note ID.
    body_hash: str  # SHA-256 of the untouched UTF-8 body.
    by: str = Field(default="", exclude_if=lambda v: not v)  # Empty when the author was deleted or banned.
    body: str  # Cut at the requested max_body_chars, then marked with THREAD_CUT_MARKER.
    at: str  # Date only, YYYY-MM-DD.


class Thread(ThreadLocation):
    """A review thread."""

    id: str
    status: ThreadStatus
    notes: list[Note]
    notes_truncated: bool = Field(default=False, exclude_if=lambda v: not v)  # More than 50 notes exist.


class ThreadHeading(BaseModel):
    """A compact thread summary with its root heading and latest fetched human note."""

    id: str
    status: ThreadStatus
    path: str = Field(default="", exclude_if=lambda v: not v)
    line: int = Field(default=0, exclude_if=lambda v: not v)
    old_line: int = Field(default=0, exclude_if=lambda v: not v)
    by: str = Field(default="", exclude_if=lambda v: not v)
    heading: str
    last_by: str
    last_at: str  # Date only, YYYY-MM-DD.
    note_count: int  # Human notes in the fetched page, not necessarily the full thread.
    notes_truncated: bool = Field(default=False, exclude_if=lambda v: not v)


class ThreadList(BaseModel):
    """One page of threads with compact merge request triage status."""

    head_sha: str = Field(default="", exclude_if=lambda v: not v)
    source_branch: str = Field(default="", exclude_if=lambda v: not v)
    merge_status: str = Field(default="", exclude_if=lambda v: not v)
    pipeline_status: str = Field(default="", exclude_if=lambda v: not v)
    threads: list[Thread | ThreadHeading]
    next_cursor: str = Field(default="", exclude_if=lambda v: not v)  # Empty when the scan finished.
    not_found_ids: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)


class MergeRequestThreadList(ThreadList):
    """One merge request's thread page in a batch result."""

    iid: int


class MergeRequestThreadBatch(BaseModel):
    """Thread pages for requested merge requests and the IIDs that were missing."""

    merge_requests: list[MergeRequestThreadList]
    missing_iids: list[int] = Field(default_factory=list, exclude_if=lambda v: not v)


class ThreadCounts(BaseModel):
    """Exact open and resolved discussion counts for one merge request."""

    open: int
    resolved: int


class MergeRequestThreadCounts(BaseModel):
    """Exact discussion counts attributed to one merge request."""

    iid: int
    open: int
    resolved: int


class MergeRequestThreadCountsBatch(BaseModel):
    """Thread counts for requested merge requests and the IIDs that were missing."""

    merge_requests: list[MergeRequestThreadCounts]
    missing_iids: list[int] = Field(default_factory=list, exclude_if=lambda v: not v)


class ThreadReplyResult(BaseModel):
    """The result of replying to or resolving one discussion thread."""

    thread_id: str
    note_id: int | None = Field(default=None, exclude_if=lambda value: value is None)
    resolved: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    # Set only after a successful resolve. The MR's open thread count after the write, or None if it could not be read.
    open_threads: int | None = Field(default=None, exclude_if=lambda value: value is None)
    warning: str = Field(default="", exclude_if=lambda value: not value)


class ThreadReplyRequest(BaseModel):
    """One validated reply or resolution in a batch."""

    iid: Iid = Field(description="Merge request IID containing this thread.")
    thread_id: str = Field(description="Discussion ID, or a full GitLab discussion GID.")
    body: str | None = Field(default=None, description="Reply text. Omit only when resolving the thread.")
    resolve: bool = Field(default=False, description="Resolve after replying, or resolve without a reply.")

    @model_validator(mode="after")
    def validate_reply(self) -> ThreadReplyRequest:
        """Reject malformed entries before any batch item can be written."""
        if not bare_id(self.thread_id).strip():
            raise ValueError("thread_id must not be empty")  # noqa: TRY003
        if self.body is not None and not self.body.strip():
            raise ValueError("body must not be empty or whitespace")  # noqa: TRY003
        if self.body is None and not self.resolve:
            raise ValueError("Provide a reply body or set resolve=True")  # noqa: TRY003
        return self


ThreadReplyBatchStatus = Literal["applied", "partial", "failed", "unknown"]


class ThreadReplyBatchItem(BaseModel):
    """The compact outcome for one requested thread reply."""

    iid: int
    thread_id: str
    status: ThreadReplyBatchStatus
    resolved: bool | None
    note_id: int | None = Field(default=None, exclude_if=lambda value: value is None)
    # Sent once per MR, on its last item with resolve=true, when that resolution succeeded.
    open_threads: int | None = Field(default=None, exclude_if=lambda value: value is None)
    warning: str = Field(default="", exclude_if=lambda value: not value)


class MergeRequestReplyStatus(BaseModel):
    """Best-effort status summary for one merge request after a reply batch."""

    iid: int
    open_threads: int | None = Field(default=None, exclude_if=lambda value: value is None)
    approved: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    approvals_left: int | None = Field(default=None, exclude_if=lambda value: value is None)
    auto_merge_strategy: str | None = Field(default=None, exclude_if=lambda value: value is None)
    warning: str = Field(default="", exclude_if=lambda value: not value)


class ThreadReplyBatchResult(BaseModel):
    """Ordered write outcomes and post-write status summaries for a reply batch."""

    results: list[ThreadReplyBatchItem]
    merge_requests: list[MergeRequestReplyStatus]


class ResolveOpenThreadsResult(BaseModel):
    """The outcome of resolving every open thread on one merge request."""

    resolved: list[str]  # Bare thread IDs, in the order GitLab listed them.
    problems: list[ThreadReplyBatchItem] = Field(default_factory=list, exclude_if=lambda value: not value)
    # The MR's open thread count after the writes, or None if it could not be read.
    open_threads: int | None = Field(default=None, exclude_if=lambda value: value is None)
    # False means more than MAX_PAGES pages of discussions exist. Run the tool again after these resolve.
    complete: bool = Field(default=True, exclude_if=lambda value: value)


def _bare_ids(ids: list[str]) -> list[str]:
    """Return bare discussion SHAs in input order, without duplicates. Full GIDs and bare SHAs both work."""
    return list(dict.fromkeys(bare_id(thread_id) for thread_id in ids))


def _thread_cursor_start(cursor: str) -> tuple[str | None, int]:
    """Return the page cursor and skip count for a threads call. Anything else is an InvalidCursorError."""
    payload = decode_cursor(cursor, "get_merge_request_threads")
    after = payload.get("after")
    if not (after is None or isinstance(after, str)) or not is_int_at_least(payload.get("skip"), 0):
        raise InvalidCursorError("get_merge_request_threads")
    return after, payload["skip"]


def _thread_status(discussion: dict[str, Any]) -> ThreadStatus:
    """Return the status of a discussion. Only resolvable threads can be open or resolved."""
    if not discussion["resolvable"]:
        return "comment"
    return "resolved" if discussion["resolved"] else "open"


def _thread_statuses(state: ThreadState | list[ThreadState]) -> frozenset[ThreadStatus]:
    """Return the thread statuses a state filter keeps. A list is the union of its states, so duplicates collapse.

    An empty list raises ToolError, because it would otherwise match nothing or everything by accident.
    """
    states = [state] if isinstance(state, str) else state
    if not states:
        raise ToolError("Pass at least one state.")  # noqa: TRY003
    return frozenset(status for item in states for status in STATE_STATUSES[item])


def _cap_note_body(body: str, max_body_chars: int = NOTE_CAP) -> str:
    """Remove a trailing review finding, then cap the remaining note body."""
    body = _strip_trailing_finding(body)
    if max_body_chars == 0:
        return ""
    if len(body) <= max_body_chars:
        return body
    return body[:max_body_chars] + THREAD_CUT_MARKER


def _strip_trailing_finding(body: str) -> str:
    """Remove a well-formed trailing review-bot finding marker from a note body."""
    finding = REVIEW_BOT_FINDING_PATTERN.search(body)
    return body[: finding.start()].rstrip() if finding else body


def _thread_heading(body: str) -> str:
    """Return the first Markdown heading, or the first nonempty line, capped at 200 characters."""
    body = _strip_trailing_finding(body)
    markdown_heading = re.search(r"(?m)^[ \t]{0,3}#{1,6}(?:[ \t]+|$)(.*)$", body)
    if markdown_heading:
        heading = re.sub(r"[ \t]+#+[ \t]*$", "", markdown_heading.group(1)).strip()
        if heading:
            return heading[:THREAD_HEADING_CAP]
    return next((line.strip()[:THREAD_HEADING_CAP] for line in body.splitlines() if line.strip()), "")


def _note_id(value: Any) -> int:
    """Convert a valid GitLab Note, DiffNote, or DiscussionNote GID to its positive REST ID."""
    match = NOTE_GID_PATTERN.fullmatch(value) if isinstance(value, str) else None
    try:
        note_id = int(match.group(1)) if match else 0
    except ValueError:
        # Python refuses to convert very long digit strings, so treat them as invalid IDs.
        note_id = 0
    if note_id <= 0:
        raise ToolError("GitLab returned an invalid note ID.")  # noqa: TRY003
    return note_id


def _to_note(node: dict[str, Any], max_body_chars: int = NOTE_CAP, compact_findings: bool = False) -> Note:
    """Convert one GraphQL note into a note with a capped body and a date-only timestamp."""
    author = node["author"]  # A deleted or banned author comes back as null.
    body = node["body"]
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if compact_findings and author is not None and author.get("bot") is True:
        body = _findings.compact_finding_body(body)
    return Note(
        note_id=_note_id(node.get("id")),
        body_hash=body_hash,
        by=author_display(author),
        body=_cap_note_body(body, max_body_chars=max_body_chars),
        at=node["createdAt"][:10],  # ISO 8601 timestamp, keep the date part.
    )


def _to_thread(
    discussion: dict[str, Any],
    max_body_chars: int = NOTE_CAP,
    compact_findings: bool = False,
) -> Thread | None:
    """Convert one discussion into a thread, or return None when it has no human notes.

    System notes, like "assigned to @user", are dropped. The location comes
    from the first human note, because a system note has no position.
    """
    notes = discussion["notes"]
    human = [node for node in notes["nodes"] if not node["system"]]
    if not human:
        return None
    return Thread(
        id=bare_id(discussion["id"]),
        status=_thread_status(discussion),
        notes=[_to_note(node, max_body_chars=max_body_chars, compact_findings=compact_findings) for node in human],
        # GitLab only returns the first 50 notes, so more may exist.
        notes_truncated=notes["pageInfo"]["hasNextPage"],
        **position_fields(human[0]["position"]),
    )


def _to_thread_heading(discussion: dict[str, Any], thread: Thread) -> ThreadHeading:
    """Convert a thread to a compact summary of its fetched human notes."""
    notes = discussion["notes"]
    human_notes = [note for note in notes["nodes"] if not note["system"]]
    root_note = human_notes[0]
    # GitLab can return deleted authors as null. The newest timestamp, not list order, determines the last note.
    last_note = max(
        human_notes,
        key=lambda note: datetime.fromisoformat(note["createdAt"].replace("Z", "+00:00").replace("z", "+00:00")),
    )
    author = root_note["author"]  # A deleted or banned author comes back as null.
    last_author = last_note["author"]
    return ThreadHeading(
        id=thread.id,
        status=thread.status,
        path=thread.path,
        line=thread.line,
        old_line=thread.old_line,
        by=author_display(author),
        heading=_thread_heading(root_note["body"]),
        last_by=author_display(last_author),
        last_at=last_note["createdAt"][:10],  # ISO 8601 timestamp, keep the date part.
        note_count=len(human_notes),
        notes_truncated=notes["pageInfo"]["hasNextPage"],
    )


@dataclass
class _ThreadFilter:
    """The filters of one call. A thread must pass all of them."""

    statuses: frozenset[ThreadStatus]
    ids: list[str] | None
    file: str | None
    author: str | None

    def matches(self, thread: Thread, discussion: dict[str, Any]) -> bool:
        """Return True when the thread passes every filter."""
        # Explicit IDs replace the state filter, so a known thread is returned in any state.
        if self.ids is not None:
            if thread.id not in self.ids:
                return False
        elif thread.status not in self.statuses:
            return False
        if self.file is not None and self.file not in (thread.path, thread.old_path):
            return False
        # Only the first 50 notes are fetched, so this can miss a late reply.
        if self.author is None:
            return True
        return any(
            not note["system"] and note["author"] is not None and note["author"]["username"] == self.author
            for note in discussion["notes"]["nodes"]
        )


async def _scan_threads(  # noqa: PLR0913, PLR0917 - each input describes the scan.
    client: GitLabClient,
    project: str,
    iid: int,
    thread_filter: _ThreadFilter,
    start: tuple[str | None, int],
    max_body_chars: int = NOTE_CAP,
    view: ThreadView = "full",
    include_mr_status: bool = True,
) -> tuple[list[Thread | ThreadHeading], str, set[str], dict[str, str]]:
    """Read discussion pages from `start` and collect the matching threads.

    The scan stops at the budget, at `MAX_PAGES`, or at the last page. Returns
    the threads, the cursor to resume from (empty when every page was read),
    every discussion ID that was read, matching or not, and compact MR status when requested.
    """
    after, skip = start
    threads: list[Thread | ThreadHeading] = []
    seen: set[str] = set()
    mr_headers: dict[str, str] = {}
    full_view = view in {"full", "finding"}
    output_size = 2  # Count the serialized list brackets.
    for _ in range(MAX_PAGES):
        node = await fetch_merge_request(
            client,
            THREADS_QUERY,
            project,
            iid,
            after=after,
            includeMrStatus=include_mr_status,
        )
        mr_headers = _thread_mr_headers(node) if include_mr_status else {}
        connection = node["discussions"]
        nodes = connection["nodes"]
        for index in range(skip, len(nodes)):
            seen.add(bare_id(nodes[index]["id"]))
            thread = _to_thread(
                nodes[index],
                max_body_chars=max_body_chars,
                compact_findings=view == "finding",
            )
            if thread is None or not thread_filter.matches(thread, nodes[index]):
                continue
            output_thread = thread if full_view else _to_thread_heading(nodes[index], thread)
            size = len(output_thread.model_dump_json()) + (1 if threads else 0)
            budget = THREAD_BODY_BUDGET if full_view else THREAD_HEADING_BUDGET
            # Always return at least one thread, even when that thread alone is over budget.
            if threads and output_size + size > budget:
                return threads, encode_cursor({"after": after, "skip": index}), seen, mr_headers
            threads.append(output_thread)
            output_size += size
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return threads, "", seen, mr_headers
        after, skip = page_info["endCursor"], 0
    # The page limit was hit. `after` is now the cursor of the next unread page.
    return threads, encode_cursor({"after": after, "skip": 0}), seen, mr_headers


def _thread_mr_headers(node: dict[str, Any]) -> dict[str, str]:
    """Convert compact MR status fields returned with a discussion page."""
    pipeline = node.get("headPipeline")
    return {
        "head_sha": diff_refs_head_sha(node.get("diffRefs")),
        "source_branch": node.get("sourceBranch") or "",
        "merge_status": lower_or_empty(node.get("detailedMergeStatus")),
        "pipeline_status": lower_or_empty(pipeline.get("status")) if pipeline else "",
    }


def _to_thread_counts(node: dict[str, Any], iid: int) -> ThreadCounts:
    """Convert GitLab's exact discussion totals, rejecting absent or invalid fields."""
    total = node.get("resolvableDiscussionsCount")
    resolved = node.get("resolvedDiscussionsCount")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or not isinstance(resolved, int)
        or isinstance(resolved, bool)
    ):
        raise ToolError(f"GitLab did not return discussion counts for merge request !{iid}.")  # noqa: TRY003
    return ThreadCounts(open=total - resolved, resolved=resolved)


async def _get_thread_counts(
    client: GitLabClient,
    project: str,
    iid: int | None,
    iids: list[int] | None,
) -> ThreadCounts | MergeRequestThreadCountsBatch:
    """Fetch exact thread counts for one MR or a batch, retaining existing missing-IID behavior."""
    if iid is not None:
        node = await fetch_merge_request(client, THREAD_COUNTS_QUERY, project, iid)
        return _to_thread_counts(node, iid)

    unique_iids = list(dict.fromkeys(iids or []))
    if not unique_iids:
        raise ToolError("Pass at least one merge request IID.")  # noqa: TRY003
    if len(unique_iids) > MAX_BATCH_MERGE_REQUESTS:
        raise ToolError("Pass no more than 20 unique merge request IIDs.")  # noqa: TRY003

    merge_requests: list[MergeRequestThreadCounts] = []
    missing_iids: list[int] = []
    for requested_iid in unique_iids:
        try:
            node = await fetch_merge_request(client, THREAD_COUNTS_QUERY, project, requested_iid)
        except MergeRequestNotFoundError:
            missing_iids.append(requested_iid)
            continue
        counts = _to_thread_counts(node, requested_iid)
        merge_requests.append(MergeRequestThreadCounts(iid=requested_iid, open=counts.open, resolved=counts.resolved))
    return MergeRequestThreadCountsBatch(merge_requests=merge_requests, missing_iids=missing_iids)


# FastMCP wraps a model union in `result`, which changes both established structured response shapes.
@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_merge_request_threads(  # noqa: C901, PLR0913, PLR0917 - filters and output modes share this dispatch.
    ctx: Context,
    project: Project,
    iid: Iid | None = None,
    state: Annotated[
        ThreadState | list[ThreadState],
        Field(description="open (default), resolved, comments, or all. A list combines states, but cannot be empty."),
    ] = "open",
    ids: Annotated[
        list[str] | None,
        Field(description="Thread IDs from threads.open. Single iid only. Ignores state."),
    ] = None,
    file: Annotated[str | None, Field(description="Only threads on this path, old or new.")] = None,
    author: Annotated[str | None, Field(description="Only threads with a note by this user.")] = None,
    view: Annotated[
        ThreadView,
        Field(description="`full` notes, compact root-note `headings`, or bot finding bodies in `finding` view."),
    ] = "full",
    include_mr_status: Annotated[
        bool,
        Field(description="Include MR head SHA, source branch, merge status, and pipeline status in the result."),
    ] = True,
    max_body_chars: Annotated[
        int,
        Field(ge=0, le=NOTE_CAP, description="Maximum body characters per note. Zero omits bodies."),
    ] = NOTE_CAP,
    cursor: Annotated[
        str | None,
        Field(description="next_cursor from an earlier call. Repeat the filters, view, and max_body_chars."),
    ] = None,
    iids: Annotated[
        list[PositiveIid] | None,
        Field(description="Batch of 1 to 20 unique positive merge request IIDs. Duplicates keep first-request order."),
    ] = None,
    count_only: Annotated[
        bool,
        Field(
            description="Return exact open/resolved counts only. Counts both states and fetches no discussion notes."
        ),
    ] = False,
) -> Any:
    """Read compact MR triage status and review thread notes for one MR or a batch, like `glab mr note list`.

    For triage, start with `view="headings"`, then pass selected thread IDs as `ids=[...]` with `view="full"` to read
    their note bodies.

    - `state`: `open` (default), `resolved`, `comments` (cannot be resolved), or `all`. `ids` ignore `state`.
    - `count_only=true` returns exact counts from GitLab's resolvable and resolved discussion totals. `open` is total
      minus resolved. It ignores `state`, counts both states, and fetches no discussion notes. It cannot be combined
      with `cursor`, `ids`, `file`, or `author`. Batch results contain `iid`, `open`, and `resolved` per MR.
    - `ids` can only be used with a single `iid`. It is rejected with `iids` because a thread ID can match multiple
      merge requests in a batch.
    - Results include the head SHA, source branch, merge status, and pipeline status when available. Use
      `include_mr_status=false` to omit and skip querying those fields. Use it when also fetching MR details with
      `get_merge_request`, which returns status and detail fields.
    - Pass exactly one of `iid` or `iids`. Batch `iids` are deduplicated in request order and allow at most 20
      unique positive values. Missing merge requests appear in `missing_iids`.
    - System notes and threads with no human notes are skipped.
    - `line` is the new-file line. `old_line` replaces it on a deleted line.
    - `notes_truncated` means the notes page has another page. Heading `note_count`, `last_by`, and `last_at` only
      cover fetched human notes when this is true, and the `author` filter can miss a late reply.
    - Full and finding notes include a REST-compatible `note_id` and `body_hash`, the SHA-256 hex digest of the full,
      untouched UTF-8 body before removing markers, compacting findings, or truncating text.
    - Trailing `review-bot:finding` comments are removed.
    - `view="headings"` returns thread details, the root human note's first Markdown heading (or first nonempty line),
      the latest fetched human note's author and date, and the number of fetched human notes. Headings are capped at
      200 characters. It includes no note bodies or replies.
    - `view="finding"` returns full notes like `full`, but compacts recognized bot findings. Numbered findings lose
      separate greetings and signoffs. Section findings keep the bold title and the "Needs to be improved", "Failure
      scenario", and "How to fix" sections, including their code and diagrams. A bold-heading action-bullet format
      loses its introductory paragraph. Human notes and unrecognized formats remain unchanged.
    - In full and finding views, note bodies are cut at `max_body_chars` (default 4000, maximum 4000). Zero omits
      body text. Each view returns about 30000 serialized thread characters, unless the first thread exceeds the budget.
      Continue with `next_cursor` using the same filters, `view`, and `max_body_chars`.
    - `not_found_ids` is only set after a full scan from the first page.
    - Each batch result includes its own `iid`, `threads`, `next_cursor`, and `not_found_ids`. Batch cursors are not
      accepted: resume a result with a single `iid` call and that result's cursor. Every merge request gets its own
      existing scan and output budget, so batch output can use up to 20 times the single-merge-request budget.
    - Pass a state list such as `["open", "comments"]` to combine states. Duplicates are ignored, and `all` keeps every
      status. An empty list is an error, including in ID and count-only modes. Valid states are ignored in those modes.
    """
    if (iid is None) == (iids is None):
        raise ToolError("Pass exactly one of iid or iids.")  # noqa: TRY003

    statuses = _thread_statuses(state)
    if count_only:
        if cursor is not None or ids is not None or file is not None or author is not None:
            raise ToolError("count_only cannot be combined with cursor, ids, file, or author.")  # noqa: TRY003
        return await _get_thread_counts(get_client(ctx), project, iid, iids)

    # An empty list means no IDs were given, so the state filter applies.
    wanted = _bare_ids(ids) if ids else None
    thread_filter = _ThreadFilter(statuses=statuses, ids=wanted, file=file, author=author)
    if iid is not None:
        client = get_client(ctx)
        start = _thread_cursor_start(cursor) if cursor else (None, 0)
        threads, next_cursor, seen, mr_headers = await _scan_threads(
            client,
            project,
            iid,
            thread_filter,
            start,
            max_body_chars,
            view,
            include_mr_status,
        )
        # A resumed call did not read the first page, so it cannot prove an ID is missing.
        not_found_ids: list[str] = []
        if wanted and cursor is None and not next_cursor:
            not_found_ids = [thread_id for thread_id in wanted if thread_id not in seen]
        return ThreadList(
            **mr_headers,
            threads=threads,
            next_cursor=next_cursor,
            not_found_ids=not_found_ids,
        )

    if cursor is not None:
        raise ToolError("Batch thread calls do not accept cursor. Resume with a single iid call.")  # noqa: TRY003
    if ids is not None:
        raise ToolError("ids can only be used with a single iid, not iids.")  # noqa: TRY003
    unique_iids = list(dict.fromkeys(iids or []))
    if not unique_iids:
        raise ToolError("Pass at least one merge request IID.")  # noqa: TRY003
    if len(unique_iids) > MAX_BATCH_MERGE_REQUESTS:
        raise ToolError("Pass no more than 20 unique merge request IIDs.")  # noqa: TRY003

    client = get_client(ctx)
    merge_requests: list[MergeRequestThreadList] = []
    missing_iids: list[int] = []
    for requested_iid in unique_iids:
        try:
            threads, next_cursor, _, mr_headers = await _scan_threads(
                client,
                project,
                requested_iid,
                thread_filter,
                (None, 0),
                max_body_chars,
                view,
                include_mr_status,
            )
        except MergeRequestNotFoundError:
            missing_iids.append(requested_iid)
            continue

        merge_requests.append(
            MergeRequestThreadList(
                **mr_headers,
                iid=requested_iid,
                threads=threads,
                next_cursor=next_cursor,
            ),
        )
    return MergeRequestThreadBatch(merge_requests=merge_requests, missing_iids=missing_iids)


async def _read_open_threads(client: GitLabClient, project: str, iid: int) -> int | None:
    """Return the MR's open thread count, or None when the read fails.

    The write before this read already succeeded, so a failed read must not fail the write result. Callers then leave
    the field out. The totals match `get_merge_request_threads(count_only=true)`.
    """
    try:
        node = await fetch_merge_request(client, THREAD_COUNTS_QUERY, project, iid)
        return _to_thread_counts(node, iid).open
    except ToolError:
        return None


def _is_unknown_approvals_left_field(error: GitLabGraphQLError) -> bool:
    """Return True only for known GraphQL wording that says MergeRequest lacks approvalsLeft."""
    message = str(error)
    return (
        "Field 'approvalsLeft' doesn't exist on type 'MergeRequest'" in message
        or "Cannot query field 'approvalsLeft' on type 'MergeRequest'" in message
    )


async def _read_thread_reply_status(  # noqa: C901 - fallback and optional status fields have distinct recovery paths.
    client: GitLabClient,
    project: str,
    iid: int,
) -> MergeRequestReplyStatus:
    """Read one MR's post-write approval, auto-merge, and exact thread status without failing successful writes."""
    approvals_left_supported = True
    try:
        try:
            node = await fetch_merge_request(client, THREAD_REPLY_STATUS_QUERY, project, iid)
        except GitLabGraphQLError as exc:
            if not _is_unknown_approvals_left_field(exc):
                raise
            approvals_left_supported = False
            node = await fetch_merge_request(client, THREAD_REPLY_STATUS_WITHOUT_APPROVALS_QUERY, project, iid)
    except ToolError as exc:
        return MergeRequestReplyStatus(iid=iid, warning=f"Could not read status for merge request !{iid}: {exc}")

    warnings: list[str] = []
    if not approvals_left_supported:
        warnings.append("This GitLab schema does not support approvalsLeft; approvals_left is unknown.")
    try:
        open_threads = _to_thread_counts(node, iid).open
    except ToolError as exc:
        open_threads = None
        warnings.append(str(exc))

    approved_value = node.get("approved")
    approved = approved_value if isinstance(approved_value, bool) else None
    if approved_value is not None and approved is None:
        warnings.append(f"GitLab returned an invalid approval status for merge request !{iid}.")

    approvals_left: int | None = None
    if approvals_left_supported:
        approvals_left_value = node.get("approvalsLeft")
        approvals_left = (
            approvals_left_value
            if isinstance(approvals_left_value, int) and not isinstance(approvals_left_value, bool)
            else None
        )
        if approvals_left_value is not None and approvals_left is None:
            warnings.append(f"GitLab returned an invalid approvals-left value for merge request !{iid}.")

    if "autoMergeStrategy" not in node:
        auto_merge_strategy = None
    elif node["autoMergeStrategy"] is None:
        auto_merge_strategy = ""
    elif isinstance(node["autoMergeStrategy"], str):
        auto_merge_strategy = node["autoMergeStrategy"]
    else:
        auto_merge_strategy = None
        warnings.append(f"GitLab returned an invalid auto-merge strategy for merge request !{iid}.")

    return MergeRequestReplyStatus(
        iid=iid,
        open_threads=open_threads,
        approved=approved,
        approvals_left=approvals_left,
        auto_merge_strategy=auto_merge_strategy,
        warning=" ".join(warnings),
    )


def _last_resolve_positions(replies: list[ThreadReplyRequest]) -> dict[int, int]:
    """Map each MR IID to the position of its last item that asks for a resolve.

    That item reports the MR's open thread count, so each MR is counted once, after its resolutions.
    """
    return {reply.iid: position for position, reply in enumerate(replies) if reply.resolve}


async def _write_thread_reply(  # noqa: C901, PLR0913, PLR0917 - distinct write outcomes and the user-facing inputs.
    client: GitLabClient,
    project: str,
    iid: int,
    thread_id: str,
    body: str | None,
    resolve: bool,
) -> ThreadReplyResult:
    """Post the reply and resolve the thread, without reading the thread count afterward.

    The public tools add the count. A batch reads it once per MR instead of once per item.
    """
    bare_thread_id = bare_id(thread_id)
    if not bare_thread_id.strip():
        raise ToolError("thread_id must not be empty.")  # noqa: TRY003
    if body is not None and not body.strip():
        raise ToolError("body must not be empty or whitespace.")  # noqa: TRY003
    if body is None and not resolve:
        raise ToolError("Provide a reply body or set resolve=True.")  # noqa: TRY003

    discussion_path = f"{mr_rest_path(project, iid)}/discussions/{quote(bare_thread_id, safe='')}"
    note_id: int | None = None

    if body is not None:
        try:
            note = await client.request("POST", f"{discussion_path}/notes", json={"body": body})
        except GitLabConnectionError as exc:
            raise ToolError(  # noqa: TRY003
                f"{exc} Reply outcome is unknown. Check get_merge_request_threads before retrying."
            ) from exc
        note_id = note.get("id") if isinstance(note, dict) else None

    if resolve:
        try:
            await client.request("PUT", discussion_path, json={"resolved": True})
        except GitLabConnectionError as exc:
            if body is None:
                raise ToolError(  # noqa: TRY003
                    f"{exc} Resolution outcome is unknown. Check get_merge_request_threads before retrying."
                ) from exc
            return ThreadReplyResult(
                thread_id=bare_thread_id,
                note_id=note_id,
                warning=(
                    f"Reply was posted (note ID {note_id}) but resolution outcome is unknown: {exc} "
                    "Check get_merge_request_threads before retrying. Do not repost the reply."
                ),
            )
        except ToolError as exc:
            if body is None:
                raise
            return ThreadReplyResult(
                thread_id=bare_thread_id,
                note_id=note_id,
                warning=f"Reply was posted (note ID {note_id}) but resolution failed: {exc} Do not repost the reply.",
            )

    return ThreadReplyResult(thread_id=bare_thread_id, note_id=note_id, resolved=True if resolve else None)


def _is_uncertain_write_error(exc: ToolError) -> bool:
    """Return True when a failed write may still have reached GitLab, so its outcome is unknown.

    Connection errors, timeouts, and 5xx responses can hide a completed write. Other API errors are definite failures.
    """
    return (
        isinstance(exc, GitLabConnectionError)
        or (isinstance(exc, GitLabAPIError) and (exc.status == HTTP_REQUEST_TIMEOUT or exc.status >= HTTP_SERVER_ERROR))
        or "outcome is unknown" in str(exc).lower()
    )


def _failed_write_item(iid: int, thread_id: str, exc: ToolError) -> ThreadReplyBatchItem:
    """Return the batch row for a write that raised. Uncertain outcomes are marked "unknown", not "failed"."""
    uncertain = _is_uncertain_write_error(exc)
    warning = str(exc)
    if uncertain and "outcome is unknown" not in warning.lower():
        warning = f"Write outcome is unknown: {warning} Check get_merge_request_threads before retrying."
    return ThreadReplyBatchItem(
        iid=iid,
        thread_id=thread_id,
        status="unknown" if uncertain else "failed",
        resolved=None,
        warning=warning,
    )


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def reply_to_thread(  # noqa: PLR0913, PLR0917 - user-facing inputs.
    ctx: Context,
    project: Project,
    iid: Iid,
    thread_id: str,
    body: Annotated[str | None, Field(description="Reply text. Omit only when resolving the thread.")] = None,
    resolve: Annotated[
        bool,
        Field(description="Resolve the thread after a successful reply, or resolve it alone."),
    ] = False,
) -> ThreadReplyResult:
    """Reply to or resolve a review thread. Like `glab mr note create --reply` and `glab mr note resolve`.

    Get user approval before posting a reply or resolving a thread. A missing body is allowed only with `resolve=True`.
    `resolve=False` leaves the current resolution state unchanged and never reopens a thread. Replying and resolving
    are separate GitLab writes. If resolution fails after a reply, the result says not to repost the reply.
    After a successful resolve, `open_threads` is the MR's open thread count after the write. It is left out if that
    count cannot be read.
    """
    client = get_client(ctx)
    outcome = await _write_thread_reply(client, project, iid, thread_id, body, resolve)
    if outcome.resolved:
        outcome.open_threads = await _read_open_threads(client, project, iid)
    return outcome


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def reply_to_threads(
    ctx: Context,
    project: Project,
    replies: Annotated[
        list[ThreadReplyRequest],
        Field(min_length=1, max_length=20, description="One to 20 thread replies or resolutions, in write order."),
    ],
) -> ThreadReplyBatchResult:
    """Reply to or resolve up to 20 review threads in order.

    Like `glab mr note create --reply` and `glab mr note resolve`. Get user approval before posting. Each item uses
    the same reply and resolution behavior as `reply_to_thread`; items run sequentially, and a failed item does not
    stop later items or roll back successful writes. Writes are never retried. Check each status and warning before
    deciding what to do next, especially when an outcome is unknown.

    Each result includes `note_id` when a reply was posted, including when a later resolution fails or is unknown.
    Resolve-only results do not include a note ID.

    After a successful `resolve=true`, `open_threads` is sent once per merge request, on that MR's last item with
    `resolve=true`. It is left out if that count cannot be read or if that last resolution failed. The top-level
    `merge_requests` list contains one best-effort post-write summary per distinct IID, in first-request order, read
    only after all writes finish. It includes `open_threads` when exact counts are available, `approved`, nullable
    `approvals_left`, and `auto_merge_strategy` (an empty string means auto-merge is cleared). Unknown optional values
    are omitted. A `warning` is included when status or count reads fail; these read failures do not change write
    results. On GitLab CE, an explicit unknown `approvalsLeft` field error triggers one read-only fallback without
    that field; writes are never retried.
    """
    client = get_client(ctx)
    last_resolve = _last_resolve_positions(replies)
    results: list[ThreadReplyBatchItem] = []
    for reply in replies:
        thread_id = bare_id(reply.thread_id)
        try:
            outcome = await _write_thread_reply(
                client,
                project,
                reply.iid,
                reply.thread_id,
                reply.body,
                reply.resolve,
            )
        except ToolError as exc:
            results.append(_failed_write_item(reply.iid, thread_id, exc))
            continue

        warning = outcome.warning
        uncertain_resolution = bool(re.search(r"GitLab returned HTTP (?:408|5\d\d)\b", warning))
        if uncertain_resolution:
            # The single tool reports all resolution ToolErrors as failures, but a timeout or 5xx can hide a write.
            warning = warning.replace("resolution failed:", "resolution outcome is unknown:")
        if uncertain_resolution or "outcome is unknown" in warning.lower():
            status: ThreadReplyBatchStatus = "unknown"
        elif warning:
            status = "partial"
        else:
            status = "applied"
        results.append(
            ThreadReplyBatchItem(
                iid=reply.iid,
                thread_id=outcome.thread_id,
                status=status,
                resolved=outcome.resolved,
                note_id=outcome.note_id,
                warning=warning,
            )
        )

    merge_requests = [
        await _read_thread_reply_status(client, project, requested_iid)
        for requested_iid in dict.fromkeys(reply.iid for reply in replies)
    ]
    status_by_iid = {summary.iid: summary for summary in merge_requests}
    for iid, position in last_resolve.items():
        if results[position].resolved is True:
            results[position].open_threads = status_by_iid[iid].open_threads

    return ThreadReplyBatchResult(results=results, merge_requests=merge_requests)


async def _open_thread_ids(client: GitLabClient, project: str, iid: int) -> tuple[list[str], bool]:
    """Return the bare IDs of open resolvable threads, and whether every discussion page was read.

    Reads at most MAX_PAGES pages. The second value is False when more pages exist after that.
    """
    open_ids: list[str] = []
    after: str | None = None
    for _ in range(MAX_PAGES):
        node = await fetch_merge_request(client, OPEN_THREAD_IDS_QUERY, project, iid, after=after)
        connection = node["discussions"]
        open_ids.extend(
            bare_id(discussion["id"])
            for discussion in connection["nodes"]
            if discussion["resolvable"] and not discussion["resolved"]
        )
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return open_ids, True
        after = page_info["endCursor"]
    return open_ids, False


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def resolve_open_threads(ctx: Context, project: Project, iid: Iid) -> ResolveOpenThreadsResult:
    """Resolve every open review thread on one merge request. Like `glab mr note resolve` for each open thread.

    Get user approval before resolving threads. This only resolves; it posts no replies. Threads resolve one at a
    time, and a failed thread does not stop the rest. Writes are never retried. `resolved` lists the bare IDs of
    threads that were resolved. `problems` lists each thread that failed or has an unknown outcome, with a status and
    warning. `open_threads` is the MR's open thread count after the writes, and it is left out if that count cannot
    be read. `complete=false` means the MR has more discussions than fit in the scan limit, so run this tool again.
    Cheaper than listing thread IDs and then calling `reply_to_threads`.
    """
    client = get_client(ctx)
    thread_ids, complete = await _open_thread_ids(client, project, iid)
    resolved: list[str] = []
    problems: list[ThreadReplyBatchItem] = []
    for thread_id in thread_ids:
        try:
            outcome = await _write_thread_reply(client, project, iid, thread_id, None, True)
        except ToolError as exc:
            problems.append(_failed_write_item(iid, thread_id, exc))
            continue
        resolved.append(outcome.thread_id)
    return ResolveOpenThreadsResult(
        resolved=resolved,
        problems=problems,
        open_threads=await _read_open_threads(client, project, iid),
        complete=complete,
    )
