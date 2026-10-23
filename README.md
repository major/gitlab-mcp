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

| Tool                             | Same as                                                    | Returns                                                                                          |
| -------------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `get_current_user`               | `glab api user --jq .username`                             | `{"username", "id"}`                                                                             |
| `list_merge_requests`            | `glab mr list` + filters                                   | `{"web_url_prefix", "merge_requests": "<TSV>", "has_more"}`                                      |
| `list_merge_train`               | `glab api projects/:id/merge_trains?scope=active&sort=asc` | `{"web_url_prefix", "entries": "<TSV>", "has_more"}`                                             |
| `get_merge_request`              | `glab mr view`                                             | `{"iid", "title", "merge_status", "threads", ...}`; `include_jobs=true` adds failed job IDs and names as TSV |
| `get_merge_requests`             | GitLab GraphQL batch lookup                                | `{"merge_requests": [...], "missing_iids": [...]}`                                               |
| `get_merge_request_threads`      | `glab mr note list` / `glab mr view --comments`            | `{"threads", "next_cursor", "not_found_ids"}`                                                    |
| `reply_to_thread`                | `glab mr note create --reply` / `glab mr note resolve`     | Reply, resolve-only, or reply then resolve; `{"thread_id", "note_id?", "resolved?", "warning?"}` |
| `get_merge_request_diff`         | `glab mr diff`                                             | `{"head_sha", "files" or "diffs", "next_cursor", "paths_not_found"}`; file rows include `new_line_ranges` and `ranges_complete` |
| `post_review_notes`              | GitLab diff discussion API                                 | Per-note `validated`, `posted`, `failed`, `unknown`, `unverified`, or `unattempted` status       |
| `land_merge_request`             | `glab mr merge`                                            | `{"result", "state", "merge_status", ...}`                                                       |
| `update_merge_request`           | `glab mr update` / `approve` / `rebase`                    | `{"applied", "pending", "unknown", "failed", ...}`                                               |
| `get_work_item`                  | GitLab GraphQL work item lookup                            | Work item details; `linked` and `children` are bounded pages with cursors                        |
| `list_work_item_notes`           | GitLab GraphQL work item notes                             | Bounded human notes; system notes are skipped; resumable with `cursor`                           |
| `edit_work_item_description`     | Read, replace one exact match, write back                  | Match counts and a changed-line diff (max 4000 characters)                                       |
| `edit_merge_request_description` | Read, replace one exact match, write back                  | Match counts and a changed-line diff (max 4000 characters)                                       |
| `create_merge_request`           | `glab mr create`                                           | Separate creation and `blocked_by` outcomes, plus label results                                  |

`get_merge_request_threads` removes trailing `review-bot:finding` markers from
note bodies. `max_body_chars` accepts 0 through 4000 and defaults to 4000 per
note. Set it to 0 to omit note text.

Get user approval before posting a reply or resolving a thread. Replying and
resolving are two GitLab writes. If a reply succeeds but resolution fails, the
result includes a warning. Check the thread before retrying, and do not repost
the reply.

For a compact unresolved thread index, use
`get_merge_request_threads(project="group/project", iid=36, state="open", view="headings")`.
Read selected threads with `ids=["<thread ID>"]` and `view="full"`.
Full thread IDs select exact threads and override `state`. When
continuing with a cursor, repeat the same filters, `view`, and `max_body_chars`. A thread heading is its
first Markdown heading, or its first nonempty line if there is no heading,
limited to 160 characters.

`update_merge_request(add_reviewers=["username"])` appends reviewers and keeps
existing reviewers. Do not use `PUT reviewer_ids` to append reviewers, because it
can replace the existing list.

`post_review_notes` posts inline review notes. Pass
`post_review_notes(project="group/project", iid=36, head_sha="<full SHA>",
notes=[{"file": "src/file.py", "line": 12, "body_path": "/absolute/path/note.txt"}])`.
Use the full 40-character `head_sha` from `get_merge_request` or
`get_merge_request_diff`, and post no more than 20 notes per call. `file` must be
an old or new diff path. Supply
exactly one of `line` or `old_line`, and exactly one of inline `body` or
`body_path`. A body file must be an absolute, server-local path to a regular
UTF-8 file no larger than 100000 bytes. Use `body` for inline text instead.

Validate a batch before requesting approval by setting `validate_only=true`.
This runs the same body, path, line-position, and SHA preflight as posting and
returns the proposed positions, but it creates no discussions. Validation does
not need `APPROVE`. After reviewing the results, get explicit user `APPROVE`
before posting. The posting call repeats the preflight, including its SHA checks,
so a changed merge request stops the batch without writing notes.

