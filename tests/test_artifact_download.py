"""Tests for bounded, credential-safe artifact downloads 📦."""

import asyncio
import itertools
import os
import ssl
from contextlib import asynccontextmanager
from types import SimpleNamespace
lazy from collections.abc import AsyncGenerator, AsyncIterator, Iterable

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError
from httpx._transports import default as httpx_default

from gitlab_mcp import artifact_download
from gitlab_mcp.client import GitLabAPIError, GitLabClient
from gitlab_mcp.config import Settings

API = "https://gitlab.example.com/api/v4"
ARTIFACT_PATH = "/projects/group%2Fproject/jobs/42/artifacts"
ARTIFACT_URL = f"{API}{ARTIFACT_PATH}"
AUTH_HEADER_VALUE = "artifact-test-token"
TRUSTED_ARTIFACT_ORIGIN = "https://artifacts.example"


@pytest.fixture
async def client():
    """Build a client with one trusted artifact host and close it after each test."""
    gitlab = GitLabClient(
        Settings(
            url="https://gitlab.example.com",
            token=AUTH_HEADER_VALUE,
            timeout=1,
            artifact_origins=(TRUSTED_ARTIFACT_ORIGIN,),
        )
    )
    yield gitlab
    await gitlab.aclose()


class SpyStream(httpx.AsyncByteStream):
    """Yield response chunks and record how much of the stream was consumed."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        """Save the chunks and initialize read and close counters."""
        self.chunks = iter(chunks)
        self.pulled = 0
        self.closed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield chunks one at a time and count each pull."""
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk

    async def aclose(self) -> None:
        """Count stream closure so tests can check early cleanup."""
        self.closed += 1


class HangingStream(httpx.AsyncByteStream):
    """Wait forever when read, allowing the overall timeout to be tested without sleeping."""

    def __init__(self) -> None:
        """Initialize a close counter for cancellation checks."""
        self.closed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Wait for cancellation before yielding any data."""
        await asyncio.Event().wait()
        yield b""

    async def aclose(self) -> None:
        """Provide the async stream close hook."""
        self.closed += 1


@pytest.mark.parametrize(
    ("body", "max_bytes", "expected"),
    [
        pytest.param(b"0123456789", 4, (b"0123", True), id="extra-byte-truncates"),
        pytest.param(b"0123", 4, (b"0123", False), id="exact-cap-is-not-truncated"),
        pytest.param(b"01", 4, (b"01", False), id="short-body"),
        pytest.param(b"", 4, (b"", False), id="empty-body"),
    ],
)
@respx.mock
async def test_artifact_prefix_obeys_byte_cap(client, body, max_bytes, expected):
    """Return the unchanged prefix and mark truncation only when an extra byte exists."""
    # Arrange
    stream = SpyStream([body])
    respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(200, stream=stream))

    # Act
    result = await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=max_bytes)

    # Assert
    assert result == expected
    assert stream.closed == 1


@respx.mock
async def test_artifact_prefix_stops_streaming_after_extra_byte(client):
    """A large response stops after the first chunk proves the cap was exceeded."""
    # Arrange
    stream = SpyStream(itertools.repeat(b"x" * 100_000))
    respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(200, stream=stream))

    # Act
    result = await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)

    # Assert
    assert result == (b"x" * 10, True)
    assert stream.pulled == 1
    assert stream.closed == 1


@respx.mock
async def test_artifact_request_uses_identity_encoding_and_private_token(client):
    """The isolated request sends its token only to the configured GitLab origin."""
    # Arrange
    route = respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(200, content=b"artifact"))

    # Act
    await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=100)

    # Assert
    request = route.calls[0].request
    assert request.headers["PRIVATE-TOKEN"] == AUTH_HEADER_VALUE
    assert request.headers["Accept-Encoding"] == "identity"
    assert "Cookie" not in request.headers
    assert "Referer" not in request.headers
    assert request.url.query == b""


@respx.mock
async def test_unicode_gitlab_hostname_uses_canonical_origin_for_authentication(client):
    """Unicode GitLab settings match the HTTPX ASCII hostname and keep authentication on that origin."""
    # Arrange
    client.settings = Settings(url="https://BÜCHER.example:443", token=AUTH_HEADER_VALUE, timeout=1)
    request_url = f"https://xn--bcher-kva.example/api/v4{ARTIFACT_PATH}"
    route = respx.get(request_url).mock(return_value=httpx.Response(200, content=b"artifact"))

    # Act
    result = await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=20)

    # Assert
    assert result == (b"artifact", False)
    assert route.calls[0].request.headers["PRIVATE-TOKEN"] == AUTH_HEADER_VALUE


@respx.mock
async def test_unicode_configured_storage_origin_matches_punycode_redirect(client):
    """A Unicode origin loaded from environment settings matches HTTPX's punycode request URL."""
    # Arrange
    client.settings = Settings.from_env(
        {
            "GITLAB_URL": "https://gitlab.example.com",
            "GITLAB_TOKEN": AUTH_HEADER_VALUE,
            "GITLAB_ARTIFACT_ORIGINS": "https://BÜCHER.example:443",
        }
    )
    first = respx.get(ARTIFACT_URL).mock(
        return_value=httpx.Response(302, headers={"Location": "https://xn--bcher-kva.example/artifact"})
    )
    destination = respx.get("https://xn--bcher-kva.example/artifact").mock(
        return_value=httpx.Response(200, content=b"ok")
    )

    # Act
    result = await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)

    # Assert
    assert result == (b"ok", False)
    assert first.calls[0].request.headers["PRIVATE-TOKEN"] == AUTH_HEADER_VALUE
    assert "PRIVATE-TOKEN" not in destination.calls[0].request.headers


