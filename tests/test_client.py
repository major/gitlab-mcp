"""Tests for the shared GitLab client: writes, error details, and GraphQL mutations 🧪.

No MCP tool uses these methods yet, so the tests call the client directly.
The HTTP layer is still mocked with respx, so no real request leaves the test.
"""

import json

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError, GitLabGraphQLError
from gitlab_mcp.config import Settings

API = "https://gitlab.example.com/api/v4"
GRAPHQL = "https://gitlab.example.com/api/graphql"
NOTES_PATH = "/projects/group%2Fproject/merge_requests/2187/notes"
NOTES_URL = f"{API}/projects/group%2Fproject/merge_requests/2187/notes"
DETAIL_CAP = 300


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
