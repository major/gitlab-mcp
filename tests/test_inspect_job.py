"""Contracts for compact job inspection."""

import json

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

import gitlab_mcp
from mcp_helpers import call_tool

pytestmark = pytest.mark.usefixtures("gitlab_env")
BASE = "https://gitlab.example.com/api/v4/projects/group%2Fproject/jobs/42"
ARGS = {"project": "group/project", "job_id": 42}
HIDDEN = ("get_job", "get_job_artifact", "list_job_artifacts")


async def test_catalog():
    """Compact exposes one smaller read-only tool without changing full."""
    # Arrange / Act
    compact = gitlab_mcp.create_app("compact")
    async with Client(compact) as client:
        short = {tool.name: tool for tool in await client.list_tools()}
        for name in HIDDEN:
            with pytest.raises(ToolError):
                await client.call_tool(name, ARGS)
    async with Client(gitlab_mcp.create_app()) as client:
        full = {tool.name: tool for tool in await client.list_tools()}
    # Assert
    assert len(full) == 44
    assert len(short) == 41
    assert all(name in full and name not in short for name in HIDDEN)
    assert "inspect_job" not in full
    assert short["inspect_job"].annotations.read_only_hint is True
    assert short["inspect_job"].annotations.open_world_hint is True
    size = lambda tool: len(json.dumps([tool.description, tool.input_schema]).encode())  # noqa: E731
    assert size(short["inspect_job"]) < sum(size(full[name]) for name in HIDDEN)
    assert "use get_job for runner-job metadata" in gitlab_mcp.mcp.instructions
    assert "inspect_job(view='metadata')" in compact.instructions
    assert "use get_job for runner-job metadata" not in compact.instructions


@pytest.mark.parametrize(
    ("arguments", "full_name", "full_arguments", "suffix", "response"),
    [
        ({}, "get_job", {}, "", {"id": 42, "status": "success"}),
        (
            {"view": "artifact", "path": "report.txt"},
            "get_job_artifact",
            {"artifact_path": "report.txt"},
            "/artifacts/report.txt",
            "hello",
        ),
        (
            {"view": "artifact", "path": "report.txt", "max_bytes": 3},
            "get_job_artifact",
            {"artifact_path": "report.txt", "max_bytes": 3},
            "/artifacts/report.txt",
            "hello",
        ),
        ({"view": "artifact_tree"}, "list_job_artifacts", {}, "/artifacts/tree", []),
        (
            {"view": "artifact_tree", "path": "folder", "recursive": True, "limit": 7},
            "list_job_artifacts",
            {"path": "folder", "recursive": True, "limit": 7},
            "/artifacts/tree",
            [{"path": "folder/a", "type": "file", "size": 7}],
        ),
    ],
)
async def test_parity(arguments, full_name, full_arguments, suffix, response):
    """All views preserve full responses and request paths."""
    # Arrange
    with respx.mock(assert_all_called=True) as router:
        route = router.get(BASE + suffix).mock(
            return_value=httpx.Response(200, text=response)
            if isinstance(response, str)
            else httpx.Response(200, json=response)
        )
        # Act
        compact = await call_tool("inspect_job", ARGS | arguments, app=gitlab_mcp.create_app("compact"))
        full = await call_tool(full_name, ARGS | full_arguments)
    # Assert
    assert compact == full
    assert route.call_count == 2
    assert route.calls[0].request.url == route.calls[1].request.url


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "x"},
        {"max_bytes": 1},
        {"recursive": False},
        {"limit": 1},
        {"cursor": "x"},
        {"view": "artifact"},
        {"view": "artifact", "path": ""},
        {"view": "artifact", "path": "../x"},
        {"view": "artifact", "path": "x", "recursive": False},
        {"view": "artifact", "path": "x", "limit": 1},
        {"view": "artifact", "path": "x", "cursor": "x"},
        {"view": "artifact_tree", "max_bytes": 1},
        {"view": "artifact", "path": "x", "max_bytes": 0},
        {"view": "artifact", "path": "x", "max_bytes": 65537},
        {"view": "artifact", "path": "x", "max_bytes": "3"},
        {"view": "artifact_tree", "recursive": 0},
        {"view": "artifact_tree", "limit": 0},
        {"view": "artifact_tree", "limit": 101},
        {"view": "artifact_tree", "limit": "2"},
        {"view": "artifact_tree", "path": "x" * 1025},
        {"view": "other"},
    ],
)
async def test_invalid_arguments(arguments):
    """Reject invalid mode selectors and bounds without requests."""
    # Arrange / Act / Assert
    with respx.mock(assert_all_called=False) as router, pytest.raises(ToolError):
        await call_tool("inspect_job", ARGS | arguments, app=gitlab_mcp.create_app("compact"))
    assert not router.calls


async def test_tree_not_found_guidance():
    """Tree 404 guidance points to the visible compact artifact view."""
    # Arrange
    with respx.mock:
        respx.get(BASE + "/artifacts/tree").mock(return_value=httpx.Response(404))
        # Act / Assert
        with pytest.raises(ToolError) as error:
            await call_tool("inspect_job", ARGS | {"view": "artifact_tree"}, app=gitlab_mcp.create_app("compact"))
    assert "inspect_job(view='artifact')" in str(error.value)
    assert "get_job_artifact" not in str(error.value)


async def test_tree_pagination():
    """Resume using a cursor bound to the original tree arguments."""
    # Arrange
    app = gitlab_mcp.create_app("compact")
    args = ARGS | {"view": "artifact_tree", "path": "folder", "recursive": True, "limit": 1}
    with respx.mock(assert_all_called=True) as router:
        route = router.get(BASE + "/artifacts/tree").mock(
            side_effect=[httpx.Response(200, json=[], headers={"X-Next-Page": "2"}), httpx.Response(200, json=[])]
        )
        # Act
        first = await call_tool("inspect_job", args, app=app)
        await call_tool("inspect_job", args | {"cursor": first["next_cursor"]}, app=app)
        # Assert
        assert route.calls[1].request.url.params["page"] == "2"
        with pytest.raises(ToolError):
            await call_tool("inspect_job", args | {"limit": 2, "cursor": first["next_cursor"]}, app=app)
        assert route.call_count == 2
