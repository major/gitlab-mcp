"""Bounded personal and project snippet operations 📝."""

import unicodedata
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.client import GitLabAPIError, GitLabConnectionError
from gitlab_mcp.common import read_server_local_utf8_file
lazy from gitlab_mcp.client import GitLabClient, WriteMethod

BYTE_LIMIT = 100000
JSON_LIMIT = 1000000
TITLE_LIMIT = 255
DESCRIPTION_LIMIT = 10000
Project = Annotated[
    str | None, Field(strict=True, description="Project path or numeric ID; omit for personal snippets.")
]
SnippetId = Annotated[int, Field(strict=True, ge=1, description="Positive snippet ID.")]
Visibility = Literal["private", "internal", "public"]


class CreateFile(BaseModel):
    """One new snippet file with inline or server-local content."""

    model_config = ConfigDict(extra="forbid")
    file_path: str = Field(description="Relative snippet file path, including any directories.")
    content: str | None = Field(
        default=None, description="Nonblank UTF-8 text without NUL; exclusive with content_file."
    )
    content_file: str | None = Field(
        default=None,
        description="Absolute server-local regular UTF-8 file, at most 100000 bytes; exclusive with content.",
    )


class UpdateFile(CreateFile):
    """One create, update, delete, or move action for a snippet file."""

    action: Literal["create", "update", "delete", "move"]
    previous_path: str | None = Field(default=None, description="Required for move only: the existing file path.")


class SnippetList(BaseModel):
    """A compact TSV page and optional continuation page."""

    snippets: str
    next_page: int | None = Field(default=None, exclude_if=lambda value: value is None)


class SnippetFile(BaseModel):
    """A snippet file path and optional GitLab raw URL."""

    path: str
    raw_url: str | None = Field(default=None, exclude_if=lambda value: value is None)


class SnippetDetail(BaseModel):
    """Compact snippet metadata without file contents."""

    id: int
    title: str
    description: str
    visibility: str
    project_id: int | None = Field(default=None, exclude_if=lambda value: value is None)
    author: str
    updated_at: str
    web_url: str
    files: list[SnippetFile]
    description_truncated: bool = Field(default=False, exclude_if=lambda value: not value)


class WriteResult(BaseModel):
    """A verified write result or an uncertain outcome."""

    status: Literal["created", "updated", "deleted", "unknown"]
    id: int | None = Field(default=None, exclude_if=lambda value: value is None)
    web_url: str | None = Field(default=None, exclude_if=lambda value: value is None)
    message: str | None = Field(default=None, exclude_if=lambda value: value is None)


def _unicode(value: str, field: str) -> bytes:
    """Encode valid Unicode without revealing the input on error."""
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolError(f"{field} must contain valid Unicode.") from exc  # noqa: TRY003