@respx.mock
async def test_artifact_transport_uses_environment_for_ca_configuration(client, monkeypatch):
    """The isolated transport enables HTTPX environment trust without reading a fake CA file."""
    # Arrange
    ca_path = "/test/ca-bundle.pem"
    monkeypatch.setenv("SSL_CERT_FILE", ca_path)
    observed: list[tuple[bool, str | None]] = []
    create_ssl_context = httpx_default.create_ssl_context

    def capture_ssl_context(*, verify, cert, trust_env):
        """Record transport trust settings while using a valid default test context."""
        observed.append((trust_env, os.environ.get("SSL_CERT_FILE")))
        return create_ssl_context(verify=verify, cert=cert, trust_env=False)

    monkeypatch.setattr(httpx_default, "create_ssl_context", capture_ssl_context)
    route = respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(200, content=b"ok"))

    # Act
    result = await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)

    # Assert
    assert result == (b"ok", False)
    assert route.called
    assert observed == [(True, ca_path)]


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("https://untrusted.example/file", id="absolute-url"),
        pytest.param("//untrusted.example/file", id="network-path-reference"),
        pytest.param("/jobs/42/artifacts", id="non-project-api-path"),
        pytest.param("/projects/group/artifacts?signature=secret", id="caller-query"),
        pytest.param("/projects/group/artifacts#fragment", id="caller-fragment"),
    ],
)
async def test_only_relative_project_api_paths_are_accepted(client, path):
    """Caller input cannot choose a host or add a query to the GitLab API URL."""
    # Act / Assert
    with pytest.raises(ToolError, match="relative /projects"):
        await client.get_artifact_prefix(path, max_bytes=10)


@pytest.mark.parametrize(
    "api_url",
    [
        pytest.param("https://gitlab.example.com:invalid", id="invalid-port"),
        pytest.param("https://user:password@gitlab.example.com", id="userinfo"),
        pytest.param("https:///missing-host", id="missing-hostname"),
    ],
)
async def test_malformed_configured_api_urls_are_rejected_by_public_method(client, api_url):
    """The public artifact method rejects API URLs with invalid authority data."""
    # Arrange
    client.settings = Settings(url=api_url, token=AUTH_HEADER_VALUE, timeout=1)

    # Act / Assert
    with pytest.raises(ToolError, match="invalid artifact API URL"):
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)


