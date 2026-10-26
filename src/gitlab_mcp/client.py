"""Thin async client for the GitLab REST API 🦊."""

import asyncio
import json
import re
import ssl
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

# Status codes used to distinguish a full trace from a ranged response.
_HTTP_OK = 200
_HTTP_PARTIAL_CONTENT = 206

# GitLab pagination headers should stay small and point to a usable page.
_MAX_NEXT_PAGE = 1_000_000
_MAX_NEXT_PAGE_LENGTH = 7

# Keep verification enabled. This tells users how to configure their own CA bundle.
_TLS_CERTIFICATE_HINT = (
    "certificate verification failed; set SSL_CERT_FILE to a PEM CA bundle that trusts "
    "the GitLab instance certificate chain, then restart MCP server."
)

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
        if not isinstance(body, dict):
            return ""
        return _as_text(body.get("message") or body.get("error"))[:_DETAIL_CAP].strip().rstrip(". ")
    except ValueError, RecursionError:  # Invalid JSON or a nested error value exceeded the recursion limit.
        return ""


def _is_certificate_verification_error(error: BaseException) -> bool:
    """Find certificate verification failures in an exception and its chained errors."""
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if "certificate_verify_failed" in str(current).casefold():
            return True
        pending.extend(chained for chained in (current.__cause__, current.__context__) if chained is not None)
    return False


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


def _parse_next_page(value: str | None) -> int | None:
    """Parse a bounded positive ASCII page number from GitLab's pagination header."""
    if value is None or value == "":
        return None
    if len(value) > _MAX_NEXT_PAGE_LENGTH or not all("0" <= char <= "9" for char in value):
        raise ToolError("GitLab returned an invalid next-page header.")  # noqa: TRY003 - explain invalid response.
    page = int(value)
    if page < 1 or page > _MAX_NEXT_PAGE:
        raise ToolError("GitLab returned an invalid next-page header.")  # noqa: TRY003 - explain invalid response.
    return page


def _trace_window_length(content_range: str | None, offset: int, requested_end: int, path: str) -> int:
    """Validate a partial trace range and return its exact expected body length."""
    match = (
        re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)", content_range.strip(), re.IGNORECASE)
        if content_range is not None
        else None
    )
    if match is None:
        raise ToolError(f"GitLab returned an invalid Content-Range for {path}.")  # noqa: TRY003
    try:
        start = int(match.group(1))
        end = int(match.group(2))
        total_text = match.group(3)
        total = None if total_text == "*" else int(total_text)
    except ValueError as exc:
        raise ToolError(f"GitLab returned an invalid Content-Range for {path}.") from exc  # noqa: TRY003
    expected_end = requested_end if total is None else min(requested_end, total - 1)
    if start != offset or end < start or end != expected_end:
        raise ToolError(f"GitLab returned an invalid Content-Range for {path}.")  # noqa: TRY003
    return end - start + 1


