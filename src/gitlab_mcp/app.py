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
from gitlab_mcp.review_batches import ReviewBatchCache

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
        yield {"client": client, "config_error": None, "review_batches": ReviewBatchCache()}
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
        'get_merge_request_threads(state="open") to triage, reply_to_thread(resolve=true) to respond or resolve; '
        "use reply_to_threads for batched replies and post-write approval and auto-merge summaries; "
        "use resolve_open_threads to close every open thread, and "
        "use edit_merge_request_description to update the description. "
        "For merge request thread triage, use get_merge_request_threads alone for compact status and review threads. "
        "When reading both merge request details and threads, set get_merge_request(include_threads=false) and "
        "get_merge_request_threads(include_mr_status=false) to avoid duplicate data. For a note cut with "
        "[truncated], pass its note_id and returned offset as body_offset to get_merge_request_note. "
        "Skip get_merge_request_threads when the open, resolved, and comment thread counts are all 0 "
        "(a missing count is 0). For re-reviews, start with "
        'view="headings" and use view="full" only for selected thread IDs. '
        "Use get_merge_request for approvals and jobs (add include_description=true for the description). "
        "For long descriptions, pass the returned next offset as description_offset with include_description=true; "
        "restart if the merge request is edited. To read part of a long description, list its headings with "
        "headings_only=true, then read only the parts you need with section. "
        "Use get_merge_request_diff for patches. "
        "For CI triage, use list_pipelines to find a pipeline, get_pipeline to inspect its jobs, then get_job_log "
        "to read a job's log; use get_job for runner-job metadata. "
        "Prepare inline notes with post_review_notes(validate_only=true) before requesting approval, then keep the "
        "returned batch_id. Get the user's explicit APPROVE before posting any notes. "
        "For partial approval, use indices to post only approved notes. When the user approves the batch plus new "
        "notes, send batch_id with extra_notes instead of resending the batch. For edited drafts, use "
        "body_overrides, show the user the final text, and get explicit APPROVE before posting. After APPROVE, submit "
        "notes or batch_id (head_sha is optional with batch_id); the call revalidates the request before the first "
        "write. Do not call validate_only again. "
        "To edit an existing note, use get_merge_request_threads(include_hashes=true) to find its note ID and "
        "body hash, show the user the final text, and get explicit APPROVE before edit_merge_request_note. "
        "Prefer exact text replacement. "
        "Use update_merge_request for reviewer, assignee, and approval changes. "
        "If a merge request has conflicts, do not call the rebase API; rebase locally and "
        "push with --force-with-lease. "
        # 🔍 Main arguments, so one tool search is enough. Client searches often cut long signatures.
        "Main arguments: "
        "get_merge_request(project, iid, include_threads, include_description, description_limit, description_offset, "
        "section, headings_only); "
        "get_merge_request_threads(project, iid or iids, state=open|resolved|comments|all, "
        "view=full|headings|finding, include_mr_status, include_hashes, count_only, cursor); "
        "get_merge_request_note(project, iid, note_id, body_offset, body_limit); "
        "edit_merge_request_note(project, iid, thread_id, note_id, expected_hash, body, old_text, new_text); "
        "post_review_notes(project, iid, head_sha (optional with batch_id), notes=[{file, line or old_line "
        "(neither for a file-level note), body or body_path}] or batch_id, indices, body_overrides, extra_notes, "
        "body_dir, add_reviewers, add_assignees, validate_only); "
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


def get_review_batch_cache(ctx: Context) -> ReviewBatchCache:
    """Return the lifespan's review batch cache or raise a helpful ToolError."""
    cache = ctx.lifespan_context.get("review_batches")
    if not isinstance(cache, ReviewBatchCache):
        raise ToolError("Review note batch cache is not configured. Send the notes again.")  # noqa: TRY003
    return cache
