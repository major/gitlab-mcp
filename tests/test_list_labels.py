"""Tests for the compact list_labels tool, using a real in-memory MCP client 🏷️."""

import httpx
import respx
from fastmcp import Client

from gitlab_mcp.server import mcp

API = "https://gitlab.example.com/api/v4"
PROJECT = "group/nested project"
LABELS_URL = f"{API}/projects/group%2Fnested%20project/labels"


async def call_tool(search: str | None = None) -> dict:
    """Call list_labels through the MCP protocol and return its structured data."""
    async with Client(mcp) as client:
        arguments = {"project": PROJECT}
        if search is not None:
            arguments["search"] = search
        result = await client.call_tool("list_labels", arguments)
    return result.structured_content


@respx.mock
async def test_returns_name_only_labels_and_reports_more_pages(gitlab_env):
    """The tool returns only names, requests ancestor labels, quotes project paths, and reports more pages."""
    # Arrange
    route = respx.get(LABELS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "bug::new", "color": "#ff0000", "description": "extra data is omitted"},
                {"name": "group::shared", "color": "#00ff00"},
            ],
            headers={"X-Next-Page": "2"},
        )
    )

    # Act
    data = await call_tool()

    # Assert
    assert data == {"labels": "bug::new\ngroup::shared", "has_more": True}
    request = route.calls.last.request
    assert request.url.raw_path.startswith(b"/api/v4/projects/group%2Fnested%20project/labels?")
    assert dict(request.url.params) == {
        "include_ancestor_groups": "true",
        "per_page": "100",
        "page": "1",
    }


@respx.mock
async def test_forwards_search_and_reports_no_more_pages(gitlab_env):
    """A supplied search is forwarded and the last page reports has_more false."""
    # Arrange
    route = respx.get(LABELS_URL).mock(return_value=httpx.Response(200, json=[{"name": "bug"}]))

    # Act
    data = await call_tool("bug")

    # Assert
    assert data == {"labels": "bug", "has_more": False}
    assert dict(route.calls.last.request.url.params) == {
        "include_ancestor_groups": "true",
        "per_page": "100",
        "page": "1",
        "search": "bug",
    }


@respx.mock
async def test_returns_empty_names_for_an_empty_page(gitlab_env):
    """An empty label page returns an empty names string and no more-pages flag."""
    # Arrange
    respx.get(LABELS_URL).mock(return_value=httpx.Response(200, json=[]))

    # Act
    data = await call_tool()

    # Assert
    assert data == {"labels": "", "has_more": False}


async def test_tool_schema_and_annotations(gitlab_env):
    """The tool exposes project and search inputs and is marked read-only and open-world."""
    # Arrange / Act
    async with Client(mcp) as client:
        tools = await client.list_tools()
    tool = next(item for item in tools if item.name == "list_labels")

    # Assert
    assert set(tool.input_schema["properties"]) == {"project", "search"}
    assert tool.input_schema["required"] == ["project"]
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.open_world_hint is True
