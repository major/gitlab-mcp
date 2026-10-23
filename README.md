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
