"""Safely stream bounded GitLab job artifact downloads 📦."""

import asyncio
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from fastmcp.exceptions import ToolError

from gitlab_mcp.client import GitLabAPIError, _is_certificate_verification_error
lazy from gitlab_mcp.config import Settings

_ERROR_BODY_CAP = 1024
_MAX_REDIRECTS = 3
_STREAM_CHUNK_SIZE = 4096
_CONTROL_CHARACTER_LIMIT = 32
_HTTP_OK = 200
_HTTP_ERROR_MIN = 400
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_UNSAFE_REDIRECT_ERROR = "GitLab returned an unsafe artifact redirect."
_UNCONFIGURED_ORIGIN_ERROR = (
    "Artifact storage redirect is not trusted. Ask an administrator to configure the trusted storage origin "
    "in GITLAB_ARTIFACT_ORIGINS."
)
_DOWNLOAD_ERROR = "Could not download the artifact from GitLab."
_TLS_DOWNLOAD_ERROR = (
    "Artifact download failed because TLS certificate verification failed. Configure SSL_CERT_FILE or SSL_CERT_DIR "
    "with a CA bundle that trusts the GitLab or artifact storage certificate chain."
)


def _origin(url: str) -> str | None:
    """Return the normalized HTTP(S) origin for `url`, or None if it has no valid origin."""
    try:
        parsed = httpx.URL(url)
        hostname = parsed.raw_host.decode("ascii")
        port = parsed.port
    except UnicodeError, ValueError, httpx.InvalidURL:
        return None
    scheme = parsed.scheme
    if scheme not in {"http", "https"} or not hostname:
        return None
    hostname = hostname.lower()
    authority = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    effective_port = port if port is not None else default_port
    if effective_port != default_port:
        authority += f":{effective_port}"
    return f"{scheme}://{authority}"


def _initial_url(settings: Settings, path: str) -> tuple[httpx.URL, str]:
    """Build a safe initial artifact URL from the configured API base and a relative project path."""
    parts = urlsplit(path)
    if (
        not path.startswith("/projects/")
        or path.startswith("//")
        or parts.scheme
        or parts.netloc
        or parts.query
        or parts.fragment
        or "?" in path
        or "#" in path
        or "\\" in path
        or any(character.isspace() or ord(character) < _CONTROL_CHARACTER_LIMIT for character in path)
    ):
        raise ToolError("Artifact path must be a relative /projects/... API path.")  # noqa: TRY003

    try:
        api = urlsplit(settings.api_url)
        api_origin = _origin(settings.api_url)
        api_path = api.path.rstrip("/")
        url = httpx.URL(urlunsplit((api.scheme, api.netloc, f"{api_path}{path}", "", "")))
    except ValueError, httpx.InvalidURL:
        raise ToolError("GitLab returned an invalid artifact API URL.") from None  # noqa: TRY003
    if (
        api_origin is None
        or api.username is not None
        or api.password is not None
        or api.query
        or api.fragment
        or "?" in settings.api_url
        or "#" in settings.api_url
    ):
        raise ToolError("GitLab returned an invalid artifact API URL.")  # noqa: TRY003

    return url, api_origin


def _redirect_url(current: httpx.URL, location: str, gitlab_origin: str, trusted_origins: set[str]) -> httpx.URL:
    """Resolve and validate one redirect destination without exposing its URL in errors."""
    if not location or "#" in location:
        raise ToolError(_UNSAFE_REDIRECT_ERROR)
    try:
        target = httpx.URL(urljoin(str(current), location))
        parsed = urlsplit(str(target))
        origin = _origin(str(target))
    except ValueError, httpx.InvalidURL:
        raise ToolError(_UNSAFE_REDIRECT_ERROR) from None

    if (
        origin is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (current.scheme == "https" and target.scheme != "https")
    ):
        raise ToolError(_UNSAFE_REDIRECT_ERROR)
    if origin != gitlab_origin and target.scheme != "https":
        raise ToolError(_UNSAFE_REDIRECT_ERROR)
    if origin != gitlab_origin and origin not in trusted_origins:
        raise ToolError(_UNCONFIGURED_ORIGIN_ERROR)
    return target


