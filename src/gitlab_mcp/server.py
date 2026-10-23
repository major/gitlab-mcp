"""FastMCP server with GitLab tools for fast developer workflows 🚀."""

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from urllib.parse import quote
lazy from collections.abc import AsyncIterator, Awaitable, Sequence

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from pydantic import BaseModel, Field

from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError
from gitlab_mcp.config import ConfigError, Settings

# Upper bound on GraphQL pages scanned when approved, has_open_threads, or
# thread filters run on our side. Keeps one tool call from walking a huge project.
MAX_PAGES = 5
MAX_BATCH_MERGE_REQUESTS = 20

# One request per page. `open_threads` is computed from the two counts.
MERGE_REQUESTS_QUERY = """
query(
  $fullPath: ID!, $state: MergeRequestState, $authorUsername: String, $search: String, $first: Int!, $after: String
) {
  project(fullPath: $fullPath) {
    mergeRequests(
      state: $state, authorUsername: $authorUsername, search: $search, first: $first, after: $after, sort: UPDATED_DESC
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        iid title state draft updatedAt
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
query($fullPath: ID!, $iid: String!, $includeDescription: Boolean!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      iid title description @include(if: $includeDescription) state draft sourceBranch targetBranch
      author { username }
      diffRefs { headSha }
      detailedMergeStatus shouldBeRebased conflicts
      approved approvedBy { nodes { username } }
      reviewers { nodes { username mergeRequestInteraction { reviewState } } }
      headPipeline { status }
      autoMergeStrategy
      diffStatsSummary { additions deletions fileCount }
      resolvableDiscussionsCount resolvedDiscussionsCount
      discussions(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id resolvable resolved
          notes(first: 1) {
            count
            nodes { system author { username } position { oldPath newPath oldLine newLine } }
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
      discussions(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id resolvable resolved
          notes(first: 50) {
            pageInfo { hasNextPage }
            nodes {
              system body createdAt
              author { username }
              position { oldPath newPath oldLine newLine }
            }
          }
        }
      }
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
# Discussion GIDs start with this. Output IDs are bare SHAs, so the prefix is only stripped from input.
DISCUSSION_PREFIX = "gid://gitlab/Discussion/"
# Maps each state filter to the thread status it keeps. "all" keeps every status.
STATE_STATUS = {"open": "open", "resolved": "resolved", "comments": "comment"}

# Total patch characters that one diff call may return. A single patch is cut to this too.
DIFF_BUDGET = 40000
# Files per page of the REST diffs endpoint, the largest page GitLab serves.
DIFF_PAGE_SIZE = 100

# A Literal is sent to the agent as an enum, so this type replaces a separate
# "list states" tool.
MergeRequestState = Literal["opened", "closed", "locked", "merged", "all"]

# Column order of the TSV output. Every row has every column, so agents
# never have to guess whether a missing key means "empty" or "not sent".
TSV_COLUMNS = ("iid", "title", "author", "state", "draft", "approved", "approved_by", "open_threads", "updated")

# Shared argument types. Every tool repeats these in the catalog, so the descriptions stay short. ✂️
Project = Annotated[str, Field(description="Project path, like group/project.")]
Iid = Annotated[int, Field(description="MR number.")]
PositiveIid = Annotated[int, Field(strict=True, ge=1)]


@lifespan
async def gitlab_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Open one shared GitLab client for the life of the server.

    A config problem does not crash the server. Instead, tools report it,
    so the agent can tell the user exactly what to fix.
    """
    try:
        client = GitLabClient(Settings.from_env())
    except ConfigError as exc:
        yield {"client": None, "config_error": str(exc)}
        return
    try:
        yield {"client": client, "config_error": None}
    finally:
        await client.aclose()


mcp = FastMCP(
    name="gitlab-mcp",
    instructions=(
        "Tools for GitLab developer workflows on gitlab.com or self-managed "
        "GitLab. Call get_current_user first to learn who you are acting as."
    ),
    lifespan=gitlab_lifespan,
)


def get_client(ctx: Context) -> GitLabClient:
    """Return the shared GitLab client or raise a helpful ToolError."""
    state = ctx.lifespan_context
    if state.get("client") is None:
        raise ToolError(state.get("config_error") or "GitLab client is not configured.")
    return state["client"]


class CurrentUser(BaseModel):
    """Authenticated GitLab user."""

    # Kept small on purpose (like `glab api user --jq .username`) because this
    # schema is sent to the agent on every tools/list. `id` is included since
    # assignee, reviewer, and author filters need the numeric ID.
    username: str
    id: int


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_current_user(ctx: Context) -> CurrentUser:
    """Return the username and ID of the user that owns the GitLab token.

    Equivalent to `glab api user --jq .username`. Use it to resolve "me"
    before filtering issues, merge requests, or reviews.
    """
    user = await get_client(ctx).get("/user")
    return CurrentUser(username=user["username"], id=user["id"])


class MergeRequestSummary(BaseModel):
    """One merge request row, with every field the TSV output needs.

    Values are kept as-is here. `_to_tsv` turns them into cells.
    """

    iid: int
    title: str
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
            "An empty cell means none. Booleans are true/false."
        ),
    )
    has_more: bool


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


class ProjectNotFoundError(ToolError):
    """The project does not exist, or the token cannot see it."""

    def __init__(self, project: str) -> None:
        """Name the project so the user can check the path."""
        super().__init__(f"Project {project!r} not found, or the token cannot see it.")


class MergeRequestNotFoundError(ToolError):
    """The merge request does not exist, or the token cannot see it."""

    def __init__(self, project: str, iid: int) -> None:
        """Name the IID and project so the user can check both."""
        super().__init__(f"Merge request !{iid} not found in {project!r}, or the token cannot see it.")


class InvalidCursorError(ToolError):
    """The cursor was not made by the tool it was passed to, or was changed."""

    def __init__(self, tool: str) -> None:
        """Tell the agent to pass next_cursor back unchanged to the same tool."""
        super().__init__(f"Invalid cursor. Pass next_cursor from an earlier {tool} call, unchanged.")


class Reviewer(BaseModel):
    """A requested reviewer."""

    username: str
    review_state: str = Field(default="", exclude_if=lambda v: not v)  # Lowercased, like "reviewed".


class ThreadLocation(BaseModel):
    """Where a thread points. General threads have none of these fields."""

    path: str = Field(default="", exclude_if=lambda v: not v)  # The new path, or the old one if the file is gone.
    old_path: str = Field(default="", exclude_if=lambda v: not v)  # Only when it differs from `path`.
    line: int = Field(default=0, exclude_if=lambda v: not v)  # New-file line.
    old_line: int = Field(default=0, exclude_if=lambda v: not v)  # Only for a comment on a deleted line.


class ThreadRef(ThreadLocation):
    """An open thread."""

    id: str
    by: str = Field(default="", exclude_if=lambda v: not v)  # Author of the first note.
    notes: int  # Notes in the thread, including replies.


