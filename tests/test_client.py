"""Tests for the shared GitLab client: writes, error details, GraphQL mutations, and trace prefixes 🧪.

No MCP tool uses these methods yet, so the tests call the client directly.
The HTTP layer is still mocked with respx, so no real request leaves the test.
"""

import itertools
import json
import ssl
lazy from collections.abc import AsyncIterator, Iterable

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from gitlab_mcp.client import (
    GitLabAPIError,
    GitLabClient,
    GitLabConnectionError,
    GitLabGraphQLError,
    GitLabRedirectError,
)
from gitlab_mcp.config import Settings

API = "https://gitlab.example.com/api/v4"
GRAPHQL = "https://gitlab.example.com/api/graphql"
NOTES_PATH = "/projects/group%2Fproject/merge_requests/2187/notes"
NOTES_URL = f"{API}/projects/group%2Fproject/merge_requests/2187/notes"
DETAIL_CAP = 300
TRACE_PATH = "/projects/group%2Fproject/jobs/42/trace"
TRACE_URL = f"{API}/projects/group%2Fproject/jobs/42/trace"


@pytest.fixture
async def client(gitlab_env):
    """Build a real client for the fake instance and close it after the test."""
    gitlab = GitLabClient(Settings.from_env())
    yield gitlab
    await gitlab.aclose()


@pytest.mark.parametrize(
    ("method", "route_for"),
    [("POST", respx.post), ("PUT", respx.put), ("DELETE", respx.delete)],
)
@respx.mock
async def test_request_sends_json_body_and_query_params(client, method, route_for):
    """The JSON body and the keyword arguments reach GitLab, and the parsed body comes back."""
    # Arrange
    route = route_for(NOTES_URL).mock(return_value=httpx.Response(200, json={"id": 7}))

    # Act
    result = await client.request(method, NOTES_PATH, json={"body": "Looks good"}, sha="6096f2d5")

    # Assert
    sent = route.calls.last.request
    assert sent.method == method
    assert sent.url.params["sha"] == "6096f2d5"
    assert json.loads(sent.content) == {"body": "Looks good"}
    assert sent.headers["PRIVATE-TOKEN"] == "glpat-test"
    assert result == {"id": 7}


@pytest.mark.parametrize("status", [202, 204])
@respx.mock
async def test_request_returns_none_for_an_empty_body(client, status):
    """A 202 or 204 with no body gives None, so a DELETE needs no JSON."""
    # Arrange
    respx.delete(NOTES_URL).mock(return_value=httpx.Response(status))

    # Act
    result = await client.request("DELETE", NOTES_PATH)

    # Assert
    assert result is None


@respx.mock
async def test_request_without_a_body_sends_none(client):
    """Without `json`, the request has no body and no query string."""
    # Arrange
    route = respx.delete(NOTES_URL).mock(return_value=httpx.Response(204))

    # Act
    await client.request("DELETE", NOTES_PATH)

    # Assert
    sent = route.calls.last.request
    assert sent.content == b""
    assert sent.url.query == b""


@respx.mock
async def test_request_reports_a_connection_error(client):
    """An unreachable instance gives the same connection error as the read methods."""
    # Arrange
    respx.post(NOTES_URL).mock(side_effect=httpx.ConnectError("boom"))

    # Act / Assert
    with pytest.raises(GitLabConnectionError, match=r"Could not reach https://gitlab\.example\.com: boom"):
        await client.request("POST", NOTES_PATH, json={"body": "hi"})


@pytest.mark.parametrize(
    ("chain", "nested_error"),
    [
        pytest.param("cause", ssl.SSLCertVerificationError("untrusted certificate"), id="typed-cause"),
        pytest.param("context", ssl.SSLCertVerificationError("untrusted certificate"), id="typed-context"),
        pytest.param("cause", RuntimeError("certificate_verify_failed from proxy"), id="nested-text"),
    ],
)
def test_connection_error_adds_tls_guidance_for_nested_certificate_errors(chain, nested_error):
    """A certificate error anywhere in the exception chain adds actionable CA guidance."""
    # Arrange
    cause = RuntimeError("connection failed")
    setattr(cause, f"__{chain}__", nested_error)

    # Act
    error = GitLabConnectionError("https://gitlab.example.com", cause)

    # Assert
    assert isinstance(error, ToolError)
    assert "Could not reach https://gitlab.example.com: connection failed" in str(error)
    assert "certificate verification failed" in str(error).lower()
    assert "SSL_CERT_FILE" in str(error)
    assert "PEM CA bundle" in str(error)
    assert "trusts the GitLab instance certificate chain" in str(error)
    assert "restart MCP server" in str(error)


