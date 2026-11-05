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
START_PIPELINE_VALIDATION_ERROR = (
    "Invalid start_pipeline arguments. Provide project, target, expected_sha, and the target's ref or iid; "
    "see the tool schema."
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


class CreatePipelineValidationMiddleware(Middleware):
    """Hide argument validation details for pipeline creation tools."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Replace pipeline argument validation errors with a tool-specific safe hint."""
        hints = {"create_pipeline": CREATE_PIPELINE_VALIDATION_ERROR, "start_pipeline": START_PIPELINE_VALIDATION_ERROR}
        if context.message.name not in hints:
            return await call_next(context)

        try:
            return await call_next(context)
        except ValidationError:
            raise ToolError(hints[context.message.name]) from None


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
        "use edit_merge_request_description to update the description; prefer edits=[...] for several changes "
        "instead of a loop. "
        "For merge request thread triage, use get_merge_request_threads alone for compact status and review threads. "
        'With state="all", resolved threads default to headings; set resolved_view="full" for full notes. '
        "When reading both merge request details and threads, set get_merge_request(include_threads=false) and "
        "get_merge_request_threads(include_mr_status=false) to avoid duplicate data. For a note cut with "
        "[truncated], pass its note_id and returned offset as body_offset to get_merge_request_note. "
        "Skip get_merge_request_threads when the open, resolved, and comment thread counts are all 0 "
        "(a missing count is 0). For re-reviews, start with "
        'view="headings" and use view="full" only for selected thread IDs. '
        "Use get_merge_request for approvals and jobs (add include_description=true for the description, or "
        "include_commits=true for commit order and subjects). "
        'Use get_merge_request(view="status") for rechecks; it omits the pipeline event type, so use the full view '
        "to classify a different pipeline SHA as merged-result, train, or stale CI. "
        "For long descriptions, set description_limit up to 12000 to read them in one call, or pass the returned "
        "next offset as description_offset with include_description=true; restart if the merge request is edited. "
        "To read part of a long description, list its headings with "
        "headings_only=true, then read only the parts you need with section. "
        "Use get_merge_request_diff without paths for the file list, then with paths=[...] for patches. "
        "For CI triage, use list_pipelines to find a pipeline, get_pipeline to inspect its jobs, then get_job_log "
        "to read a job's log; use get_job for runner-job metadata. "
        "Prepare inline notes with post_review_notes(validate_only=true) before requesting approval, then keep the "
        "batch_id returned when all notes pass. "
        "To check LOW/NIT positions before drafting, omit both body sources with file and optional line or old_line "
        "under validate_only=true. Validate HIGH/MEDIUM bodies and LOW/NIT positions together in one batch: every "
        "note is cached at its input index, and each selected position-only entry needs an approved body_overrides "
        "entry; a missing override names every missing index and keeps the batch_id valid. Use those same indices "
        "for selection, body overrides, and validation errors; position_only_indices lists the entries that need "
        "overrides. "
        "If the user can open a server-local preview file, use notes_summary to build a short approval index table "
        "without rereading the preview; escape table cells and link to the file instead of repeating its bodies. "
        "Get the user's explicit APPROVE before posting any notes. "
        "For partial approval, use indices to post only approved notes. When the user approves the batch plus new "
        "notes, send batch_id with extra_notes instead of resending the batch. For edited drafts, use "
        "body_overrides and optionally re-preview with validate_only=true and a new preview_path; add indices to drop "
        "notes and extra_notes to add new ones instead of resending the batch. The new batch renumbers the selected "
        "notes, then the extras. "
        "Re-preview consumes the old handle; use the new handle and get fresh explicit APPROVE for its final text. "
        "Show the user final text and get APPROVE before posting. "
        "After APPROVE, submit "
        "notes or batch_id (head_sha is optional with batch_id); the call revalidates the request before the first "
        "write. Do not call validate_only again unless re-previewing edited content for fresh approval. "
        "To edit an existing note, use get_merge_request_threads(include_hashes=true) to find its note ID and "
        "body hash, show the user the final text, and get explicit APPROVE before edit_merge_request_note. "
        "Prefer exact text replacement. "
        "Use update_merge_request for reviewer, assignee, and approval changes. "
        "If a merge request has conflicts, do not call the rebase API; rebase locally and "
        "push with --force-with-lease. "
        # 🔍 Main arguments, so one tool search is enough. Client searches often cut long signatures.
        "Main arguments: "
        "get_merge_request(project, iid, view=full|status, include_threads, include_description, "
        "description_limit, description_offset, "
        "section, headings_only); "
        "get_merge_request_threads(project, iid or iids, state=open|resolved|comments|all, "
        "view=full|headings|finding, include_mr_status, include_hashes, count_only, cursor); "
        "get_merge_request_note(project, iid, note_id, body_offset, body_limit); "
        "edit_merge_request_note(project, iid, thread_id, note_id, expected_hash, body, old_text, new_text); "
        "edit_merge_request_description(project, iid, old_text, new_text, edits=[{old_text, new_text}], "
        "return_diff); "
        "post_review_notes(project, iid, head_sha (optional with batch_id), notes=[{file, line or old_line "
        "(neither for a file-level note), body or body_path}] or batch_id or body_dir alone (one .md note file "
        "each, front matter file/line/old_line), indices, "
        "body_overrides=[{index, body or body_path}], extra_notes, "
        "body_dir, preview_path, add_reviewers, add_assignees, validate_only); "
        "update_merge_request(project, iid, add_reviewers, remove_reviewers, add_assignees, remove_assignees, "
        "add_labels, remove_labels, add_blocked_by, remove_blocked_by, approve with sha, rebase)."
    ),
    lifespan=gitlab_lifespan,
)
mcp.add_middleware(CreatePipelineValidationMiddleware())


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