class ThreadIndex(BaseModel):
    """Thread counts and open threads."""

    open_count: int
    resolved_count: int
    open: list[ThreadRef] = Field(default_factory=list, exclude_if=lambda v: not v)
    index_complete: bool


# "comment" means the discussion cannot be resolved, like a plain MR comment.
ThreadStatus = Literal["open", "resolved", "comment"]


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


class ThreadList(BaseModel):
    """One page of threads."""

    threads: list[Thread]
    next_cursor: str = Field(default="", exclude_if=lambda v: not v)  # Empty when the scan finished.
    not_found_ids: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)


class ThreadReplyResult(BaseModel):
    """The result of replying to or resolving one discussion thread."""

    thread_id: str
    note_id: int | None = Field(default=None, exclude_if=lambda value: value is None)
    resolved: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    warning: str = Field(default="", exclude_if=lambda value: not value)


class DiffSummary(BaseModel):
    """Diff size totals."""

    files: int
    additions: int
    deletions: int


class FileStat(BaseModel):
    """A changed file."""

    path: str
    additions: int
    deletions: int


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
    files: list[FileStat] = Field(default_factory=list, exclude_if=lambda v: not v)
    diffs: list[FileDiff] = Field(default_factory=list, exclude_if=lambda v: not v)
    next_cursor: str = Field(default="", exclude_if=lambda v: not v)  # Empty when the scan finished.
    paths_not_found: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)


class MergeRequestDetail(BaseModel):
    """One merge request's status facts."""

    iid: int
    title: str
    state: str
    draft: bool = Field(default=False, exclude_if=lambda v: not v)
    author: str = Field(default="", exclude_if=lambda v: not v)
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


def _to_tsv(mrs: list[MergeRequestSummary]) -> str:
    """Render merge requests as TSV: a header row, then one line per MR, no trailing newline."""
    lines = ["\t".join(TSV_COLUMNS)]
    lines.extend("\t".join(_cell(getattr(mr, column)) for column in TSV_COLUMNS) for mr in mrs)
    return "\n".join(lines)


