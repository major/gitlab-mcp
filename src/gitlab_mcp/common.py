"""Generic constants, argument types, and helpers shared by every tool area 🧰."""

import base64
import json
import os
import re
import stat
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
lazy from collections.abc import Sequence

from fastmcp.exceptions import ToolError
from pydantic import Field

from gitlab_mcp.errors import InvalidCursorError, raise_review_preflight_error
lazy from gitlab_mcp.client import GitLabClient

# Upper bound on GraphQL pages scanned when approved, has_open_threads, or
# thread filters run on our side. Keeps one tool call from walking a huge project.
MAX_PAGES = 5
MAX_BATCH_MERGE_REQUESTS = 20
# GitLab rejects a GraphQL query above complexity 250. Each aliased merge request costs about 23 points,
# so 8 per query (about 184) leaves headroom. Larger batches run as several sequential queries.
GRAPHQL_MR_CHUNK = 8
HTTP_REQUEST_TIMEOUT = 408
HTTP_SERVER_ERROR = 500
# Discussion GIDs start with this. Output IDs are bare SHAs, so the prefix is only stripped from input.
DISCUSSION_PREFIX = "gid://gitlab/Discussion/"
# Files per page of the REST diffs endpoint, the largest page GitLab serves.
DIFF_PAGE_SIZE = 100
# Labels per page on the project labels endpoint. The largest page GitLab serves.
PROJECT_LABELS_PAGE_SIZE = 100
# Bodies are small enough to bound memory and request size, including file-backed notes.
REVIEW_NOTE_BODY_CAP = 100000
# A line that starts with a slash and a letter may be a GitLab quick action.
_QUICK_ACTION = re.compile(r"/[A-Za-z]")
# Shared argument types. Every tool repeats these in the catalog, so the descriptions stay short. ✂️
Project = Annotated[str, Field(description="Project path, like group/project.")]
Iid = Annotated[int, Field(description="MR number.")]
PositiveIid = Annotated[int, Field(strict=True, ge=1)]


def lower_or_empty(value: str | None) -> str:
    """Lowercase a GitLab enum value. Null means unknown, so it becomes empty and is left out."""
    return (value or "").lower()


def bare_id(thread_id: str) -> str:
    """Strip the discussion GID prefix. Bare SHAs pass through unchanged."""
    return thread_id.removeprefix(DISCUSSION_PREFIX)


def encode_cursor(data: dict[str, Any]) -> str:
    """Pack a tool's resume position into one opaque, URL-safe string."""
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def decode_cursor(cursor: str, tool: str) -> dict[str, Any]:
    """Unpack a cursor made by `encode_cursor`. Each tool checks the keys it needs.

    Anything that is not base64 JSON holding an object raises InvalidCursorError.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor))
    except ValueError as exc:  # Bad base64 and bad JSON both raise ValueError subclasses.
        raise InvalidCursorError(tool) from exc
    if not isinstance(payload, dict):
        raise InvalidCursorError(tool)
    return payload


def is_int_at_least(value: Any, minimum: int) -> bool:
    """Return True for an int of at least `minimum`. Booleans are ints in Python, so they are rejected too."""
    return type(value) is int and value >= minimum


def has_quick_action(body: str) -> bool:
    """Return True when any line starts like a GitLab quick action, such as `/close` or `/merge`.

    GitLab runs quick actions in note bodies, so a stray line could change the MR. This also matches
    lines inside code fences, so a harmless example can be rejected too.
    """
    return any(_QUICK_ACTION.match(line.lstrip()) for line in body.replace("\r", "").splitlines())


def page_text(text: str, limit: int, offset: int) -> tuple[str, bool, int | None]:
    """Return one page of `text` from `offset`, whether more text follows, and the offset that continues it.

    Offsets count characters. A page holds up to `limit` characters, plus a final tail of up to a quarter
    of `limit`, so a short tail costs no extra call. Past-the-end offsets return an empty page.
    """
    if offset >= len(text):
        return "", False, None
    end = offset + limit
    # 25% slack, rounded down: limits 1 to 3 add no characters, and limits 4 to 7 add one.
    if len(text) - end <= limit // 4:
        return text[offset:], False, None
    return text[offset:end], True, end


def read_server_local_utf8_file(
    path_value: str,
    *,
    path_field: str,
    size_field: str,
    byte_limit: int,
) -> str:
    """Read an absolute server-local regular UTF-8 file with a strict byte limit."""
    path = Path(path_value)
    if not path.is_absolute():
        raise_review_preflight_error(f"{path_field} must be an absolute server-local path")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise_review_preflight_error(f"{path_field} must point to a regular file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            encoded = source.read(byte_limit + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(encoded) > byte_limit:
        raise_review_preflight_error(f"{size_field} exceeds {byte_limit} UTF-8 bytes")
    try:
        return encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise_review_preflight_error(f"{path_field} is not valid UTF-8", cause=exc)


async def validate_project_labels(
    client: GitLabClient,
    project: str,
    labels: Sequence[str],
) -> tuple[list[str], dict[str, str]]:
    """Return requested labels that exist and failures for labels that cannot be checked.

    Names are matched exactly, including scoped and ancestor-group labels. The
    scan stops when every requested name is found or after ``MAX_PAGES``.
    Failure keys are requested label names; callers can add tool-specific context.
    """
    requested = list(dict.fromkeys(labels))
    if not requested:
        return [], {}

    wanted = set(requested)
    found: set[str] = set()
    path = f"/projects/{quote(project, safe='')}/labels"
    page = 1
    try:
        for _ in range(MAX_PAGES):
            data, next_page = await client.get_page(
                path,
                include_ancestor_groups=True,
                per_page=PROJECT_LABELS_PAGE_SIZE,
                page=page,
            )
            if not isinstance(data, list) or any(
                not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in data
            ):
                return [name for name in labels if name in found], {
                    name: "could not verify label: invalid response" for name in requested if name not in found
                }
            found.update(label["name"] for label in data if label["name"] in wanted)
            if found == wanted:
                return [name for name in labels if name in found], {}
            if next_page is None:
                return [name for name in labels if name in found], {
                    name: "unknown label" for name in requested if name not in found
                }
            page = next_page
    except ToolError as exc:
        return [name for name in labels if name in found], {name: str(exc) for name in requested if name not in found}

    return [name for name in labels if name in found], {
        name: f"could not verify label after {MAX_PAGES} pages" for name in requested if name not in found
    }