def test_connection_error_handles_cycles_in_exception_chain():
    """A certificate error in an exception chain with a cycle still adds guidance."""
    # Arrange
    cause = RuntimeError("connection failed")
    nested = ssl.SSLCertVerificationError("untrusted certificate")
    cause.__cause__ = nested
    nested.__context__ = cause

    # Act
    error = GitLabConnectionError("https://gitlab.example.com", cause)

    # Assert
    assert "SSL_CERT_FILE" in str(error)


def test_connection_error_ignores_noncertificate_cycles_in_exception_chain():
    """A non-certificate cause and context cycle terminates without adding TLS guidance."""
    # Arrange
    cause = RuntimeError("connection reset")
    context = RuntimeError("intermediate failure")
    cause.__cause__ = context
    context.__context__ = cause

    # Act
    error = GitLabConnectionError("https://gitlab.example.com", cause)

    # Assert
    assert str(error) == "Could not reach https://gitlab.example.com: connection reset"
    assert "SSL_CERT_FILE" not in str(error)


def test_connection_error_preserves_generic_network_error_text():
    """A non-certificate failure keeps the original connection error message unchanged."""
    # Arrange
    cause = RuntimeError("connection reset")

    # Act
    error = GitLabConnectionError("https://gitlab.example.com", cause)

    # Assert
    assert str(error) == "Could not reach https://gitlab.example.com: connection reset"


@pytest.mark.parametrize("path", ["rest", "graphql", "post", "trace-open", "trace-read"])
@respx.mock
async def test_tls_connection_errors_share_guidance_across_request_paths(client, path):
    """REST, GraphQL, write, and trace failures all report the TLS guidance as a connection error."""
    # Arrange
    network_error = httpx.ConnectError("CERTIFICATE_VERIFY_FAILED: untrusted certificate")
    if path == "rest":
        respx.get(f"{API}/x").mock(side_effect=network_error)
    elif path == "graphql":
        respx.post(GRAPHQL).mock(side_effect=network_error)
    elif path == "post":
        respx.post(NOTES_URL).mock(side_effect=network_error)
    elif path == "trace-open":
        respx.get(TRACE_URL).mock(side_effect=network_error)
    else:
        stream_error = httpx.ReadError("CERTIFICATE_VERIFY_FAILED: untrusted certificate")
        respx.get(TRACE_URL).mock(return_value=httpx.Response(200, stream=SpyStream([b"abc"], error=stream_error)))

    # Act / Assert
    if path == "rest":
        request = client.get("/x")
    elif path == "graphql":
        request = client.graphql("query { x }", {})
    elif path == "post":
        request = client.request("POST", NOTES_PATH, json={"body": "hi"})
    else:
        request = client.get_trace_prefix(TRACE_PATH, max_bytes=100)
    with pytest.raises(GitLabConnectionError) as info:
        await request
    assert "Could not reach https://gitlab.example.com:" in str(info.value)
    assert "certificate verification failed" in str(info.value).lower()
    assert "SSL_CERT_FILE" in str(info.value)


@pytest.mark.parametrize(
    ("body", "expected_message"),
    [
        ({"message": "Title is too long"}, "GitLab returned HTTP 400 for /x: Title is too long."),
        ({"error": "invalid_grant"}, "GitLab returned HTTP 400 for /x: invalid_grant."),
        ({"message": "First", "error": "Second"}, "GitLab returned HTTP 400 for /x: First."),
        (
            {"message": ["Source branch is missing", "Target is blank"]},
            "GitLab returned HTTP 400 for /x: Source branch is missing; Target is blank.",
        ),
        (
            {"message": {"title": ["is too long", "is blank"], "base": "invalid"}},
            "GitLab returned HTTP 400 for /x: title: is too long; is blank; base: invalid.",
        ),
        ({"message": "Blocked by a rule..."}, "GitLab returned HTTP 400 for /x: Blocked by a rule."),
        ({"message": ""}, "GitLab returned HTTP 400 for /x."),  # Empty detail is left out.
        ({"other": "no known key"}, "GitLab returned HTTP 400 for /x."),
        (["a", "list", "body"], "GitLab returned HTTP 400 for /x."),  # A JSON list is not an error object.
    ],
    ids=[
        "message-string",
        "error-key",
        "message-wins-over-error",
        "message-list",
        "message-dict-with-lists",
        "trailing-dots-removed",
        "empty-message",
        "unknown-key",
        "json-list-body",
    ],
)
@respx.mock
async def test_error_detail_comes_from_the_json_body(client, body, expected_message):
    """GitLab's `message` or `error` text goes into the error, and other shapes give no detail."""
    # Arrange
    respx.get(f"{API}/x").mock(return_value=httpx.Response(400, json=body))

    # Act / Assert
    with pytest.raises(GitLabAPIError) as info:
        await client.get("/x")
    assert str(info.value) == expected_message