def _to_summary(node: dict[str, Any]) -> MergeRequestSummary:
    """Convert one GraphQL merge request node into a summary."""
    # A deleted or banned author comes back as null.
    author = node["author"]
    return MergeRequestSummary(
        iid=int(node["iid"]),  # GraphQL sends IIDs as strings.
        title=node["title"],
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
        _lower(car["status"]),
        pipeline_id,
        _lower(pipeline["status"]) if pipeline else "",
    )
    return "\t".join(_cell(value) for value in values)


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
    `approved` and `has_open_threads` are filtered after fetching, so the tool
    scans at most 5 pages. If `has_more` is true but fewer than `limit` MRs
    came back, the scan stopped early and more matches may exist.
    """
    client = get_client(ctx)
    # Client-side filters can drop nodes, so fetch big pages to find matches faster.
    filtered = approved is not None or has_open_threads is not None
    variables: dict[str, Any] = {
        "fullPath": project,
        "state": state,
        "authorUsername": author,
        "search": search,
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
        summaries = [_to_summary(node) for node in connection["nodes"]]
        collected.extend(mr for mr in summaries if _matches(mr, approved, has_open_threads))
        page_info = connection["pageInfo"]
        has_next = page_info["hasNextPage"]
        if len(collected) >= limit or not has_next:
            break
        variables["after"] = page_info["endCursor"]

    return MergeRequestList(
        web_url_prefix=f"{client.settings.url}/{project}/-/merge_requests/",
        merge_requests=_to_tsv(collected[:limit]),
        has_more=len(collected) > limit or has_next,
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


def _lower(value: str | None) -> str:
    """Lowercase a GitLab enum value. Null means unknown, so it becomes empty and is left out."""
    return (value or "").lower()


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
    return Reviewer(username=node["username"], review_state=_lower(review_state))


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


def _position_fields(position: dict[str, Any] | None) -> dict[str, Any]:
    """Map a GraphQL note position to the location fields, stating each fact once.

    Only fields with a value are returned, so the models fill in the rest.
    General threads have no position, so the result is empty.
    """
    position = position or {}
    old_path, new_path = position.get("oldPath"), position.get("newPath")
    path = new_path or old_path
    new_line, old_line = position.get("newLine"), position.get("oldLine")
    fields: dict[str, Any] = {}
    if path:
        fields["path"] = path
    if old_path and old_path != path:
        fields["old_path"] = old_path
    # A comment on an unchanged line has both line numbers, and the new one is enough.
    # A comment on a deleted line has only oldLine, so that one is kept.
    if new_line is not None:
        fields["line"] = new_line
    elif old_line is not None:
        fields["old_line"] = old_line
    return fields


def _to_thread_ref(discussion: dict[str, Any]) -> ThreadRef:
    """Convert one open discussion into a thread ref, using its first note for the details."""
    notes = discussion["notes"]
    first = notes["nodes"][0]
    author = first["author"]  # A deleted or banned author comes back as null.
    return ThreadRef(
        id=_bare_id(discussion["id"]),
        by=author["username"] if author else "",
        notes=notes["count"],
        **_position_fields(first["position"]),
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


async def _fetch_merge_request(
    client: GitLabClient,
    query: str,
    project: str,
    iid: int,
    **variables: Any,
) -> dict[str, Any]:
    """Run a query for one merge request and return its node.

    Raises ProjectNotFoundError or MergeRequestNotFoundError when GitLab has no
    such project or merge request, because it returns null for both.
    """
    data = await client.graphql(query, {"fullPath": project, "iid": str(iid), **variables})
    if data["project"] is None:
        raise ProjectNotFoundError(project)
    node = data["project"]["mergeRequest"]
    if node is None:
        raise MergeRequestNotFoundError(project, iid)
    return node


def _head_sha(diff_refs: dict[str, Any] | None) -> str:
    """Return the full head commit SHA. Null diffRefs means unknown, so it is empty.

    The full SHA is kept on purpose. Write tools send it back as a guard, and GitLab compares it exactly.
    """
    return diff_refs["headSha"] if diff_refs else ""


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
    description, description_truncated = _cap_description(
        node.get("description") if include_description else None,
        description_limit,
    )
    return MergeRequestDetail(
        iid=int(node["iid"]),  # GraphQL sends IIDs as strings.
        title=node["title"],
        state=node["state"],
        draft=node["draft"],
        author=author["username"] if author else "",
        source_branch=node["sourceBranch"],
        target_branch=node["targetBranch"],
        head_sha=_head_sha(node["diffRefs"]),
        description=description,
        description_truncated=description_truncated,
        merge_status=_lower(node["detailedMergeStatus"]),
        should_be_rebased=node["shouldBeRebased"],
        conflicts=node["conflicts"],
        approved=node["approved"],
        approved_by=[user["username"] for user in node["approvedBy"]["nodes"]],
        reviewers=[_to_reviewer(reviewer) for reviewer in node["reviewers"]["nodes"]],
        pipeline_status=_lower(pipeline["status"]) if pipeline else "",
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
    description, description_truncated = _cap_description(
        node.get("description") if include_description else None,
        description_limit,
    )
    return CompactMergeRequest(
        iid=int(node["iid"]),  # GraphQL sends IIDs as strings.
        title=node["title"],
        state=node["state"],
        draft=node["draft"],
        author=author["username"] if author else "",
        source_branch=node["sourceBranch"],
        target_branch=node["targetBranch"],
        head_sha=_head_sha(node["diffRefs"]),
        merge_status=_lower(node["detailedMergeStatus"]),
        should_be_rebased=node["shouldBeRebased"],
        conflicts=node["conflicts"],
        approved=node["approved"],
        approved_by=[user["username"] for user in node["approvedBy"]["nodes"]],
        pipeline_status=_lower(pipeline["status"]) if pipeline else "",
        description=description,
        description_truncated=description_truncated,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_merge_request(
    ctx: Context,
    project: Project,
    iid: Iid,
    include_description: Annotated[bool, Field(description="False skips the description to save tokens.")] = True,
    description_limit: Annotated[
        int,
        Field(ge=0, le=DESCRIPTION_CAP, description="Maximum description length in characters, from 0 to 4000."),
    ] = DESCRIPTION_CAP,
) -> MergeRequestDetail:
    """Show one merge request's status, approvals, and open threads. Like `glab mr view`.

    - `merge_status` is the source of truth for mergeability. Open threads and failed CI do not always block a merge.
    - `approved`: Free means one approval. Premium and Ultimate mean all approval rules pass.
    - `auto_merge_strategy`: `merge_when_checks_pass`, `merge_train`, `add_to_merge_train_when_checks_pass` (waiting).
    - `threads.open` IDs go to get_merge_request_threads. `index_complete` false means some are missing.
    - `line` is the new-file line. `old_line` replaces it on a deleted line.
    - Empty fields are left out: a missing `draft` means false, a missing list is empty.
    - `description_limit` controls description length. Zero skips it; the default is 4000 characters.
    """
    include_description = include_description and description_limit > 0
    node = await _fetch_merge_request(
        get_client(ctx),
        MERGE_REQUEST_QUERY,
        project,
        iid,
        includeDescription=include_description,
    )
    return _to_detail(node, include_description=include_description, description_limit=description_limit)


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


def _bare_id(thread_id: str) -> str:
    """Strip the discussion GID prefix. Bare SHAs pass through unchanged."""
    return thread_id.removeprefix(DISCUSSION_PREFIX)


def _bare_ids(ids: list[str]) -> list[str]:
    """Return bare discussion SHAs in input order, without duplicates. Full GIDs and bare SHAs both work."""
    return list(dict.fromkeys(_bare_id(thread_id) for thread_id in ids))


def _encode_cursor(data: dict[str, Any]) -> str:
    """Pack a tool's resume position into one opaque, URL-safe string."""
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def _decode_cursor(cursor: str, tool: str) -> dict[str, Any]:
    """Unpack a cursor made by `_encode_cursor`. Each tool checks the keys it needs.

    Anything that is not base64 JSON holding an object raises InvalidCursorError.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor))
    except ValueError as exc:  # Bad base64 and bad JSON both raise ValueError subclasses.
        raise InvalidCursorError(tool) from exc
    if not isinstance(payload, dict):
        raise InvalidCursorError(tool)
    return payload


def _is_int_at_least(value: Any, minimum: int) -> bool:
    """Return True for an int of at least `minimum`. Booleans are ints in Python, so they are rejected too."""
    return type(value) is int and value >= minimum


def _thread_cursor_start(cursor: str) -> tuple[str | None, int]:
    """Return the page cursor and skip count for a threads call. Anything else is an InvalidCursorError."""
    payload = _decode_cursor(cursor, "get_merge_request_threads")
    after = payload.get("after")
    if not (after is None or isinstance(after, str)) or not _is_int_at_least(payload.get("skip"), 0):
        raise InvalidCursorError("get_merge_request_threads")
    return after, payload["skip"]


def _thread_status(discussion: dict[str, Any]) -> ThreadStatus:
    """Return the status of a discussion. Only resolvable threads can be open or resolved."""
    if not discussion["resolvable"]:
        return "comment"
    return "resolved" if discussion["resolved"] else "open"


def _cap_note_body(body: str, max_body_chars: int = NOTE_CAP) -> str:
    """Remove a trailing review finding, then cap the remaining note body."""
    finding = REVIEW_BOT_FINDING_PATTERN.search(body)
    if finding:
        body = body[: finding.start()].rstrip()
    if max_body_chars == 0:
        return ""
    if len(body) <= max_body_chars:
        return body
    return body[:max_body_chars] + THREAD_CUT_MARKER


def _to_note(node: dict[str, Any], max_body_chars: int = NOTE_CAP) -> Note:
    """Convert one GraphQL note into a note with a capped body and a date-only timestamp."""
    author = node["author"]  # A deleted or banned author comes back as null.
    return Note(
        by=author["username"] if author else "",
        body=_cap_note_body(node["body"], max_body_chars=max_body_chars),
        at=node["createdAt"][:10],  # ISO 8601 timestamp, keep the date part.
    )


def _to_thread(discussion: dict[str, Any], max_body_chars: int = NOTE_CAP) -> Thread | None:
    """Convert one discussion into a thread, or return None when it has no human notes.

    System notes, like "assigned to @user", are dropped. The location comes
    from the first human note, because a system note has no position.
    """
    notes = discussion["notes"]
    human = [node for node in notes["nodes"] if not node["system"]]
    if not human:
        return None
    return Thread(
        id=_bare_id(discussion["id"]),
        status=_thread_status(discussion),
        notes=[_to_note(node, max_body_chars=max_body_chars) for node in human],
        # GitLab only returns the first 50 notes, so more may exist.
        notes_truncated=notes["pageInfo"]["hasNextPage"],
        **_position_fields(human[0]["position"]),
    )


@dataclass
class _ThreadFilter:
    """The filters of one call. A thread must pass all of them."""

    state: str
    ids: list[str] | None
    file: str | None
    author: str | None

    def matches(self, thread: Thread) -> bool:
        """Return True when the thread passes every filter."""
        # Explicit IDs replace the state filter, so a known thread is returned in any state.
        if self.ids is not None:
            if thread.id not in self.ids:
                return False
        elif self.state != "all" and STATE_STATUS[self.state] != thread.status:
            return False
        if self.file is not None and self.file not in (thread.path, thread.old_path):
            return False
        # Only the first 50 notes are fetched, so this can miss a late reply.
        return self.author is None or any(note.by == self.author for note in thread.notes)


async def _scan_threads(  # noqa: PLR0913, PLR0917 - each input describes the scan.
    client: GitLabClient,
    project: str,
    iid: int,
    thread_filter: _ThreadFilter,
    start: tuple[str | None, int],
    max_body_chars: int = NOTE_CAP,
) -> tuple[list[Thread], str, set[str]]:
    """Read discussion pages from `start` and collect the matching threads.

    The scan stops at the budget, at `MAX_PAGES`, or at the last page. Returns
    the threads, the cursor to resume from (empty when every page was read),
    and every discussion ID that was read, matching or not.
    """
    after, skip = start
    threads: list[Thread] = []
    seen: set[str] = set()
    body_chars = 0
    for _ in range(MAX_PAGES):
        node = await _fetch_merge_request(client, THREADS_QUERY, project, iid, after=after)
        connection = node["discussions"]
        nodes = connection["nodes"]
        for index in range(skip, len(nodes)):
            seen.add(_bare_id(nodes[index]["id"]))
            thread = _to_thread(nodes[index], max_body_chars=max_body_chars)
            if thread is None or not thread_filter.matches(thread):
                continue
            size = sum(len(note.body) for note in thread.notes)
            # Always return at least one thread, even when that thread alone is over budget.
            if threads and body_chars + size > THREAD_BODY_BUDGET:
                return threads, _encode_cursor({"after": after, "skip": index}), seen
            threads.append(thread)
            body_chars += size
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return threads, "", seen
        after, skip = page_info["endCursor"], 0
    # The page limit was hit. `after` is now the cursor of the next unread page.
    return threads, _encode_cursor({"after": after, "skip": 0}), seen


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_merge_request_threads(  # noqa: PLR0913, PLR0917 - each argument is a user-facing filter.
    ctx: Context,
    project: Project,
    iid: Iid,
    state: Literal["open", "resolved", "comments", "all"] = "open",
    ids: Annotated[list[str] | None, Field(description="Thread IDs from threads.open. Ignores state.")] = None,
    file: Annotated[str | None, Field(description="Only threads on this path, old or new.")] = None,
    author: Annotated[str | None, Field(description="Only threads with a note by this user.")] = None,
    max_body_chars: Annotated[
        int,
        Field(ge=0, le=NOTE_CAP, description="Maximum body characters per note. Zero omits bodies."),
    ] = NOTE_CAP,
    cursor: Annotated[
        str | None,
        Field(description="next_cursor from an earlier call. Repeat the filters and max_body_chars."),
    ] = None,
) -> ThreadList:
    """Read review thread notes, with filters and a size budget. Like `glab mr note list`.

    - `state`: `open` (default), `resolved`, `comments` (cannot be resolved), or `all`. `ids` ignore `state`.
    - System notes and threads with no human notes are skipped.
    - `line` is the new-file line. `old_line` replaces it on a deleted line.
    - `notes_truncated`: over 50 notes, so `author` can miss a late reply.
    - Trailing `review-bot:finding` comments are removed.
    - Note bodies are cut at `max_body_chars` (default 4000, maximum 4000). Zero omits body text. A call returns
      about 30000 body characters. Continue with `next_cursor` using the same filters and `max_body_chars`.
    - `not_found_ids` is only set after a full scan from the first page.
    """
    client = get_client(ctx)
    start = _thread_cursor_start(cursor) if cursor else (None, 0)
    # An empty list means no IDs were given, so the state filter applies.
    wanted = _bare_ids(ids) if ids else None
    thread_filter = _ThreadFilter(state=state, ids=wanted, file=file, author=author)
    threads, next_cursor, seen = await _scan_threads(client, project, iid, thread_filter, start, max_body_chars)

    # A resumed call did not read the first page, so it cannot prove an ID is missing.
    not_found_ids: list[str] = []
    if wanted and cursor is None and not next_cursor:
        not_found_ids = [thread_id for thread_id in wanted if thread_id not in seen]
    return ThreadList(threads=threads, next_cursor=next_cursor, not_found_ids=not_found_ids)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def reply_to_thread(  # noqa: C901, PLR0913, PLR0917 - user-facing inputs and distinct write outcomes.
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
    """
    bare_thread_id = _bare_id(thread_id)
    if not bare_thread_id.strip():
        raise ToolError("thread_id must not be empty.")  # noqa: TRY003
    if body is not None and not body.strip():
        raise ToolError("body must not be empty or whitespace.")  # noqa: TRY003
    if body is None and not resolve:
        raise ToolError("Provide a reply body or set resolve=True.")  # noqa: TRY003

    client = get_client(ctx)
    discussion_path = f"{_mr_rest_path(project, iid)}/discussions/{quote(bare_thread_id, safe='')}"
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


