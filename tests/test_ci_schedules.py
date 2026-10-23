"""Tests for the pipeline schedule list tool, run through a real in-memory MCP client 🗓️."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp import mcp

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
PREFIX = "https://gitlab.example.com/group/project/-/pipeline_schedules/"
TEXT_LIMIT = 200  # Matches the cap for descriptions and refs in ci_metadata.
HEADER = "schedule_id\tdescription\tref\tactive\tcron\ttimezone\tnext_run\towner\tlast_pipeline_id\tlast_status"


async def call_tool(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call list_pipeline_schedules through the MCP protocol and return its structured data."""
    async with Client(mcp) as client:
        result = await client.call_tool("list_pipeline_schedules", {"project": PROJECT} | (arguments or {}))
    return result.structured_content


def schedule_node(schedule_id: int = 9, **overrides: Any) -> dict[str, Any]:
    """Build one GraphQL pipeline schedule with the fields the tool reads."""
    node = {
        "id": f"gid://gitlab/Ci::PipelineSchedule/{schedule_id}",
        "description": "Nightly build",
        "ref": "main",
        "active": True,
        "cron": "0 1 * * *",
        "cronTimezone": "UTC",
        "nextRunAt": "2026-10-24T01:00:00Z",
        "owner": {"username": "jdoe"},
        "lastPipeline": {"id": "gid://gitlab/Ci::Pipeline/901", "status": "FAILED"},
    }
    return node | overrides


