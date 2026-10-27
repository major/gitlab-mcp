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

<details>
<summary>Goose</summary>

Merge this into `~/.config/goose/config.yaml` on Linux or macOS:

```yaml
extensions:
  gitlab:
    name: GitLab
    cmd: uvx
    args:
      - --from
      - git+https://github.com/major/gitlab-mcp.git
      - gitlab-mcp
    enabled: true
    type: stdio
    timeout: 300
```

Run `goose session` from a shell with `GITLAB_TOKEN` exported, and optionally `GITLAB_URL` for a self-hosted instance, as described in [Prerequisites](#prerequisites). The extension inherits these environment variables, so do not add literal secrets to the YAML file. Start a new session after changing the configuration. Verified with Goose CLI 1.56.0 on 2026-10-26. See the [Goose extensions documentation](https://github.com/aaif-goose/goose/blob/v1.56.0/documentation/docs/getting-started/using-extensions.md).

</details>

## Tools 🛠️

| Tool | Purpose |
| --- | --- |
| `api_get` | Read selected fields from allowlisted project, member, merge request commit, or repository file resources. |
| `create_issue` | Create an issue in a project. |
| `create_merge_request` | Create a merge request from existing branches with optional labels, assignees, and blockers. |
| `create_pipeline` | Create a pipeline for a ref after checking its expected commit SHA. |
| `edit_merge_request_description` | Replace one unique exact-text match in a merge request description. |
| `edit_work_item_description` | Apply guarded text replacements or edit a selected table cell in a work item description. |
| `get_current_user` | Return the username and ID of the user who owns the GitLab token. |
| `get_job` | Read a runner job's status, timing, failure reason, and runner metadata. |
| `get_job_artifact` | Read a bounded UTF-8 text prefix from a known job artifact path. |
| `get_job_log` | Read a bounded job log view, such as its tail, search results, or failure markers. |
| `get_merge_request` | Read merge request status, reviewers, approvals, CI, and optional details. |
| `get_merge_request_diff` | List changed files or read selected merge request patches, optionally compared with an earlier version. |
| `get_merge_request_threads` | Read review threads and status for one or more merge requests. |
| `get_merge_requests` | Read compact status for up to 20 merge requests. |
| `get_pipeline` | Read pipeline metadata and one page of its jobs. |
| `get_pipeline_failures` | List failed runner and build jobs with optional log tails. |
| `get_pipeline_test_report` | Read pipeline test suite summaries or failed and errored test cases. |
| `get_work_item` | Read a project or group work item and selected description content. |
| `land_merge_request` | Merge one merge request or cancel its queued merge. |
| `land_merge_requests` | Merge or cancel up to 20 merge requests in order. |
| `list_job_artifacts` | List one page of a job's artifact tree. |
| `list_labels` | List project and ancestor-group labels or check requested label names. |
| `list_merge_requests` | List project merge requests with optional filters. |
| `list_merge_train` | List active merge train entries. |
| `list_pipeline_schedules` | List one page of a project's pipeline schedules. |
| `list_pipelines` | List one page of project, merge request, or schedule pipelines. |
| `list_work_item_notes` | Read a bounded page of human notes for a work item. |
| `post_review_notes` | Validate or post a batch of general, file-level, or inline merge request review notes. |
| `reply_to_thread` | Reply to or resolve one merge request review thread. |
| `reply_to_threads` | Reply to or resolve up to 20 review threads in order. |
| `resolve_open_threads` | Resolve open review threads on one merge request. |
| `retry_ci` | Retry a failed or canceled CI job or pipeline after checking its expected SHA. |
| `update_merge_request` | Update merge request details, people, labels, blockers, approval, state, or rebase status. |