def _to_file_stats(stats: list[dict[str, Any]] | None) -> list[FileStat]:
    """Convert the diffStats list. GitLab can return null, which means no files are known."""
    return [
        FileStat(path=stat["path"], additions=stat["additions"], deletions=stat["deletions"]) for stat in stats or []
    ]


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
    payload = _decode_cursor(cursor, "get_merge_request_diff")
    if not _is_int_at_least(payload.get("page"), 1) or not _is_int_at_least(payload.get("skip"), 0):
        raise InvalidCursorError("get_merge_request_diff")
    return payload["page"], payload["skip"]


async def _fetch_diff_page(
    client: GitLabClient,
    project: str,
    iid: int,
    page: int,
) -> tuple[list[dict[str, Any]], int | None]:
    """Fetch one page of merge request diffs and return the files and the next page number."""
    return await client.get_page(
        f"{_mr_rest_path(project, iid)}/diffs",
        per_page=DIFF_PAGE_SIZE,
        page=page,
    )


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
        files, next_page = await _fetch_diff_page(client, project, iid, page)
        for index in range(skip, len(files)):
            hits = _requested_hits(files[index], wanted)
            if not hits:
                continue
            diff = _to_file_diff(files[index])
            # Always return at least one patch, even when that one alone is over budget.
            if diffs and total + len(diff.diff) > DIFF_BUDGET:
                return diffs, _encode_cursor({"page": page, "skip": index}), matched
            diffs.append(diff)
            total += len(diff.diff)
            matched |= hits
        if next_page is None:
            return diffs, "", matched
        # Later pages start at their first file.
        page, skip = next_page, 0
    # The page limit was hit. `page` is now the next unread page.
    return diffs, _encode_cursor({"page": page, "skip": 0}), matched


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_merge_request_diff(
    ctx: Context,
    project: Project,
    iid: Iid,
    paths: Annotated[
        list[str] | None,
        Field(description="Paths to read patches for, old or new. Omit for the file list."),
    ] = None,
    cursor: Annotated[str | None, Field(description="next_cursor from an earlier call, with the same paths.")] = None,
) -> MergeRequestDiff:
    """List a merge request's changed files, or read patches of chosen files. Like `glab mr diff`.

    - Call without `paths` for a file list with +/- counts, then with `paths` for patches.
    - `head_sha` changes when new commits are pushed, so older patches may be stale.
    - `status`: added, deleted, renamed, or modified. `old_path` is only set when it differs.
    - `collapsed`: hidden as big, the web UI may show it. `too_large`: never shown. `generated`: usually skippable.
    - Up to 40000 patch characters and 5 pages per call. Cut patches have `truncated`. Next: `next_cursor`, same paths.
    - `paths_not_found` is only set after a full scan from the first page. GitLab limits can still hide a file.
    """
    client = get_client(ctx)
    wanted = list(dict.fromkeys(paths)) if paths else []
    # Decode the cursor before any request, so a bad one fails fast. The file list ignores it.
    start = _diff_cursor_start(cursor) if wanted and cursor is not None else (1, 0)
    node = await _fetch_merge_request(client, DIFF_FILES_QUERY, project, iid)
    head_sha = _head_sha(node["diffRefs"])

    if not wanted:
        return MergeRequestDiff(head_sha=head_sha, files=_to_file_stats(node["diffStats"]))

    diffs, next_cursor, matched = await _scan_diffs(client, project, iid, set(wanted), start)
    # A resumed call did not read the first page, so it cannot prove a path is missing.
    not_found = [path for path in wanted if path not in matched] if cursor is None and not next_cursor else []
    return MergeRequestDiff(head_sha=head_sha, diffs=diffs, next_cursor=next_cursor, paths_not_found=not_found)


