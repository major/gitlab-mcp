"""GitLab MCP server for fast developer workflows 🦊."""

from gitlab_mcp.server import mcp


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()
