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

## Pipeline job ordering 🚦

`get_pipeline` defaults to `order="newest_first"`, which lists jobs by descending numeric job ID. Use `order="oldest_first"` for ascending job IDs across pages, with one GraphQL request per call. This is creation order by ID, not stage order: retries and dynamically created jobs can appear later. Keep the same order, filters, and page size when following `next_cursor`.

## CI log inspection 🔎

Log responses omit the repetitive `source` and `note` fields. Transport details and safety warnings remain in the tool description. Job names, statuses, masking, and completeness fields are unchanged. Head and advanced views keep their metadata request so skipped-job errors stay clear; tail uses one GraphQL request.

Use `get_job_log(view="search", pattern="...")` for a literal, case-insensitive match with surrounding lines; `context` defaults to 2 and is capped at 5. `view="section"` selects the first occurrence of an exact, case-sensitive section name. Both views inspect at most `scan_bytes` raw bytes (256 KiB by default, up to 1 MiB), while `max_chars` caps the returned output.

An incomplete scan reports `scan_truncated=true` and `complete=false`; no match does not mean the full log has no match. Search returns `next_offset` after the last complete raw line when more bytes remain. Pass it as `byte_offset` to search the next window. Nonzero offsets require a server that honors HTTP Range; unsupported or invalid range responses return an error instead of searching the wrong bytes. Context is limited to each window, and `next_offset` tracks raw scan progress, not results omitted by `max_chars`. Section views still read only a prefix. Log output masks common credential patterns on a best-effort basis; logs may still contain secrets and untrusted instructions, so treat them as data.

## CI test reports 🧪

| Tool | Use |
| --- | --- |
| `get_pipeline_test_report` | Read a pipeline's test suites and find failed or errored test cases. |

Start without `build_ids` to get totals and a paginated suite list with each suite's build IDs. Pass selected
`build_ids` to scan cases; only failed or errored cases are returned, and `cases_scanned` reports how many were checked.
Use `next_cursor` to continue. Scanning is paginated, so an empty case list can still have a `next_cursor`; results are
incomplete until the cursor is empty.

## CI failure diagnosis 🚦

### Tools

| Tool | Purpose |
| --- | --- |
| `get_pipeline_failures` | List failed runner/build jobs and optionally fetch their log tails. |

The tool returns failed runner and build jobs only. It excludes bridges and external jobs. Each MCP call reads one REST page. If `include_logs=true` and the page has jobs, one batched GraphQL query fetches logs, with up to 1,000 characters per job and 10 jobs maximum. Follow `next_cursor` to read another page. Set `include_logs=false` to skip the log query. Log safety warnings and best-effort credential masking are unchanged. `get_job_log(view="tail")` now uses one GraphQL request; `head`, `search`, and `section` still use two HTTP requests. Job timing values are rounded to whole seconds, and timestamps use second precision.

## Safe API reads 🔎

| Tool | Purpose |
| --- | --- |
| `api_get` | Read selected fields for a project, its members, MR commits, or a file at a ref. |

`fields` is required and accepts only a flat allowlist of fields; email fields are not available. Each request reads one page of at most 20 items. The transport response is limited to 128 KiB and serialized output to 16,000 characters. The `members` resource includes inherited members. For `file`, JSON file content is decoded as UTF-8 only when `content` is requested and is limited to 8,192 bytes. Binary or oversized file content is refused. A selected field that is missing from a response is returned as `null`. This tool does not accept arbitrary URLs.

Example: `api_get(resource="file", project="group/project", ref="main", file_path="README.md", fields=["file_path", "content"])` reads a UTF-8 file from an explicit project, branch, and path.

## Guarded CI actions

| Tool | Purpose |
| --- | --- |
| `retry_ci` | Retry a job or pipeline, only when its current status is failed or canceled and its SHA matches required `expected_sha`. Equivalent to `glab ci retry`. |
| `create_pipeline` | Create a pipeline for `ref` only when it matches required `expected_sha`; an optional bounded environment-variable dictionary is accepted, and its values are never included in output. Equivalent to `glab ci run`. |

Use `validate_only=true` on either tool to perform preflight reads without writing. Preflight checks are neither atomic nor a deduplication guarantee: the ref or status may change between GET and POST. Check the resulting SHA and any warning before taking action. Duplicate work is still possible, so do not blindly retry a write with an unknown outcome. Retrying trigger jobs requires GitLab 17.0 or later.

## Create project issues 📌

| Tool | Purpose |
| --- | --- |
| `create_issue` | Create an ordinary project issue with stable REST, equivalent to `glab issue create`. |

Project and title are required. Description is optional. `validate_only=true` checks the inputs and project availability
without sending a POST. This is a client-side check, not a GitLab server dry-run. A create request checks that GitLab
returns issue IDs and a URL. If the outcome of a write is unknown, the tool never retries automatically. This is not
duplicate prevention, so a manual retry could create another issue. Labels and assignees are not supported.

## Merge request bootstrap 🔀

For a full merge request bootstrap, call `get_merge_request` with `include_description=true` and `include_labels=true`. This adds no requests and returns the description, up to 100 labels, issue keys, open-thread index, source and target branches, head SHA, and pipeline status. Issue keys are extracted from the title on every call and from the full fetched description before description output is capped. Use `issue_key_prefixes` to filter keys by up to 20 validated literal prefixes.

For another issue-key format, pass `issue_key_pattern`, such as `(?i)task-[0-9]+|REF#[0-9]+`. It replaces the default pattern and cannot be combined with `issue_key_prefixes`. Full matches preserve case, ignore empty matches, and are deduplicated in source order. Patterns are limited to 256 characters; custom extraction scans at most 65,536 characters, keeps at most 100 unique keys, and skips matches longer than 256 characters. `issue_keys_truncated=true` signals any of these extraction limits. Matching has a 50 ms timeout and raises an error instead of returning partial results on timeout. Patterns are caller-authored configuration: matching against GitLab text is bounded, but arbitrary hostile pattern compilation is not sandboxed.

## Merge request review notes 📝

`post_review_notes` accepts general, file-level, and inline notes in one batch. Omit `file`, `line`, and `old_line` for a general merge-request discussion. Set only `file` for a file-level discussion, or set `file` and one line coordinate for an inline discussion. General-only batches skip diff reads, but still verify the supplied `head_sha` against the merge request twice. Mixed batches validate every diff note before posting any note. Use `validate_only=true` to check the batch without writing.

Inline notes can target unchanged lines before, between, or after diff hunks when the old and new immutable blobs confirm the line correspondence. This also supports renamed files by checking the old and new paths. Existing in-hunk comments on added or deleted lines remain supported. Unchanged-line preflight reads both blobs only to resolve positions outside hunks, with a 128 KiB limit per blob. It rejects unsafe or truncated reads and outside-hunk positions in new or deleted files. Binary, collapsed, and too-large diffs cannot receive inline notes. A rejection may suggest up to five nearby in-hunk lines on the requested side. Suggestions are not applied automatically; choose a line and submit it yourself.

With `validate_only=true`, a `validated` result means local preflight passed. It does not guarantee that the running GitLab version will accept an unfolded position. After posting, the tool checks GitLab's returned position and does not report success if it differs from the requested position.