@respx.mock
async def test_same_origin_redirect_keeps_token_but_does_not_replay_cookie(client):
    """Same-origin redirects keep the GitLab token, but each hop has a fresh cookie jar."""
    # Arrange
    first = respx.get(ARTIFACT_URL).mock(
        return_value=httpx.Response(302, headers={"Location": "/stored/artifact", "Set-Cookie": "sid=secret"})
    )
    second = respx.get("https://gitlab.example.com/stored/artifact").mock(
        return_value=httpx.Response(200, content=b"artifact")
    )

    # Act
    result = await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=100)

    # Assert
    assert result == (b"artifact", False)
    assert first.calls[0].request.headers["PRIVATE-TOKEN"] == AUTH_HEADER_VALUE
    assert second.calls[0].request.headers["PRIVATE-TOKEN"] == AUTH_HEADER_VALUE
    assert "Cookie" not in second.calls[0].request.headers
    assert "Referer" not in second.calls[0].request.headers


@respx.mock
async def test_signed_external_redirect_keeps_query_and_never_regains_token(client):
    """Signed queries survive redirects, and GitLab credentials stay off external and later hops."""
    # Arrange
    signed_url = f"{TRUSTED_ARTIFACT_ORIGIN}/file?X-Amz-Signature=signature-secret&part=1"
    first = respx.get(ARTIFACT_URL).mock(
        return_value=httpx.Response(302, headers={"Location": signed_url, "Set-Cookie": "sid=secret"})
    )
    external = respx.get(signed_url).mock(
        return_value=httpx.Response(
            302,
            headers={"Location": "https://gitlab.example.com/final", "Set-Cookie": "storage-session=secret"},
        )
    )
    final = respx.get("https://gitlab.example.com/final").mock(return_value=httpx.Response(200, content=b"ok"))

    # Act
    result = await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=20)

    # Assert
    assert result == (b"ok", False)
    assert first.calls[0].request.headers["PRIVATE-TOKEN"] == AUTH_HEADER_VALUE
    assert "PRIVATE-TOKEN" not in external.calls[0].request.headers
    assert external.calls[0].request.url.query == b"X-Amz-Signature=signature-secret&part=1"
    assert "Cookie" not in external.calls[0].request.headers
    assert "Referer" not in external.calls[0].request.headers
    assert "PRIVATE-TOKEN" not in final.calls[0].request.headers
    assert "Cookie" not in final.calls[0].request.headers


@respx.mock
async def test_relative_redirect_resolves_without_inheriting_old_query(client):
    """A relative Location resolves against the current path and does not inherit a prior query."""
    # Arrange
    first = respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(302, headers={"Location": "file?signature=kept"}))
    destination_url = f"{API}/projects/group%2Fproject/jobs/42/file?signature=kept"
    destination = respx.get(destination_url).mock(return_value=httpx.Response(200, content=b"ok"))

    # Act
    result = await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=20)

    # Assert
    assert result == (b"ok", False)
    assert first.called
    assert destination.called
    assert destination.calls[0].request.url.query == b"signature=kept"


@pytest.mark.parametrize(
    "location",
    [
        pytest.param("http://gitlab.example.com/file", id="https-downgrade"),
        pytest.param("http://artifacts.example/file", id="external-http"),
        pytest.param("https://user:password@artifacts.example/file", id="userinfo"),
        pytest.param("https://artifacts.example/file#fragment", id="fragment"),
    ],
)
@respx.mock
async def test_unsafe_redirect_is_blocked_before_request(client, location):
    """Reject redirect destinations that violate the exact-origin trust rules."""
    # Arrange
    first = respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(302, headers={"Location": location}))

    # Act / Assert
    with pytest.raises(ToolError, match="unsafe artifact redirect"):
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=20)
    assert len(first.calls) == 1