def schedules_page(*nodes: dict[str, Any], has_next: bool = False, cursor: str | None = None) -> dict[str, Any]:
    """Build one `pipelineSchedules` connection page."""
    return {"nodes": list(nodes), "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}


def project_response(schedules: dict[str, Any] | None, project_found: bool = True) -> httpx.Response:
    """Wrap one `pipelineSchedules` page in a successful GraphQL response. A missing project is null."""
    project = {"pipelineSchedules": schedules} if project_found else None
    return httpx.Response(200, json={"data": {"project": project}})


def sent_variables(route: respx.Route, index: int = -1) -> dict[str, Any]:
    """Return the GraphQL variables of one request the route received. The default is the last request."""
    return json.loads(route.calls[index].request.content)["variables"]


# ---------- list_pipeline_schedules ----------


@respx.mock
async def test_lists_schedules_in_one_graphql_call(gitlab_env):
    """One GraphQL call returns each schedule as a row, with the full next run time and the numeric IDs."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=project_response(schedules_page(schedule_node(9))))

    # Act
    data = await call_tool()

    # Assert
    assert route.call_count == 1
    assert sent_variables(route) == {"project": PROJECT, "first": 20, "after": None, "status": None}
    assert data == {
        "web_url_prefix": PREFIX,
        "schedules": f"{HEADER}\n9\tNightly build\tmain\ttrue\t0 1 * * *\tUTC\t2026-10-24T01:00:00Z\tjdoe\t901\tfailed",
    }


@pytest.mark.parametrize(
    ("active", "status"),
    [(True, "ACTIVE"), (False, "INACTIVE"), (None, None)],
    ids=["active", "inactive", "both"],
)
@respx.mock
async def test_active_filter_maps_to_schedule_status(gitlab_env, active, status):
    """`active` maps to the schedule status filter. Leaving it unset returns both kinds of schedule."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=project_response(schedules_page()))

    # Act
    await call_tool({"active": active})

    # Assert
    assert sent_variables(route)["status"] == status


@respx.mock
async def test_null_last_pipeline_and_owner_are_blank_not_never_ran(gitlab_env):
    """A schedule with no pipeline has blank last-pipeline cells. A deleted owner is blank too."""
    # Arrange
    node = schedule_node(9, lastPipeline=None, owner=None, active=False)
    respx.post(GRAPHQL).mock(return_value=project_response(schedules_page(node)))

    # Act
    data = await call_tool()

    # Assert
    assert data["schedules"] == (f"{HEADER}\n9\tNightly build\tmain\tfalse\t0 1 * * *\tUTC\t2026-10-24T01:00:00Z\t\t\t")


@respx.mock
async def test_long_description_is_capped_and_line_breaks_flattened(gitlab_env):
    """A description cannot split its row: tabs and line breaks become spaces, and long text is capped."""
    # Arrange
    node = schedule_node(9, description="Nightly\tbuild\n" + "x" * 300, ref="y" * 300)
    respx.post(GRAPHQL).mock(return_value=project_response(schedules_page(node)))
    description = "Nightly build " + "x" * (TEXT_LIMIT - 3 - len("Nightly build ")) + "..."
    ref = "y" * (TEXT_LIMIT - 3) + "..."

    # Act
    data = await call_tool()

    # Assert
    row = data["schedules"].split("\n")[1].split("\t")
    assert len(row) == len(HEADER.split("\t"))
    assert row[1] == description
    assert row[2] == ref


@respx.mock
async def test_empty_list_returns_header_only_and_no_cursor(gitlab_env):
    """A project with no schedules returns the header row alone, and the result has no next_cursor key."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=project_response(schedules_page()))

    # Act
    data = await call_tool()

    # Assert
    assert data == {"web_url_prefix": PREFIX, "schedules": HEADER}


def paged_schedules(request: httpx.Request) -> httpx.Response:
    """Answer the first page with one more page to come, and the second page as the last."""
    if json.loads(request.content)["variables"]["after"] is None:
        return project_response(schedules_page(schedule_node(9), has_next=True, cursor="cursor-page-2"))
    return project_response(schedules_page(schedule_node(8)))


@respx.mock
async def test_next_cursor_resumes_the_same_call_on_the_next_page(gitlab_env):
    """The cursor fetches the next page with the same arguments, and the last page has no cursor."""
    # Arrange
    route = respx.post(GRAPHQL).mock(side_effect=paged_schedules)
    first = await call_tool({"limit": 1})

    # Act
    second = await call_tool({"limit": 1, "cursor": first["next_cursor"]})

    # Assert
    assert first["next_cursor"]
    assert sent_variables(route, index=1) == {"project": PROJECT, "first": 1, "after": "cursor-page-2", "status": None}
    assert second["schedules"].split("\n")[1].startswith("8\t")
    assert "next_cursor" not in second


@pytest.mark.parametrize(
    "changed",
    [
        {"active": True},
        {"limit": 5},
        {"project": "other/project"},
    ],
    ids=["active", "limit", "project"],
)
@respx.mock
async def test_cursor_is_rejected_when_any_argument_changes(gitlab_env, changed):
    """A cursor is bound to its project, active filter, and limit. Changing one fails before the next request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(side_effect=paged_schedules)
    first = await call_tool({"limit": 1})

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool({"limit": 1} | changed | {"cursor": first["next_cursor"]})
    assert route.call_count == 1


@respx.mock
async def test_garbage_cursor_fails_before_network(gitlab_env):
    """Text that is not a cursor fails before any request is made."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=project_response(schedules_page()))

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool({"cursor": "not-a-cursor!"})
    assert route.call_count == 0


@respx.mock
async def test_more_pages_without_end_cursor_is_an_error(gitlab_env):
    """GitLab says more schedules exist but gives no cursor. That is an error, not a silent end of the list."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=project_response(schedules_page(schedule_node(9), has_next=True)))

    # Act / Assert
    with pytest.raises(ToolError, match="did not return a cursor"):
        await call_tool()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (project_response(None, project_found=False), "not found, or the token cannot see it"),
        (project_response(None), "did not return the pipeline schedules"),
    ],
    ids=["null-project", "null-connection"],
)
@respx.mock
async def test_null_project_or_connection_is_an_error_not_an_empty_list(gitlab_env, response, message):
    """A null project or a null schedule list is an error. It must not look like a healthy, empty project."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=response)

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": "gid://gitlab/Ci::Pipeline/9"}, "expected Ci::PipelineSchedule"),
        ({"id": "not-a-gid"}, "unexpected format"),
        ({"id": "gid://gitlab/Ci::PipelineSchedule/0"}, "unexpected format"),
        ({"lastPipeline": {"id": "gid://gitlab/Ci::Build/5", "status": "FAILED"}}, "expected Ci::Pipeline"),
    ],
    ids=["wrong-schedule-model", "malformed-schedule", "zero-schedule-id", "wrong-last-pipeline-model"],
)
@respx.mock
async def test_unexpected_gid_is_an_error_not_a_guess(gitlab_env, overrides, message):
    """A schedule or last pipeline with an unexpected GID is an error, so the tool never shows a made-up ID."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=project_response(schedules_page(schedule_node(9, **overrides))))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool()


@pytest.mark.parametrize(
    ("limit", "message"),
    [
        (0, "greater than or equal to 1"),
        (101, "less than or equal to 100"),
    ],
    ids=["too-small", "too-large"],
)
@respx.mock
async def test_limit_out_of_range_fails_before_network(gitlab_env, limit, message):
    """A limit outside 1 to 100 fails before any request is made."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=project_response(schedules_page()))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool({"limit": limit})
    assert route.call_count == 0


@respx.mock
async def test_blank_project_fails_before_network(gitlab_env):
    """A blank project path is rejected before any request is made."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=project_response(schedules_page()))

    # Act / Assert
    with pytest.raises(ToolError, match="Pass a project path"):
        await call_tool({"project": "  "})
    assert route.call_count == 0
