"""Behavioral coverage for owned and borrowed in-memory MCP clients 🧪."""

from typing import Any
lazy from collections.abc import AsyncIterator

import pytest
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
lazy from mcp.types import TextContent

from mcp_helpers import call_tool, call_tool_result

EXPECTED_ERROR = "expected failure"


@pytest.fixture
def counting_app() -> tuple[FastMCP, list[str]]:
    """Build a real MCP app with observable, lifespan-local state."""
    events: list[str] = []

    @lifespan
    async def record_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        """Record lifecycle boundaries and reset the counter for each lifespan."""
        events.append("open")
        try:
            yield {"calls": []}
        finally:
            events.append("close")

    app = FastMCP("helper-tests", lifespan=record_lifespan)

    @app.tool
    def count_calls(ctx: Context, *, fail: bool = False) -> dict[str, int]:
        """Count successful calls in this lifespan or return a known tool error."""
        if fail:
            raise ToolError(EXPECTED_ERROR)
        calls = ctx.lifespan_context["calls"]
        calls.append(None)
        return {"calls": len(calls)}

    return app, events


async def test_default_helpers_own_separate_lifespans(counting_app):
    """Both helpers close their own clients and keep subsequent calls isolated."""
    # Arrange
    app, events = counting_app

    # Act
    structured = await call_tool("count_calls", app=app)
    raw = await call_tool_result("count_calls", {}, app=app)

    # Assert
    assert structured == raw.structured_content == {"calls": 1}
    assert raw.is_error is False
    assert events == ["open", "close", "open", "close"]


async def test_default_helpers_preserve_tool_error_handling(counting_app):
    """Tool errors raise by default or remain available as raw protocol results."""
    # Arrange
    app, events = counting_app

    # Act
    with pytest.raises(ToolError, match="expected failure"):
        await call_tool("count_calls", {"fail": True}, app=app)
    raw = await call_tool_result("count_calls", {"fail": True}, app=app, raise_on_error=False)

    # Assert
    assert raw.is_error is True
    assert isinstance(raw.content[0], TextContent)
    assert raw.content[0].text == "expected failure"
    assert events == ["open", "close", "open", "close"]
