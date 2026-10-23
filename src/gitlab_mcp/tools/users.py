"""Current-user lookup, so agents know which account the token acts as 👤."""

from pydantic import BaseModel
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp


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