async def _read_capped(response: httpx.Response, max_bytes: int, *, detect_extra_byte: bool = True) -> bytes:
    """Read a bounded number of raw bytes, optionally retaining one byte for truncation detection."""
    read_limit = max_bytes + int(detect_extra_byte)
    collected = bytearray()
    async for chunk in response.aiter_raw(chunk_size=_STREAM_CHUNK_SIZE):
        collected.extend(chunk[: read_limit - len(collected)])
        if len(collected) >= read_limit:
            break
    return bytes(collected)


async def _read_final_response(response: httpx.Response, path: str, max_bytes: int) -> tuple[bytes, bool]:
    """Validate a final response and read its bounded artifact prefix."""
    content_encoding = response.headers.get("Content-Encoding")
    if content_encoding is not None and content_encoding.strip().casefold() != "identity":
        raise ToolError("GitLab returned an unsupported artifact Content-Encoding.")  # noqa: TRY003
    if response.status_code >= _HTTP_ERROR_MIN:
        await _read_capped(response, _ERROR_BODY_CAP, detect_extra_byte=False)
        raise GitLabAPIError(response.status_code, path)
    if response.status_code != _HTTP_OK:
        raise ToolError("GitLab returned an unexpected artifact response.")  # noqa: TRY003
    body = await _read_capped(response, max_bytes)
    return body[:max_bytes], len(body) > max_bytes


async def _request_hop(
    settings: Settings,
    path: str,
    current: httpx.URL,
    max_bytes: int,
    headers: dict[str, str],
) -> tuple[str | None, tuple[bytes, bool] | None]:
    """Send one isolated request and return its redirect location or bounded final body."""
    async with (
        httpx.AsyncClient(trust_env=True, follow_redirects=False, timeout=settings.timeout) as client,
        client.stream("GET", current, headers=headers) as response,
    ):
        if response.status_code in _REDIRECT_STATUSES:
            return response.headers.get("Location"), None
        return None, await _read_final_response(response, path, max_bytes)


async def _download(
    settings: Settings,
    path: str,
    max_bytes: int,
    initial_url: httpx.URL,
    gitlab_origin: str,
) -> tuple[bytes, bool]:
    """Follow only approved artifact redirects and return a bounded raw body prefix."""
    trusted_origins = {
        normalized
        for configured_origin in settings.artifact_origins
        if (normalized := _origin(configured_origin)) is not None
    }
    current = initial_url
    visited = {str(current)}
    still_on_gitlab = True
    redirects = 0

    while True:
        current_origin = _origin(str(current))
        if current_origin != gitlab_origin:
            still_on_gitlab = False

        headers = {"Accept-Encoding": "identity"}
        if still_on_gitlab and current_origin == gitlab_origin:
            headers["PRIVATE-TOKEN"] = settings.token

        location, body = await _request_hop(settings, path, current, max_bytes, headers)
        if body is not None:
            return body
        if redirects >= _MAX_REDIRECTS:
            raise ToolError("Artifact download exceeded the redirect limit.")  # noqa: TRY003
        if location is None:
            raise ToolError(_UNSAFE_REDIRECT_ERROR)
        redirect_target = _redirect_url(current, location, gitlab_origin, trusted_origins)
        if str(redirect_target) in visited:
            raise ToolError("Artifact download returned a redirect loop.")  # noqa: TRY003
        visited.add(str(redirect_target))
        redirects += 1
        current = redirect_target


async def get_artifact_prefix(settings: Settings, path: str, *, max_bytes: int) -> tuple[bytes, bool]:
    """Download a bounded artifact prefix while keeping GitLab credentials on the GitLab origin only."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")  # noqa: TRY003 - explain invalid input.
    initial_url, gitlab_origin = _initial_url(settings, path)
    try:
        async with asyncio.timeout(settings.timeout):
            return await _download(settings, path, max_bytes, initial_url, gitlab_origin)
    except httpx.HTTPError as exc:
        message = _TLS_DOWNLOAD_ERROR if _is_certificate_verification_error(exc) else _DOWNLOAD_ERROR
        raise ToolError(message) from None
    except TimeoutError:
        raise ToolError(_DOWNLOAD_ERROR) from None
