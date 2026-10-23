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


class GitLabGraphQLError(ToolError):
    """GitLab answered the GraphQL request with one or more errors."""

    def __init__(self, messages: list[str]) -> None:
        """Join every error message so the agent sees them all."""
        super().__init__("GitLab GraphQL error: " + "; ".join(messages))


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

    async def _send_get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        """Send a GET and return the response. Error statuses raise, so callers only see good responses.

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
        return response

    async def get(self, path: str, **params: Any) -> Any:
        """GET `path` and return the parsed JSON body.

        Raises:
            GitLabConnectionError: When the instance cannot be reached.
            GitLabAPIError: When GitLab returns an error status.
        """
        return (await self._send_get(path, params)).json()

    async def get_page(self, path: str, **params: Any) -> tuple[Any, int | None]:
        """GET one page of a list and return its parsed JSON body and the next page number.

        The next page comes from the `X-Next-Page` header. GitLab sends it as an
        empty string on the last page, and some endpoints leave it out, so both
        mean None.

        Raises:
            GitLabConnectionError: When the instance cannot be reached.
            GitLabAPIError: When GitLab returns an error status.
        """
        response = await self._send_get(path, params)
        next_page = response.headers.get("X-Next-Page")  # Empty string on the last page.
        return response.json(), int(next_page) if next_page else None

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run one GraphQL `query` and return its `data` object.

        GraphQL reports most failures inside a 200 response, so the
        `errors` list is checked before the data is returned.

        Raises:
            GitLabConnectionError: When the instance cannot be reached.
            GitLabAPIError: When GitLab returns an error status.
            GitLabGraphQLError: When the response contains GraphQL errors.
        """
        url = f"{self.settings.url}/api/graphql"  # Absolute URL, because base_url is /api/v4.
        try:
            response = await self._http.post(url, json={"query": query, "variables": variables})
        except httpx.HTTPError as exc:
            raise GitLabConnectionError(self.settings.url, exc) from exc

        if response.is_error:
            raise GitLabAPIError(response.status_code, "/api/graphql")
        body = response.json()
        if errors := body.get("errors"):
            raise GitLabGraphQLError([error.get("message", str(error)) for error in errors])
        return body["data"]
