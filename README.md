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
| `get_merge_request_threads` | `glab mr note list` / `glab mr view --comments` | `{"threads", "next_cursor", "not_found_ids"}`                        |
| `get_merge_request_diff`    | `glab mr diff`                                  | `{"head_sha", "files" or "diffs", "next_cursor", "paths_not_found"}` |
| `land_merge_request`        | `glab mr merge`                                 | `{"result", "state", "merge_status", ...}`                           |
| `update_merge_request`      | `glab mr update` / `approve` / `rebase`         | `{"applied", "pending", "unknown", "failed", ...}`                   |

`list_merge_requests` needs a token with the `read_api` scope. Pass `project`
as the full path, like `group/subgroup/project`, not the numeric ID or the
URL. The TSV has a header row with the columns `iid`, `title`, `author`,
`state`, `draft`, `approved`, `approved_by`, `open_threads`, and `updated`.
Every row has every column, and an empty cell means none.

`list_merge_train` lists active merge train cars on GitLab Premium and Ultimate.
Its `position` is one-based within each `target_branch`, not a global queue position.

## Development

```bash
make check   # format, lint, types, tests with coverage, build
```
