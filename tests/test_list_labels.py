"""Tests for the compact list_labels tool, using a real in-memory MCP client 🏷️."""

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import MAX_PAGES, mcp

API = "https://gitlab.example.com/api/v4"
PROJECT = "group/nested project"
LABELS_URL = f"{API}/projects/group%2Fnested%20project/labels"


async def call_tool(search: str | None = None, names: list[str] | None = None) -> dict:
    """Call list_labels through the MCP protocol and return its structured data."""
    async with Client(mcp) as client:
        arguments = {"project": PROJECT}
        if search is not None:
            arguments["search"] = search
        if names is not None:
            arguments["names"] = names
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
    assert data == {"has_more": False}


async def test_tool_schema_and_annotations(gitlab_env):
    """The tool exposes project, search, and names inputs and is marked read-only and open-world."""
    # Arrange / Act
    async with Client(mcp) as client:
        tools = await client.list_tools()
    tool = next(item for item in tools if item.name == "list_labels")

    # Assert
    assert set(tool.input_schema["properties"]) == {"project", "search", "names"}
    assert tool.input_schema["required"] == ["project"]
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.open_world_hint is True


@respx.mock
async def test_names_check_stops_early_when_every_name_is_found(gitlab_env):
    """When page 1 has every requested name, the scan stops there and reports no missing names."""
    # Arrange
    route = respx.get(LABELS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[{"name": "bug"}, {"name": "group::shared"}, {"name": "extra"}],
            headers={"X-Next-Page": "2"},
        )
    )

    # Act
    data = await call_tool(names=["group::shared", "bug"])

    # Assert
    assert data == {"has_more": False, "existing": ["group::shared", "bug"]}
    assert route.call_count == 1
    assert dict(route.calls.last.request.url.params) == {
        "include_ancestor_groups": "true",
        "per_page": "100",
        "page": "1",
    }


@respx.mock
async def test_names_check_reads_pages_until_missing_names_are_settled(gitlab_env):
    """Names are matched exactly, duplicates are dropped, and a missing name is settled at the last page."""
    # Arrange
    route = respx.get(LABELS_URL).mock(
        side_effect=[
            httpx.Response(200, json=[{"name": "bug"}, {"name": "other"}], headers={"X-Next-Page": "2"}),
            httpx.Response(200, json=[{"name": "Urgent"}, {"name": "group::shared"}]),
        ]
    )

    # Act
    data = await call_tool(names=["urgent", "bug", "missing", "bug", "Urgent"])

    # Assert
    assert data == {"has_more": False, "existing": ["bug", "Urgent"], "missing": ["urgent", "missing"]}
    assert [int(call.request.url.params["page"]) for call in route.calls] == [1, 2]


@respx.mock
async def test_names_check_reports_more_when_the_page_cap_leaves_names_missing(gitlab_env):
    """When MAX_PAGES pages are read and a name is still missing, the result says the missing list may be incomplete."""

    # Arrange
    def serve_page(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"name": f"filler-{page}"}], headers={"X-Next-Page": str(page + 1)})

    route = respx.get(LABELS_URL).mock(side_effect=serve_page)

    # Act
    data = await call_tool(names=["wanted"])

    # Assert
    assert data == {"has_more": True, "missing": ["wanted"]}
    assert route.call_count == MAX_PAGES


@pytest.mark.parametrize(
    "arguments",
    [
        {"search": "bug", "names": ["bug"]},
        {"search": "", "names": []},
    ],
)
@respx.mock
async def test_names_and_search_together_are_rejected(gitlab_env, arguments):
    """Passing both search and names is an error, and no label request is sent."""
    # Arrange
    route = respx.get(LABELS_URL).mock(return_value=httpx.Response(200, json=[]))

    # Act / Assert
    with pytest.raises(ToolError, match="either search or names"):
        await call_tool(**arguments)
    assert route.call_count == 0