@respx.mock
async def test_non_json_error_body_gives_no_detail(client):
    """An HTML error page from a proxy does not crash the client, and gives no detail."""
    # Arrange
    respx.get(f"{API}/x").mock(return_value=httpx.Response(502, text="<html>Bad Gateway</html>"))

    # Act / Assert
    with pytest.raises(GitLabAPIError) as info:
        await client.get("/x")
    assert info.value.detail == ""
    assert str(info.value) == "GitLab returned HTTP 502 for /x."


@respx.mock
async def test_detail_is_capped_and_exposed_on_the_error(client):
    """A long detail is cut to 300 characters, and the error keeps it on `.detail`."""
    # Arrange
    respx.get(f"{API}/x").mock(return_value=httpx.Response(400, json={"message": "x" * 500}))

    # Act / Assert
    with pytest.raises(GitLabAPIError) as info:
        await client.get("/x")
    assert info.value.detail == "x" * DETAIL_CAP
    assert str(info.value) == f"GitLab returned HTTP 400 for /x: {'x' * DETAIL_CAP}."


@pytest.mark.parametrize(
    ("status", "hint"),
    [
        (405, "GitLab refused this action in the current MR state."),
        (406, "The merge request cannot be merged right now."),
        (409, "Conflict, for example the SHA does not match the MR head."),
        (422, "GitLab rejected the request as invalid."),
    ],
)
@respx.mock
async def test_write_status_codes_have_hints(client, status, hint):
    """The status codes that write tools hit most often get a hint after the detail."""
    # Arrange
    respx.post(NOTES_URL).mock(return_value=httpx.Response(status, json={"message": "Nope"}))

    # Act / Assert
    with pytest.raises(GitLabAPIError) as info:
        await client.request("POST", NOTES_PATH, json={"body": "hi"})
    assert str(info.value) == f"GitLab returned HTTP {status} for {NOTES_PATH}: Nope. {hint}"


@respx.mock
async def test_graphql_http_error_includes_detail(client):
    """An HTTP error on the GraphQL endpoint shows GitLab's detail and the full URL."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(500, json={"message": "Database is busy"}))

    # Act / Assert
    with pytest.raises(GitLabAPIError) as info:
        await client.graphql("query { x }", {})
    assert str(info.value) == f"GitLab returned HTTP 500 for {GRAPHQL}: Database is busy."


@respx.mock
async def test_mutate_returns_the_payload_on_success(client):
    """A mutation with no payload errors returns the payload, and sends the variables."""
    # Arrange
    payload = {"note": {"id": "gid://gitlab/Note/9", "body": "Looks good"}, "errors": []}
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"createNote": payload}}))

    # Act
    result = await client.mutate("mutation($body: String!) { createNote }", {"body": "Looks good"}, "createNote")

    # Assert
    assert result == payload
    assert json.loads(route.calls.last.request.content)["variables"] == {"body": "Looks good"}


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        ({"note": None, "errors": ["Body is too long"]}, "GitLab GraphQL error: Body is too long"),
        ({"errors": ["Body is blank", "Iid is invalid"]}, "GitLab GraphQL error: Body is blank; Iid is invalid"),
    ],
    ids=["one-error", "many-errors"],
)
@respx.mock
async def test_mutate_raises_payload_errors(client, payload, expected_message):
    """Errors inside `data.<field>.errors` raise, even when the top-level `errors` list is empty."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"createNote": payload}}))

    # Act / Assert
    with pytest.raises(GitLabGraphQLError) as info:
        await client.mutate("mutation { createNote }", {}, "createNote")
    assert str(info.value) == expected_message