def _path(value: str, field: str) -> str:
    """Validate and fully encode a project or file path."""
    _unicode(value, field)
    if (
        not value.strip()
        or "\\" in value
        or any(unicodedata.category(c) == "Cc" for c in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ToolError(f"{field} must be a valid nonblank path without control characters or dot segments.")  # noqa: TRY003
    return quote(value, safe="")


def _base(project: str | None) -> str:
    """Build the personal or project snippet endpoint."""
    return "/snippets" if project is None else f"/projects/{_path(project, 'project')}/snippets"


def _text(value: Any) -> str:
    """Return a string representation for a compact response field."""
    return "" if value is None else str(value)


def _author(item: dict[str, Any]) -> str:
    """Extract the author username from GitLab metadata."""
    author = item.get("author")
    return _text(author.get("username")) if isinstance(author, dict) else ""


def _tsv(value: Any) -> str:
    """Escape delimiters and backslashes so each item stays in one TSV cell."""
    return _text(value).replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def _validate_title(title: str) -> None:
    """Reject invalid titles before sending a write."""
    _unicode(title, "title")
    if (
        not title.strip()
        or len(title) > TITLE_LIMIT
        or any(unicodedata.category(c) == "Cc" or c in "\u2028\u2029" for c in title)
    ):
        raise ToolError("title must be nonblank, at most 255 characters, and contain no controls.")  # noqa: TRY003


def _validate_description(description: str) -> None:
    """Check the description length and Unicode."""
    _unicode(description, "description")
    if len(description) > DESCRIPTION_LIMIT:
        raise ToolError("description exceeds 10000 characters.")  # noqa: TRY003


def _files(files: list[CreateFile] | list[UpdateFile], *, update: bool) -> list[dict[str, str]]:  # noqa: C901
    """Validate file actions and resolve content, enforcing the aggregate byte limit."""
    result: list[dict[str, str]] = []
    targets: set[str] = set()
    total = 0
    for file in files:
        _path(file.file_path, "file_path")
        if file.file_path in targets:
            raise ToolError("Duplicate file_path targets are not allowed.")  # noqa: TRY003
        targets.add(file.file_path)
        action = file.action if isinstance(file, UpdateFile) else "create"
        previous = file.previous_path if isinstance(file, UpdateFile) else None
        if previous is not None:
            _path(previous, "previous_path")
        if (action == "move") != (previous is not None):
            raise ToolError("previous_path is required only for move actions.")  # noqa: TRY003
        sources = int(file.content is not None) + int(file.content_file is not None)
        if sources > 1 or (action in {"create", "update"} and sources != 1) or (action == "delete" and sources):
            raise ToolError("Provide the required content or content_file for the file action, but not both.")  # noqa: TRY003
        content = file.content
        if file.content_file is not None:
            try:
                content = read_server_local_utf8_file(
                    file.content_file, path_field="content_file", size_field="content_file", byte_limit=BYTE_LIMIT
                )
            except (OSError, ValueError) as exc:
                raise ToolError("Could not read content_file.") from exc  # noqa: TRY003
        entry = {"file_path": file.file_path}
        if update:
            entry["action"] = action
        if previous is not None:
            entry["previous_path"] = previous
        if content is not None:
            total += len(_unicode(content, "content"))
            if not content.strip() or "\x00" in content:
                raise ToolError("content must not be blank or contain NUL bytes.")  # noqa: TRY003
            entry["content"] = content
        result.append(entry)
    if total > BYTE_LIMIT:
        raise ToolError("File content exceeds 100000 UTF-8 bytes per write.")  # noqa: TRY003
    return result


async def _write(  # noqa: PLR0913
    client: GitLabClient,
    method: WriteMethod,
    path: str,
    body: dict[str, Any] | None,
    *,
    operation: str,
    snippet_id: int | None = None,
) -> WriteResult:
    """Send exactly one write and return only verified identifiers or an unknown outcome."""
    unknown = WriteResult(
        status="unknown", message=f"The snippet {operation} outcome is unknown. Check GitLab before retrying."
    )
    try:
        response = await client.request(
            method, path, json=body, expected_status={"POST": 201, "PUT": 200, "DELETE": 204}[method]
        )
    except GitLabConnectionError, ValueError:
        return unknown
    except GitLabAPIError as exc:
        if exc.status == 408 or not 400 <= exc.status < 500:  # noqa: PLR2004
            return unknown
        raise ToolError(f"GitLab rejected snippet {operation} with HTTP {exc.status}.") from exc  # noqa: TRY003
    if method == "DELETE":
        return WriteResult(status="deleted", id=snippet_id)
    if not isinstance(response, dict):
        return unknown
    result_id, url = response.get("id"), response.get("web_url")
    if (
        type(result_id) is not int
        or result_id < 1
        or (snippet_id is not None and result_id != snippet_id)
        or not isinstance(url, str)
        or not url.strip()
    ):
        return unknown
    return WriteResult(status="created" if method == "POST" else "updated", id=result_id, web_url=url)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_snippets(
    ctx: Context,
    project: Project = None,
    scope: Literal["mine", "all", "public"] = "mine",
    page: Annotated[int, Field(strict=True, ge=1)] = 1,
    per_page: Annotated[int, Field(strict=True, ge=1, le=100)] = 20,
) -> SnippetList:
    """List one snippet page, like `glab api snippets`.

    Without project, mine lists the current user's snippets, all lists accessible snippets, and public lists public
    snippets. With project, leave scope=mine to list all accessible snippets in that project, not just your own.
    Pass next_page as page to continue. TSV cells escape backslashes, tabs, and line breaks.
    """
    path = _base(project)
    if project is not None and scope != "mine":
        raise ToolError("scope must be mine when project is set.")  # noqa: TRY003
    if project is None and scope != "mine":
        path += f"/{scope}"
    data, next_page = await get_client(ctx).get_bounded_json(path, max_bytes=JSON_LIMIT, page=page, per_page=per_page)
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ToolError("GitLab returned invalid snippet list metadata.")  # noqa: TRY003
    rows = ["id\ttitle\tvisibility\tproject_id\tauthor\tupdated\tweb_url"]
    for item in data:
        rows.append(  # noqa: PERF401
            "\t".join(
                _tsv(value)
                for value in (
                    item.get("id"),
                    item.get("title"),
                    item.get("visibility"),
                    item.get("project_id"),
                    _author(item),
                    _text(item.get("updated_at"))[:10],
                    item.get("web_url"),
                )
            )
        )
    return SnippetList(snippets="\n".join(rows), next_page=next_page)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_snippet(ctx: Context, snippet_id: SnippetId, project: Project = None) -> SnippetDetail:
    """Read compact snippet metadata, like `glab api snippets/:id`. File content is not included.

    Descriptions over 10000 characters are clipped and marked description_truncated. Use get_snippet_file for content.
    """
    data, _ = await get_client(ctx).get_bounded_json(f"{_base(project)}/{snippet_id}", max_bytes=JSON_LIMIT)
    if not isinstance(data, dict) or type(data.get("id")) is not int:
        raise ToolError("GitLab returned invalid snippet metadata.")  # noqa: TRY003
    files = []
    for item in data.get("files") or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            files.append(SnippetFile(path=item["path"], raw_url=item.get("raw_url")))  # noqa: PERF401
    if not files and isinstance(data.get("file_name"), str):
        files.append(SnippetFile(path=data["file_name"]))
    description = _text(data.get("description"))
    return SnippetDetail(
        id=data["id"],
        title=_text(data.get("title")),
        description=description[:DESCRIPTION_LIMIT],
        description_truncated=len(description) > DESCRIPTION_LIMIT,
        visibility=_text(data.get("visibility")),
        project_id=data.get("project_id"),
        author=_author(data),
        updated_at=_text(data.get("updated_at"))[:10],
        web_url=_text(data.get("web_url")),
        files=files,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_snippet_file(
    ctx: Context, snippet_id: SnippetId, project: Project = None, file_path: str | None = None, ref: str | None = None
) -> dict[str, str]:
    """Read a snippet file, like `glab api snippets/:id/raw`, with a 100000-byte cap.

    Omit file_path and ref for the first file, or supply both. Obtain ref from get_snippet.files[].raw_url;
    this tool constructs its own API path and never follows returned URLs. Content must be UTF-8 without NUL bytes.
    """
    path = f"{_base(project)}/{snippet_id}"
    if (file_path is None) != (ref is None):
        raise ToolError("file_path and ref must be provided together.")  # noqa: TRY003
    if file_path is not None and ref is not None:
        _unicode(ref, "ref")
        if not ref.strip() or ref in {".", ".."} or any(unicodedata.category(c) == "Cc" for c in ref):
            raise ToolError("ref must be nonblank, not a dot segment, and contain no control characters.")  # noqa: TRY003
        path += f"/files/{quote(ref, safe='')}/{_path(file_path, 'file_path')}"
    raw, truncated = await get_client(ctx).get_trace_prefix(f"{path}/raw", max_bytes=BYTE_LIMIT)
    if truncated:
        raise ToolError("Snippet file exceeds the 100000-byte read limit.")  # noqa: TRY003
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("Snippet file is not valid UTF-8.") from exc  # noqa: TRY003
    if "\x00" in content:
        raise ToolError("Snippet file contains a NUL byte.")  # noqa: TRY003
    return {"content": content}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
async def create_snippet(  # noqa: PLR0913, PLR0917
    ctx: Context,
    title: str,
    files: Annotated[list[CreateFile], Field(min_length=1, max_length=10)],
    project: Project = None,
    description: str | None = None,
    visibility: Visibility = "private",
) -> WriteResult:
    """Create a snippet, like `glab snippet create`. Internal visibility is unavailable on GitLab.com.

    Defaults to private. Supply 1 to 10 files with nonblank content or content_file, not both, with at most 100000
    aggregate UTF-8 bytes. content_file paths refer to this server, not the client. Titles are at most 255 characters
    and descriptions at most 10000 characters. Writes never auto-retry; check GitLab before retrying an unknown outcome.
    """
    path = _base(project)
    _validate_title(title)
    body: dict[str, Any] = {"title": title, "files": _files(files, update=False), "visibility": visibility}
    if description is not None:
        _validate_description(description)
        body["description"] = description
    return await _write(get_client(ctx), "POST", path, body, operation="creation")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
async def update_snippet(  # noqa: PLR0913, PLR0917
    ctx: Context,
    snippet_id: SnippetId,
    project: Project = None,
    title: str | None = None,
    description: str | None = None,
    visibility: Visibility | None = None,
    files: Annotated[list[UpdateFile] | None, Field(min_length=1, max_length=10)] = None,
) -> WriteResult:
    """Update a snippet, like `glab api --method PUT snippets/:id`; file actions can delete files.

    Read before updating: writes are last-write-wins with no atomic guard. Up to 10 actions and 100000 aggregate
    UTF-8 content bytes are allowed per write; GitLab enforces the resulting 10-file limit.
    File create/update require nonblank content or content_file. Move requires previous_path and may include content.
    Delete accepts no content. Omitted metadata is unchanged; an empty description clears it. Titles are at most 255
    characters and descriptions at most 10000 characters. content_file is server-local. Internal visibility is
    unavailable on GitLab.com. Writes never auto-retry; check GitLab before retrying an unknown outcome.
    """
    path = f"{_base(project)}/{snippet_id}"
    body: dict[str, Any] = {}
    if title is not None:
        _validate_title(title)
        body["title"] = title
    if description is not None:
        _validate_description(description)
        body["description"] = description
    if visibility is not None:
        body["visibility"] = visibility
    if files is not None:
        body["files"] = _files(files, update=True)
    if not body:
        raise ToolError("At least one update field is required.")  # noqa: TRY003
    return await _write(get_client(ctx), "PUT", path, body, operation="update", snippet_id=snippet_id)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
async def delete_snippet(ctx: Context, snippet_id: SnippetId, project: Project = None) -> WriteResult:
    """Permanently delete a snippet and all its files, like `glab api --method DELETE snippets/:id`.

    Writes never auto-retry; check GitLab before retrying an unknown outcome.
    """
    return await _write(
        get_client(ctx), "DELETE", f"{_base(project)}/{snippet_id}", None, operation="deletion", snippet_id=snippet_id
    )
