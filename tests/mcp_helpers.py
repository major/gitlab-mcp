"""Shared helpers that call MCP tools through a real in-memory client 🧪.

This lives outside conftest.py because pytest does not make `tests` importable as a
package here, so `from tests.conftest import ...` fails. Test files import it as
`from mcp_helpers import ...` instead.
"""

from typing import Any, cast

from fastmcp import Client, FastMCP
lazy from fastmcp.client.client import CallToolResult

from gitlab_mcp import mcp


async def call_tool_result(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    app: FastMCP = mcp,
    raise_on_error: bool = True,
    client: Client | None = None,
) -> CallToolResult:
    """Call one tool through the in-memory MCP client and return the FastMCP call result.

    Args:
        name: The registered tool name.
        arguments: The tool arguments. None means no arguments.
        app: The FastMCP app used only when creating an owned client.
        raise_on_error: When False, tool errors come back as results instead of exceptions.
        client: An already-open client borrowed from the caller, who retains ownership.

    Returns:
        The raw MCP call result, including error results when `raise_on_error` is False.
    """
    if client is not None:
        return await client.call_tool(name, arguments or {}, raise_on_error=raise_on_error)
    async with Client(app) as owned_client:
        return await owned_client.call_tool(name, arguments or {}, raise_on_error=raise_on_error)


async def call_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    app: FastMCP = mcp,
    client: Client | None = None,
) -> dict[str, Any]:
    """Call one tool through the in-memory MCP client and return its structured content.

    Args:
        name: The registered tool name.
        arguments: The tool arguments. None means no arguments.
        app: The FastMCP app used only when creating an owned client.
        client: An already-open client borrowed from the caller, who retains ownership.

    Returns:
        The structured content of a successful tool call.
    """
    result = await call_tool_result(name, arguments, app=app, client=client)
    # Tools called through this helper return object content.
    return cast("dict[str, Any]", result.structured_content)
