"""Shared helpers for CI tools: TSV cells, text caps, GID checks, and opaque cursors 🧰."""

import base64
import json
import re
from typing import Any, NoReturn
lazy from collections.abc import Collection

from fastmcp.exceptions import ToolError

# A GitLab global ID is gid://gitlab/<Model>/<number>. Numbers start at 1, so "0" and leading zeros fail.
GID_PATTERN = re.compile(r"gid://gitlab/([A-Za-z:]+)/([1-9][0-9]*)")
# Runs of tabs and line breaks. Each run becomes one space so one value cannot split a TSV row.
CONTROL_RUN_PATTERN = re.compile(r"[\t\r\n]+")
CUT_MARKER = "..."


class InvalidCursorError(ToolError):
    """The cursor is malformed, or it was made for different arguments."""

    def __init__(self) -> None:
        """Tell the agent to pass next_cursor back unchanged."""
        super().__init__("Invalid cursor. Pass next_cursor from an earlier call, unchanged.")


def raise_tool_error(message: str) -> NoReturn:
    """Raise a ToolError with a message the agent can act on.

    The message is passed in as a variable, which keeps the long-message lint rule quiet.
    """
    raise ToolError(message)


def cell(value: object) -> str:
    """Render one value as a TSV cell.

    None becomes an empty cell, booleans become true/false, and lists become
    comma-separated. Tabs and line breaks become one space, so they cannot split a row.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return CONTROL_RUN_PATTERN.sub(" ", str(value))


def cap(value: str | None, limit: int) -> str:
    """Cut text to at most `limit` characters, counting the "..." that marks a cut.

    None becomes "". Text within the limit is returned unchanged. `limit` must be at least 3.
    """
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[: limit - len(CUT_MARKER)] + CUT_MARKER


def parse_gid(value: object) -> tuple[str, int]:
    """Split a GitLab global ID into its model name and numeric ID.

    Raises ToolError for anything else. The value is not echoed, because it came from GitLab.
    """
    match = GID_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise_tool_error("GitLab returned an ID in an unexpected format.")
    return match[1], int(match[2])


def numeric_gid(value: object, models: Collection[str]) -> int:
    """Return the numeric ID of a GID whose model is one of `models`.

    Raises ToolError when the GID is malformed or its model is not in `models`.
    """
    model, number = parse_gid(value)
    if model not in models:
        raise_tool_error(f"GitLab returned an ID of type {model}, expected {' or '.join(models)}.")
    return number


def encode_cursor(payload: dict[str, Any]) -> str:
    """Pack a resume position into one opaque, URL-safe string.

    Callers put the call's scope inside `payload`, so `decode_cursor` results can be checked against it.
    """
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def decode_cursor(cursor: str) -> dict[str, Any]:
    """Unpack a cursor made by `encode_cursor`.

    Raises InvalidCursorError when the text is not base64 JSON holding an object.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor))
    except ValueError:  # Bad base64, bad JSON, and non-ASCII text all raise ValueError subclasses.
        raise InvalidCursorError from None
    if not isinstance(payload, dict):
        raise InvalidCursorError
    return payload
