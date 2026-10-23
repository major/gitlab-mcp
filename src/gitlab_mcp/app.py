"""FastMCP app, shared lifespan, and client access for every tool 🦊."""

from typing import Any
lazy from collections.abc import AsyncIterator

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan

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
        "GitLab. Call get_current_user first to learn who you are acting as. "
        "For merge request lifecycle work, push the branch before create_merge_request; "
        "use list_labels(search=...) to find labels, "
        'get_merge_request_threads(state="open") to triage, reply_to_thread(resolve=true) to respond or resolve, and '
        "edit_merge_request_description to update the description. "
        "For merge request thread triage, use get_merge_request_threads alone for compact status and review threads. "
        "Use get_merge_request for approvals and jobs (add include_description=true for the description), "
        "and get_merge_request_diff for patches. "
        "Prepare inline notes with post_review_notes(validate_only=true) before requesting approval. "
        "Get the user's explicit APPROVE before posting notes with post_review_notes. "
        "After APPROVE, call post_review_notes directly; it reruns the same checks before the first write. "
        "Do not call validate_only again. "
        "Use update_merge_request for reviewer, assignee, and approval changes."
    ),
    lifespan=gitlab_lifespan,
)


def get_client(ctx: Context) -> GitLabClient:
    """Return the shared GitLab client or raise a helpful ToolError."""
    state = ctx.lifespan_context
    if state.get("client") is None:
        raise ToolError(state.get("config_error") or "GitLab client is not configured.")
    return state["client"]
