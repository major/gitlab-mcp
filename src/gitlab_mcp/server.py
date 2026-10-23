"""FastMCP server with GitLab tools for fast developer workflows 🚀."""

import base64
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import quote
lazy from collections.abc import AsyncIterator

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from pydantic import BaseModel, Field

from gitlab_mcp.client import GitLabClient
from gitlab_mcp.config import ConfigError, Settings

# Upper bound on GraphQL pages scanned when approved, has_open_threads, or
# thread filters run on our side. Keeps one tool call from walking a huge project.
MAX_PAGES = 5

# One request per page. `open_threads` is computed from the two counts.
MERGE_REQUESTS_QUERY = """
query($fullPath: ID!, $state: MergeRequestState, $authorUsername: String, $first: Int!, $after: String) {
  project(fullPath: $fullPath) {
    mergeRequests(state: $state, authorUsername: $authorUsername, first: $first, after: $after, sort: UPDATED_DESC) {
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
query($fullPath: ID!, $iid: String!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      iid title description state draft sourceBranch targetBranch
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
# Total characters of note bodies that one thread call may return. Keeps one tool result small.
THREAD_BODY_BUDGET = 30000
# Discussion GIDs start with this. Output IDs are bare SHAs, so the prefix is only stripped from input.
DISCUSSION_PREFIX = "gid://gitlab/Discussion/"
# head_sha is cut to this many characters in output. Twelve is enough to find a commit.
SHORT_SHA = 12
# Maps each state filter to the thread status it keeps. "all" keeps every status.
STATE_STATUS = {"open": "open", "resolved": "resolved", "comments": "comment"}

# Total patch characters that one diff call may return. A single patch is cut to this too.
DIFF_BUDGET = 40000
# Files per page of the REST diffs endpoint, the largest page GitLab serves.
DIFF_PAGE_SIZE = 100

# A Literal is sent to the agent as an enum, so this type replaces a separate
# "list states" tool.
MergeRequestState = Literal["opened", "closed", "locked", "merged", "all"]


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
    """One merge request with the fields an agent needs to pick the next step.

    Empty values are left out to save tokens. A missing `draft` means not a
    draft, a missing `open_threads` means zero, and a missing `approved_by`
    means nobody approved.
    """

    iid: int
    title: str
    author: str | None = Field(default=None, exclude_if=lambda v: v is None)
    state: str | None = Field(default=None, exclude_if=lambda v: v is None)  # Only set when listing state "all".
    draft: bool = Field(default=False, exclude_if=lambda v: not v)
    approved: bool
    approved_by: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    open_threads: int = Field(default=0, exclude_if=lambda v: v == 0)
    updated: str  # Date only, YYYY-MM-DD.


class MergeRequestList(BaseModel):
    """A page of merge requests."""

    web_url_prefix: str = Field(description="Append an iid to get the MR link.")
    merge_requests: list[MergeRequestSummary]
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
    body: str  # Cut at NOTE_CAP characters, then marked with THREAD_CUT_MARKER.
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


def _to_summary(node: dict[str, Any], include_state: bool) -> MergeRequestSummary:
    """Convert one GraphQL merge request node into a summary.

    The state is only kept when `include_state` is True, because callers that
    filter on one state already know it.
    """
    # A deleted or banned author comes back as null.
    author = node["author"]
    return MergeRequestSummary(
        iid=int(node["iid"]),  # GraphQL sends IIDs as strings.
        title=node["title"],
        author=author["username"] if author else None,
        state=node["state"] if include_state else None,
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


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_merge_requests(  # noqa: PLR0913, PLR0917 - each argument is a user-facing filter.
    ctx: Context,
    project: Annotated[str, Field(description="Full project path, like 'group/subgroup/project'.")],
    state: MergeRequestState = "opened",
    author: Annotated[str | None, Field(description="Author username.")] = None,
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
    scope. For a link, append an MR's `iid` to `web_url_prefix`. Empty fields
    are left out: no `draft` means not a draft, no `open_threads` means zero,
    and no `approved_by` means nobody approved. `updated` is a date.

    `approved` follows the instance rules: on Free it means at least one
    approval, on Premium and Ultimate it means all approval rules are met.
    `approved` and `has_open_threads` are filtered after fetching, so the tool
    scans at most 5 pages. If `has_more` is true but fewer than `limit` MRs
    came back, the scan stopped early and more matches may exist.
    """
    client = get_client(ctx)
    include_state = state == "all"
    # Client-side filters can drop nodes, so fetch big pages to find matches faster.
    filtered = approved is not None or has_open_threads is not None
    variables: dict[str, Any] = {
        "fullPath": project,
        "state": state,
        "authorUsername": author,
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
        summaries = [_to_summary(node, include_state) for node in connection["nodes"]]
        collected.extend(mr for mr in summaries if _matches(mr, approved, has_open_threads))
        page_info = connection["pageInfo"]
        has_next = page_info["hasNextPage"]
        if len(collected) >= limit or not has_next:
            break
        variables["after"] = page_info["endCursor"]

    return MergeRequestList(
        web_url_prefix=f"{client.settings.url}/{project}/-/merge_requests/",
        merge_requests=collected[:limit],
        has_more=len(collected) > limit or has_next,
    )


def _lower(value: str | None) -> str:
    """Lowercase a GitLab enum value. Null means unknown, so it becomes empty and is left out."""
    return (value or "").lower()


def _cap_description(description: str | None) -> tuple[str, bool]:
    """Cut a description to `DESCRIPTION_CAP` characters.

    Returns the text and whether it was cut. Empty text stays empty, so it is left out.
    """
    if not description:
        return "", False
    return description[:DESCRIPTION_CAP], len(description) > DESCRIPTION_CAP


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


def _short_head_sha(diff_refs: dict[str, Any] | None) -> str:
    """Return the first `SHORT_SHA` characters of the head commit. Null diffRefs means unknown, so it is empty."""
    return diff_refs["headSha"][:SHORT_SHA] if diff_refs else ""


def _to_detail(node: dict[str, Any]) -> MergeRequestDetail:
    """Convert one GraphQL merge request node into the detail shape."""
    # A deleted or banned author comes back as null.
    author = node["author"]
    # headPipeline can be null, so check before reading it.
    pipeline = node["headPipeline"]
    description, description_truncated = _cap_description(node["description"])
    return MergeRequestDetail(
        iid=int(node["iid"]),  # GraphQL sends IIDs as strings.
        title=node["title"],
        state=node["state"],
        draft=node["draft"],
        author=author["username"] if author else "",
        source_branch=node["sourceBranch"],
        target_branch=node["targetBranch"],
        head_sha=_short_head_sha(node["diffRefs"]),
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


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_merge_request(
    ctx: Context,
    project: Annotated[str, Field(description="Full project path, like group/project.")],
    iid: Annotated[int, Field(description="MR number, like 2187 for !2187.")],
) -> MergeRequestDetail:
    """Show one merge request's status, approvals, and open threads. Like `glab mr view`.

    - `merge_status` is the source of truth for mergeability. Open threads and failed CI do not always block a merge.
    - `approved`: Free means one approval. Premium and Ultimate mean all approval rules pass.
    - `auto_merge_strategy`: `merge_when_checks_pass`, `merge_train`, `add_to_merge_train_when_checks_pass` (waiting).
    - `threads.open` IDs go to get_merge_request_threads. `index_complete` false means some are missing.
    - `line` is the new-file line. `old_line` replaces it on a deleted line.
    - Empty fields are left out: a missing `draft` means false, a missing list is empty.
    """
    node = await _fetch_merge_request(get_client(ctx), MERGE_REQUEST_QUERY, project, iid)
    return _to_detail(node)


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


def _cap_note_body(body: str) -> str:
    """Cut a note body to `NOTE_CAP` characters and mark the cut."""
    if len(body) <= NOTE_CAP:
        return body
    return body[:NOTE_CAP] + THREAD_CUT_MARKER


def _to_note(node: dict[str, Any]) -> Note:
    """Convert one GraphQL note into a note with a capped body and a date-only timestamp."""
    author = node["author"]  # A deleted or banned author comes back as null.
    return Note(
        by=author["username"] if author else "",
        body=_cap_note_body(node["body"]),
        at=node["createdAt"][:10],  # ISO 8601 timestamp, keep the date part.
    )


def _to_thread(discussion: dict[str, Any]) -> Thread | None:
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
        notes=[_to_note(node) for node in human],
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


async def _scan_threads(
    client: GitLabClient,
    project: str,
    iid: int,
    thread_filter: _ThreadFilter,
    start: tuple[str | None, int],
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
            thread = _to_thread(nodes[index])
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
    project: Annotated[str, Field(description="Full project path, like group/project.")],
    iid: Annotated[int, Field(description="MR number, like 2187 for !2187.")],
    state: Literal["open", "resolved", "comments", "all"] = "open",
    ids: Annotated[list[str] | None, Field(description="Thread IDs from threads.open. Ignores state.")] = None,
    file: Annotated[str | None, Field(description="Only threads on this path, old or new.")] = None,
    author: Annotated[str | None, Field(description="Only threads with a note by this user.")] = None,
    cursor: Annotated[str | None, Field(description="next_cursor from an earlier call, with the same filters.")] = None,
) -> ThreadList:
    """Read review thread notes, with filters and a size budget. Like `glab mr note list`.

    - `state`: `open` (default), `resolved`, `comments` (cannot be resolved), or `all`. `ids` ignore `state`.
    - System notes and threads with no human notes are skipped.
    - `line` is the new-file line. `old_line` replaces it on a deleted line.
    - `notes_truncated`: over 50 notes, so `author` can miss a late reply.
    - Bodies are cut at 4000 characters. A call returns about 30000. Continue with `next_cursor` and the same filters.
    - `not_found_ids` is only set after a full scan from the first page.
    """
    client = get_client(ctx)
    start = _thread_cursor_start(cursor) if cursor else (None, 0)
    # An empty list means no IDs were given, so the state filter applies.
    wanted = _bare_ids(ids) if ids else None
    thread_filter = _ThreadFilter(state=state, ids=wanted, file=file, author=author)
    threads, next_cursor, seen = await _scan_threads(client, project, iid, thread_filter, start)

    # A resumed call did not read the first page, so it cannot prove an ID is missing.
    not_found_ids: list[str] = []
    if wanted and cursor is None and not next_cursor:
        not_found_ids = [thread_id for thread_id in wanted if thread_id not in seen]
    return ThreadList(threads=threads, next_cursor=next_cursor, not_found_ids=not_found_ids)


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
    """Fetch one page of merge request diffs and return the files and the next page number.

    The project path must be URL-encoded, so `group/project` becomes `group%2Fproject`.
    """
    encoded = quote(project, safe="")
    return await client.get_page(
        f"/projects/{encoded}/merge_requests/{iid}/diffs",
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
    project: Annotated[str, Field(description="Full project path, like group/project.")],
    iid: Annotated[int, Field(description="MR number, like 2187 for !2187.")],
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
    head_sha = _short_head_sha(node["diffRefs"])

    if not wanted:
        return MergeRequestDiff(head_sha=head_sha, files=_to_file_stats(node["diffStats"]))

    diffs, next_cursor, matched = await _scan_diffs(client, project, iid, set(wanted), start)
    # A resumed call did not read the first page, so it cannot prove a path is missing.
    not_found = [path for path in wanted if path not in matched] if cursor is None and not next_cursor else []
    return MergeRequestDiff(head_sha=head_sha, diffs=diffs, next_cursor=next_cursor, paths_not_found=not_found)
