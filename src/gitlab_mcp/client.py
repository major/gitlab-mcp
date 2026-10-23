"""Thin async client for the GitLab REST API 🦊."""

from typing import Any

import httpx
from fastmcp.exceptions import ToolError

lazy from gitlab_mcp.config import Settings

# Friendly hints for the status codes developers hit most often.
_STATUS_HINTS = {
    401: "The token is invalid, expired, or revoked.",
    403: "The token does not have the scope or permission for this request.",
    404: "Not found, or the token cannot see it.",
    429: "Rate limited by GitLab. Try again shortly.",
}


class GitLabConnectionError(ToolError):
    """The GitLab instance could not be reached."""

    def __init__(self, url: str, cause: Exception) -> None:
        """Include the instance URL and the network error."""
        super().__init__(f"Could not reach {url}: {cause}")


class GitLabAPIError(ToolError):
    """GitLab answered with an HTTP error status."""

    def __init__(self, status: int, path: str) -> None:
        """Include the status, the path, and a hint when we have one."""
        self.status = status
        super().__init__(f"GitLab returned HTTP {status} for {path}. {_STATUS_HINTS.get(status, '')}".strip())


class GitLabClient:
    """Small wrapper around httpx that knows how to talk to GitLab."""

    def __init__(self, settings: Settings) -> None:
        """Create a client for the instance described by `settings`."""
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.api_url,
            headers={"PRIVATE-TOKEN": settings.token, "Accept": "application/json"},
            timeout=settings.timeout,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()

    async def get(self, path: str, **params: Any) -> Any:
        """GET `path` and return the parsed JSON body.

        Raises:
            GitLabConnectionError: When the instance cannot be reached.
            GitLabAPIError: When GitLab returns an error status.
        """
        try:
            response = await self._http.get(path, params=params or None)
        except httpx.HTTPError as exc:
            raise GitLabConnectionError(self.settings.url, exc) from exc

        if response.is_error:
            raise GitLabAPIError(response.status_code, path)
        return response.json()
