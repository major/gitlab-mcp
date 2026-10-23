"""FastMCP server with GitLab tools for fast developer workflows 🚀."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, model_validator
lazy from fastmcp import Context

from gitlab_mcp import findings as _findings
from gitlab_mcp import tools  # noqa: F401 - importing the package registers the moved tools.
from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError
from gitlab_mcp.common import (
    DIFF_PAGE_SIZE,
    HTTP_REQUEST_TIMEOUT,
    HTTP_SERVER_ERROR,
    MAX_BATCH_MERGE_REQUESTS,
    MAX_PAGES,
    REVIEW_NOTE_BODY_CAP,
    Iid,
    PositiveIid,
    Project,
    bare_id,
    decode_cursor,
    encode_cursor,
    is_int_at_least,
    lower_or_empty,
    read_server_local_utf8_file,
)
from gitlab_mcp.errors import (
    InvalidCursorError,
    MergeRequestNotFoundError,
    ProjectNotFoundError,
    raise_review_preflight_error,
)
from gitlab_mcp.mr_common import (
    DIFF_HUNK_PATTERN,
    ThreadIndex,
    ThreadLocation,
    ThreadRef,
    author_display,
    diff_refs_head_sha,
    fetch_diff_page,
    fetch_merge_request,
    full_sha,
    mr_rest_path,
    position_fields,
)

# One request per page. `open_threads` is computed from the two counts.
MERGE_REQUESTS_QUERY = """
query(
  $fullPath: ID!, $state: MergeRequestState, $authorUsername: String, $search: String,
  $createdAfter: Time, $updatedAfter: Time, $targetBranches: [String!],
  $includeSourceBranch: Boolean!, $includeLabels: Boolean!, $first: Int!, $after: String
) {
  project(fullPath: $fullPath) {
    mergeRequests(
      state: $state, authorUsername: $authorUsername, search: $search,
      createdAfter: $createdAfter, updatedAfter: $updatedAfter, targetBranches: $targetBranches,
      first: $first, after: $after, sort: UPDATED_DESC
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        iid title state draft updatedAt
        sourceBranch @include(if: $includeSourceBranch)
        labels(first: 100) @include(if: $includeLabels) {
          nodes { title }
          pageInfo { hasNextPage }
        }
        author { username }
        approved
        approvedBy { nodes { username } }
        resolvableDiscussionsCount resolvedDiscussionsCount
      }
    }
  }
}
"""

# One merge request with its status facts and the first page of discussions.
MERGE_REQUEST_QUERY = """
query($fullPath: ID!, $iid: String!, $includeDescription: Boolean!, $includeJobs: Boolean! = false) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      iid title description @include(if: $includeDescription) state draft sourceBranch targetBranch
      author { username }
      mergedAt mergeUser { username }
      diffRefs { headSha }
      detailedMergeStatus shouldBeRebased conflicts
      approved approvedBy { nodes { username } }
      reviewers { nodes { username mergeRequestInteraction { reviewState } } }
      headPipeline { status id @include(if: $includeJobs) }
      autoMergeStrategy
      diffStatsSummary { additions deletions fileCount }
      resolvableDiscussionsCount resolvedDiscussionsCount
      discussions(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id resolvable resolved
          notes(first: 1) {
            count
            nodes { system author { username bot name id } position { oldPath newPath oldLine newLine } }
          }
        }
      }
    }
  }
}
"""

# Shared fields for compact batch lookups. Keep discussion, reviewer, and change data out. 📦
COMPACT_MERGE_REQUEST_FRAGMENT = """
fragment CompactMergeRequest on MergeRequest {
  iid title description @include(if: $includeDescription) state draft sourceBranch targetBranch
  author { username }
  mergedAt mergeUser { username }
  diffRefs { headSha }
  detailedMergeStatus shouldBeRebased conflicts
  approved approvedBy { nodes { username } }
  headPipeline { status }
}
"""

# One active train and one page of its active cars per request, limiting nested overfetch.
# GraphQL's car index includes historical cars, so positions come from active car order instead.
MERGE_TRAIN_QUERY = """
query($fullPath: ID!, $targetBranches: [String!], $after: String, $carAfter: String, $first: Int!) {
  project(fullPath: $fullPath) {
    mergeTrains(status: ACTIVE, targetBranches: $targetBranches, first: 1, after: $after) {
      nodes {
        targetBranch
        cars(activityStatus: ACTIVE, first: $first, after: $carAfter) {
          nodes {
            status
            mergeRequest { iid title author { username } }
            pipeline { id status }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

# One page of discussions with their notes, for get_merge_request_threads. 🧵
THREADS_QUERY = """
query($fullPath: ID!, $iid: String!, $after: String) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      sourceBranch
      diffRefs { headSha }
      detailedMergeStatus
      headPipeline { status }
      discussions(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id resolvable resolved
          notes(first: 50) {
            pageInfo { hasNextPage }
            nodes {
              system body createdAt
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

# Long descriptions are cut to keep one tool result small. The flag tells the agent it was cut.
DESCRIPTION_CAP = 4000
# The open-thread list is capped. Counts stay exact, and `index_complete` says whether the list is whole.
THREAD_INDEX_CAP = 20
# Long note bodies are cut to this many characters, then marked with THREAD_CUT_MARKER.
NOTE_CAP = 4000
THREAD_CUT_MARKER = " [truncated]"
# Do not let a malformed marker consume a later HTML comment.
REVIEW_BOT_FINDING_PATTERN = re.compile(r"<!-- review-bot:finding\s(?:(?!-->|<!--).)*-->\s*$", re.DOTALL)
# Total characters of note bodies that one thread call may return. Keeps one tool result small.
THREAD_BODY_BUDGET = 30000
# Serialized character budget for compact heading results.
THREAD_HEADING_BUDGET = 30000
# Headings are capped so one unusually long note cannot dominate a compact result.
THREAD_HEADING_CAP = 160

# Total patch characters that one diff call may return. A single patch is cut to this too.
DIFF_BUDGET = 40000
DIFF_HUNK_NORMALIZE_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)
INVALID_SINCE_SHA_MESSAGE = "since_sha must be a full 40-character commit SHA."

# A Literal is sent to the agent as an enum, so this type replaces a separate
# "list states" tool.
MergeRequestState = Literal["opened", "closed", "locked", "merged", "all"]

# Column order of the TSV output. Every row has every column, so agents
# never have to guess whether a missing key means "empty" or "not sent".
TSV_COLUMNS = ("iid", "title", "author", "state", "draft", "approved", "approved_by", "open_threads", "updated")


class MergeRequestSummary(BaseModel):
    """One merge request row, with every field the TSV output needs.

    Values are kept as-is here. `_to_tsv` turns them into cells.
    """

    iid: int
    title: str
    source_branch: str | None = None
    labels: list[str] = Field(default_factory=list)
    labels_truncated: bool = False
    author: str | None  # Null when the author was deleted or banned.
    state: str
    draft: bool
    approved: bool
    approved_by: list[str]
    open_threads: int
    updated: str  # Date only, YYYY-MM-DD.


class MergeRequestList(BaseModel):
    """A page of merge requests."""

    web_url_prefix: str = Field(description="Append an iid to get the MR link.")
    merge_requests: str = Field(
        description=(
            "TSV with a header row. Columns: iid, title, author, state, draft, approved, "
            "approved_by, open_threads, updated. approved_by is comma-separated usernames. "
            "An empty cell means none. Booleans are true/false. source_branch and labels are appended only when asked; "
            "labels is a JSON array in one cell, and labels_complete reports whether all returned labels were included."
        ),
    )
    has_more: bool
    labels_complete: bool | None = Field(default=None, exclude_if=lambda value: value is None)


class MergeTrainList(BaseModel):
    """A page of active merge train entries."""

    web_url_prefix: str = Field(description="Append an iid to get the MR link.")
    entries: str = Field(
        description=(
            "TSV with a header row. Columns: target_branch, position, iid, title, author, status, "
            "pipeline_id, pipeline_status. Empty cells mean unknown or none."
        ),
    )
    has_more: bool


class Reviewer(BaseModel):
    """A requested reviewer."""

    username: str
    review_state: str = Field(default="", exclude_if=lambda v: not v)  # Lowercased, like "reviewed".


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
    # Sent once per MR, on its last item with resolve=true, when that resolution succeeded.
    open_threads: int | None = Field(default=None, exclude_if=lambda value: value is None)
    warning: str = Field(default="", exclude_if=lambda value: not value)


class ThreadReplyBatchResult(BaseModel):
    """Ordered outcomes for a batch of thread replies."""

    results: list[ThreadReplyBatchItem]


class DiffSummary(BaseModel):
    """Diff size totals."""

    files: int
    additions: int
    deletions: int


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


class MergeRequestDiff(BaseModel):
    """A file list or the requested patches."""

    head_sha: str = Field(default="", exclude_if=lambda v: not v)
    since_version_found: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    files: list[FileStat] = Field(default_factory=list, exclude_if=lambda v: not v)
    diffs: list[FileDiff] = Field(default_factory=list, exclude_if=lambda v: not v)
    next_cursor: str = Field(default="", exclude_if=lambda v: not v)  # Empty when the scan finished.
    paths_not_found: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    warning: str = Field(default="", exclude_if=lambda value: not value)


class MergeRequestDetail(BaseModel):
    """One merge request's status facts."""

    iid: int
    title: str
    state: str
    draft: bool = Field(default=False, exclude_if=lambda v: not v)
    author: str = Field(default="", exclude_if=lambda v: not v)
    merged_at: str | None = Field(default=None, exclude_if=lambda value: value is None)
    merged_by: str | None = Field(default=None, exclude_if=lambda value: value is None)
    source_branch: str
    target_branch: str
    head_sha: str = Field(default="", exclude_if=lambda v: not v)
    description: str = Field(default="", exclude_if=lambda v: not v)
    description_truncated: bool = Field(default=False, exclude_if=lambda v: not v)
    merge_status: str = Field(default="", exclude_if=lambda v: not v)  # GitLab's detailed status.
    should_be_rebased: bool = Field(default=False, exclude_if=lambda v: not v)
    conflicts: bool = Field(default=False, exclude_if=lambda v: not v)
    approved: bool
    approved_by: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    reviewers: list[Reviewer] = Field(default_factory=list, exclude_if=lambda v: not v)
    pipeline_status: str = Field(default="", exclude_if=lambda v: not v)  # Head pipeline, lowercased.
    failed_jobs: str = Field(default="", exclude_if=lambda value: not value)
    jobs_complete: bool = Field(default=True, exclude_if=lambda value: value)
    auto_merge_strategy: str = Field(default="", exclude_if=lambda v: not v)
    changes: DiffSummary | None = Field(default=None, exclude_if=lambda v: v is None)
    threads: ThreadIndex


class CompactMergeRequest(BaseModel):
    """One merge request's compact status facts, without reviewers or threads."""

    iid: int
    title: str
    state: str
    draft: bool = Field(default=False, exclude_if=lambda v: not v)
    author: str = Field(default="", exclude_if=lambda v: not v)
    merged_at: str | None = Field(default=None, exclude_if=lambda value: value is None)
    merged_by: str | None = Field(default=None, exclude_if=lambda value: value is None)
    source_branch: str
    target_branch: str
    head_sha: str = Field(default="", exclude_if=lambda v: not v)
    merge_status: str = Field(default="", exclude_if=lambda v: not v)
    should_be_rebased: bool = Field(default=False, exclude_if=lambda v: not v)
    conflicts: bool = Field(default=False, exclude_if=lambda v: not v)
    approved: bool
    approved_by: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    pipeline_status: str = Field(default="", exclude_if=lambda v: not v)
    description: str = Field(default="", exclude_if=lambda v: not v)
    description_truncated: bool = Field(default=False, exclude_if=lambda v: not v)


class MergeRequestBatch(BaseModel):
    """Compact results and the requested IIDs GitLab did not return."""

    merge_requests: list[CompactMergeRequest]
    missing_iids: list[int] = Field(default_factory=list, exclude_if=lambda v: not v)


def _cell(value: object) -> str:
    """Render one value as a TSV cell.

    None becomes an empty cell, booleans become true/false, and lists become
    comma-separated. Tabs and line breaks in text would split the row, so each
    run of them becomes one space. Titles can contain them.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(value)
    return re.sub(r"[\t\r\n]+", " ", str(value))


def _to_tsv(
    mrs: list[MergeRequestSummary],
    *,
    include_source_branch: bool = False,
    include_labels: bool = False,
) -> str:
    """Render merge requests as TSV, appending optional data columns when requested."""
    columns = list(TSV_COLUMNS)
    if include_source_branch:
        columns.append("source_branch")
    if include_labels:
        columns.append("labels")
    lines = ["\t".join(columns)]
    for mr in mrs:
        values = [_cell(getattr(mr, column)) for column in TSV_COLUMNS]
        if include_source_branch:
            values.append(_cell(mr.source_branch))
        if include_labels:
            # JSON escaping keeps tabs and newlines inside one TSV cell.
            values.append(json.dumps(mr.labels, ensure_ascii=False, separators=(",", ":")))
        lines.append("\t".join(values))
    return "\n".join(lines)


def _to_summary(
    node: dict[str, Any],
    *,
    include_source_branch: bool = False,
    include_labels: bool = False,
) -> MergeRequestSummary:
    """Convert one GraphQL merge request node into a summary."""
    # A deleted or banned author comes back as null.
    author = node["author"]
    label_connection = node.get("labels") if include_labels else None
    return MergeRequestSummary(
        iid=int(node["iid"]),  # GraphQL sends IIDs as strings.
        title=node["title"],
        source_branch=node.get("sourceBranch") if include_source_branch else None,
        labels=[label["title"] for label in label_connection["nodes"]] if label_connection else [],
        labels_truncated=label_connection["pageInfo"]["hasNextPage"] if label_connection else False,
        author=author["username"] if author else None,
        state=node["state"],
        draft=node["draft"],
        approved=node["approved"],
        approved_by=[user["username"] for user in node["approvedBy"]["nodes"]],
        open_threads=(node["resolvableDiscussionsCount"] or 0) - (node["resolvedDiscussionsCount"] or 0),
        updated=node["updatedAt"][:10],  # ISO 8601 timestamp, keep the date part.
    )


def _matches(mr: MergeRequestSummary, approved: bool | None, has_open_threads: bool | None) -> bool:
    """Return True when `mr` passes the client-side filters. `None` means no filter."""
    if approved is not None and mr.approved != approved:
        return False
    return has_open_threads is None or (mr.open_threads > 0) == has_open_threads


def _to_merge_train_entry(target_branch: str, position: int, car: dict[str, Any]) -> str:
    """Render one merge train car as a sanitized TSV row."""
    merge_request = car["mergeRequest"]
    author = merge_request["author"]
    pipeline = car["pipeline"]
    pipeline_id = pipeline["id"] if pipeline else None
    if pipeline_id:
        pipeline_id = pipeline_id.rsplit("/", 1)[-1]
    values = (
        target_branch,
        position,
        merge_request["iid"],
        merge_request["title"],
        author["username"] if author else None,
        lower_or_empty(car["status"]),
        pipeline_id,
        lower_or_empty(pipeline["status"]) if pipeline else "",
    )
    return "\t".join(_cell(value) for value in values)


def _normalize_timestamp(value: str | None, name: str) -> str | None:
    """Validate a timezone-aware ISO 8601 datetime and normalize it to UTC for GraphQL."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except OverflowError, ValueError:
        raise ToolError(  # noqa: TRY003 - the parameter name makes the validation error actionable.
            f"{name} must be a timezone-aware ISO 8601 datetime, such as 2026-10-01T12:30:00Z."
        ) from None
    if "T" not in value.upper() or parsed.utcoffset() is None:
        raise ToolError(  # noqa: TRY003 - the parameter name makes the validation error actionable.
            f"{name} must be a timezone-aware ISO 8601 datetime, such as 2026-10-01T12:30:00Z."
        )
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_merge_requests(  # noqa: PLR0913, PLR0917 - each argument is a user-facing filter.
    ctx: Context,
    project: Project,
    state: MergeRequestState = "opened",
    author: Annotated[str | None, Field(description="Author username.")] = None,
    search: Annotated[
        str | None,
        Field(description="Search merge request titles or descriptions."),
    ] = None,
    approved: Annotated[
        bool | None,
        Field(description="True: only fully approved. False: only not fully approved."),
    ] = None,
    has_open_threads: Annotated[
        bool | None,
        Field(description="True: only MRs with unresolved threads. False: only MRs without."),
    ] = None,
    created_after: Annotated[
        str | None,
        Field(description="Timezone-aware ISO 8601 datetime. Date-only and timezone-naive values are not accepted."),
    ] = None,
    updated_after: Annotated[
        str | None,
        Field(description="Timezone-aware ISO 8601 datetime. Date-only and timezone-naive values are not accepted."),
    ] = None,
    target_branch: Annotated[str | None, Field(description="Only merge requests targeting this branch.")] = None,
    include_source_branch: Annotated[
        bool,
        Field(description="Append the source branch column. Defaults to false to keep output compact."),
    ] = False,
    include_labels: Annotated[
        bool,
        Field(description="Append labels as a JSON array in one TSV cell. Defaults to false."),
    ] = False,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> MergeRequestList:
    """List merge requests in a project, newest-updated first.

    Equivalent to `glab mr list` with filters. The token needs the `read_api`
    scope. Results are TSV with a header row, and every row has every column.
    An empty cell means none, like no author or no approvers. Booleans are
    true or false. `updated` is a date. For a link, append an MR's `iid` to
    `web_url_prefix`.

    `approved` follows the instance rules: on Free it means at least one
    approval, on Premium and Ultimate it means all approval rules are met.
    `search` uses GitLab's `Project.mergeRequests(search:)` filter, which matches
    text in the title or description.
    `created_after` and `updated_after` accept timezone-aware ISO 8601 datetimes.
    `target_branch` filters by target branch. `include_source_branch` and `include_labels`
    append columns only when requested. Labels are JSON arrays inside a TSV cell;
    `labels_complete` is false when any returned label list exceeds its first 100 labels.
    `approved` and `has_open_threads` are filtered after fetching, so the tool
    scans at most 5 pages. If `has_more` is true but fewer than `limit` MRs
    came back, the scan stopped early and more matches may exist.
    """
    created_after = _normalize_timestamp(created_after, "created_after")
    updated_after = _normalize_timestamp(updated_after, "updated_after")
    client = get_client(ctx)
    # Client-side filters can drop nodes, so fetch big pages to find matches faster.
    filtered = approved is not None or has_open_threads is not None
    variables: dict[str, Any] = {
        "fullPath": project,
        "state": state,
        "authorUsername": author,
        "search": search,
        "createdAfter": created_after,
        "updatedAfter": updated_after,
        "targetBranches": [target_branch] if target_branch is not None else None,
        "includeSourceBranch": include_source_branch,
        "includeLabels": include_labels,
        "after": None,
        "first": 100 if filtered else limit,
    }

    collected: list[MergeRequestSummary] = []
    has_next = False
    for _ in range(MAX_PAGES):
        data = await client.graphql(MERGE_REQUESTS_QUERY, variables)
        if data["project"] is None:
            raise ProjectNotFoundError(project)
        connection = data["project"]["mergeRequests"]
        summaries = [
            _to_summary(node, include_source_branch=include_source_branch, include_labels=include_labels)
            for node in connection["nodes"]
        ]
        collected.extend(mr for mr in summaries if _matches(mr, approved, has_open_threads))
        page_info = connection["pageInfo"]
        has_next = page_info["hasNextPage"]
        if len(collected) >= limit or not has_next:
            break
        variables["after"] = page_info["endCursor"]

    displayed = collected[:limit]
    return MergeRequestList(
        web_url_prefix=f"{client.settings.url}/{project}/-/merge_requests/",
        merge_requests=_to_tsv(
            displayed,
            include_source_branch=include_source_branch,
            include_labels=include_labels,
        ),
        has_more=len(collected) > limit or has_next,
        labels_complete=not any(mr.labels_truncated for mr in displayed) if include_labels else None,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_merge_train(
    ctx: Context,
    project: Project,
    target_branch: Annotated[
        str | None,
        Field(description="Only this target branch. Omit for all active trains."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> MergeTrainList:
    """List active merge train entries, like `glab api projects/:id/merge_trains?scope=active&sort=asc`.

    Requires the `read_api` scope and GitLab Premium or Ultimate. Active trains include cars that are waiting,
    not only cars currently running. TSV columns are `target_branch`, `position`, `iid`, `title`, `author`, `status`,
    `pipeline_id`, and `pipeline_status`; positions are one-based within each target branch, not a global queue.
    Empty cells mean unknown or none. The scan reads at most 5 pages; `has_more` means entries or trains remain,
    or the page cap stopped the scan. Set `target_branch` to narrow the scan, or increase `limit` for more entries.
    """
    client = get_client(ctx)
    entries: list[str] = []
    outer_after: str | None = None
    car_after: str | None = None
    position = 1
    has_more = False

    for _ in range(MAX_PAGES):
        variables = {
            "fullPath": project,
            "targetBranches": [target_branch] if target_branch is not None else None,
            "after": outer_after,
            "carAfter": car_after,
            "first": limit - len(entries),
        }
        data = await client.graphql(MERGE_TRAIN_QUERY, variables)
        if data["project"] is None:
            raise ProjectNotFoundError(project)
        trains = data["project"]["mergeTrains"]
        train_nodes = trains["nodes"]
        if not train_nodes:
            # The connection can drain between pages. An empty page is complete even if pageInfo is stale.
            has_more = False
            break

        train = train_nodes[0]
        cars = train["cars"]
        car_nodes = cars["nodes"]
        entries.extend(
            _to_merge_train_entry(train["targetBranch"], car_position, car)
            for car_position, car in enumerate(car_nodes, start=position)
        )
        position += len(car_nodes)

        cars_page_info = cars["pageInfo"]
        trains_page_info = trains["pageInfo"]
        has_more = cars_page_info["hasNextPage"] or trains_page_info["hasNextPage"]
        if len(entries) >= limit or not has_more:
            break
        if cars_page_info["hasNextPage"]:
            car_after = cars_page_info["endCursor"]
        else:
            outer_after = trains_page_info["endCursor"]
            car_after = None
            position = 1

    header = "target_branch\tposition\tiid\ttitle\tauthor\tstatus\tpipeline_id\tpipeline_status"
    return MergeTrainList(
        web_url_prefix=f"{client.settings.url}/{project}/-/merge_requests/",
        entries="\n".join([header, *entries]),
        has_more=has_more,
    )


def _cap_description(description: str | None, limit: int = DESCRIPTION_CAP) -> tuple[str, bool]:
    """Cut a description to the requested number of characters.

    Returns the text and whether it was cut. Empty text stays empty, so it is left out.
    """
    if not description or limit == 0:
        return "", False
    return description[:limit], len(description) > limit


def _to_reviewer(node: dict[str, Any]) -> Reviewer:
    """Convert one reviewer node. A null interaction means GitLab has no review state for them."""
    interaction = node["mergeRequestInteraction"]
    review_state = interaction["reviewState"] if interaction else None
    return Reviewer(username=node["username"], review_state=lower_or_empty(review_state))


def _to_diff_summary(stats: dict[str, Any] | None) -> DiffSummary | None:
    """Convert the diff stats. The stats can be null, so check before reading them."""
    if stats is None:
        return None
    return DiffSummary(files=stats["fileCount"], additions=stats["additions"], deletions=stats["deletions"])


def _is_open_thread(discussion: dict[str, Any]) -> bool:
    """Return True for an unresolved, resolvable thread that starts with a human note.

    System notes, like "changed the title", are skipped because agents act on
    human feedback.
    """
    first = discussion["notes"]["nodes"][0]
    return discussion["resolvable"] and not discussion["resolved"] and not first["system"]


def _to_thread_ref(discussion: dict[str, Any]) -> ThreadRef:
    """Convert one open discussion into a thread ref, using its first note for the details."""
    notes = discussion["notes"]
    first = notes["nodes"][0]
    author = first["author"]  # A deleted or banned author comes back as null.
    return ThreadRef(
        id=bare_id(discussion["id"]),
        by=author_display(author),
        notes=notes["count"],
        **position_fields(first["position"]),
    )


def _to_thread_index(node: dict[str, Any]) -> ThreadIndex:
    """Build the thread index from the exact counts and the first page of discussions.

    Only one page of discussions is read, so `index_complete` is false when
    GitLab has another page.
    """
    # Null counts mean zero. Only the list below can be partial, never the counts.
    resolvable = node["resolvableDiscussionsCount"] or 0
    resolved = node["resolvedDiscussionsCount"] or 0
    discussions = node["discussions"]
    open_discussions = [discussion for discussion in discussions["nodes"] if _is_open_thread(discussion)]
    return ThreadIndex(
        open_count=resolvable - resolved,
        resolved_count=resolved,
        open=[_to_thread_ref(discussion) for discussion in open_discussions[:THREAD_INDEX_CAP]],
        index_complete=len(open_discussions) <= THREAD_INDEX_CAP and not discussions["pageInfo"]["hasNextPage"],
    )


def _merged_metadata(node: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return date-only merge metadata for merged requests with available values."""
    if node["state"] != "merged":
        return None, None
    merged_at = node.get("mergedAt")
    merge_user = node.get("mergeUser")
    return merged_at[:10] if merged_at else None, merge_user.get("username") if merge_user else None


def _to_detail(
    node: dict[str, Any],
    *,
    include_description: bool,
    description_limit: int,
) -> MergeRequestDetail:
    """Convert one GraphQL merge request node into the detail shape.

    When the description was skipped or is absent from the query result, it and its flag are left out.
    """
    # A deleted or banned author comes back as null.
    author = node["author"]
    # headPipeline can be null, so check before reading it.
    pipeline = node["headPipeline"]
    merged_at, merged_by = _merged_metadata(node)
    description, description_truncated = _cap_description(
        node.get("description") if include_description else None,
        description_limit,
    )
    return MergeRequestDetail(
        iid=int(node["iid"]),  # GraphQL sends IIDs as strings.
        title=node["title"],
        state=node["state"],
        merged_at=merged_at,
        merged_by=merged_by,
        draft=node["draft"],
        author=author["username"] if author else "",
        source_branch=node["sourceBranch"],
        target_branch=node["targetBranch"],
        head_sha=diff_refs_head_sha(node["diffRefs"]),
        description=description,
        description_truncated=description_truncated,
        merge_status=lower_or_empty(node["detailedMergeStatus"]),
        should_be_rebased=node["shouldBeRebased"],
        conflicts=node["conflicts"],
        approved=node["approved"],
        approved_by=[user["username"] for user in node["approvedBy"]["nodes"]],
        reviewers=[_to_reviewer(reviewer) for reviewer in node["reviewers"]["nodes"]],
        pipeline_status=lower_or_empty(pipeline["status"]) if pipeline else "",
        auto_merge_strategy=node["autoMergeStrategy"] or "",
        changes=_to_diff_summary(node["diffStatsSummary"]),
        threads=_to_thread_index(node),
    )


def _to_compact_detail(
    node: dict[str, Any],
    *,
    include_description: bool,
    description_limit: int,
) -> CompactMergeRequest:
    """Convert one compact GraphQL merge request node to its output model."""
    # A deleted or banned author comes back as null.
    author = node["author"]
    # headPipeline can be null, so check before reading it.
    pipeline = node["headPipeline"]
    merged_at, merged_by = _merged_metadata(node)
    description, description_truncated = _cap_description(
        node.get("description") if include_description else None,
        description_limit,
    )
    return CompactMergeRequest(
        iid=int(node["iid"]),  # GraphQL sends IIDs as strings.
        title=node["title"],
        state=node["state"],
        merged_at=merged_at,
        merged_by=merged_by,
        draft=node["draft"],
        author=author["username"] if author else "",
        source_branch=node["sourceBranch"],
        target_branch=node["targetBranch"],
        head_sha=diff_refs_head_sha(node["diffRefs"]),
        merge_status=lower_or_empty(node["detailedMergeStatus"]),
        should_be_rebased=node["shouldBeRebased"],
        conflicts=node["conflicts"],
        approved=node["approved"],
        approved_by=[user["username"] for user in node["approvedBy"]["nodes"]],
        pipeline_status=lower_or_empty(pipeline["status"]) if pipeline else "",
        description=description,
        description_truncated=description_truncated,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_merge_request(  # noqa: PLR0913, PLR0917 - each argument controls one optional detail.
    ctx: Context,
    project: Project,
    iid: Iid,
    include_jobs: Annotated[
        bool,
        Field(description="Include failed job IDs and names as TSV. Defaults to false to keep output compact."),
    ] = False,
    include_description: Annotated[
        bool,
        Field(description="True includes the description. Defaults to false to save tokens."),
    ] = False,
    description_limit: Annotated[
        int,
        Field(ge=0, le=DESCRIPTION_CAP, description="Maximum description length in characters, from 0 to 4000."),
    ] = DESCRIPTION_CAP,
) -> MergeRequestDetail:
    """Review merge request status, reviewers, approvals, and CI. Like `glab mr view`.

    - `merge_status` is the source of truth for mergeability. Open threads and failed CI do not always block a merge.
    - `approved`: Free means one approval. Premium and Ultimate mean all approval rules pass.
    - `auto_merge_strategy`: `merge_when_checks_pass`, `merge_train`, `add_to_merge_train_when_checks_pass` (waiting).
    - `threads.open` IDs go to get_merge_request_threads. `index_complete` false means some are missing.
    - `line` is the new-file line. `old_line` replaces it on a deleted line.
    - Empty fields are left out: a missing `draft` means false, a missing list is empty.
    - The description is skipped by default. Set `include_description=true` to read it when you need it.
    - `description_limit` controls description length when the description is included. Zero skips it; the default is
      4000 characters.
    - `include_jobs` adds failed job IDs and names as a compact TSV only when requested. `jobs_complete` is false when
      the failed-job scan reaches its page cap. No jobs are fetched when the merge request has no head pipeline.
    """
    include_description = include_description and description_limit > 0
    client = get_client(ctx)
    node = await fetch_merge_request(
        client,
        MERGE_REQUEST_QUERY,
        project,
        iid,
        includeDescription=include_description,
        includeJobs=include_jobs,
    )
    detail = _to_detail(node, include_description=include_description, description_limit=description_limit)
    pipeline = node["headPipeline"]
    if not include_jobs or pipeline is None:
        return detail

    pipeline_gid = pipeline.get("id")
    pipeline_match = (
        re.fullmatch(r"gid://gitlab/Ci::Pipeline/([0-9]+)", pipeline_gid) if isinstance(pipeline_gid, str) else None
    )
    if pipeline_match is None:
        raise ToolError("The pipeline ID format is invalid; expected a numeric pipeline GID.")  # noqa: TRY003

    pipeline_id = pipeline_match.group(1)
    jobs: list[dict[str, Any]] = []
    page = 1
    for _ in range(MAX_PAGES):
        page_jobs, next_page = await client.get_page(
            f"/projects/{quote(project, safe='')}/pipelines/{pipeline_id}/jobs",
            **{"scope[]": "failed", "per_page": 100, "page": page},
        )
        jobs.extend(page_jobs)
        if next_page is None:
            break
        page = next_page
    else:
        detail.jobs_complete = False

    if jobs:
        detail.failed_jobs = "\n".join(
            ["id\tname", *("\t".join((_cell(job["id"]), _cell(job["name"]))) for job in jobs)],
        )
    return detail


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_merge_requests(
    ctx: Context,
    project: Project,
    iids: Annotated[list[PositiveIid], Field(description="Positive merge request IIDs. Up to 20 unique IIDs.")],
    include_description: Annotated[bool, Field(description="True includes descriptions. Defaults to false.")] = False,
    description_limit: Annotated[
        int,
        Field(ge=0, le=DESCRIPTION_CAP, description="Maximum description length in characters, from 0 to 4000."),
    ] = DESCRIPTION_CAP,
) -> MergeRequestBatch:
    """Return compact status for up to 20 merge requests, similar to `glab mr view`.

    IIDs are deduplicated in requested order. More than 20 raw IIDs are accepted when there are at most 20 unique
    values. Missing IIDs are listed separately. This compact result has no reviewers, changes, or discussion/thread
    indexes. Descriptions are skipped by default; set `include_description` to true to include up to 4000 characters.
    Set `description_limit` to zero to skip descriptions even when requested.
    """
    unique_iids = list(dict.fromkeys(iids))
    if not unique_iids:
        raise ToolError("Pass at least one merge request IID.")  # noqa: TRY003
    if len(unique_iids) > MAX_BATCH_MERGE_REQUESTS:
        raise ToolError("Pass no more than 20 unique merge request IIDs.")  # noqa: TRY003

    include_description = include_description and description_limit > 0
    declarations = ["$fullPath: ID!", "$includeDescription: Boolean!"]
    variables: dict[str, Any] = {
        "fullPath": project,
        "includeDescription": include_description,
    }
    selections = []
    for index, iid in enumerate(unique_iids):
        variable = f"iid{index}"
        declarations.append(f"${variable}: String!")
        variables[variable] = str(iid)
        selections.append(f"mr_{index}: mergeRequest(iid: ${variable}) {{ ...CompactMergeRequest }}")

    query = (
        f"query({', '.join(declarations)}) {{\n"
        "  project(fullPath: $fullPath) {\n"
        f"    {' '.join(selections)}\n"
        "  }\n"
        "}\n"
        f"{COMPACT_MERGE_REQUEST_FRAGMENT}"
    )
    data = await get_client(ctx).graphql(query, variables)
    if data["project"] is None:
        raise ProjectNotFoundError(project)

    merge_requests = []
    missing_iids = []
    for index, iid in enumerate(unique_iids):
        node = data["project"][f"mr_{index}"]
        if node is None:
            missing_iids.append(iid)
            continue
        merge_requests.append(
            _to_compact_detail(
                node,
                include_description=include_description,
                description_limit=description_limit,
            ),
        )
    return MergeRequestBatch(merge_requests=merge_requests, missing_iids=missing_iids)


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
    """Return the first Markdown heading, or the first nonempty line, capped at 160 characters."""
    body = _strip_trailing_finding(body)
    markdown_heading = re.search(r"(?m)^[ \t]{0,3}#{1,6}(?:[ \t]+|$)(.*)$", body)
    if markdown_heading:
        heading = re.sub(r"[ \t]+#+[ \t]*$", "", markdown_heading.group(1)).strip()
        if heading:
            return heading[:THREAD_HEADING_CAP]
    return next((line.strip()[:THREAD_HEADING_CAP] for line in body.splitlines() if line.strip()), "")


def _to_note(node: dict[str, Any], max_body_chars: int = NOTE_CAP, compact_findings: bool = False) -> Note:
    """Convert one GraphQL note into a note with a capped body and a date-only timestamp."""
    author = node["author"]  # A deleted or banned author comes back as null.
    body = node["body"]
    if compact_findings and author is not None and author.get("bot") is True:
        body = _findings.compact_finding_body(body)
    return Note(
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
) -> tuple[list[Thread | ThreadHeading], str, set[str], dict[str, str]]:
    """Read discussion pages from `start` and collect the matching threads.

    The scan stops at the budget, at `MAX_PAGES`, or at the last page. Returns
    the threads, the cursor to resume from (empty when every page was read),
    every discussion ID that was read, matching or not, and compact MR status.
    """
    after, skip = start
    threads: list[Thread | ThreadHeading] = []
    seen: set[str] = set()
    mr_headers: dict[str, str] = {}
    full_view = view in {"full", "finding"}
    output_size = 2 if view == "headings" else 0  # Count the serialized list brackets.
    for _ in range(MAX_PAGES):
        node = await fetch_merge_request(client, THREADS_QUERY, project, iid, after=after)
        mr_headers = _thread_mr_headers(node)
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
            size = (
                sum(len(note.body) for note in thread.notes)
                if full_view
                else len(output_thread.model_dump_json()) + (1 if threads else 0)
            )
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
      `get_merge_request` when you need a description, approvals, or jobs.
    - Pass exactly one of `iid` or `iids`. Batch `iids` are deduplicated in request order and allow at most 20
      unique positive values. Missing merge requests appear in `missing_iids`.
    - System notes and threads with no human notes are skipped.
    - `line` is the new-file line. `old_line` replaces it on a deleted line.
    - `notes_truncated` means the notes page has another page. Heading `note_count`, `last_by`, and `last_at` only
      cover fetched human notes when this is true, and the `author` filter can miss a late reply.
    - Trailing `review-bot:finding` comments are removed.
    - `view="headings"` returns thread details, the root human note's first Markdown heading (or first nonempty line),
      the latest fetched human note's author and date, and the number of fetched human notes. Headings are capped at
      160 characters. It includes no note bodies or replies.
    - `view="finding"` returns full notes like `full`, but compacts recognized bot findings. Numbered findings lose
      separate greetings and signoffs. Section findings keep the bold title and the "Needs to be improved", "Failure
      scenario", and "How to fix" sections, including their code and diagrams. A bold-heading action-bullet format
      loses its introductory paragraph. Human notes and unrecognized formats remain unchanged.
    - In full and finding views, note bodies are cut at `max_body_chars` (default 4000, maximum 4000). Zero omits
      body text. A call returns about 30000 body characters. Headings view returns about 30000 serialized characters.
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

    After a successful `resolve=true`, `open_threads` is sent once per merge request, on that MR's last item with
    `resolve=true`. It is left out if that count cannot be read or if that last resolution failed.
    """
    client = get_client(ctx)
    last_resolve = _last_resolve_positions(replies)
    results: list[ThreadReplyBatchItem] = []
    for position, reply in enumerate(replies):
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
            uncertain = (
                isinstance(exc, GitLabConnectionError)
                or (
                    isinstance(exc, GitLabAPIError)
                    and (exc.status == HTTP_REQUEST_TIMEOUT or exc.status >= HTTP_SERVER_ERROR)
                )
                or "outcome is unknown" in str(exc).lower()
            )
            warning = str(exc)
            if uncertain and "outcome is unknown" not in warning.lower():
                warning = f"Write outcome is unknown: {warning} Check get_merge_request_threads before retrying."
            results.append(
                ThreadReplyBatchItem(
                    iid=reply.iid,
                    thread_id=thread_id,
                    status="unknown" if uncertain else "failed",
                    resolved=None,
                    warning=warning,
                )
            )
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
        open_threads = None
        if outcome.resolved and last_resolve.get(reply.iid) == position:
            open_threads = await _read_open_threads(client, project, reply.iid)
        results.append(
            ThreadReplyBatchItem(
                iid=reply.iid,
                thread_id=outcome.thread_id,
                status=status,
                resolved=outcome.resolved,
                open_threads=open_threads,
                warning=warning,
            )
        )
    return ThreadReplyBatchResult(results=results)


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
        return MergeRequestDiff(head_sha=head_sha, files=_to_file_stats(stats, patch_ranges))

    diffs, next_cursor, matched = await _scan_diffs(client, project, iid, set(wanted), start)
    # A resumed call did not read the first page, so it cannot prove a path is missing.
    not_found = [path for path in wanted if path not in matched] if cursor is None and not next_cursor else []
    return MergeRequestDiff(head_sha=head_sha, diffs=diffs, next_cursor=next_cursor, paths_not_found=not_found)


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
