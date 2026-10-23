"""Shared helpers that call MCP tools through a real in-memory client 🧪.

This lives outside conftest.py because pytest does not make `tests` importable as a
package here, so `from tests.conftest import ...` fails. Test files import it as
`from mcp_helpers import ...` instead.
"""

from typing import Any

from fastmcp import Client, FastMCP
lazy from mcp.types import CallToolResult

from gitlab_mcp.server import mcp


async def call_tool_result(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    app: FastMCP = mcp,
    raise_on_error: bool = True,
) -> CallToolResult:
    """Call one tool through the in-memory MCP client and return the protocol result.

    Args:
        name: The registered tool name.
        arguments: The tool arguments. None means no arguments.
        app: The FastMCP app to call. Defaults to the production server.
        raise_on_error: When False, tool errors come back as results instead of exceptions.

    Returns:
        The raw MCP call result, including error results when `raise_on_error` is False.
    """
    async with Client(app) as client:
        return await client.call_tool(name, arguments or {}, raise_on_error=raise_on_error)


async def call_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    app: FastMCP = mcp,
) -> dict[str, Any]:
    """Call one tool through the in-memory MCP client and return its structured content.

    Args:
        name: The registered tool name.
        arguments: The tool arguments. None means no arguments.
        app: The FastMCP app to call. Defaults to the production server.

    Returns:
        The structured content of a successful tool call.
    """
    result = await call_tool_result(name, arguments, app=app)
    return result.structured_content