@respx.mock
async def test_mutate_raises_when_the_payload_is_null(client):
    """A null payload means GitLab refused the mutation, so it is an error and not an empty success."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"createNote": None}}))

    # Act / Assert
    with pytest.raises(GitLabGraphQLError, match="createNote returned no payload"):
        await client.mutate("mutation { createNote }", {}, "createNote")


@respx.mock
async def test_mutate_raises_top_level_graphql_errors_first(client):
    """Top-level GraphQL errors raise before the payload is read, as in `graphql`."""
    # Arrange
    body = {"data": None, "errors": [{"message": "Not authorized"}]}
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=body))

    # Act / Assert
    with pytest.raises(GitLabGraphQLError, match="GitLab GraphQL error: Not authorized"):
        await client.mutate("mutation { createNote }", {}, "createNote")


def test_api_error_defaults_to_no_detail():
    """Callers that have no body can leave out the detail, and the error is still a ToolError."""
    # Arrange / Act
    error = GitLabAPIError(404, "/x")

    # Assert
    assert isinstance(error, ToolError)
    assert error.detail == ""
    assert str(error) == "GitLab returned HTTP 404 for /x. Not found, or the token cannot see it."


class SpyStream(httpx.AsyncByteStream):
    """A fake response body. It yields `chunks`, then raises `error` if one is set, and counts pulls and closes."""

    def __init__(self, chunks: Iterable[bytes], error: Exception | None = None) -> None:
        """Keep the chunks to yield and the error to raise once they run out."""
        self.chunks = iter(chunks)
        self.error = error
        self.pulled = 0
        self.closed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield each chunk and count it, then raise the error if there is one."""
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self) -> None:
        """Count the close, so tests can prove the stream was always released."""
        self.closed += 1


@pytest.mark.parametrize(
    ("chunks", "max_bytes", "expected"),
    [
        pytest.param([b"0123456789"], 4, (b"0123", True), id="extra-byte-means-truncated"),
        pytest.param([b"0123"], 4, (b"0123", False), id="exactly-the-limit-is-not-truncated"),
        pytest.param([b"01"], 4, (b"01", False), id="shorter-than-the-limit"),
        pytest.param([], 4, (b"", False), id="empty-body"),
        pytest.param([b"he", b"llo", b" world"], 5, (b"hello", True), id="small-chunks-are-joined"),
    ],
)
@respx.mock
async def test_trace_prefix_keeps_the_first_bytes_and_flags_truncation(client, chunks, max_bytes, expected):
    """The prefix has at most `max_bytes` bytes, and `truncated` is True only when there is more."""
    # Arrange
    stream = SpyStream(chunks)
    respx.get(TRACE_URL).mock(return_value=httpx.Response(200, stream=stream))

    # Act
    result = await client.get_trace_prefix(TRACE_PATH, max_bytes=max_bytes)

    # Assert
    assert result == expected
    assert stream.closed == 1


@respx.mock
async def test_trace_prefix_stops_reading_a_huge_body_early(client):
    """A body that never ends is cut off after the first piece, so the rest is never read."""
    # Arrange
    stream = SpyStream(itertools.repeat(b"x" * 100_000))
    respx.get(TRACE_URL).mock(return_value=httpx.Response(200, stream=stream))

    # Act
    result = await client.get_trace_prefix(TRACE_PATH, max_bytes=10)

    # Assert
    assert result == (b"x" * 10, True)
    assert stream.pulled == 1
    assert stream.closed == 1


@respx.mock
async def test_trace_prefix_keeps_utf8_bytes_unchanged_even_mid_character(client):
    """The prefix is raw bytes, so a cut in the middle of a UTF-8 character is kept as it is."""
    # Arrange
    body = "caf\u00e9 \u2713".encode()  # The "é" is two bytes, so byte 4 is half of it.
    respx.get(TRACE_URL).mock(return_value=httpx.Response(200, stream=SpyStream([body])))

    # Act
    result = await client.get_trace_prefix(TRACE_PATH, max_bytes=4)

    # Assert
    assert result == (body[:4], True)
    assert result[0] == b"caf\xc3"


@respx.mock
async def test_trace_prefix_asks_for_plain_text_with_the_token(client):
    """The request asks for `text/plain` and still sends the private token."""
    # Arrange
    route = respx.get(TRACE_URL).mock(return_value=httpx.Response(200, stream=SpyStream([b"log"])))

    # Act
    await client.get_trace_prefix(TRACE_PATH, max_bytes=100)

    # Assert
    sent = route.calls.last.request
    assert sent.headers["Accept"] == "text/plain"
    assert sent.headers["PRIVATE-TOKEN"] == "glpat-test"