class GitLabConnectionError(ToolError):
    """The GitLab instance could not be reached."""

    def __init__(self, url: str, cause: Exception) -> None:
        """Include the instance URL and the network error."""
        message = f"Could not reach {url}: {cause}"
        if _is_certificate_verification_error(cause):
            message += f". {_TLS_CERTIFICATE_HINT}"
        super().__init__(message)


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

    async def get_bounded_json(
        self,
        path: str,
        *,
        max_bytes: int = 131072,
        **params: Any,
    ) -> tuple[Any, int | None]:
        """Stream a bounded JSON response and return its body and next-page number.

        Compression is disabled and rejected if the server ignores the request,
        so the byte cap also bounds the bytes passed to the JSON parser.

        Raises:
            ValueError: When `max_bytes` is not positive.
            GitLabConnectionError: When the instance cannot be reached or the stream exceeds its timeout.
            GitLabAPIError: When GitLab returns an error status.
            GitLabRedirectError: When GitLab answers with a redirect.
            ToolError: When the body is too large, the JSON is invalid, or pagination is invalid.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")  # noqa: TRY003 - explain invalid input.
        try:
            # The deadline bounds async opening and reading, but cannot interrupt synchronous JSON parsing.
            async with asyncio.timeout(self.settings.timeout):
                return await self._read_bounded_json(path, max_bytes, params)
        except httpx.HTTPError as exc:
            raise GitLabConnectionError(self.settings.url, exc) from exc
        except TimeoutError as exc:
            cause = TimeoutError(f"timed out after {self.settings.timeout} seconds")
            raise GitLabConnectionError(self.settings.url, cause) from exc

    async def _read_bounded_json(
        self,
        path: str,
        max_bytes: int,
        params: dict[str, Any],
    ) -> tuple[Any, int | None]:
        """Open a streamed JSON GET, validate its headers, and read a capped body."""
        async with self._http.stream(
            "GET",
            path,
            params=params or None,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            follow_redirects=False,
        ) as response:
            if response.status_code in _REDIRECT_STATUSES:
                raise GitLabRedirectError(path, response.status_code)
            content_encoding = response.headers.get("Content-Encoding")
            if content_encoding is not None and content_encoding.strip().casefold() != "identity":
                raise ToolError(f"GitLab returned an unsupported Content-Encoding for {path}.")  # noqa: TRY003
            if response.is_error:
                raw = await _read_capped(response, _ERROR_BODY_CAP)
                raise GitLabAPIError(response.status_code, path, _detail_from_bytes(raw))

            data = await _read_capped(response, max_bytes)
            if len(data) > max_bytes:
                raise ToolError(f"GitLab response for {path} exceeds the {max_bytes}-byte limit.")  # noqa: TRY003
            next_page = _parse_next_page(response.headers.get("X-Next-Page"))
            try:
                body = json.loads(data)
            except (ValueError, RecursionError) as exc:
                raise ToolError(f"GitLab returned invalid JSON for {path}.") from exc  # noqa: TRY003
        return body, next_page

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

    async def get_trace_window(self, path: str, *, offset: int, max_bytes: int) -> tuple[bytes, bool]:
        """Stream a bounded plain-text window from `path`, starting at byte `offset` 🪵.

        Requests one byte past `max_bytes` so the caller can tell if more trace
        data follows. The response stays as raw bytes, without decoding.

        Returns `(window, truncated)`. `window` contains at most `max_bytes`
        bytes, and `truncated` is True only when the response has more.

        Redirects are not followed. A nonzero offset requires a valid partial
        response because falling back to the full trace would not be bounded.

        Raises:
            ValueError: When `offset` is negative or `max_bytes` is not positive.
            GitLabConnectionError: When the instance cannot be reached, or the read takes longer than the timeout.
            GitLabAPIError: When GitLab returns an HTTP error status.
            GitLabRedirectError: When GitLab answers with a redirect.
            ToolError: When range continuation is unsupported or response headers are invalid.
        """
        if offset < 0:
            raise ValueError("offset must be nonnegative")  # noqa: TRY003 - explain invalid input.
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")  # noqa: TRY003 - explain invalid input.
        try:
            # The overall deadline covers the whole stream. httpx's own timeouts cover each network step.
            async with asyncio.timeout(self.settings.timeout):
                return await self._read_trace_window(path, offset, max_bytes)
        except httpx.HTTPError as exc:
            raise GitLabConnectionError(self.settings.url, exc) from exc
        except TimeoutError as exc:
            cause = TimeoutError(f"timed out after {self.settings.timeout} seconds")
            raise GitLabConnectionError(self.settings.url, cause) from exc

    async def _read_trace_window(self, path: str, offset: int, max_bytes: int) -> tuple[bytes, bool]:
        """Open and validate a ranged trace GET, then read no more than one extra byte."""
        requested_end = offset + max_bytes
        expected_length: int | None = None
        async with self._http.stream(
            "GET",
            path,
            headers={
                "Accept": "text/plain",
                "Accept-Encoding": "identity",
                "Range": f"bytes={offset}-{requested_end}",
            },
            follow_redirects=False,
        ) as response:
            if response.status_code in _REDIRECT_STATUSES:
                raise GitLabRedirectError(path, response.status_code)
            content_encoding = response.headers.get("Content-Encoding")
            if content_encoding is not None and content_encoding.strip().casefold() != "identity":
                raise ToolError(f"GitLab returned an unsupported Content-Encoding for {path}.")  # noqa: TRY003
            if response.is_error:
                raw = await _read_capped(response, _ERROR_BODY_CAP)
                raise GitLabAPIError(response.status_code, path, _detail_from_bytes(raw))
            if response.status_code == _HTTP_OK:
                if offset != 0:
                    raise ToolError(  # noqa: TRY003 - explain unsupported range behavior.
                        f"GitLab server does not support bounded trace continuation for {path}."
                    )
            elif response.status_code == _HTTP_PARTIAL_CONTENT:
                expected_length = _trace_window_length(
                    response.headers.get("Content-Range"), offset, requested_end, path
                )
            else:
                raise ToolError(  # noqa: TRY003 - explain the unexpected response.
                    f"GitLab returned unexpected HTTP {response.status_code} for {path} while reading a trace window."
                )
            data = await _read_capped(response, expected_length if expected_length is not None else max_bytes)
            if expected_length is not None and len(data) != expected_length:
                raise ToolError(f"GitLab returned an invalid trace window body length for {path}.")  # noqa: TRY003
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
