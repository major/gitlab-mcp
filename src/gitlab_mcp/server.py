"""FastMCP server with GitLab tools for fast developer workflows 🚀."""

from typing import Annotated, Any, Literal
lazy from collections.abc import AsyncIterator

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from pydantic import BaseModel, Field

from gitlab_mcp.client import GitLabClient
from gitlab_mcp.config import ConfigError, Settings

# Upper bound on GraphQL pages scanned when approved or has_open_threads
# filters run on our side. Keeps one tool call from walking a huge project.
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