# How a land request is sent. "auto" queues the merge, "now" merges right away, "cancel" undoes a queued merge.
LandMode = Literal["auto", "now", "cancel"]
# What happened after a land request. "pending" means GitLab accepted it, but nothing has merged or queued yet.
LandResult = Literal["merge_train", "auto_merge", "merged", "cancelled", "pending"]
# GitLab's strategy names for an MR that waits in the merge train. Compared lowercased.
TRAIN_STRATEGIES = frozenset({"merge_train", "add_to_merge_train_when_checks_pass"})
# Statuses GitLab sends when it refuses a merge or cancel because of the MR's current state.
REFUSED_STATUSES = frozenset({405, 406, 409, 422})

# The state that a land request reads back. Only the fields that decide the result.
LAND_QUERY = """
query($fullPath: ID!, $iid: String!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      state detailedMergeStatus autoMergeStrategy mergeCommitSha
    }
  }
}
"""


class MergeRequestLanding(BaseModel):
    """What a land request did, read back from GitLab after the write."""

    result: LandResult
    state: str = Field(default="", exclude_if=lambda v: not v)  # For example "opened" or "merged".
    merge_status: str = Field(default="", exclude_if=lambda v: not v)  # GitLab's detailed status, lowercased.
    auto_merge_strategy: str = Field(default="", exclude_if=lambda v: not v)  # Lowercased.
    merge_commit_sha: str = Field(default="", exclude_if=lambda v: not v)
    warning: str = Field(default="", exclude_if=lambda v: not v)  # Set when the write worked but the read did not.


class ShaRequiredError(ToolError):
    """A merge needs the head SHA, and none was given."""

    def __init__(self, mode: str) -> None:
        """Name the mode and point at where the SHA comes from."""
        super().__init__(f"Mode {mode!r} needs sha, the full head SHA from get_merge_request.")


class CancelOptionsError(ToolError):
    """A cancel was sent with merge options, which it does not take."""

    def __init__(self) -> None:
        """Tell the agent which arguments to drop."""
        super().__init__("Mode 'cancel' takes only project and iid. Drop sha, squash, and remove_source_branch.")


class OutcomeUnknownError(ToolError):
    """The connection failed during a write, so GitLab may or may not have acted."""

    def __init__(self, cause: GitLabConnectionError) -> None:
        """Keep the connection detail and say the outcome is unknown."""
        super().__init__(f"{cause} The outcome is unknown. Check get_merge_request before retrying.")


class MergeRefusedError(ToolError):
    """GitLab refused the merge or cancel because of the MR's current state."""

    def __init__(self, error: GitLabAPIError) -> None:
        """Keep GitLab's reason and point at the blockers tool."""
        super().__init__(f"{error} Check blockers with get_merge_request.")


def _mr_rest_path(project: str, iid: int) -> str:
    """Return the REST path of one merge request.

    The project path is URL-encoded, so `group/project` becomes `group%2Fproject`.
    """
    encoded = quote(project, safe="")
    return f"/projects/{encoded}/merge_requests/{iid}"


def _check_land_args(mode: LandMode, sha: str | None, squash: bool | None, remove_source_branch: bool | None) -> None:
    """Refuse a bad land request before anything is sent to GitLab.

    `auto` and `now` need the head SHA. GitLab then merges only the commits
    that were reviewed. `cancel` takes no merge options, so any option given
    with it is a mistake.
    """
    if mode == "cancel":
        if sha is not None or squash is not None or remove_source_branch is not None:
            raise CancelOptionsError
    elif not sha:
        raise ShaRequiredError(mode)


def _merge_body(
    mode: LandMode, sha: str | None, squash: bool | None, remove_source_branch: bool | None
) -> dict[str, Any]:
    """Build the JSON body for the merge request. Unset options are left out, so GitLab uses the project defaults."""
    body: dict[str, Any] = {"sha": sha}
    if mode == "auto":
        body["auto_merge"] = True
    if squash is not None:
        body["squash"] = squash
    if remove_source_branch is not None:
        body["should_remove_source_branch"] = remove_source_branch
    return body


def _land_result(mode: LandMode, node: dict[str, Any]) -> LandResult:
    """Name what GitLab did, from the MR state read back after the write.

    A merged MR wins over any strategy still set on it. A cancel counts as
    cancelled only when no strategy is left.
    """
    strategy = _lower(node["autoMergeStrategy"])
    if node["state"] == "merged":
        return "merged"
    if strategy in TRAIN_STRATEGIES:
        return "merge_train"
    if strategy:
        return "auto_merge"
    # 🚦 Nothing is queued and nothing merged, so a cancel went through.
    return "cancelled" if mode == "cancel" else "pending"


