"""Create ordinary GitLab project issues through the REST API 🎫."""

import unicodedata
from typing import Annotated, Literal
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError
from gitlab_mcp.common import HTTP_REQUEST_TIMEOUT, HTTP_SERVER_ERROR

MAX_PROJECT_PATH_LENGTH = 1024
MAX_ISSUE_TITLE_LENGTH = 255
MAX_ISSUE_DESCRIPTION_LENGTH = 100000
MAX_ISSUE_DESCRIPTION_BYTES = 100000

ProjectPath = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=MAX_PROJECT_PATH_LENGTH,
        description="Project path, like group/project, up to 1024 characters.",
    ),
]
IssueTitle = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=MAX_ISSUE_TITLE_LENGTH, description="Nonblank issue title."),
]
IssueDescription = Annotated[
    str | None,
    Field(
        strict=True,
        max_length=MAX_ISSUE_DESCRIPTION_LENGTH,
        description="Optional issue description, up to 100000 characters and 100000 UTF-8 bytes.",
    ),
]


class IssueCreateResult(BaseModel):
    """Compact result for a validated, created, or uncertain issue request."""

    status: Literal["validated", "created", "unknown"]
    title: str | None = Field(default=None, exclude_if=lambda value: value is None)
    id: int | None = Field(default=None, exclude_if=lambda value: value is None)
    iid: int | None = Field(default=None, exclude_if=lambda value: value is None)
    web_url: str | None = Field(default=None, exclude_if=lambda value: value is None)
    message: str | None = Field(default=None, exclude_if=lambda value: value is None)


def _validate_project_path(project: str) -> str:
    """Validate a project path and return its fully encoded REST path segment."""
    if not project.strip():
        raise ToolError("project must not be empty.")  # noqa: TRY003
    if any(unicodedata.category(char) == "Cc" for char in project):
        raise ToolError("project must not contain control characters.")  # noqa: TRY003
    if any(char in project for char in "\\%?#"):
        raise ToolError("project must not contain backslashes, percent signs, question marks, or fragments.")  # noqa: TRY003
    if any(segment in {"", ".", ".."} for segment in project.split("/")):
        raise ToolError("project must be a path with no empty or dot segments.")  # noqa: TRY003
    try:
        project.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolError("project must contain valid Unicode text.") from exc  # noqa: TRY003
    return f"/projects/{quote(project, safe='')}"


def _validate_issue_text(title: str, description: str | None) -> None:
    """Validate title and optional description without returning or logging their contents."""
    if not title.strip():
        raise ToolError("title must not be blank.")  # noqa: TRY003
    if any(unicodedata.category(char) == "Cc" or char in "\u2028\u2029" for char in title):
        raise ToolError("title must not contain control characters or newlines.")  # noqa: TRY003
    try:
        title.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolError("title must contain valid Unicode text.") from exc  # noqa: TRY003

    if description is None:
        return
    if len(description) > MAX_ISSUE_DESCRIPTION_LENGTH:
        raise ToolError(f"description exceeds {MAX_ISSUE_DESCRIPTION_LENGTH} characters.")  # noqa: TRY003
    try:
        description_bytes = description.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolError("description must contain valid UTF-8 text.") from exc  # noqa: TRY003
    if len(description_bytes) > MAX_ISSUE_DESCRIPTION_BYTES:
        raise ToolError(f"description exceeds {MAX_ISSUE_DESCRIPTION_BYTES} UTF-8 bytes.")  # noqa: TRY003


async def _check_issue_settings(client: GitLabClient, project_path: str) -> None:
    """Confirm the project response does not explicitly disable issues."""
    try:
        project = await client.get(project_path)
    except ValueError as exc:
        raise ToolError("GitLab returned invalid project settings; issue creation was not attempted.") from exc  # noqa: TRY003
    if not isinstance(project, dict):
        raise ToolError("GitLab returned invalid project settings; issue creation was not attempted.")  # noqa: TRY003
    if project.get("issues_enabled") is False or project.get("issues_access_level") == "disabled":
        raise ToolError("Issues are disabled for this project; issue creation was not attempted.")  # noqa: TRY003


def _unknown_create_result() -> IssueCreateResult:
    """Return a non-sensitive result for a write that may have reached GitLab."""
    return IssueCreateResult(
        status="unknown",
        message="The issue creation outcome is unknown. Check GitLab before retrying.",
    )


def _created_issue_result(title: str, response: object) -> IssueCreateResult:
    """Return verified identifiers from a create response, or mark it unknown."""
    if not isinstance(response, dict):
        return _unknown_create_result()
    issue_id = response.get("id")
    issue_iid = response.get("iid")
    web_url = response.get("web_url")
    if (
        type(issue_id) is not int
        or issue_id < 1
        or type(issue_iid) is not int
        or issue_iid < 1
        or not isinstance(web_url, str)
        or not web_url.strip()
    ):
        return _unknown_create_result()
    return IssueCreateResult(status="created", title=title, id=issue_id, iid=issue_iid, web_url=web_url)


async def _post_issue(
    client: GitLabClient,
    project_path: str,
    title: str,
    description: str | None,
) -> IssueCreateResult:
    """Post one issue request and report definite or uncertain outcomes without retrying."""
    body: dict[str, str] = {"title": title, "issue_type": "issue"}
    if description is not None:
        body["description"] = description
    try:
        response = await client.request("POST", f"{project_path}/issues", json=body)
    except GitLabConnectionError:
        return _unknown_create_result()
    except GitLabAPIError as exc:
        if exc.status == HTTP_REQUEST_TIMEOUT or exc.status >= HTTP_SERVER_ERROR:
            return _unknown_create_result()
        raise ToolError(  # noqa: TRY003
            f"GitLab rejected issue creation with HTTP {exc.status}. Check the project and issue fields."
        ) from exc
    except ValueError:
        # The POST may have succeeded even if its response body was not valid JSON.
        return _unknown_create_result()
    return _created_issue_result(title, response)


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def create_issue(
    ctx: Context,
    project: ProjectPath,
    title: IssueTitle,
    description: IssueDescription = None,
    validate_only: Annotated[
        bool, Field(description="Check local input and project issue settings without creating.")
    ] = False,
) -> IssueCreateResult:
    """Create an ordinary project issue, like `glab issue create`, with a project-settings preflight.

    `validate_only=True` checks local inputs and reads the project settings, but does not ask GitLab to validate the
    issue or guarantee that the token can create one. It does not check for duplicates or provide idempotency. A
    connection error, HTTP 408 or 5xx response, or malformed success response can leave the write outcome unknown.
    Check GitLab before retrying. This tool does not add labels or assignees.
    """
    project_path = _validate_project_path(project)
    _validate_issue_text(title, description)
    client = get_client(ctx)
    await _check_issue_settings(client, project_path)
    if validate_only:
        return IssueCreateResult(status="validated", title=title)
    return await _post_issue(client, project_path, title, description)