@pytest.mark.parametrize(
    ("location", "private_detail"),
    [
        pytest.param("https://artifacts.example:invalid/file", "artifacts.example:invalid", id="invalid-port"),
        pytest.param("https://[2001:db8::zz]/file", "2001:db8::zz", id="invalid-ipv6-hostname"),
    ],
)
async def test_malformed_redirect_authorities_fail_safely_without_following(location, private_detail):
    """Malformed redirect host or port data is rejected without exposing or requesting it."""
    # Arrange
    with respx.mock(assert_all_mocked=False) as router:
        router.get(ARTIFACT_URL).mock(return_value=httpx.Response(302, headers={"Location": location}))
        client = GitLabClient(Settings(url="https://gitlab.example.com", token=AUTH_HEADER_VALUE, timeout=1))

        # Act / Assert
        try:
            with pytest.raises(ToolError) as caught:
                await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
            assert private_detail not in str(caught.value)
            assert len(router.calls) == 1
        finally:
            await client.aclose()


@pytest.mark.parametrize(
    ("location", "destination_host"),
    [
        pytest.param(
            "https://untrusted.example/file?signature=secret-value",
            "untrusted.example",
            id="unconfigured-host",
        ),
        pytest.param(
            "https://gitlab.example.com:444/file?signature=secret-value",
            "gitlab.example.com",
            id="port-mismatch",
        ),
    ],
)
@respx.mock
async def test_unconfigured_https_origin_returns_setup_hint_without_destination_details(
    client, location, destination_host
):
    """An unconfigured HTTPS storage origin gets a safe administrator setup hint."""
    # Arrange
    first = respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(302, headers={"Location": location}))

    # Act / Assert
    with pytest.raises(ToolError) as caught:
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=20)
    message = str(caught.value)
    assert "GITLAB_ARTIFACT_ORIGINS" in message
    assert "administrator" in message
    assert "trusted storage origin" in message
    assert destination_host not in message
    assert "signature=secret-value" not in message
    assert location not in message
    assert len(first.calls) == 1


@respx.mock
async def test_configured_private_artifact_origin_is_trusted():
    """An explicitly configured private HTTPS destination is accepted without DNS policy checks."""
    # Arrange
    settings = Settings(
        url="https://gitlab.example.com",
        token=AUTH_HEADER_VALUE,
        timeout=1,
        artifact_origins=("https://10.0.0.8:8443",),
    )
    gitlab = GitLabClient(settings)
    respx.get(ARTIFACT_URL).mock(
        return_value=httpx.Response(302, headers={"Location": "https://10.0.0.8:8443/artifact"})
    )
    private = respx.get("https://10.0.0.8:8443/artifact").mock(return_value=httpx.Response(200, content=b"ok"))

    # Act
    result = await gitlab.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)

    # Assert
    await gitlab.aclose()
    assert result == (b"ok", False)
    assert "PRIVATE-TOKEN" not in private.calls[0].request.headers


@pytest.mark.parametrize(
    ("origin", "location"),
    [
        pytest.param("https://[2001:db8::1]", "https://[2001:db8::1]/artifact", id="ipv6"),
        pytest.param("https://cache.example:8443", "https://cache.example:8443/artifact", id="nondefault-port"),
    ],
)
@respx.mock
async def test_configured_ipv6_and_nondefault_origins_are_matched_exactly(client, origin, location):
    """Configured IPv6 and nondefault-port origins accept exact matching HTTPS redirects."""
    # Arrange
    client.settings = Settings(
        url="https://gitlab.example.com",
        token=AUTH_HEADER_VALUE,
        timeout=1,
        artifact_origins=(origin,),
    )
    first = respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(302, headers={"Location": location}))
    destination = respx.get(location).mock(return_value=httpx.Response(200, content=b"ok"))

    # Act
    result = await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)

    # Assert
    assert result == (b"ok", False)
    assert first.called
    assert destination.called
    assert "PRIVATE-TOKEN" not in destination.calls[0].request.headers