def _to_landing(mode: LandMode, node: dict[str, Any]) -> MergeRequestLanding:
    """Convert the read-back state into the landing result."""
    return MergeRequestLanding(
        result=_land_result(mode, node),
        state=node["state"],
        merge_status=_lower(node["detailedMergeStatus"]),
        auto_merge_strategy=_lower(node["autoMergeStrategy"]),
        merge_commit_sha=node["mergeCommitSha"] or "",
    )


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
async def land_merge_request(  # noqa: PLR0913, PLR0917 - each argument is a user-facing option.
    ctx: Context,
    project: Project,
    iid: Iid,
    sha: Annotated[
        str | None,
        Field(description="Full head SHA from get_merge_request. Required for auto and now."),
    ] = None,
    mode: Annotated[LandMode, Field(description="auto (default), now, or cancel.")] = "auto",
    squash: Annotated[bool | None, Field(description="Squash the commits. Omit for the project default.")] = None,
    remove_source_branch: Annotated[
        bool | None,
        Field(description="Delete the source branch after merging. Omit for the project default."),
    ] = None,
) -> MergeRequestLanding:
    """Merge a merge request, or cancel a queued merge. Like `glab mr merge`.

    - `auto` (default): join the merge train when the project has one, otherwise merge when checks pass.
    - `now`: merge right away and skip the train. Use it only when the user asks for it.
    - `cancel`: leave the merge train or cancel auto-merge. Takes no `sha`, `squash`, or `remove_source_branch`.
    - Get `sha` from get_merge_request. GitLab refuses the merge with 409 when the head has moved.
    - `result`: `merge_train`, `auto_merge`, `merged`, `cancelled`, or `pending`. Use get_merge_request to see blockers.
    - A refused merge (405, 406, 409, 422) says to check blockers. Never retry a failed write without checking first.
    """
    _check_land_args(mode, sha, squash, remove_source_branch)
    client = get_client(ctx)
    path = _mr_rest_path(project, iid)
    try:
        if mode == "cancel":
            await client.request("POST", f"{path}/cancel_merge_when_pipeline_succeeds")
        else:
            await client.request("PUT", f"{path}/merge", json=_merge_body(mode, sha, squash, remove_source_branch))
    except GitLabConnectionError as exc:
        # The request may have reached GitLab before the connection failed, so the outcome is unknown.
        raise OutcomeUnknownError(exc) from exc
    except GitLabAPIError as exc:
        if exc.status not in REFUSED_STATUSES:
            raise
        raise MergeRefusedError(exc) from exc

    # The write was accepted, so a failed read must not look like a failed merge. 🧾
    try:
        node = await _fetch_merge_request(client, LAND_QUERY, project, iid)
    except ToolError:
        return MergeRequestLanding(
            result="pending",
            warning="GitLab accepted the request, but its state could not be read. Check get_merge_request.",
        )
    return _to_landing(mode, node)


# Queries and mutations for update_merge_request. Each one changes one thing, so each step can fail on its own. 🔧
USERS_QUERY = """
query($usernames: [String!]) {
  users(usernames: $usernames) {
    nodes { username }
  }
}
"""

SET_DRAFT_MUTATION = """
mutation($projectPath: ID!, $iid: String!, $draft: Boolean!) {
  mergeRequestSetDraft(input: {projectPath: $projectPath, iid: $iid, draft: $draft}) {
    errors
  }
}
"""

REVIEWERS_MUTATION = """
mutation($projectPath: ID!, $iid: String!, $usernames: [String!]!, $mode: MutationOperationMode) {
  mergeRequestSetReviewers(
    input: {projectPath: $projectPath, iid: $iid, reviewerUsernames: $usernames, operationMode: $mode}
  ) {
    errors
  }
}
"""

ASSIGNEES_MUTATION = """
mutation($projectPath: ID!, $iid: String!, $usernames: [String!]!, $mode: MutationOperationMode) {
  mergeRequestSetAssignees(
    input: {projectPath: $projectPath, iid: $iid, assigneeUsernames: $usernames, operationMode: $mode}
  ) {
    errors
  }
}
"""

# Maps each person list to its mutation and the payload field that holds the errors.
PEOPLE_MUTATIONS = {
    "reviewers": (REVIEWERS_MUTATION, "mergeRequestSetReviewers"),
    "assignees": (ASSIGNEES_MUTATION, "mergeRequestSetAssignees"),
}

# A small re-read for the final state, so the tool does not fetch discussions it does not need.
MERGE_REQUEST_STATE_QUERY = """
query($fullPath: ID!, $iid: String!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      state draft detailedMergeStatus
      diffRefs { headSha }
    }
  }
}
"""

# Reasons that replace the generic HTTP hint for one step. Seen live on GitLab 19.2:
# approving your own MR gives 401 (not a bad token), and unapproving when you never approved gives 404.
STEP_STATUS_REASONS = {
    ("approve", 401): "You may not approve this MR, for example because you are its author.",
    ("unapprove", 404): "You have not approved this MR.",
}

# Blocks per page on the REST blocks endpoint. The largest page GitLab serves.
BLOCKS_PAGE_SIZE = 100


class MergeRequestUpdate(BaseModel):
    """What one update did. Each step appears once, in the list that matches its outcome."""

    # GitLab confirmed these changes.
    applied: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    # GitLab accepted these, but the work is not done yet. Rebase runs in the background.
    pending: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    # The write may have reached GitLab. Check with get_merge_request. Do not retry blindly.
    unknown: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    failed: dict[str, str] = Field(default_factory=dict, exclude_if=lambda v: not v)  # Step or item -> reason.
    state: str = Field(default="", exclude_if=lambda v: not v)  # opened, closed, merged, or locked.
    draft: bool = Field(default=False, exclude_if=lambda v: not v)
    merge_status: str = Field(default="", exclude_if=lambda v: not v)  # GitLab's detailed status, lowercased.
    head_sha: str = Field(default="", exclude_if=lambda v: not v)  # Full head SHA after the update.
    warning: str = Field(default="", exclude_if=lambda v: not v)  # Set when the final state could not be read.


class NothingToUpdateError(ToolError):
    """The call did not ask for any change."""

    def __init__(self) -> None:
        """Tell the agent to pass at least one change."""
        super().__init__("Nothing to update. Pass at least one change.")


class OverlapError(ToolError):
    """One item is both added and removed in the same call."""

    def __init__(self, label: str, names: list[str]) -> None:
        """Name the list type and the items, so the agent can fix the call."""
        super().__init__(f"Cannot add and remove the same {label} in one call: {', '.join(names)}.")


class ApprovalShaError(ToolError):
    """Approve was requested without the head SHA that guards it."""

    def __init__(self) -> None:
        """Point the agent to the SHA source."""
        super().__init__("approve=true needs sha, the full head SHA from get_merge_request.")


class ApprovalSeparateError(ToolError):
    """Approve was combined with a change that can undo it."""

    def __init__(self) -> None:
        """Ask the agent to make the approval its own call."""
        super().__init__("Approve in a separate call. Approval may not survive a rebase or a target branch change.")


def _check_overlap(add: Sequence[Any], remove: Sequence[Any], label: str) -> None:
    """Raise OverlapError when one item is both added and removed in the same call."""
    overlap = sorted(str(item) for item in set(add) & set(remove))
    if overlap:
        raise OverlapError(label, overlap)


