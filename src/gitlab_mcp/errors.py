"""Caller-facing errors shared by the GitLab tool modules 🚨."""

from typing import NoReturn

from fastmcp.exceptions import ToolError


class ProjectNotFoundError(ToolError):
    """The project does not exist, or the token cannot see it."""

    def __init__(self, project: str) -> None:
        """Name the project so the user can check the path."""
        super().__init__(f"Project {project!r} not found, or the token cannot see it.")


class MergeRequestNotFoundError(ToolError):
    """The merge request does not exist, or the token cannot see it."""

    def __init__(self, project: str, iid: int) -> None:
        """Name the IID and project so the user can check both."""
        super().__init__(f"Merge request !{iid} not found in {project!r}, or the token cannot see it.")


class InvalidCursorError(ToolError):
    """The cursor was not made by the tool it was passed to, or was changed."""

    def __init__(self, tool: str) -> None:
        """Tell the agent to pass next_cursor back unchanged to the same tool."""
        super().__init__(f"Invalid cursor. Pass next_cursor from an earlier {tool} call, unchanged.")


def raise_review_preflight_error(message: str, *, cause: BaseException | None = None) -> NoReturn:
    """Stop review-note processing with a caller-facing validation reason."""
    if cause is None:
        raise ValueError(message)
    raise ValueError(message) from cause
