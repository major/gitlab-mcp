"""Isolated MCP tool catalog profiles."""

from fastmcp import FastMCP

from gitlab_mcp.app import CreatePipelineValidationMiddleware, gitlab_lifespan, mcp
from gitlab_mcp.tools.snippets import read_snippet


def create_app(profile: str = "full") -> FastMCP:
    """Return the full server or a fresh compact server with shared tools."""
    if profile == "full":
        return mcp
    if profile != "compact":
        raise ValueError(f"Unknown GitLab MCP profile: {profile!r}")  # noqa: TRY003
    app = FastMCP(name="gitlab-mcp", instructions=mcp.instructions, lifespan=gitlab_lifespan)
    app.add_provider(mcp.local_provider)
    app.disable(names={"get_snippet", "get_snippet_file"}, components={"tool"})
    app.tool(annotations={"readOnlyHint": True, "openWorldHint": True})(read_snippet)
    app.add_middleware(CreatePipelineValidationMiddleware())
    return app