All notes are preflighted before any writes. Diff paths are checked across all
available pages, up to five. If GitLab reports more pages at that limit, the
scan is incomplete and no notes are posted. Scanning all available pages also
checks for duplicate old or new path matches. The tool rechecks the diff head
before posting, but this is not atomic with the writes. Notes are posted
sequentially, and the first failure stops the call. Results report each note's
status (`validated`, `posted`, `failed`, `unknown`, `unverified`, or
`unattempted`) and its GitLab ID and position when available. Never blindly
retry `unknown` or `unverified` notes, and do not repost notes already marked
`posted`.

In file-list mode, `get_merge_request_diff` includes `new_line_ranges`, the
inclusive new-side ranges for each nonempty hunk, including context lines.
Deletion-only hunks have no new-side ranges. `ranges_complete` is false when a
file's patch was not scanned or GitLab did not provide usable patch text, so an
empty range list is not treated as proof that no anchors exist in those cases.
Use `post_review_notes(validate_only=true)` to check exact comment positions.

`get_merge_request(include_jobs=true)` adds failed pipeline jobs in the
`failed_jobs` TSV, with `id` and `name` columns. The list is omitted when jobs
are not requested, the merge request has no pipeline, or no failed jobs are
found. `jobs_complete` is false only when the failed-job scan reaches its page
cap; otherwise it is omitted.

`list_merge_requests` needs a token with the `read_api` scope. Pass `project`
as the full path, like `group/subgroup/project`, not the numeric ID or the
URL. The TSV has a header row with the columns `iid`, `title`, `author`,
`state`, `draft`, `approved`, `approved_by`, `open_threads`, and `updated`.
Every row has every column, and an empty cell means none.

Use `list_merge_requests(search="LWLP-36")` to search merge request titles or
descriptions. GitLab applies this search server-side.

`list_merge_requests` also takes `created_after`, `updated_after`, and
`target_branch`. Dates must be timezone-aware ISO 8601 datetimes, like
`2026-10-01T00:00:00+00:00`. Date-only and timezone-naive values are rejected.
`include_source_branch=true` and `include_labels=true` are opt-in and append
columns. Labels are a JSON array in one cell. `labels_complete` is `false` when
a label list was cut at its first 100 labels. The default TSV does not change.

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

### Work items 🧩

`get_work_item(project="group/project", iid=42)` returns the title, type, state,
labels, assignees, linked items, and children. `linked_items` and `children` are
bounded pages. When a page is cut short, pass the returned cursor back with the
same arguments. Use `scope="group"` for group-level work items. Set
`include_description=false` to skip the description, or cap it with
`description_limit` (0 through 4000 characters).
Group epics need GitLab 18.1 or later and the required license and permissions.
The work-item GraphQL API is experimental, so availability can vary by instance.

`list_work_item_notes(project="group/project", iid=42)` returns human notes
only. System notes, such as label changes, are skipped. `max_body_chars` caps
each note at 4000 characters. Continue with `cursor`.
Each discussion includes at most 50 notes. `notes_truncated` reports omitted
notes or text; the discussion cursor does not retrieve omitted nested notes.

### Editing descriptions ✏️

`edit_work_item_description` and `edit_merge_request_description` replace one
exact, literal piece of a description. Both take `old_text` and `new_text`.
The rules are:

- The old text must not be empty, and the new text must differ from it. A no-op is rejected.
- The old text must match exactly one place. Zero matches or several matches fail, and nothing is written.
- Match counts include overlapping matches.
- The result includes a changed-line diff, capped at 4000 characters.
- The tool reads the description, edits it, and writes it back. There is no
  atomic concurrency guard. If someone else edits the same description during
  that window, their change can be lost. Read the description again after the call.
- Get explicit user approval before editing.

### Creating merge requests 🆕

`create_merge_request` needs `project`, `source_branch`, `target_branch`, and
`title`. Pass exactly one of:

- `description`: inline text
- `description_file`: an absolute, server-local path to a regular UTF-8 file of at most 100000 bytes

Optional arguments are `labels`, `remove_source_branch`, and `blocked_by`, a
list of up to 20 IIDs from the same project.

- The tool never pushes. The source branch must already exist on GitLab.
- Description-file and blocker preflight failures prevent creation.
- Creation and dependency results are separate. If the merge request is
  created but a `blocked_by` link fails, the merge request stays. Do not create
  it again. Check the result and fix the link.
- Unknown labels are reported as failed `labels:<name>` items. The tool never
  creates labels silently. Valid labels and unrelated updates still apply.
- Get explicit user approval before creating a merge request.

### Reloading after server changes 🔄

An MCP client reads the tool catalog when it starts the server. After you
change this repo, reload the MCP server in your client. A missing tool can mean
an older running process, a cached catalog, or client filtering. Compare both
tool names and schemas with the local inventory after reloading.

The client runs the checkout named in `--directory`. Make sure that path is the
worktree you are testing. The `gitlab-mcp` entry point runs `gitlab_mcp:main`
from that checkout.

## Development

```bash
make check   # format, lint, types, tests with coverage, build
```