@respx.mock
async def test_allowed_storage_redirect_to_unconfigured_origin_is_blocked(client):
    """An allowed storage host cannot redirect onward to a different, unconfigured host."""
    # Arrange
    first = respx.get(ARTIFACT_URL).mock(
        return_value=httpx.Response(302, headers={"Location": f"{TRUSTED_ARTIFACT_ORIGIN}/first"})
    )
    storage = respx.get(f"{TRUSTED_ARTIFACT_ORIGIN}/first").mock(
        return_value=httpx.Response(302, headers={"Location": "https://blocked.example/final?signature=secret"})
    )
    blocked = respx.get("https://blocked.example/final?signature=secret").mock(
        return_value=httpx.Response(200, content=b"not requested")
    )

    # Act / Assert
    with pytest.raises(ToolError, match="GITLAB_ARTIFACT_ORIGINS"):
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
    assert first.called
    assert "PRIVATE-TOKEN" not in storage.calls[0].request.headers
    assert not blocked.called


@respx.mock
async def test_http_gitlab_origin_is_allowed_when_configured_as_http():
    """HTTP redirects are allowed only when HTTP is the configured GitLab origin."""
    # Arrange
    settings = Settings(url="http://gitlab.example.com", token=AUTH_HEADER_VALUE, timeout=1)
    gitlab = GitLabClient(settings)
    http_url = "http://gitlab.example.com/api/v4/projects/group%2Fproject/jobs/42/artifacts"
    route = respx.get(http_url).mock(return_value=httpx.Response(302, headers={"Location": "/stored"}))
    destination = respx.get("http://gitlab.example.com/stored").mock(return_value=httpx.Response(200, content=b"ok"))

    # Act
    result = await gitlab.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)

    # Assert
    await gitlab.aclose()
    assert result == (b"ok", False)
    assert route.calls[0].request.headers["PRIVATE-TOKEN"] == AUTH_HEADER_VALUE
    assert destination.calls[0].request.headers["PRIVATE-TOKEN"] == AUTH_HEADER_VALUE


@respx.mock
async def test_http_gitlab_cannot_redirect_to_a_different_http_origin():
    """An HTTP GitLab instance cannot redirect credentials or artifact requests to another HTTP origin."""
    # Arrange
    gitlab = GitLabClient(Settings(url="http://gitlab.example.com", token=AUTH_HEADER_VALUE, timeout=1))
    api_url = f"http://gitlab.example.com/api/v4{ARTIFACT_PATH}"
    first = respx.get(api_url).mock(
        return_value=httpx.Response(302, headers={"Location": "http://storage.example/artifact"})
    )
    destination = respx.get("http://storage.example/artifact").mock(
        return_value=httpx.Response(200, content=b"must not be requested")
    )

    # Act / Assert
    try:
        with pytest.raises(ToolError, match="unsafe artifact redirect"):
            await gitlab.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
    finally:
        await gitlab.aclose()
    assert first.called
    assert not destination.called


@pytest.mark.parametrize("max_bytes", [0, -1])
@respx.mock
async def test_artifact_prefix_requires_positive_limit_before_http(client, max_bytes):
    """Nonpositive byte limits fail before the artifact request is sent."""
    # Arrange
    route = respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(200, content=b"not requested"))

    # Act / Assert
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=max_bytes)
    assert not route.called


@respx.mock
async def test_redirect_loop_is_detected_before_repeating_request(client):
    """A redirect back to an already requested URL is rejected immediately."""
    # Arrange
    route = respx.get(ARTIFACT_URL).mock(
        return_value=httpx.Response(302, headers={"Location": f"/api/v4{ARTIFACT_PATH}"})
    )

    # Act / Assert
    with pytest.raises(ToolError, match="redirect loop"):
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
    assert len(route.calls) == 1


