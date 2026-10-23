"""Exact-text edits for merge request and work-item descriptions ✏️.

Each helper reads the full description, applies exact replacements, and writes the
whole description back once. Nothing is written unless every match is unique.
GitLab has no compare-and-swap for descriptions, so a concurrent edit can still
be overwritten. Writes are never retried.
"""

import difflib
import hashlib
import re
from itertools import islice
from typing import Literal, NoReturn
from urllib.parse import quote
lazy from collections.abc import Awaitable

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

lazy from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError, GitLabGraphQLError
lazy from gitlab_mcp.description_tables import DescriptionTableCell, replace_table_cell
lazy from gitlab_mcp.work_items import WorkItemScope, read_work_item_for_edit

# The changed lines sent to the agent never exceed this many characters.
DIFF_CAP = 4000
MAX_BATCH_EDITS = 100
SERVER_ERROR_STATUS = 500
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


class DescriptionReplacement(BaseModel):
    """One exact-text replacement for a work-item description."""

    model_config = {"extra": "forbid"}

    old_text: str = Field(description="Text that must appear exactly once in the current description.")
    new_text: str = Field(description="Replacement text. An empty string deletes the matched text.")


class DescriptionEditResult(BaseModel):
    """The outcome of one exact-text edit, kept small for the agent.

    `changed_lines` is a zero-context unified diff of the whole description, so
    its `@@` headers carry real line numbers. It is only filled when the write was
    applied. `truncated` is true when the diff was cut to its requested character
    limit. `description_hash` is the SHA-256 hash of the submitted description.
    """

    status: Literal["applied", "failed", "unknown"]
    changed_lines: str = Field(default="", exclude_if=lambda value: not value)
    truncated: bool = False
    warning: str = Field(default="", exclude_if=lambda value: not value)
    description_hash: str | None = Field(default=None, exclude_if=lambda value: value is None)


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


def _validated_work_item_replacements(
    old_text: str | None,
    new_text: str | None,
    edits: list[DescriptionReplacement] | None,
    table_cell: DescriptionTableCell | None,
) -> list[DescriptionReplacement] | None:
    """Validate work-item edit inputs before fetching any data from GitLab."""
    if table_cell is not None:
        if old_text is not None or new_text is not None or edits is not None:
            _raise_edit_error("Provide only one edit mode: old_text/new_text, edits, or table_cell.")
        return None

    if edits is None:
        if old_text is None or new_text is None:
            _raise_edit_error("Provide both old_text and new_text, edits, or table_cell.")
        replacements = [DescriptionReplacement(old_text=old_text, new_text=new_text)]
    else:
        if old_text is not None or new_text is not None:
            _raise_edit_error("Provide either old_text/new_text or edits, not both.")
        if not edits:
            _raise_edit_error("edits must contain at least one replacement.")
        if len(edits) > MAX_BATCH_EDITS:
            _raise_edit_error(f"edits must contain no more than {MAX_BATCH_EDITS} replacements.")
        replacements = edits

    for replacement in replacements:
        _validate_edit(replacement.old_text, replacement.new_text)
    return replacements


def _validate_work_item_options(expected_hash: str | None, diff_limit: int) -> None:
    """Validate the optional hash guard and diff bounds before fetching the description."""
    if expected_hash is not None and re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
        _raise_edit_error("expected_hash must be a lowercase 64-character SHA-256 hash.")
    if not 0 <= diff_limit <= DIFF_CAP:
        _raise_edit_error(f"diff_limit must be between 0 and {DIFF_CAP}.")


def _lines(text: str) -> list[str]:
    """Split text into lines. Empty text has no lines, so a deletion shows no blank line."""
    return text.split("\n") if text else []