@pytest.mark.parametrize(
    ("status", "body", "hint"),
    [
        (403, {"message": "403 Forbidden"}, "The token does not have the scope or permission for this request."),
        (404, {"message": "404 Not Found"}, "Not found, or the token cannot see it."),
    ],
    ids=["forbidden", "not-found"],
)
@respx.mock
async def test_trace_prefix_error_status_raises_with_detail_and_hint(client, status, body, hint):
    """A 403 or 404 raises the usual API error, with GitLab's detail and a friendly hint."""
    # Arrange
    stream = SpyStream([json.dumps(body).encode()])
    respx.get(TRACE_URL).mock(return_value=httpx.Response(status, stream=stream))

    # Act / Assert
    with pytest.raises(GitLabAPIError) as info:
        await client.get_trace_prefix(TRACE_PATH, max_bytes=100)
    assert str(info.value) == f"GitLab returned HTTP {status} for {TRACE_PATH}: {body['message']}. {hint}"
    assert stream.closed == 1


@respx.mock
async def test_trace_prefix_error_body_is_capped_and_not_leaked(client):
    """A huge HTML error page is read only up to the cap, and its markup stays out of the error."""
    # Arrange
    stream = SpyStream(itertools.repeat(b"<html>" + b"x" * 100_000))
    respx.get(TRACE_URL).mock(return_value=httpx.Response(500, stream=stream))

    # Act / Assert
    with pytest.raises(GitLabAPIError) as info:
        await client.get_trace_prefix(TRACE_PATH, max_bytes=100)
    assert str(info.value) == f"GitLab returned HTTP 500 for {TRACE_PATH}."
    assert stream.pulled == 1
    assert stream.closed == 1


async def test_trace_prefix_does_not_follow_redirects(client):
    """A redirect is an error, and the new location is never requested, so the token stays home."""
    # Arrange
    # The explicit router lets the "elsewhere" route stay uncalled without failing the test.
    with respx.mock(assert_all_called=False) as router:
        router.get(TRACE_URL).mock(
            return_value=httpx.Response(302, headers={"Location": "https://attacker.example.net/trace"}),
        )
        elsewhere = router.get("https://attacker.example.net/trace").mock(
            return_value=httpx.Response(200, text="secret"),
        )

        # Act / Assert
        with pytest.raises(GitLabRedirectError, match="redirect"):
            await client.get_trace_prefix(TRACE_PATH, max_bytes=100)
        assert not elsewhere.called


@respx.mock
async def test_trace_prefix_reports_a_connection_error_on_open(client):
    """An unreachable instance gives the same connection error as the other methods."""
    # Arrange
    respx.get(TRACE_URL).mock(side_effect=httpx.ConnectError("boom"))

    # Act / Assert
    with pytest.raises(GitLabConnectionError, match=r"Could not reach https://gitlab\.example\.com: boom"):
        await client.get_trace_prefix(TRACE_PATH, max_bytes=100)


@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        pytest.param(httpx.ReadError("reset"), "Could not reach https://gitlab.example.com: reset", id="read-error"),
        pytest.param(
            httpx.ReadTimeout("slow"),
            "Could not reach https://gitlab.example.com: slow",
            id="httpx-read-timeout",
        ),
        pytest.param(
            TimeoutError(),
            "Could not reach https://gitlab.example.com: timed out after 30.0 seconds",
            id="deadline-timeout",
        ),
    ],
)
@respx.mock
async def test_trace_prefix_failure_while_reading_is_a_connection_error_and_closes(client, error, expected_message):
    """A network failure or timeout in the middle of the body becomes a connection error, and the stream closes."""
    # Arrange
    stream = SpyStream([b"abc"], error=error)
    respx.get(TRACE_URL).mock(return_value=httpx.Response(200, stream=stream))

    # Act / Assert
    with pytest.raises(GitLabConnectionError) as info:
        await client.get_trace_prefix(TRACE_PATH, max_bytes=100)
    assert str(info.value) == expected_message
    assert stream.closed == 1


@pytest.mark.parametrize("max_bytes", [0, -1])
@respx.mock
async def test_trace_prefix_rejects_a_limit_that_is_not_positive(client, max_bytes):
    """A zero or negative limit is a programming error, and no request is sent."""
    # Arrange
    route = respx.get(TRACE_URL).mock(return_value=httpx.Response(200, stream=SpyStream([b"log"])))

    # Act / Assert
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        await client.get_trace_prefix(TRACE_PATH, max_bytes=max_bytes)
    assert not route.called
