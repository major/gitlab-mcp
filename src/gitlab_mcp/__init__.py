"""GitLab MCP server for fast developer workflows 🦊."""

from gitlab_mcp import tools  # noqa: F401 - importing the package registers every tool.
from gitlab_mcp.app import mcp


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()
