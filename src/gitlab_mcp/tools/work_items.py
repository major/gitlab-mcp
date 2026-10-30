"""Work-item tools: read items and notes, and edit descriptions with guarded replacements 🧾."""

from typing import Annotated

from pydantic import Field
lazy from fastmcp import Context

from gitlab_mcp import description_edits as _description_edits
from gitlab_mcp import description_tables as _description_tables
from gitlab_mcp import work_items as _work_items
from gitlab_mcp.app import get_client, mcp
lazy from gitlab_mcp.common import Iid, Project

WorkItemIid = Annotated[int, Field(description="Work item (issue) number.")]


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_work_item(  # noqa: PLR0913, PLR0917 - each argument is a user-facing read option.
    ctx: Context,
    project: Project,
    iid: WorkItemIid,
    scope: _work_items.WorkItemScope = "project",
    include_description: bool = True,
    description_limit: Annotated[
        int,
        Field(
            ge=0,
            le=4000,
            description="Maximum description length, from 0 to 4000 characters. Full-description pages are soft: "
            "a final tail up to a quarter of the limit is included to save a call.",
        ),
    ] = 4000,
    cursor: Annotated[
        str | None,
        Field(description="next_cursor from an earlier call with the same arguments."),
    ] = None,
    section: Annotated[
        str | list[str] | None,
        Field(
            description="One exact heading or a list of headings. A string returns description and, when cut, "
            "description_truncated. A list, even with one heading, returns sections with heading, content, "
            "and, when cut, truncated."
        ),
    ] = None,
    headings_only: Annotated[
        bool,
        Field(description="Return the description's ATX heading names without section content."),
    ] = False,
    description_offset: Annotated[
        int,
        Field(
            ge=0,
            description="Unicode character offset for full-description paging. Use next_description_offset; "
            "positive offsets exclude section, headings_only, widget cursor, skipped descriptions, and limit 0.",
        ),
    ] = 0,
) -> _work_items.WorkItemDetail:
    """Read one project or group work item, like a scoped `glab api graphql` query.

    `scope` is explicit. Use `group` for group work items such as epics. The group work-item API is experimental in
    GitLab 18.1 and later, so permission or API errors can occur. Widget lists fetch up to 100 entries each;
    `next_cursor` resumes incomplete lists. Merge each returned list across pages. Set `include_description` false or
    lower `description_limit` to reduce output. A string `section` returns selected content in `description`, with
    `description_truncated` when cut. A list `section`, even with one heading, returns `sections` entries with
    `heading`, `content`, and `truncated` when cut. Set `headings_only` to list all ATX headings. Focused reads require
    the description and a positive limit, and do not accept a widget cursor. The limit caps total content across
    selected sections. For full descriptions, use `next_description_offset` as `description_offset` for the next page.
    A final tail up to a quarter of `description_limit` joins the last page to save a call. Offsets count Unicode code
    points, not bytes or grapheme clusters. Positive offsets cannot be combined with `section`, `headings_only`, a
    widget `cursor`, `include_description=false`, or `description_limit=0`. Continue widget cursors with offset 0.
    Compare `description_hash` across description pages; it hashes the full description.
    If the hash changes, the description was edited and paging must restart from offset 0.
    """
    return await _work_items.read_work_item(
        get_client(ctx),
        project,
        iid,
        scope,
        include_description,
        description_limit,
        cursor,
        section,
        headings_only,
        description_offset,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_work_item_notes(  # noqa: PLR0913, PLR0917 - each argument is a user-facing read option.
    ctx: Context,
    project: Project,
    iid: WorkItemIid,
    scope: _work_items.WorkItemScope = "project",
    cursor: Annotated[
        str | None,
        Field(description="next_cursor from an earlier call with the same arguments."),
    ] = None,
    max_body_chars: Annotated[
        int,
        Field(ge=0, le=4000, description="Maximum characters per note body, from 0 to 4000."),
    ] = 4000,
) -> _work_items.WorkItemNotes:
    """Read a bounded page of human work-item notes, like a scoped `glab api graphql` query.

    `scope="group"` reads group work items such as epics. Group work-item support is experimental in GitLab 18.1 and
    later, so permission or API errors can occur. System notes are omitted. Each note has a `note_id` for use with
    `get_work_item_note`. Each discussion returns up to 50 notes, and each call returns up to 20 discussions. The
    nested 50-note limit cannot be paged here. `next_cursor` advances discussions, not note bodies. The aggregate body
    budget can stop a page before all fetched notes are returned. Restart the list scan with `max_body_chars=0` and
    follow its own `next_cursor` values to discover their IDs; cursors are bound to the body limit. Use
    `get_work_item_note` to read full bodies in chunks. `notes_truncated` reports omitted nested notes or content cut
    by the body limits. `discussions_complete` reports whether all discussions were read.
    """
    return await _work_items.read_work_item_notes(get_client(ctx), project, iid, scope, cursor, max_body_chars)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_work_item_note(  # noqa: PLR0913, PLR0917 - each argument is a user-facing read option.
    ctx: Context,
    project: Project,
    iid: WorkItemIid,
    note_id: Annotated[str, Field(description="Canonical GitLab Note global ID from list_work_item_notes.")],
    scope: _work_items.WorkItemScope = "project",
    body_offset: Annotated[int, Field(ge=0, description="Unicode code-point offset for the body page.")] = 0,
    body_limit: Annotated[
        int, Field(ge=1, le=4000, description="Maximum body characters per page, from 1 to 4000.")
    ] = 4000,
) -> _work_items.WorkItemNotePage:
    """Read one work-item note body page, like a scoped `glab api graphql` query.

    Use a `note_id` from `list_work_item_notes` and the same explicit project or group scope. GitLab's note query and
    group work-item support are experimental in GitLab 18.1 and later. The API fetches the full body each time; only
    MCP output is paginated. Use `next_body_offset` for the next page. Offsets count Unicode code points, not bytes or
    grapheme clusters. `body_hash` is the SHA-256 hash of the full UTF-8 body. If it changes between pages, the note
    was edited and paging must restart from offset 0. System notes and notes on other work items are rejected.
    """
    return await _work_items.read_work_item_note(get_client(ctx), project, iid, note_id, scope, body_offset, body_limit)


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def create_work_item_note(  # noqa: PLR0913, PLR0917 - each argument is a user-facing note option.
    ctx: Context,
    project: Project,
    iid: WorkItemIid,
    body: Annotated[
        str | None,
        Field(description="Note text, up to 100000 UTF-8 bytes. Provide exactly one of body or body_file."),
    ] = None,
    body_file: Annotated[
        str | None,
        Field(description="Absolute server-local regular UTF-8 file, up to 100000 bytes. Use instead of body."),
    ] = None,
    scope: _work_items.WorkItemScope = "project",
    validate_only: Annotated[
        bool,
        Field(description="Validate the body and target without writing; does not verify write permission."),
    ] = False,
) -> _work_items.WorkItemNoteCreateResult:
    """Create one work-item note, like `glab issue note`.

    Bodies containing truncation markers are rejected.
    Scope is explicit, with no project/group fallback. Group work-item support is experimental in GitLab 18.1 and
    later, so permission or API errors can occur. Provide exactly one nonblank inline body or server-local body_file.
    `validate_only` checks the input and target but does not guarantee write permission. GitLab quick actions may run
    without saving a note. An unknown outcome requires checking GitLab before retrying; writes are never retried.
    """
    return await _work_items.post_work_item_note(get_client(ctx), project, iid, body, body_file, scope, validate_only)


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
async def edit_merge_request_description(  # noqa: PLR0913, PLR0917 - each argument is a user-facing edit option.
    ctx: Context,
    project: Project,
    iid: Iid,
    old_text: Annotated[str, Field(description="Text that must appear exactly once in the description.")],
    new_text: Annotated[str, Field(description="Replacement text. Empty text deletes the match.")],
    return_diff: Annotated[bool, Field(description="False omits changed lines. Defaults to true.")] = True,
) -> _description_edits.DescriptionEditResult:
    """Replace one exact text match, like a guarded `glab api --method PUT projects/:id/merge_requests/:iid`.

    The full description is read and written, and replacement cannot be automatically undone. `old_text` must match
    exactly once, including overlapping matches. GitLab has no compare-and-swap, so a concurrent edit may be
    overwritten. Require explicit user `APPROVE` before writing. Writes are never retried. `status` is `applied`,
    `failed`, or `unknown`; re-read before retrying unknown. Set `return_diff=false` to omit changed lines.
    """
    return await _description_edits.edit_merge_request_description(
        get_client(ctx), project, iid, old_text, new_text, return_diff
    )


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
)
async def edit_work_item_description(  # noqa: PLR0913, PLR0917 - each argument is a user-facing edit option.
    ctx: Context,
    project: Project,
    iid: WorkItemIid,
    old_text: Annotated[
        str | None,
        Field(description="Legacy mode: text that must appear exactly once. Use with new_text, not edits."),
    ] = None,
    new_text: Annotated[
        str | None,
        Field(description="Legacy mode: replacement text. Empty text deletes the match. Use with old_text."),
    ] = None,
    scope: _work_items.WorkItemScope = "project",
    return_diff: Annotated[bool, Field(description="False omits changed lines. Defaults to true.")] = True,
    edits: Annotated[
        list[_description_edits.DescriptionReplacement] | None,
        Field(description="Ordered exact-text replacements. Use instead of old_text/new_text; provide 1 to 100."),
    ] = None,
    expected_hash: Annotated[
        str | None,
        Field(description="Optional lowercase SHA-256 hash of the full current UTF-8 description."),
    ] = None,
    diff_limit: Annotated[
        int,
        Field(ge=0, le=4000, description="Maximum changed_lines characters, from 0 to 4000. Defaults to 1000."),
    ] = 1000,
    table_cell: Annotated[
        _description_tables.DescriptionTableCell | None,
        Field(description="Edit one uniquely selected Markdown table cell. Use instead of old_text/new_text or edits."),
    ] = None,
) -> _description_edits.DescriptionEditResult:
    """Apply exact-text replacements with the `glab api graphql` WorkItemUpdate equivalent.

    `scope="group"` supports group work items such as epics. Group work-item support is experimental in GitLab 18.1
    and later, so permission or API errors can occur. The full description is read and written. Each replacement must
    match exactly once, including overlapping matches. Use either the legacy `old_text`/`new_text` pair or `edits`
    with 1 to 100 ordered replacements, or `table_cell` to edit one uniquely selected cell in a standalone
    pipe-delimited Markdown table. Table selection uses the exact first-column row key and header text, optionally
    within an exact ATX `section`. Fenced code is ignored. Table-cell mode rejects any `<` outside fenced code,
    including HTML comments and markup. Use legacy exact-text replacement or `edits` batch mode for descriptions
    containing HTML. Cell text cannot contain pipes, line breaks, backslashes, or backticks. Empty `new_text` deletes
    its match. `expected_hash` can guard against a changed description, but
    GitLab does not provide a server-side compare-and-swap, so a concurrent edit may still be overwritten. The
    replacement cannot be automatically undone. Require explicit user `APPROVE` before writing. Writes are never
    retried. `status` is `applied`, `failed`, or `unknown`; re-read before retrying unknown. Set `return_diff=false`
    or `diff_limit=0` to omit changed lines. The default diff limit is 1000 characters, up to 4000.
    """
    return await _description_edits.edit_work_item_description(
        get_client(ctx),
        project,
        iid,
        old_text,
        new_text,
        scope,
        return_diff,
        edits,
        expected_hash,
        diff_limit,
        table_cell,
    )
