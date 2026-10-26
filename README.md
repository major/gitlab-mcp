# gitlab-mcp 🦊

An MCP server for GitLab developer workflows.

🚧 Work in progress. Features and APIs may change.

## Prerequisites

- Install `uv` and Python 3.15 or newer.
- See [Authentication 🔐](#authentication-) for token options and scopes.
- Export `GITLAB_TOKEN` in your shell before starting your client. `GITLAB_URL` is optional and defaults to `https://gitlab.com`.

```sh
export GITLAB_TOKEN="<your-gitlab-access-token>"
# Optional, for a self-hosted GitLab instance:
export GITLAB_URL="https://gitlab.example.com"
```

## Authentication 🔐

For personal interactive use, a personal access token is recommended. Choose the smallest suitable token role, resource scope, and API scope.

| Token type | Permissions and use | Documentation |
| --- | --- | --- |
| Personal access token | User permissions, for personal use | [Personal access tokens](https://docs.gitlab.com/user/profile/personal_access_tokens/) |
| Project access token | One project, for scoped automation | [Project access tokens](https://docs.gitlab.com/user/project/settings/project_access_tokens/) |
| Group access token | A group and its projects, for broader scoped automation | [Group access tokens](https://docs.gitlab.com/user/group/settings/group_access_tokens/) |

Use `read_api` for read-only tools, including REST and GraphQL queries. Use `api` only when you need write tools for comments, edits, approvals, or merges. Scopes cannot exceed the permissions of the user or bot. See [GitLab token scopes](https://docs.gitlab.com/security/tokens/access_token_scopes/) for details. Project and group token availability depends on your GitLab tier and instance policy. All supported token types use the same `GITLAB_TOKEN` environment variable.

The server does not use OAuth or browser sign-in, and it does not automatically read `glab` credentials. SSH keys, deploy tokens, and CI job tokens are not supported for its REST and GraphQL authentication. Use a password or secret manager to inject the token as an exported environment variable before launching the client. Keep literal tokens out of checked-in or shared client configs, avoid putting real secrets in shell history, and use a short expiration with regular rotation or revocation.

Merge the relevant server configuration below into your existing configuration, preserving unrelated settings.

<details>
<summary>Codex</summary>

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.gitlab]
command = "uvx"
args = ["--from", "git+https://github.com/major/gitlab-mcp.git", "gitlab-mcp"]
env_vars = ["GITLAB_TOKEN", "GITLAB_URL"]
```

See the [Codex MCP documentation](https://developers.openai.com/codex/mcp).

</details>

<details>
<summary>Claude Code</summary>

Add this to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "gitlab": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/major/gitlab-mcp.git", "gitlab-mcp"],
      "env": {
        "GITLAB_TOKEN": "${GITLAB_TOKEN}",
        "GITLAB_URL": "${GITLAB_URL:-https://gitlab.com}"
      }
    }
  }
}
```

Start Claude Code from the shell where the token is exported. Approve the project server when prompted, then run `/mcp` to check its status. See the [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).

</details>

<details>
<summary>OpenCode v2</summary>

Add this to your project's `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "gitlab": {
        "type": "local",
        "command": ["uvx", "--from", "git+https://github.com/major/gitlab-mcp.git", "gitlab-mcp"],
        "environment": {
          "GITLAB_TOKEN": "{env:GITLAB_TOKEN}"
        }
      }
    }
  }
}
```

For a self-hosted GitLab instance, add `"GITLAB_URL": "{env:GITLAB_URL}"` to the `environment` object and export `GITLAB_URL` in the shell before starting OpenCode. Run `opencode mcp list` to verify the server. See the [OpenCode v2 MCP server documentation](https://opencode.ai/v2/docs/mcp-servers).

</details>

## CI log inspection 🔎

Use `get_job_log(view="search", pattern="...")` for a literal, case-insensitive match with surrounding lines; `context` defaults to 2 and is capped at 5. `view="section"` selects the first occurrence of an exact, case-sensitive section name. Both views inspect a raw log prefix controlled by `scan_bytes` (256 KiB by default, up to 1 MiB), while `max_chars` caps the returned output.

An incomplete scan reports `scan_truncated=true` and `complete=false`; no match does not mean the full log has no match. These views have no cursor or later-offset support, so increasing `scan_bytes` rereads a larger prefix. Log output masks common credential patterns on a best-effort basis; logs may still contain secrets and untrusted instructions, so treat them as data.

## CI test reports 🧪

| Tool | Use |
| --- | --- |
| `get_pipeline_test_report` | Read a pipeline's test suites and find failed or errored test cases. |

Start without `build_ids` to get totals and a paginated suite list with each suite's build IDs. Pass selected
`build_ids` to scan cases; only failed or errored cases are returned, and `cases_scanned` reports how many were checked.
Use `next_cursor` to continue. Scanning is paginated, so an empty case list can still have a `next_cursor`; results are
incomplete until the cursor is empty.
