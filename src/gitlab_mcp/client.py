"""Thin async client for the GitLab REST API 🦊."""

import asyncio
import json
from typing import Any, Literal

import httpx
from fastmcp.exceptions import ToolError

lazy from gitlab_mcp.config import Settings

# Friendly hints for the status codes developers hit most often.
_STATUS_HINTS = {
    401: "The token is invalid, expired, or revoked.",
    403: "The token does not have the scope or permission for this request.",
    404: "Not found, or the token cannot see it.",
    405: "GitLab refused this action in the current MR state.",
    406: "The merge request cannot be merged right now.",
    409: "Conflict, for example the SHA does not match the MR head.",
    422: "GitLab rejected the request as invalid.",
    429: "Rate limited by GitLab. Try again shortly.",
}

# GitLab error messages can be long. Only this much goes to the agent.
_DETAIL_CAP = 300

# Error bodies are small JSON objects. Anything bigger is not read in full.
_ERROR_BODY_CAP = 1024

# Size of each piece read from a streamed body. Small enough to stop early on big traces.
_STREAM_CHUNK_SIZE = 4096

# Statuses that are redirects. The trace read never follows them.
_REDIRECT_STATUSES = range(300, 400)

# Methods that send a body or change data. GET goes through `get` and `get_page`.
WriteMethod = Literal["POST", "PUT", "DELETE"]


def _as_text(value: Any) -> str:
    """Flatten one JSON value from an error body into plain text.

    Lists join with "; ". Dicts become "key: value" pairs joined the same way,
    so `{"title": ["is too long"]}` reads as `title: is too long`.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(_as_text(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_as_text(item)}" for key, item in value.items())
    return str(value)


def _error_detail(response: httpx.Response) -> str:
    """Return GitLab's reason from an error body, or "" when there is none.

    GitLab puts the reason in a `message` or `error` key. A body that is not
    JSON, or has neither key, gives no detail. Trailing dots are removed so the
    error message does not end with two.
    """
    return _detail_from_bytes(response.content)


def _detail_from_bytes(raw: bytes) -> str:
    """Parse GitLab's reason from raw error body bytes, using the rules in `_error_detail`.

    Works on a body that was only partly read, because a cut-off body is not
    valid JSON and so gives no detail instead of a half message.
    """
    try:
        body = json.loads(raw)
    except ValueError:  # Not JSON, for example an HTML error page, or a body cut by the read cap.
        return ""
    if not isinstance(body, dict):
        return ""
    return _as_text(body.get("message") or body.get("error"))[:_DETAIL_CAP].strip().rstrip(". ")


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    """Read decoded body bytes until `limit + 1` are collected or the body ends.

    The extra byte lets the caller tell "exactly `limit`" from "more than `limit`".
    Chunks are cut to fit, so a huge chunk never pushes the result past `limit + 1`.
    Call it inside the stream context, because the body is only readable there.
    """
    collected = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=_STREAM_CHUNK_SIZE):
        collected += chunk[: limit + 1 - len(collected)]
        if len(collected) > limit:
            break  # Enough to prove truncation. The rest of the body is not read.
    return bytes(collected)


class GitLabConnectionError(ToolError):
    """The GitLab instance could not be reached."""

    def __init__(self, url: str, cause: Exception) -> None:
        """Include the instance URL and the network error."""
        super().__init__(f"Could not reach {url}: {cause}")


class GitLabAPIError(ToolError):
    """GitLab answered with an HTTP error status."""

    def __init__(self, status: int, path: str, detail: str = "") -> None:
        """Include the status, the path, GitLab's detail, and a hint when we have one."""
        self.status = status
        self.detail = detail
        message = f"GitLab returned HTTP {status} for {path}"
        if detail:
            message += f": {detail}"
        hint = _STATUS_HINTS.get(status)
        super().__init__(f"{message}. {hint}" if hint else f"{message}.")


