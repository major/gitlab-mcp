# gitlab-mcp 🦊

An MCP server for fast GitLab developer workflows. It works with gitlab.com
and self-managed GitLab, and it talks to the REST API directly, so you do
**not** need `glab` installed.

## Configuration

| Variable         | Required | Default              | Notes                                         |
| ---------------- | -------- | -------------------- | --------------------------------------------- |
| `GITLAB_TOKEN`   | yes      |                      | Access token with `read_api` or `api` scope   |
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

| Tool               | Same as                          | Returns                 |
| ------------------ | -------------------------------- | ----------------------- |
| `get_current_user` | `glab api user --jq .username`   | `{"username", "id"}`    |

## Development

```bash
make check   # format, lint, types, tests with coverage, build
```
