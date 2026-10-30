"""Isolated MCP tool catalog profiles."""

from fastmcp import FastMCP

from gitlab_mcp.app import CreatePipelineValidationMiddleware, gitlab_lifespan, mcp
from gitlab_mcp.tools.ci import inspect_job
from gitlab_mcp.tools.snippets import read_snippet


def create_app(profile: str = "full") -> FastMCP:
    """Return the full server or a fresh compact server with shared tools."""
    if profile == "full":
        return mcp
    if profile != "compact":
        raise ValueError(f"Unknown GitLab MCP profile: {profile!r}")  # noqa: TRY003
    instructions = (mcp.instructions or "").replace(
        "use get_job for runner-job metadata", "use inspect_job(view='metadata') for runner-job metadata"
    )
    app = FastMCP(name="gitlab-mcp", instructions=instructions, lifespan=gitlab_lifespan)
    app.add_provider(mcp.local_provider)
    app.disable(
        names={
            "get_snippet",
            "get_snippet_file",
            "get_job",
            "get_job_artifact",
            "list_job_artifacts",
            "land_merge_request",
        },
        components={"tool"},
    )
    app.tool(annotations={"readOnlyHint": True, "openWorldHint": True})(read_snippet)
    app.tool(annotations={"readOnlyHint": True, "openWorldHint": True})(inspect_job)
    app.add_middleware(CreatePipelineValidationMiddleware())
    return app
