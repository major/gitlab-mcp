# gitlab-mcp 🦊

An MCP server for fast GitLab developer workflows. It works with gitlab.com
and self-managed GitLab, and it talks to the REST API directly, so you do
**not** need `glab` installed.

## Configuration

| Variable         | Required | Default              | Notes                                         |
| ---------------- | -------- | -------------------- | --------------------------------------------- |
| `GITLAB_TOKEN`   | yes      |                      | `read_api` for read tools, `api` for write    |
| `GITLAB_URL`     | no       | `https://gitlab.com` | Instance URL; a trailing `/api/v4` is fine    |
| `GITLAB_TIMEOUT` | no       | `30`                 | Request timeout in seconds                    |

For a self-managed instance with a private CA, set `SSL_CERT_FILE` to your CA bundle.

## Run it

```bash
GITLAB_TOKEN=glpat-... uv run gitlab-mcp
```

Example MCP client config:

```json
{
  "mcpServers": {
    "gitlab": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/gitlab-mcp", "gitlab-mcp"],
      "env": { "GITLAB_URL": "https://gitlab.example.com", "GITLAB_TOKEN": "glpat-..." }
    }
  }
}
```

## Tools

| Tool                        | Same as                                         | Returns                                                              |
| --------------------------- | ----------------------------------------------- | -------------------------------------------------------------------- |
| `get_current_user`          | `glab api user --jq .username`                  | `{"username", "id"}`                                                 |
| `list_merge_requests`       | `glab mr list` + filters                        | `{"web_url_prefix", "merge_requests": "<TSV>", "has_more"}`          |
| `list_merge_train`          | `glab api projects/:id/merge_trains?scope=active&sort=asc` | `{"web_url_prefix", "entries": "<TSV>", "has_more"}`                |
| `get_merge_request`         | `glab mr view`                                  | `{"iid", "title", "merge_status", "threads", ...}`                   |
| `get_merge_requests`        | GitLab GraphQL batch lookup                     | `{"merge_requests": [...], "missing_iids": [...]}`                   |
| `get_merge_request_threads` | `glab mr note list` / `glab mr view --comments` | `{"threads", "next_cursor", "not_found_ids"}`                        |
| `reply_to_thread`           | `glab mr note create --reply` / `glab mr note resolve` | Reply, resolve-only, or reply then resolve; `{"thread_id", "note_id?", "resolved?", "warning?"}` |
| `get_merge_request_diff`    | `glab mr diff`                                  | `{"head_sha", "files" or "diffs", "next_cursor", "paths_not_found"}` |
| `land_merge_request`        | `glab mr merge`                                 | `{"result", "state", "merge_status", ...}`                           |
| `update_merge_request`      | `glab mr update` / `approve` / `rebase`         | `{"applied", "pending", "unknown", "failed", ...}`                   |

`get_merge_request_threads` removes trailing `review-bot:finding` markers from
note bodies. `max_body_chars` accepts 0 through 4000 and defaults to 4000 per
note. Set it to 0 to omit note text.

Get user approval before posting a reply or resolving a thread. Replying and
resolving are two GitLab writes. If a reply succeeds but resolution fails, the
result includes a warning. Check the thread before retrying, and do not repost
the reply.

`list_merge_requests` needs a token with the `read_api` scope. Pass `project`
as the full path, like `group/subgroup/project`, not the numeric ID or the
URL. The TSV has a header row with the columns `iid`, `title`, `author`,
`state`, `draft`, `approved`, `approved_by`, `open_threads`, and `updated`.
Every row has every column, and an empty cell means none.

Use `list_merge_requests(search="LWLP-36")` to search merge request titles or
descriptions. GitLab applies this search server-side.

`get_merge_request` includes descriptions by default for compatibility. Set
`include_description=false` to skip them. `get_merge_requests` skips descriptions
by default; set `include_description=true` to include them. Both tools cap descriptions with
`description_limit` (0 through 4000 characters, default 4000). A limit of 0
omits both the description and its `description_truncated` flag. For example:

```text
get_merge_request(project="group/project", iid=36, include_description=true, description_limit=500)
get_merge_requests(project="group/project", iids=[36, 38], include_description=true, description_limit=500)
```

`get_merge_requests` takes up to 20 unique positive IIDs. Duplicate IIDs are
removed while keeping their first-seen order. It returns unavailable IIDs in
`missing_iids` and fetches the batch with one GraphQL request. Descriptions are
not included unless requested.
Batch results contain compact status facts, without reviewers, change summaries,
or thread indexes. Use `get_merge_request` when those details are needed.

`list_merge_train` lists active merge train cars on GitLab Premium and Ultimate.
Its `position` is one-based within each `target_branch`, not a global queue position.

## Development

```bash
make check   # format, lint, types, tests with coverage, build
```
