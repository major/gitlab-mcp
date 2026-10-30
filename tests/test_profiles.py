"""Contracts for isolated compact tool profiles."""

import json
import re

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

import gitlab_mcp
from mcp_helpers import call_tool


async def test_compact_inventory_and_schema_budget(gitlab_env):
    """Compact isolates its catalog and keeps landing guidance within the full schema budget."""
    # Arrange / Act
    compact = gitlab_mcp.create_app("compact")
    async with Client(compact) as client:
        short = {tool.name: tool for tool in await client.list_tools()}
    async with Client(gitlab_mcp.create_app()) as client:
        full = {tool.name: tool for tool in await client.list_tools()}
    # Assert
    assert set(short) == set(full) - {
        "get_snippet",
        "get_snippet_file",
        "get_job",
        "get_job_artifact",
        "list_job_artifacts",
        "land_merge_request",
        "reply_to_thread",
        "create_pipeline",
        "create_merge_request_pipeline",
    } | {"read_snippet", "inspect_job", "start_pipeline"}
    assert short["read_snippet"].annotations.read_only_hint is True
    assert short["read_snippet"].annotations.open_world_hint is True
    size = lambda tool: len(json.dumps([tool.description, tool.input_schema]).encode())  # noqa: E731
    assert size(short["read_snippet"]) < sum(size(full[name]) for name in ("get_snippet", "get_snippet_file"))
    assert len(full) == 44  # noqa: PLR2004
    assert len(short) == 38  # noqa: PLR2004
    start = short["start_pipeline"]
    assert start.annotations.read_only_hint is False
    assert start.annotations.destructive_hint is False
    assert start.annotations.idempotent_hint is False
    assert start.annotations.open_world_hint is True
    assert start.output_schema == full["create_pipeline"].output_schema
    assert size(start) < sum(size(full[name]) for name in ("create_pipeline", "create_merge_request_pipeline"))
    for tool in short.values():
        text = json.dumps([tool.description, tool.input_schema, tool.output_schema])
        assert not re.search(r"\b(?:create_pipeline|create_merge_request_pipeline)\b", text)
    assert not re.search(r"\b(?:create_pipeline|create_merge_request_pipeline)\b", compact.instructions or "")
    batch = short["reply_to_threads"]
    assert batch.annotations.read_only_hint is False
    assert batch.annotations.destructive_hint is False
    assert batch.annotations.idempotent_hint is False
    assert size(batch) < sum(size(full[name]) for name in ("reply_to_thread", "reply_to_threads"))
    assert "resolve_open_threads" in short
    assert not re.search(r"\breply_to_thread\b", compact.instructions or "")
    assert not re.search(r"\breply_to_thread\b", batch.description)
    assert "reply_to_threads" in (compact.instructions or "")
    assert "reply_to_thread(resolve=true)" in (gitlab_mcp.mcp.instructions or "")
    for text in ("one-item list", "APPROVE", "resolve=true", "resolve=false", "note_id", "repost"):
        assert text in batch.description
    landing = short["land_merge_requests"]
    assert landing.annotations.read_only_hint is False
    assert landing.annotations.destructive_hint is True
    assert landing.annotations.idempotent_hint is False
    assert landing.annotations.open_world_hint is True
    assert size(landing) < sum(size(full[name]) for name in ("land_merge_request", "land_merge_requests"))
    description = landing.description
    for text in (
        "one-item list",
        "merge train",
        "checks pass",
        "explicit",
        "bypass",
        "false",
        "head has moved",
        "landing.result",
        "pending",
        "non-atomic",
        "later items",
        "without checking GitLab",
        "unknown",
    ):
        assert text in description
    assert "land_merge_request`" not in description
    assert "land_merge_request" not in (compact.instructions or "")
    assert gitlab_mcp.create_app() is gitlab_mcp.mcp
    assert gitlab_mcp.create_app("compact") is not compact


