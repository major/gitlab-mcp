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
that contain numbered findings. `count_only=true` returns exact `open` and `resolved` totals without fetching threads
or note bodies. It counts both states regardless of `state`, and cannot be combined with `cursor`, `ids`, `file`, or
`author`. For batch calls, count results include each `iid` and missing merge requests remain in `missing_iids`.

The `ids` thread filter is available only with a single `iid`. It is rejected with batch `iids` because one thread ID
can be ambiguous across merge requests.

`state` takes one value (`open`, `resolved`, `comments`, or `all`) or a list such as `["open", "comments"]` to keep
several kinds of thread in one call. Duplicates are ignored, `all` in a list keeps every thread, and an empty list is an
error. A list uses the same scan, cursor, and output budget as a single value.

## Tools

| Tool | Description |
| --- | --- |
| `list_labels` | List the first 100 project and ancestor label names; refine `search` when `has_more` is true. |
| `reply_to_threads` | Reply to or resolve up to 20 review threads sequentially, with a per-thread outcome. |
