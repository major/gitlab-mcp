# gitlab-mcp 🦊

An MCP server for GitLab developer workflows.

🚧 Work in progress. Features and APIs may change.

## Incremental merge request diffs

Call `get_merge_request_diff` with `since_sha` set to an earlier full commit SHA to compare collected GitLab diff
versions. The returned patches remain relative to the latest merge request diff, not commit-to-commit patches. Numeric
unified-diff hunk ranges are ignored only when checking whether a file changed, so target-only line offset rebases do
not appear as changes.

If GitLab has no matching version, or either snapshot is unavailable or incomplete, the result includes a warning
instead of claiming the diff is unchanged. Files removed from the current merge request diff are included with an empty
patch and a warning because they have no current comment anchors. When a patch result includes `next_cursor`, pass it
back with the same `since_sha` and requested paths. Cursors are rejected if those inputs or the latest head change.

## Merge request thread triage

For triage, start with `view="headings"`, then pass selected thread IDs as `ids=[...]` with `view="full"` to read their
note bodies.

Use `get_merge_request_threads` for a compact review view: `view="full"` returns capped note bodies, `view="headings"`
returns root-note headings without bodies, and `view="finding"` trims separate greetings and signoffs from bot notes
that contain numbered findings. For recognized section-format bot findings, it keeps the bold title and the “Needs to
be improved,” “Failure scenario,” and “How to fix” sections. It also removes introductory chatter before action bullets
in a recognized bold-heading layout. `count_only=true` returns exact `open` and `resolved` totals without fetching
threads or note bodies. It counts both states regardless of `state`, and cannot be combined with `cursor`, `ids`, `file`,
or `author`. For batch calls, count results include each `iid` and missing merge requests remain in `missing_iids`.

The `ids` thread filter is available only with a single `iid`. It is rejected with batch `iids` because one thread ID
can be ambiguous across merge requests.

`state` takes one value (`open`, `resolved`, `comments`, or `all`) or a list such as `["open", "comments"]` to keep
several kinds of thread in one call. Duplicates are ignored, `all` in a list keeps every thread, and an empty list is an
error. A list uses the same scan, cursor, and output budget as a single value.

## Tools

### Focused work-item reads 📖

Use `get_work_item(section="WP8")` for one section or `section=["Context", "WP8"]` for several sections in one call.
Use `headings_only=true` to find heading names first. Section content shares the `description_limit` budget, and
each cut section is marked as truncated. Missing or ambiguous headings fail the whole request. Focused reads
require descriptions and cannot use a widget cursor. `description_hash` covers the full fetched description,
even when the returned content is focused or truncated.

| Tool | Description |
| --- | --- |
| `get_work_item` | Read work-item details, all headings, or selected sections with a full-description hash. |
| `get_merge_request` / `get_merge_requests` | Read MR details; merged MRs include the merge date and merging username when available. `get_merge_request` skips the description unless `include_description=true`. |
| `edit_work_item_description` | Edit a work-item description using exact text, ordered replacements, or a unique table cell. |
| `list_labels` | List the first 100 project and ancestor label names; refine `search` when `has_more` is true. Pass `names` to check which exact label names exist (`existing`/`missing`) before `create_merge_request(labels=...)`. |
| `reply_to_threads` | Reply to or resolve up to 20 review threads sequentially, with a per-thread outcome. |
| `create_merge_request` | Create a merge request with checked labels and assignees, then add requested blockers. |
| `update_merge_request` | Change MR fields, reviewers, assignees, blockers, or approval; rebase reports `applied`, `pending` (still running after about 10 seconds), or `failed` (for example, when the MR has conflicts). |
| `land_merge_request` | Queue, merge now, or cancel an MR; `auto` and `now` also return the queued `head_sha` and `pipeline_status`. |

Work-item table-cell edits support standalone pipe-delimited Markdown tables with a unique, non-empty header and a
valid separator row. The first column is the row key. A section can limit selection to an exact ATX heading and its
nested content. Fenced code is ignored. Table-cell mode rejects any `<` outside fenced code, including HTML comments
and markup. Use legacy exact-text replacement or the `edits` batch mode for descriptions containing HTML. The edit
fails without writing when a selected table is malformed or the row-and-column match is missing or ambiguous. Cell
replacements cannot contain pipes, line breaks, backslashes, or backticks.