def _changed_lines(before: str, after: str, max_chars: int = DIFF_CAP) -> tuple[str, bool]:
    """Return the zero-context diff of two full descriptions, capped at `max_chars`.

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
        if len(candidate) > max_chars:
            return candidate[:max_chars], True
        text = candidate
    return text, False


def _unknown_result(exc: ToolError) -> DescriptionEditResult:
    """Build the result for a write whose outcome GitLab never confirmed.

    The error text may end with a period already, so one is added only when it is missing.
    """
    return DescriptionEditResult(status="unknown", warning=f"{str(exc).rstrip('.')}. {UNKNOWN_WARNING}")


async def _write_edit(
    write: Awaitable[object], before: str, after: str, return_diff: bool, diff_limit: int = DIFF_CAP
) -> DescriptionEditResult:
    """Send one write once and turn its outcome into a result. Writes are never retried.

    A network error or a 5xx can happen after GitLab saved the change, so those
    are `unknown`. A 4xx or a GraphQL payload error means GitLab refused the
    change, so it is `failed`. When `return_diff` is false or `diff_limit` is
    zero, changed lines are not computed.
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
    changed, truncated = _changed_lines(before, after, diff_limit) if return_diff and diff_limit else ("", False)
    return DescriptionEditResult(
        status="applied",
        changed_lines=changed,
        truncated=truncated,
        description_hash=hashlib.sha256(after.encode("utf-8")).hexdigest(),
    )


async def edit_merge_request_description(  # noqa: PLR0913, PLR0917 - each argument is part of the helper API.
    client: GitLabClient,
    project: str,
    iid: int,
    old_text: str,
    new_text: str,
    return_diff: bool = True,
) -> DescriptionEditResult:
    """Replace one exact match of `old_text` in a merge request's description.

    The full description is read first, so nothing is cut before the write. A null
    description counts as empty. Set `return_diff` to false to omit changed lines.

    Raises:
        ToolError: Before any write, when `old_text` is empty, equals `new_text`,
            or does not match exactly once. Also when the read fails.
    """
    _validate_edit(old_text, new_text)
    path = f"/projects/{quote(project, safe='')}/merge_requests/{iid}"
    merge_request = await client.get(path)
    before = merge_request.get("description") or ""
    after = _replace_exactly_once(before, old_text, new_text)
    return await _write_edit(client.request("PUT", path, json={"description": after}), before, after, return_diff)


async def edit_work_item_description(  # noqa: PLR0913, PLR0917 - each argument is part of the tool API.
    client: GitLabClient,
    project: str,
    iid: int,
    old_text: str | None = None,
    new_text: str | None = None,
    scope: WorkItemScope = "project",
    return_diff: bool = True,
    edits: list[DescriptionReplacement] | None = None,
    expected_hash: str | None = None,
    diff_limit: int = 1000,
    table_cell: DescriptionTableCell | None = None,
) -> DescriptionEditResult:
    """Apply exact-text replacements or edit one keyed table cell in a work-item description.

    The work-item global ID and the full description come from
    `read_work_item_for_edit`, so nothing is cut before the write. Choose either
    the legacy `old_text` and `new_text` pair, a bounded `edits` list, or one
    `table_cell` selector. Set `expected_hash` to guard against editing a
    description that has changed since it was read. Set `return_diff` to false or
    `diff_limit` to zero to omit changed lines.

    Raises:
        ToolError: Before any write, when the input mode is invalid, a replacement
            is empty, a replacement matches other than exactly once, the expected
            hash does not match, or the final description is unchanged. Also when
            the read fails.
    """
    replacements = _validated_work_item_replacements(old_text, new_text, edits, table_cell)
    _validate_work_item_options(expected_hash, diff_limit)

    source = await read_work_item_for_edit(client, project, iid, scope)
    before = source.description
    if expected_hash is not None and hashlib.sha256(before.encode("utf-8")).hexdigest() != expected_hash:
        _raise_edit_error("expected_hash does not match the current description. No write was made.")

    after = replace_table_cell(before, table_cell) if table_cell is not None else before
    if replacements is not None:
        for index, replacement in enumerate(replacements, start=1):
            try:
                after = _replace_exactly_once(after, replacement.old_text, replacement.new_text)
            except ToolError as exc:
                _raise_edit_error(f"Edit {index} failed: {exc}")
    if after == before:
        _raise_edit_error("The replacements do not change the description. No write was made.")

    variables = {"input": {"id": source.id, "descriptionWidget": {"description": after}}}
    write = client.mutate(WORK_ITEM_UPDATE_MUTATION, variables, "workItemUpdate")
    return await _write_edit(write, before, after, return_diff, diff_limit)