@dataclass
class _Changes:
    """The changes that one update asks for. Checked before any write is sent."""

    approve: bool | None
    sha: str | None
    state: Literal["close", "reopen"] | None
    draft: bool | None
    title: str | None
    description: str | None
    target_branch: str | None
    add_labels: list[str]
    remove_labels: list[str]
    add_reviewers: list[str]
    remove_reviewers: list[str]
    add_assignees: list[str]
    remove_assignees: list[str]
    add_blocked_by: list[int]
    remove_blocked_by: list[int]
    rebase: bool

    def check(self) -> None:
        """Raise ToolError when the request asks for nothing, contradicts itself, or cannot be sent safely.

        Runs before any write, so a rejected request changes nothing.
        """
        if not self._has_change():
            raise NothingToUpdateError
        _check_overlap(self.add_labels, self.remove_labels, "label")
        _check_overlap(self.add_reviewers, self.remove_reviewers, "reviewer")
        _check_overlap(self.add_assignees, self.remove_assignees, "assignee")
        _check_overlap(self.add_blocked_by, self.remove_blocked_by, "blocking merge request")
        if self.approve and not self.sha:
            raise ApprovalShaError
        # Approval may not survive a rebase or a target branch change, so GitLab needs them in separate calls.
        if self.approve is not None and (self.rebase or self.target_branch is not None):
            raise ApprovalSeparateError

    def fields_body(self) -> dict[str, Any]:
        """Return the body of the one PUT for the plain fields. Only the fields that were given are included.

        An empty description is kept on purpose, because it clears the description.
        """
        given = {
            "state_event": self.state,
            "title": self.title,
            "description": self.description,
            "target_branch": self.target_branch,
        }
        body: dict[str, Any] = {key: value for key, value in given.items() if value is not None}
        if self.add_labels:
            body["add_labels"] = ",".join(self.add_labels)
        if self.remove_labels:
            body["remove_labels"] = ",".join(self.remove_labels)
        return body

    def _has_change(self) -> bool:
        """Return True when at least one field, list, or rebase was requested."""
        given = (self.approve, self.state, self.draft, self.title, self.description, self.target_branch)
        lists = (
            self.add_labels,
            self.remove_labels,
            self.add_reviewers,
            self.remove_reviewers,
            self.add_assignees,
            self.remove_assignees,
            self.add_blocked_by,
            self.remove_blocked_by,
        )
        return any(value is not None for value in given) or any(lists) or self.rebase


async def _known_usernames(client: GitLabClient, usernames: set[str]) -> dict[str, str]:
    """Return the known users among `usernames`, keyed by lowercased name.

    GitLab leaves unknown names out of the answer without an error, so a missing
    name means unknown. Matching ignores case, because usernames ignore case too.
    """
    data = await client.graphql(USERS_QUERY, {"usernames": sorted(usernames)})
    return {node["username"].lower(): node["username"] for node in data["users"]["nodes"]}


async def _list_blocks(client: GitLabClient, mr_path: str) -> tuple[list[dict[str, Any]], bool]:
    """Return the block entries of a merge request, and whether the scan stopped before the last page.

    The scan is bounded by MAX_PAGES, like the other list scans in this file.
    """
    entries: list[dict[str, Any]] = []
    page = 1
    for _ in range(MAX_PAGES):
        data, next_page = await client.get_page(f"{mr_path}/blocks", per_page=BLOCKS_PAGE_SIZE, page=page)
        entries.extend(data)
        if next_page is None:
            return entries, False
        page = next_page
    return entries, True


def _find_block(entries: list[dict[str, Any]], iid: int) -> dict[str, Any] | None:
    """Return the entry where merge request `iid` blocks this merge request, or None.

    The blocker must be in the same project. GitLab can list an IID from another
    project too, so the project IDs have to match. Entries with no blocker are skipped.
    """
    for entry in entries:
        blocker = entry["blocking_merge_request"]
        if blocker is None:  # The blocking MR is gone, so there is nothing to match.
            continue
        same_project = blocker["project_id"] == entry["blocked_merge_request"]["project_id"]
        if same_project and blocker["iid"] == iid:
            return entry
    return None


