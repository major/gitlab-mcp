"""Tests for the get_current_user tool, run through a real in-memory MCP client 🧪."""

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import mcp

API = "https://gitlab.example.com/api/v4"

USER = {
    "id": 42,
    "username": "jdoe",
    "name": "Jane Doe",
    "state": "active",
    "avatar_url": "https://example.com/a.png",  # extra noise we should drop
}


@pytest.fixture
def gitlab_env(monkeypatch):
    """Point the server at a fake self-managed GitLab instance."""
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com/api/v4/")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")


async def call_tool() -> dict:
    """Call get_current_user through the MCP protocol and return its data."""
    async with Client(mcp) as client:
        result = await client.call_tool("get_current_user", {})
    return result.structured_content


@respx.mock
async def test_returns_only_username_and_id(gitlab_env):
    """The tool sends the token and returns only username and id."""
    # Arrange
    route = respx.get(f"{API}/user").mock(return_value=httpx.Response(200, json=USER))

    # Act
    data = await call_tool()

    # Assert
    assert route.calls.last.request.headers["PRIVATE-TOKEN"] == "glpat-test"
    assert data == {"username": "jdoe", "id": 42}


@pytest.mark.parametrize(
    ("status", "hint"),
    [(401, "invalid, expired, or revoked"), (403, "scope or permission")],
)
@respx.mock
async def test_api_errors_become_helpful_tool_errors(gitlab_env, status, hint):
    """HTTP errors become ToolErrors with a hint."""
    # Arrange
    respx.get(f"{API}/user").mock(return_value=httpx.Response(status))

    # Act / Assert
    with pytest.raises(ToolError, match=hint):
        await call_tool()


async def test_missing_token_is_reported_by_the_tool(monkeypatch):
    """A missing token is reported by the tool, not a server crash."""
    # Arrange
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)

    # Act / Assert
    with pytest.raises(ToolError, match="GITLAB_TOKEN is not set"):
        await call_tool()


@respx.mock
async def test_network_failure_names_the_instance(gitlab_env):
    """A connection error says which instance could not be reached."""
    # Arrange
    respx.get(f"{API}/user").mock(side_effect=httpx.ConnectError("boom"))

    # Act / Assert
    with pytest.raises(ToolError, match=r"Could not reach https://gitlab\.example\.com: boom"):
        await call_tool()
