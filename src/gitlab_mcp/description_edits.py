"""Exact-text edits for merge request and work-item descriptions ✏️.

Each helper reads the full description, replaces exactly one match of `old_text`,
and writes the whole description back. Nothing is written unless the match is
unique. GitLab has no compare-and-swap for descriptions, so a concurrent edit can
still be overwritten. Writes are never retried.
"""

import difflib
from itertools import islice
from typing import Literal, NoReturn
from urllib.parse import quote
lazy from collections.abc import Awaitable

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

lazy from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError, GitLabGraphQLError
lazy from gitlab_mcp.work_items import WorkItemScope, read_work_item_for_edit

# The changed lines sent to the agent never exceed this many characters.
DIFF_CAP = 4000
SERVER_ERROR_STATUS = 500
CONCURRENCY_WARNING = (
    "GitLab has no compare-and-swap for descriptions. "
    "An edit made by someone else during this call could be overwritten."
)
UNKNOWN_WARNING = (
    "The write outcome is unknown, so the description may or may not have changed. Re-read it before you retry."
)

# One input object carries both the work-item global ID and the new description. 🧩
WORK_ITEM_UPDATE_MUTATION = """
mutation($input: WorkItemUpdateInput!) {
  workItemUpdate(input: $input) {
    workItem { id }
    errors
  }
}
"""


class DescriptionEditResult(BaseModel):
    """The outcome of one exact-text edit, kept small for the agent.

    `changed_lines` is a zero-context unified diff of the whole description, so
    its `@@` headers carry real line numbers. It is only filled when the write was
    applied. `truncated` is true when the diff was cut to `DIFF_CAP` characters.
    """

    status: Literal["applied", "failed", "unknown"]
    changed_lines: str = Field(default="", exclude_if=lambda value: not value)
    truncated: bool = False
    warning: str = Field(default="", exclude_if=lambda value: not value)


def _raise_edit_error(message: str) -> NoReturn:
    """Raise one caller-facing error for a rejected edit."""
    raise ToolError(message)


def _validate_edit(old_text: str, new_text: str) -> None:
    """Reject edits that cannot succeed or would change nothing, before any request is sent.

    Empty `new_text` is allowed, because it deletes the matched text.
    """
    if not old_text:
        _raise_edit_error("old_text must not be empty.")
    if old_text == new_text:
        _raise_edit_error("old_text and new_text are identical, so there is nothing to change. No write was made.")


def _replace_exactly_once(description: str, old_text: str, new_text: str) -> str:
    """Return `description` with its only match of `old_text` replaced.

    The second search starts one character after the first match. That way
    overlapping matches, such as two "aa" in "aaa", count as two matches.

    Raises:
        ToolError: When `old_text` is missing or matches more than once.
    """
    first = description.find(old_text)
    if first == -1:
        _raise_edit_error("old_text was not found in the description. No write was made.")
    if description.find(old_text, first + 1) != -1:
        _raise_edit_error("old_text matches more than once. Add nearby text so it matches once. No write was made.")
    return description[:first] + new_text + description[first + len(old_text) :]


def _lines(text: str) -> list[str]:
    """Split text into lines. Empty text has no lines, so a deletion shows no blank line."""
    return text.split("\n") if text else []


def _changed_lines(before: str, after: str) -> tuple[str, bool]:
    """Return the zero-context diff of two full descriptions, capped at `DIFF_CAP` characters.

    Both texts are split the same way, so a newline-only edit shows up as a changed
    line. The `---` and `+++` file headers are dropped. The `@@` hunk headers stay,
    because they give the real line numbers. Reading stops once the text passes the cap,
    so a large diff is never built in full.

    Returns:
        The diff text, and whether the full diff was longer than the cap.
    """
    diff = difflib.unified_diff(_lines(before), _lines(after), n=0, lineterm="")
    text = ""
    for line in islice(diff, 2, None):  # The first two lines are the file headers.
        candidate = f"{text}\n{line}" if text else line
        if len(candidate) > DIFF_CAP:
            return candidate[:DIFF_CAP], True
        text = candidate
    return text, False


def _unknown_result(exc: ToolError) -> DescriptionEditResult:
    """Build the result for a write whose outcome GitLab never confirmed.

    The error text may end with a period already, so one is added only when it is missing.
    """
    return DescriptionEditResult(status="unknown", warning=f"{str(exc).rstrip('.')}. {UNKNOWN_WARNING}")


async def _write_edit(write: Awaitable[object], before: str, after: str) -> DescriptionEditResult:
    """Send one write once and turn its outcome into a result. Writes are never retried.

    A network error or a 5xx can happen after GitLab saved the change, so those
    are `unknown`. A 4xx or a GraphQL payload error means GitLab refused the
    change, so it is `failed`.
    """
    try:
        await write
    except GitLabConnectionError as exc:
        return _unknown_result(exc)
    except GitLabAPIError as exc:
        if exc.status >= SERVER_ERROR_STATUS:  # A gateway can time out after GitLab already saved the change.
            return _unknown_result(exc)
        return DescriptionEditResult(status="failed", warning=str(exc))
    except GitLabGraphQLError as exc:
        return DescriptionEditResult(status="failed", warning=str(exc))
    changed, truncated = _changed_lines(before, after)
    return DescriptionEditResult(
        status="applied",
        changed_lines=changed,
        truncated=truncated,
        warning=CONCURRENCY_WARNING,
    )


async def edit_merge_request_description(
    client: GitLabClient,
    project: str,
    iid: int,
    old_text: str,
    new_text: str,
) -> DescriptionEditResult:
    """Replace one exact match of `old_text` in a merge request's description.

    The full description is read first, so nothing is cut before the write. A null
    description counts as empty.

    Raises:
        ToolError: Before any write, when `old_text` is empty, equals `new_text`,
            or does not match exactly once. Also when the read fails.
    """
    _validate_edit(old_text, new_text)
    path = f"/projects/{quote(project, safe='')}/merge_requests/{iid}"
    merge_request = await client.get(path)
    before = merge_request.get("description") or ""
    after = _replace_exactly_once(before, old_text, new_text)
    return await _write_edit(client.request("PUT", path, json={"description": after}), before, after)


async def edit_work_item_description(  # noqa: PLR0913, PLR0917 - each argument is part of the tool API.
    client: GitLabClient,
    project: str,
    iid: int,
    old_text: str,
    new_text: str,
    scope: WorkItemScope = "project",
) -> DescriptionEditResult:
    """Replace one exact match of `old_text` in a work item's description.

    The work-item global ID and the full description come from
    `read_work_item_for_edit`, so nothing is cut before the write.

    Raises:
        ToolError: Before any write, when `old_text` is empty, equals `new_text`,
            or does not match exactly once. Also when the read fails.
    """
    _validate_edit(old_text, new_text)
    source = await read_work_item_for_edit(client, project, iid, scope)
    after = _replace_exactly_once(source.description, old_text, new_text)
    variables = {"input": {"id": source.id, "descriptionWidget": {"description": after}}}
    write = client.mutate(WORK_ITEM_UPDATE_MUTATION, variables, "workItemUpdate")
    return await _write_edit(write, source.description, after)