@dataclass
class _Update:
    """Runs the steps of one update_merge_request call. Steps are independent, so one failure never stops the rest."""

    client: GitLabClient
    project: str
    iid: int
    changes: _Changes
    applied: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def path(self) -> str:
        """Return the REST path of this merge request."""
        return _mr_rest_path(self.project, self.iid)

    async def run(self) -> None:
        """Run every requested step, in the order the docs describe."""
        await self._fields()
        await self._draft()
        await self._people()
        await self._blocks()
        await self._approval()
        await self._rebase()

    async def attempt(self, name: str, action: Awaitable[Any], accepted: list[str] | None = None) -> None:
        """Await one write and record how it ended under `name`.

        A connection error means the write may have reached GitLab, so it goes to
        `unknown`. A refusal from GitLab goes to `failed`, with its reason. A success
        goes to `accepted`, which defaults to `applied`.
        """
        try:
            await action
        except GitLabConnectionError:
            self.unknown.append(name)
        except GitLabAPIError as exc:
            # Some steps reuse a status for a common, harmless case. The generic hint would mislead there. 🧭
            reason = STEP_STATUS_REASONS.get((name, exc.status))
            self.failed[name] = f"GitLab returned HTTP {exc.status}. {reason}" if reason else str(exc)
        except ToolError as exc:
            self.failed[name] = str(exc)
        else:
            (self.applied if accepted is None else accepted).append(name)

    async def read_back(self) -> dict[str, Any]:
        """Re-read the merge request and return its final fields, or a warning when the read fails."""
        try:
            node = await _fetch_merge_request(self.client, MERGE_REQUEST_STATE_QUERY, self.project, self.iid)
        except ToolError as exc:
            return {"warning": f"Could not read the merge request back. Check it with get_merge_request. {exc}"}
        return {
            "state": node["state"],
            "draft": node["draft"],
            "merge_status": _lower(node["detailedMergeStatus"]),
            "head_sha": _head_sha(node["diffRefs"]),
        }

    async def _fields(self) -> None:
        """Send one PUT with every plain field that was given."""
        body = self.changes.fields_body()
        if body:
            await self.attempt("fields", self.client.request("PUT", self.path, json=body))

    async def _draft(self) -> None:
        """Set or clear draft status. GitLab only accepts this change through GraphQL."""
        if self.changes.draft is not None:
            variables = {"projectPath": self.project, "iid": str(self.iid), "draft": self.changes.draft}
            await self.attempt("draft", self.client.mutate(SET_DRAFT_MUTATION, variables, "mergeRequestSetDraft"))

    async def _people(self) -> None:
        """Check every username with one query, then send one call per list that still has known names."""
        changes = self.changes
        jobs = [
            ("reviewers", "add_reviewers", "APPEND", changes.add_reviewers),
            ("reviewers", "remove_reviewers", "REMOVE", changes.remove_reviewers),
            ("assignees", "add_assignees", "APPEND", changes.add_assignees),
            ("assignees", "remove_assignees", "REMOVE", changes.remove_assignees),
        ]
        requested = [job for job in jobs if job[3]]
        if not requested:
            return
        try:
            known = await _known_usernames(self.client, {name for *_, names in requested for name in names})
        except ToolError as exc:
            self.failed["usernames"] = str(exc)
            return
        for kind, step, mode, names in requested:
            found = self._resolve(kind, names, known)
            if found:
                mutation, payload_field = PEOPLE_MUTATIONS[kind]
                variables = {"projectPath": self.project, "iid": str(self.iid), "usernames": found, "mode": mode}
                await self.attempt(step, self.client.mutate(mutation, variables, payload_field))

    def _resolve(self, kind: str, names: list[str], known: dict[str, str]) -> list[str]:
        """Return GitLab's spelling of each known name. Each unknown name is recorded as a failure."""
        found: list[str] = []
        for name in names:
            if name.lower() in known:
                found.append(known[name.lower()])
            else:
                self.failed[f"{kind}:{name}"] = "unknown user"
        return found

    async def _blocks(self) -> None:
        """Add or remove blocks. Each blocker IID gets its own request, so one failure stays on its own item."""
        for iid in self.changes.add_blocked_by:
            request = self.client.request("POST", f"{self.path}/blocks", blocking_merge_request_iid=iid)
            await self.attempt(f"blocked_by:{iid}", request)
        if self.changes.remove_blocked_by:
            await self._remove_blocks(self.changes.remove_blocked_by)

    async def _remove_blocks(self, iids: list[int]) -> None:
        """Delete the block that each IID has on this merge request. A missing block is a failure."""
        try:
            entries, truncated = await _list_blocks(self.client, self.path)
        except ToolError as exc:
            self.failed["remove_blocked_by"] = str(exc)
            return
        for iid in iids:
            key = f"blocked_by:{iid}"
            entry = _find_block(entries, iid)
            if entry is None:
                missing = f"not found in the first {MAX_PAGES} pages of blocks" if truncated else "not a blocker"
                self.failed[key] = missing
            else:
                await self.attempt(key, self.client.request("DELETE", f"{self.path}/blocks/{entry['id']}"))

    async def _approval(self) -> None:
        """Approve or unapprove. The SHA guards the approval, so GitLab refuses it when new commits arrived."""
        if self.changes.approve is True:
            body = {"sha": self.changes.sha}
            await self.attempt("approve", self.client.request("POST", f"{self.path}/approve", json=body))
        elif self.changes.approve is False:
            await self.attempt("unapprove", self.client.request("POST", f"{self.path}/unapprove"))

    async def _rebase(self) -> None:
        """Ask GitLab to rebase. GitLab accepts the job and works on it later, so the result is pending."""
        if self.changes.rebase:
            await self.attempt("rebase", self.client.request("PUT", f"{self.path}/rebase"), accepted=self.pending)


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def update_merge_request(  # noqa: PLR0913, PLR0917 - each argument is one user-facing change.
    ctx: Context,
    project: Project,
    iid: Iid,
    approve: Annotated[bool | None, Field(description="True approves. False removes your approval.")] = None,
    sha: Annotated[
        str | None,
        Field(description="Full head SHA from get_merge_request. Required when approve is true."),
    ] = None,
    state: Annotated[Literal["close", "reopen"] | None, Field(description="Close or reopen the MR.")] = None,
    draft: Annotated[bool | None, Field(description="True marks it as draft. False marks it ready.")] = None,
    title: Annotated[str | None, Field(description="New title.")] = None,
    description: Annotated[str | None, Field(description="New description. An empty string clears it.")] = None,
    target_branch: Annotated[str | None, Field(description="New target branch.")] = None,
    add_labels: Annotated[list[str] | None, Field(description="Labels to add.")] = None,
    remove_labels: Annotated[list[str] | None, Field(description="Labels to remove.")] = None,
    add_reviewers: Annotated[list[str] | None, Field(description="Usernames to add as reviewers.")] = None,
    remove_reviewers: Annotated[list[str] | None, Field(description="Usernames to remove as reviewers.")] = None,
    add_assignees: Annotated[list[str] | None, Field(description="Usernames to add as assignees.")] = None,
    remove_assignees: Annotated[list[str] | None, Field(description="Usernames to remove as assignees.")] = None,
    add_blocked_by: Annotated[
        list[int] | None,
        Field(description="IIDs of MRs in this same project that should block this one."),
    ] = None,
    remove_blocked_by: Annotated[
        list[int] | None,
        Field(description="IIDs of blocking MRs in this same project to unblock."),
    ] = None,
    rebase: Annotated[bool, Field(description="True asks GitLab to rebase the source branch.")] = False,
) -> MergeRequestUpdate:
    """Change one merge request. Like `glab mr update`, `glab mr approve`, and `glab mr rebase`.

    Lower risk than a merge, but not always reversible. A rebase rewrites the
    source branch. An approval can let an auto-merge that is already enabled go through.

    - Several changes go in one call. Each step runs even when an earlier step failed.
    - `applied`: GitLab confirmed the change. `pending`: GitLab accepted a rebase, but it is not done yet.
    - `unknown`: the request may have reached GitLab. Check with get_merge_request. Do not retry blindly.
    - `failed` maps each step or item to a reason. Items look like `reviewers:bob` (unknown user) or `blocked_by:12`.
    - `approve` needs `sha`, the full `head_sha` from get_merge_request. Approve in its own call, because
      approval may not survive a rebase or a target_branch change.
    - Usernames must be real users. Blockers are IIDs of MRs in this same project.
    - An invalid request sends nothing: no changes, the same item in an add and a remove list,
      or approve with rebase or target_branch.
    - Needs a token with the `api` scope.
    """
    client = get_client(ctx)
    changes = _Changes(
        approve=approve,
        sha=sha,
        state=state,
        draft=draft,
        title=title,
        description=description,
        target_branch=target_branch,
        add_labels=add_labels or [],
        remove_labels=remove_labels or [],
        add_reviewers=add_reviewers or [],
        remove_reviewers=remove_reviewers or [],
        add_assignees=add_assignees or [],
        remove_assignees=remove_assignees or [],
        add_blocked_by=add_blocked_by or [],
        remove_blocked_by=remove_blocked_by or [],
        rebase=rebase,
    )
    changes.check()
    update = _Update(client=client, project=project, iid=iid, changes=changes)
    await update.run()
    if not (update.applied or update.pending or update.unknown):
        raise ToolError("; ".join(f"{name}: {reason}" for name, reason in update.failed.items()))
    final = await update.read_back()
    return MergeRequestUpdate(
        applied=update.applied,
        pending=update.pending,
        unknown=update.unknown,
        failed=update.failed,
        **final,
    )
