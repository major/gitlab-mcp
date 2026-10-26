"""FastMCP app, shared lifespan, and client access for every tool 🦊."""

from typing import Any
lazy from collections.abc import AsyncIterator

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
lazy from fastmcp.tools.base import ToolResult
lazy from mcp.types import CallToolRequestParams

from gitlab_mcp.client import GitLabClient
from gitlab_mcp.config import ConfigError, Settings

CREATE_PIPELINE_VALIDATION_ERROR = (
    "Invalid create_pipeline arguments. Provide project, ref, expected_sha, and valid variables; see the tool schema."
)


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


class _CreatePipelineValidationMiddleware(Middleware):
    """Hide argument validation details for create_pipeline calls."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Replace only create_pipeline argument validation errors with a safe hint."""
        if context.message.name != "create_pipeline":
            return await call_next(context)

        try:
            return await call_next(context)
        except ValidationError:
            raise ToolError(CREATE_PIPELINE_VALIDATION_ERROR) from None


mcp = FastMCP(
    name="gitlab-mcp",
    instructions=(
        "Tools for GitLab developer workflows on gitlab.com or self-managed "
        "GitLab. Call get_current_user first to learn who you are acting as. "
        "For merge request lifecycle work, push the branch before create_merge_request; "
        "use list_labels(search=...) to find labels, "
        'get_merge_request_threads(state="open") to triage, reply_to_thread(resolve=true) to respond or resolve, '
        "resolve_open_threads to close every open thread, and "
        "edit_merge_request_description to update the description. "
        "For merge request thread triage, use get_merge_request_threads alone for compact status and review threads. "
        "Use get_merge_request for approvals and jobs (add include_description=true for the description), "
        "and get_merge_request_diff for patches. "
        "For CI triage, use list_pipelines to find a pipeline, get_pipeline to inspect its jobs, then get_job_log "
        "to read a job's log; use get_job for runner-job metadata. "
        "Prepare inline notes with post_review_notes(validate_only=true) before requesting approval. "
        "Get the user's explicit APPROVE before posting notes with post_review_notes. "
        "After APPROVE, call post_review_notes directly; it reruns the same checks before the first write. "
        "Do not call validate_only again. "
        "Use update_merge_request for reviewer, assignee, and approval changes. "
        "If a merge request has conflicts, do not call the rebase API; rebase locally and "
        "push with --force-with-lease. "
        # 🔍 Main arguments, so one tool search is enough. Client searches often cut long signatures.
        "Main arguments: "
        "get_merge_request_threads(project, iid or iids, state=open|resolved|comments|all, view=full|headings|finding, "
        "count_only, cursor); "
        "post_review_notes(project, iid, head_sha, notes=[{file, line or old_line (neither for a file-level note), "
        "body or body_path}], add_reviewers, add_assignees, validate_only); "
        "update_merge_request(project, iid, add_reviewers, remove_reviewers, add_assignees, remove_assignees, "
        "add_labels, remove_labels, add_blocked_by, remove_blocked_by, approve with sha, rebase)."
    ),
    lifespan=gitlab_lifespan,
)
mcp.add_middleware(_CreatePipelineValidationMiddleware())


def get_client(ctx: Context) -> GitLabClient:
    """Return the shared GitLab client or raise a helpful ToolError."""
    state = ctx.lifespan_context
    if state.get("client") is None:
        raise ToolError(state.get("config_error") or "GitLab client is not configured.")
    return state["client"]