@respx.mock
async def test_artifact_download_stops_after_three_redirects(client):
    """The downloader allows three redirects and refuses a fourth hop."""
    # Arrange
    first = respx.get(ARTIFACT_URL).mock(
        return_value=httpx.Response(302, headers={"Location": f"{TRUSTED_ARTIFACT_ORIGIN}/one"})
    )
    second = respx.get(f"{TRUSTED_ARTIFACT_ORIGIN}/one").mock(
        return_value=httpx.Response(302, headers={"Location": f"{TRUSTED_ARTIFACT_ORIGIN}/two"})
    )
    third = respx.get(f"{TRUSTED_ARTIFACT_ORIGIN}/two").mock(
        return_value=httpx.Response(302, headers={"Location": f"{TRUSTED_ARTIFACT_ORIGIN}/three"})
    )
    fourth = respx.get(f"{TRUSTED_ARTIFACT_ORIGIN}/three").mock(
        return_value=httpx.Response(302, headers={"Location": f"{TRUSTED_ARTIFACT_ORIGIN}/four"})
    )
    last = respx.get(f"{TRUSTED_ARTIFACT_ORIGIN}/four").mock(return_value=httpx.Response(200, content=b"bad"))

    # Act / Assert
    with pytest.raises(ToolError, match="redirect limit"):
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
    assert first.called
    assert second.called
    assert third.called
    assert fourth.called
    assert not last.called


@respx.mock
async def test_artifact_download_allows_exactly_three_redirects(client):
    """Three redirects are allowed, with authentication limited to GitLab-origin hops."""
    # Arrange
    first = respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(302, headers={"Location": "/stored/one"}))
    second = respx.get("https://gitlab.example.com/stored/one").mock(
        return_value=httpx.Response(302, headers={"Location": "/stored/two"})
    )
    third = respx.get("https://gitlab.example.com/stored/two").mock(
        return_value=httpx.Response(302, headers={"Location": f"{TRUSTED_ARTIFACT_ORIGIN}/stored/three"})
    )
    final = respx.get(f"{TRUSTED_ARTIFACT_ORIGIN}/stored/three").mock(return_value=httpx.Response(200, content=b"ok"))

    # Act
    result = await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)

    # Assert
    assert result == (b"ok", False)
    assert first.calls[0].request.headers["PRIVATE-TOKEN"] == AUTH_HEADER_VALUE
    assert second.calls[0].request.headers["PRIVATE-TOKEN"] == AUTH_HEADER_VALUE
    assert third.calls[0].request.headers["PRIVATE-TOKEN"] == AUTH_HEADER_VALUE
    assert "PRIVATE-TOKEN" not in final.calls[0].request.headers


@pytest.mark.parametrize("status", [206, 204, 302])
@respx.mock
async def test_only_http_200_is_accepted_as_final_response(client, status):
    """Partial, empty, or redirect statuses are never treated as a completed artifact."""
    # Arrange
    stream = SpyStream([b"must not read"])
    respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(status, stream=stream))

    # Act / Assert
    with pytest.raises(ToolError):
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
    assert stream.pulled == 0
    assert stream.closed == 1


@respx.mock
async def test_compressed_response_is_rejected_before_body_read(client):
    """A server cannot bypass the byte limit by ignoring Accept-Encoding identity."""
    # Arrange
    stream = SpyStream([b"compressed payload"])
    respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream))

    # Act / Assert
    with pytest.raises(ToolError, match="Content-Encoding"):
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
    assert stream.pulled == 0
    assert stream.closed == 1


@respx.mock
async def test_http_error_uses_safe_path_and_never_includes_body(client):
    """HTTP error detail is consumed but neither body text nor a signed URL reaches the caller."""
    # Arrange
    sensitive_detail = "body-detail https://artifacts.example/file?signature=private"
    respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(403, content=sensitive_detail.encode()))

    # Act / Assert
    with pytest.raises(GitLabAPIError) as caught:
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
    message = str(caught.value)
    assert message.startswith(f"GitLab returned HTTP 403 for {ARTIFACT_PATH}")
    assert sensitive_detail not in message
    assert "artifacts.example" not in message
    assert caught.value.detail == ""


