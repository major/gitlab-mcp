"""GitLab MCP server for fast developer workflows 🦊."""

import os

from gitlab_mcp import tools  # noqa: F401 - importing the package registers every tool.
from gitlab_mcp.app import mcp as mcp
from gitlab_mcp.profiles import create_app as create_app


def main() -> None:
    """Run the MCP server over stdio."""
    create_app(os.environ.get("GITLAB_MCP_PROFILE", "full")).run()