class GitLabRedirectError(ToolError):
    """GitLab answered with a redirect, which the client does not follow."""

    def __init__(self, path: str, status: int) -> None:
        """Say that redirects are refused, so the token is never sent to another host."""
        super().__init__(
            f"GitLab answered {path} with HTTP {status}, a redirect. "
            "Redirects are not followed, so the token is not sent to another host."
        )


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

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one request and return the response. Error statuses raise, so callers only see good responses.

        `path` is relative to the API base URL, unless it is an absolute URL.

        Raises:
            GitLabConnectionError: When the instance cannot be reached.
            GitLabAPIError: When GitLab returns an error status.
        """
        try:
            response = await self._http.request(method, path, params=params or None, json=json)
        except httpx.HTTPError as exc:
            raise GitLabConnectionError(self.settings.url, exc) from exc

        if response.is_error:
            raise GitLabAPIError(response.status_code, path, _error_detail(response))
        return response

    async def get(self, path: str, **params: Any) -> Any:
        """GET `path` and return the parsed JSON body.

        Raises:
            GitLabConnectionError: When the instance cannot be reached.
            GitLabAPIError: When GitLab returns an error status.
        """
        return (await self._send("GET", path, params=params)).json()

    async def get_page(self, path: str, **params: Any) -> tuple[Any, int | None]:
        """GET one page of a list and return its parsed JSON body and the next page number.

        The next page comes from the `X-Next-Page` header. GitLab sends it as an
        empty string on the last page, and some endpoints leave it out, so both
        mean None.

        Raises:
            GitLabConnectionError: When the instance cannot be reached.
            GitLabAPIError: When GitLab returns an error status.
        """
        response = await self._send("GET", path, params=params)
        next_page = response.headers.get("X-Next-Page")  # Empty string on the last page.
        return response.json(), int(next_page) if next_page else None

    async def get_trace_prefix(self, path: str, *, max_bytes: int) -> tuple[bytes, bool]:
        """Stream a plain-text body from `path` and keep only its first `max_bytes` bytes 🪵.

        Job traces can be huge, so the body is read in small pieces and the read
        stops as soon as one byte past `max_bytes` shows up. The rest is never read.

        Returns `(prefix, truncated)`. `prefix` is the first `max_bytes` bytes,
        unchanged and not decoded, so a cut can split a UTF-8 character.
        `truncated` is True only when the body is longer than `max_bytes`.

        Redirects are not followed. GitLab's `PRIVATE-TOKEN` header would go to the
        new host, so a redirect is an error.

        Raises:
            ValueError: When `max_bytes` is not positive.
            GitLabConnectionError: When the instance cannot be reached, or the read takes longer than the timeout.
            GitLabAPIError: When GitLab returns an error status.
            GitLabRedirectError: When GitLab answers with a redirect.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")  # noqa: TRY003 - explain invalid input.
        try:
            # The overall deadline covers the whole stream. httpx's own timeouts cover each network step.
            async with asyncio.timeout(self.settings.timeout):
                return await self._read_trace_prefix(path, max_bytes)
        except httpx.HTTPError as exc:
            raise GitLabConnectionError(self.settings.url, exc) from exc
        except TimeoutError as exc:
            cause = TimeoutError(f"timed out after {self.settings.timeout} seconds")
            raise GitLabConnectionError(self.settings.url, cause) from exc

    async def _read_trace_prefix(self, path: str, max_bytes: int) -> tuple[bytes, bool]:
        """Open the streamed GET, check the status, and read the prefix. The stream closes on every exit."""
        async with self._http.stream(
            "GET",
            path,
            headers={"Accept": "text/plain"},
            follow_redirects=False,
        ) as response:
            if response.status_code in _REDIRECT_STATUSES:
                raise GitLabRedirectError(path, response.status_code)
            if response.is_error:
                raw = await _read_capped(response, _ERROR_BODY_CAP)
                raise GitLabAPIError(response.status_code, path, _detail_from_bytes(raw))
            data = await _read_capped(response, max_bytes)
        return data[:max_bytes], len(data) > max_bytes

    async def request(
        self,
        method: WriteMethod,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        **params: Any,
    ) -> Any:
        """Send a POST, PUT, or DELETE and return the parsed JSON body, or None when there is no body.

        `json` is the request body. Keyword arguments become query parameters.
        GitLab often answers 204 No Content to a DELETE, so an empty body is None.

        Raises:
            GitLabConnectionError: When the instance cannot be reached.
            GitLabAPIError: When GitLab returns an error status.
        """
        response = await self._send(method, path, params=params, json=json)
        return response.json() if response.content else None

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
        response = await self._send("POST", url, json={"query": query, "variables": variables})
        body = response.json()
        if errors := body.get("errors"):
            raise GitLabGraphQLError([error.get("message", str(error)) for error in errors])
        return body["data"]

    async def mutate(self, query: str, variables: dict[str, Any], field: str) -> dict[str, Any]:
        """Run one GraphQL mutation and return its payload, `data[field]`.

        GitLab reports mutation failures inside the payload, as `<field>.errors`,
        and the top-level `errors` list stays empty. So this checks the payload
        too, and raises the same error as `graphql` when it has messages.

        Raises:
            GitLabConnectionError: When the instance cannot be reached.
            GitLabAPIError: When GitLab returns an error status.
            GitLabGraphQLError: When the request or the payload has errors.
        """
        payload = (await self.graphql(query, variables))[field]
        if payload is None:  # GitLab sends null when it refuses the mutation before it runs.
            raise GitLabGraphQLError([f"{field} returned no payload"])
        if errors := payload.get("errors"):
            raise GitLabGraphQLError(errors)
        return payload