@pytest.mark.parametrize(
    ("arguments", "endpoint", "response", "expected"),
    [
        (
            {},
            "/snippets/42",
            {
                "id": 42,
                "title": "Example",
                "files": [{"path": "a.txt"}, {"path": "b.txt", "raw_url": "https://example.com/raw"}],
            },
            {"id": 42, "title": "Example"},
        ),
        ({"view": "file"}, "/snippets/42/raw", "hello", {"content": "hello"}),
        (
            {"view": "file", "project": "group/project", "file_path": "a/b", "ref": "main"},
            "/projects/group%2Fproject/snippets/42/files/main/a%2Fb/raw",
            "hello",
            {"content": "hello"},
        ),
    ],
)
async def test_read_routing(gitlab_env, arguments, endpoint, response, expected):
    """Both views preserve the delegated endpoint and response shape."""
    # Arrange
    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://gitlab.example.com/api/v4" + endpoint).mock(
            return_value=httpx.Response(200, json=response)
            if isinstance(response, dict)
            else httpx.Response(200, text=response)
        )
        # Act
        result = await call_tool("read_snippet", {"snippet_id": 42, **arguments}, app=gitlab_mcp.create_app("compact"))
        full_arguments = {key: value for key, value in arguments.items() if key != "view"}
        full_name = "get_snippet_file" if arguments.get("view") == "file" else "get_snippet"
        full_result = await call_tool(full_name, {"snippet_id": 42, **full_arguments})
    # Assert
    assert all(result[key] == value for key, value in expected.items())
    assert result == full_result
    assert route.call_count == 2  # noqa: PLR2004


@pytest.mark.parametrize(
    "arguments",
    [
        {"file_path": "a"},
        {"ref": "main"},
        {"view": "file", "file_path": "a"},
        {"view": "file", "ref": "main"},
        {"view": "other"},
        {"snippet_id": "42"},
    ],
)
async def test_invalid_arguments_do_not_request(gitlab_env, arguments):
    """Invalid selectors, modes, and IDs fail before any GitLab request."""
    # Arrange / Act / Assert
    with respx.mock(assert_all_called=False) as router, pytest.raises(ToolError):
        await call_tool("read_snippet", {"snippet_id": 42, **arguments}, app=gitlab_mcp.create_app("compact"))
    assert not router.calls


async def test_hidden_tools_and_pipeline_validation(gitlab_env):
    """Hidden calls fail and pipeline validation does not expose secret values."""
    # Arrange
    app = gitlab_mcp.create_app("compact")
    with respx.mock(assert_all_called=False) as router:
        # Act / Assert
        for name in ("get_snippet", "get_snippet_file"):
            with pytest.raises(ToolError):
                await call_tool(name, {"snippet_id": 42}, app=app)
        with pytest.raises(ToolError, match="Unknown tool"):
            await call_tool("land_merge_request", {"project": "group/project", "iid": 42}, app=app)
        with pytest.raises(ToolError, match="Unknown tool"):
            await call_tool("reply_to_thread", {"project": "group/project", "iid": 42, "thread_id": "x"}, app=app)
        with pytest.raises(ToolError) as error:
            await call_tool(
                "start_pipeline", {"variables": [{"key": "x", "value": {"secret": "private-value"}}]}, app=app
            )
        assert "private-value" not in str(error.value)
        assert "target" in str(error.value)
        assert not router.calls


def test_profile_selection_and_main(monkeypatch):
    """Startup defaults to full and rejects unknown or empty profiles."""
    # Arrange
    calls = []
    monkeypatch.setattr(type(gitlab_mcp.mcp), "run", lambda self: calls.append(self))  # noqa: PLW0108
    monkeypatch.delenv("GITLAB_MCP_PROFILE", raising=False)
    # Act / Assert
    gitlab_mcp.main()
    assert calls == [gitlab_mcp.mcp]
    monkeypatch.setenv("GITLAB_MCP_PROFILE", "compact")
    gitlab_mcp.main()
    assert len(calls) == 2  # noqa: PLR2004
    assert calls[1] is not gitlab_mcp.mcp
    for profile in ("", "other"):
        with pytest.raises(ValueError, match="Unknown GitLab MCP profile"):
            gitlab_mcp.create_app(profile)
        monkeypatch.setenv("GITLAB_MCP_PROFILE", profile)
        with pytest.raises(ValueError, match="Unknown GitLab MCP profile"):
            gitlab_mcp.main()


async def test_missing_config_is_graceful(monkeypatch):
    """Compact startup survives missing GitLab credentials and reports them on use."""
    # Arrange
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool("read_snippet", {"snippet_id": 42}, app=gitlab_mcp.create_app("compact"))
