# AGENTS.md 🦊

FastMCP server (stdio) that gives agents GitLab tools. It talks to the GitLab REST and GraphQL APIs with `httpx`, so `glab` is never required at runtime.

## Toolchain gotchas ⚠️

- Python **3.15** and uv **0.13.5** are pinned (`.python-version`, `[tool.uv] required-version`). Code uses PEP 810 `lazy from ... import ...`; keep it, it is valid syntax here.
- Always run tools through `uv run --locked ...` (matches Makefile and pre-commit). Do not use `pip` or a bare `python`.
- Ruff selects almost every rule family, with Google docstrings (`D`) enforced. Every function and class needs a docstring. Line length is 120.
- `ty` runs with `error-on-warning = true` over `src` and `tests`.

## Commands

| Goal | Command |
| --- | --- |
| Full gate (run before declaring done) | `make check` (fmt, lint, types, coverage, build) |
| Auto-format | `make fmt-fix` |
| One test | `uv run --locked pytest tests/test_list_merge_requests.py -k approved` |
| Coverage gate | `make coverage` (95% line **and** 95% branch, both fail the build) |

## Layout

- `src/gitlab_mcp/server.py`: the `mcp` FastMCP app, lifespan, and every `@mcp.tool`. New tools go here.
- `src/gitlab_mcp/client.py`: `GitLabClient.get()` and `.get_page()` (REST, base `/api/v4`; `get_page` also returns `X-Next-Page`) and `.graphql()` (absolute `/api/graphql` URL). HTTP, network, and GraphQL errors become `ToolError` subclasses with friendly hints.
- `src/gitlab_mcp/config.py`: `Settings.from_env()` reads `GITLAB_URL`, `GITLAB_TOKEN`, `GITLAB_TIMEOUT`.
- Config errors must not crash the server: the lifespan stores the error and `get_client(ctx)` raises it as a `ToolError` on the next tool call.
- When you add a tool, also add a row to the README `Tools` table.

## Tool design rules

- Tool output is sent to an agent on every call, so keep it token-lean: small Pydantic models, `exclude_if` to drop empty fields, dates trimmed to `YYYY-MM-DD`.
- Tool docstrings and `Field(description=...)` are the agent-facing docs. Name the matching `glab` command in the docstring.
- Use `Literal[...]` for enum-like args instead of a separate "list values" tool.
- Mark read-only tools with `annotations={"readOnlyHint": True, "openWorldHint": True}`.
- Prefer one GraphQL query per page over many REST calls when it saves round trips. Bound client-side scans (see `MAX_PAGES`).

## Testing rules 🧪

- The whole suite must finish almost instantly (about 1s today). No real sleeps, retries with real delays, big loops, or CPU-heavy fixtures. If you add `pytest-xdist`, cap it at `-n 8` or lower.
- Network is blocked by `pytest-socket` (`--disable-socket` in `addopts`). Mock GitLab with `respx`; never hit a real instance.
- Write sociable, behavioral tests: call tools through the real in-memory MCP client (`async with Client(mcp)`) and assert on `structured_content` and the requests `respx` captured. Do not unit-test private helpers like `_to_summary` directly.
- Use Arrange-Act-Assert comments, `pytest.mark.parametrize`, and fixtures. `asyncio_mode = "auto"`, so async tests need no marker.
- `pytest-randomly` shuffles order; tests must not depend on each other.
- Rule of Three for test helpers: shared fixtures like `gitlab_env` live in `tests/conftest.py`. Keep other helpers in their test file until a third file needs them, then move them there.
- Known noise: one `SocketBlockedError` UserWarning and one beartype `DeprecationWarning` show up on a passing run.

## Local MCP config

Configure your MCP client with the server command and the GitLab environment variables described in this project. Restart the MCP server after code changes.

## Writing and workflow

- US English, simple wording for non-native readers. Never use em dashes. Use emojis freely in code comments, docs, and commit messages.
- KISS, Boy Scout Rule, DRY only after the Rule of Three. Add comments for edge cases (see the GraphQL IID-as-string and null-author notes).
- Work in a git worktree under `.worktrees/` on a new branch (no detached HEAD). The repo has no remote yet, so branch from local `main`.
- Commits: signed, with a `Signed-off-by` trailer, Conventional Commit subject ending in an emoji (for example `feat: add list_merge_requests tool 🔀`). Remove any lines a pre-commit hook adds, such as `rh-pre-commit`.
- MRs/PRs need unicode art showing the before/after logic flow. Never post MR/PR comments until the user says `APPROVE`.
