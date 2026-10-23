"""FastMCP server with GitLab tools for fast developer workflows 🚀."""

from typing import Any
lazy from collections.abc import AsyncIterator

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from pydantic import BaseModel

from gitlab_mcp.client import GitLabClient
from gitlab_mcp.config import ConfigError, Settings


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
