"""FastMCP server, kept so `gitlab_mcp.server.mcp` keeps working 🦊.

The app now lives in `gitlab_mcp.app`, and every tool lives in `gitlab_mcp.tools`. New code should import from those.
"""

from gitlab_mcp import tools  # noqa: F401 - importing the package registers every tool.
from gitlab_mcp.app import mcp

__all__ = ["mcp"]