@respx.mock
async def test_http_error_body_is_capped_and_closed(client):
    """An error body is read only to the fixed diagnostic cap, then closed."""
    # Arrange
    stream = SpyStream(itertools.repeat(b"x" * 100_000))
    respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(500, stream=stream))

    # Act / Assert
    with pytest.raises(GitLabAPIError):
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
    assert stream.pulled == 1
    assert stream.closed == 1


@respx.mock
async def test_network_exception_does_not_leak_exception_text(client):
    """Network errors use a safe generic message instead of exposing the exception or URL."""
    # Arrange
    sensitive_detail = "transport-detail https://artifacts.example/file?signature=private"
    respx.get(ARTIFACT_URL).mock(side_effect=httpx.ConnectError(sensitive_detail))

    # Act / Assert
    with pytest.raises(ToolError) as caught:
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
    assert sensitive_detail not in str(caught.value)
    assert "artifacts.example" not in str(caught.value)


@respx.mock
async def test_certificate_failure_gets_safe_ca_configuration_hint(client):
    """Certificate failures offer CA configuration guidance without exposing transport details."""
    # Arrange
    sensitive_detail = "private proxy URL https://proxy.example/path?secret=private"
    network_error = httpx.ConnectError(f"certificate_verify_failed: {sensitive_detail}")
    network_error.__cause__ = ssl.SSLCertVerificationError("certificate_verify_failed: private certificate detail")
    respx.get(ARTIFACT_URL).mock(side_effect=network_error)

    # Act / Assert
    with pytest.raises(ToolError) as caught:
        await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
    message = str(caught.value)
    assert "SSL_CERT_FILE" in message
    assert "SSL_CERT_DIR" in message
    assert sensitive_detail not in message
    assert "proxy.example" not in message
    assert "private certificate detail" not in message


@respx.mock
async def test_one_timeout_context_covers_multiple_redirect_hops(client, monkeypatch):
    """A single total-timeout context surrounds the full redirect chain without sleeping."""
    # Arrange
    seen_timeouts: list[float] = []

    @asynccontextmanager
    async def timeout_spy(seconds: float) -> AsyncGenerator[None]:
        """Record the overall timeout while allowing the mocked chain to run immediately."""
        seen_timeouts.append(seconds)
        yield

    monkeypatch.setattr(artifact_download, "asyncio", SimpleNamespace(timeout=timeout_spy))
    first = respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(302, headers={"Location": "/stored"}))
    second = respx.get("https://gitlab.example.com/stored").mock(
        return_value=httpx.Response(302, headers={"Location": f"{TRUSTED_ARTIFACT_ORIGIN}/artifact"})
    )
    final = respx.get(f"{TRUSTED_ARTIFACT_ORIGIN}/artifact").mock(return_value=httpx.Response(200, content=b"ok"))

    # Act
    result = await client.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)

    # Assert
    assert result == (b"ok", False)
    assert first.called
    assert second.called
    assert final.called
    assert seen_timeouts == [client.settings.timeout]


@respx.mock
async def test_timeout_covers_stream_read_without_leaking_transport_details():
    """The total timeout interrupts a hanging body read and closes the response stream."""
    # Arrange
    settings = Settings(url="https://gitlab.example.com", token=AUTH_HEADER_VALUE, timeout=0.001)
    gitlab = GitLabClient(settings)
    stream = HangingStream()
    respx.get(ARTIFACT_URL).mock(return_value=httpx.Response(200, stream=stream))

    # Act / Assert
    with pytest.raises(ToolError, match="Could not download the artifact"):
        await gitlab.get_artifact_prefix(ARTIFACT_PATH, max_bytes=10)
    await gitlab.aclose()
    assert stream.closed == 1
