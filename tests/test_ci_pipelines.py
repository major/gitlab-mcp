"""Tests for the pipeline list and detail tools, run through a real in-memory MCP client 🚦."""

import base64
import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp import mcp

API = "https://gitlab.example.com/api/v4"
GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
ENCODED_PROJECT = "group%2Fproject"
PIPELINES_URL = f"{API}/projects/{ENCODED_PROJECT}/pipelines"
TEXT_LIMIT = 200  # Matches the cap for names and stages in ci_pipelines.
TWO_GRAPHQL_REQUESTS = 2
EXCERPT_CHAR_LIMIT = 1000
LIST_HEADER = "pipeline_id\tname\tstatus\tsource\tref\tsha\tcreated\tupdated"
JOB_HEADER = (
    "job_id\ttype\tname\tstage\tstatus\tallow_failure\tduration_s\tscheduled_at"
    "\tdownstream_project\tdownstream_pipeline_id"
)


async def call_tool(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call one pipeline tool through the MCP protocol and return its structured data."""
    async with Client(mcp) as client:
        result = await client.call_tool(tool, {"project": PROJECT} | (arguments or {}))
    return result.structured_content


def rows(tsv: str) -> list[dict[str, str]]:
    """Parse a TSV string into one dict per data row, keyed by the header."""
    header, *lines = tsv.split("\n")
    return [dict(zip(header.split("\t"), line.split("\t"), strict=True)) for line in lines]


def rest_pipeline(pipeline_id: int = 901, **overrides: Any) -> dict[str, Any]:
    """Build one pipeline shaped like a REST list response, with the fields the tools read."""
    pipeline = {
        "id": pipeline_id,
        "iid": 5,
        "project_id": 1,
        "name": "test",
        "status": "failed",
        "source": "push",
        "ref": "main",
        "sha": "abc123",
        "created_at": "2026-10-01T12:00:00.000Z",
        "updated_at": "2026-10-02T08:30:00.000Z",
        "web_url": f"https://gitlab.example.com/group/project/-/pipelines/{pipeline_id}",
    }
    return pipeline | overrides


def rest_page(*pipelines: dict[str, Any], next_page: str = "") -> httpx.Response:
    """Answer one REST page. GitLab sends an empty X-Next-Page on the last page."""
    return httpx.Response(200, json=list(pipelines), headers={"X-Next-Page": next_page})


def job_node(model: str, number: int, **overrides: Any) -> dict[str, Any]:
    """Build one GraphQL job with a GID of the given model."""
    node = {
        "id": f"gid://gitlab/{model}/{number}",
        "name": "unit",
        "status": "SUCCESS",
        "allowFailure": False,
        "duration": 12,
        "scheduledAt": "2026-10-01T12:00:00Z",
        "stage": {"name": "test"},
        "downstreamPipeline": None,
    }
    return node | overrides


def pipeline_node(
    *jobs: dict[str, Any],
    has_next: bool = False,
    cursor: str | None = None,
    has_previous: bool = False,
    start_cursor: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build one GraphQL pipeline with a jobs page, shaped like the `project.pipeline` response."""
    node = {
        "id": "gid://gitlab/Ci::Pipeline/901",
        "status": "FAILED",
        "source": "PUSH",
        "ref": "main",
        "sha": "abc123",
        "createdAt": "2026-10-01T12:00:00Z",
        "startedAt": "2026-10-01T12:01:00Z",
        "finishedAt": None,
        "duration": 300,
        "jobs": {
            "nodes": list(jobs),
            "pageInfo": {
                "hasNextPage": has_next,
                "endCursor": cursor,
                "hasPreviousPage": has_previous,
                "startCursor": start_cursor,
            },
        },
    }
    return node | overrides


def graphql_response(project: dict[str, Any] | None) -> httpx.Response:
    """Wrap one project object in a successful GraphQL response."""
    return httpx.Response(200, json={"data": {"project": project}})


def pipeline_response(pipeline: dict[str, Any] | None) -> httpx.Response:
    """Answer a `project.pipeline` query with one pipeline, or with null when it does not exist."""
    return graphql_response({"pipeline": pipeline})


def failure_log_response(*summaries: str | None) -> httpx.Response:
    """Build an aliased log response for summaries in the requested job order."""
    project = {
        f"job_{index}": {"trace": {"htmlSummary": summary}} if summary is not None else None
        for index, summary in enumerate(summaries)
    }
    return graphql_response(project)


def sent_variables(route: respx.Route, index: int = -1) -> dict[str, Any]:
    """Return the GraphQL variables of one request the route received. The default is the last request."""
    return json.loads(route.calls[index].request.content)["variables"]


def sent_params(route: respx.Route) -> dict[str, str]:
    """Return the query parameters of the last REST request the route received."""
    return dict(route.calls.last.request.url.params)


def encode_payload(payload: Any) -> str:
    """Encode any JSON value the way a cursor is encoded, so tests can send malformed payloads."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def forged_cursor(cursor: str, **changes: Any) -> str:
    """Return a real cursor with some payload fields replaced. The scope stays as the server made it."""
    payload = json.loads(base64.urlsafe_b64decode(cursor))
    return encode_payload(payload | changes)


# ---------- list_pipelines ----------


@respx.mock
async def test_project_scope_lists_newest_first_and_formats_timestamps(gitlab_env):
    """A project list asks for the newest pipelines first and keeps second-resolution timestamps."""
    # Arrange
    route = respx.get(PIPELINES_URL).mock(return_value=rest_page(rest_pipeline(901)))

    # Act
    data = await call_tool("list_pipelines")

    # Assert
    assert sent_params(route) == {"order_by": "id", "sort": "desc", "per_page": "20", "page": "1"}
    assert data == {
        "web_url_prefix": "https://gitlab.example.com/group/project/-/pipelines/",
        "pipelines": (
            f"{LIST_HEADER}\n901\ttest\tfailed\tpush\tmain\tabc123\t2026-10-01T12:00:00Z\t2026-10-02T08:30:00Z"
        ),
    }


@respx.mock
async def test_list_includes_pipeline_name_and_preserves_timestamp_timezones(gitlab_env):
    """A list includes names when present and preserves each timestamp's timezone."""
    # Arrange
    named = rest_pipeline(
        901,
        name="build",
        created_at="2026-10-01T12:00:00.987Z",
        updated_at="2026-10-02T08:30:00.456+02:00",
    )
    unnamed = rest_pipeline(
        900,
        created_at="2026-10-01T12:00:00.987-04:00",
        updated_at="2026-10-02T08:30:00.456Z",
    )
    del unnamed["name"]
    respx.get(PIPELINES_URL).mock(return_value=rest_page(named, unnamed))

    # Act
    data = await call_tool("list_pipelines")

    # Assert
    assert data["pipelines"] == (
        f"{LIST_HEADER}\n"
        "901\tbuild\tfailed\tpush\tmain\tabc123\t"
        "2026-10-01T12:00:00Z\t2026-10-02T08:30:00+02:00\n"
        "900\t\tfailed\tpush\tmain\tabc123\t"
        "2026-10-01T12:00:00-04:00\t2026-10-02T08:30:00Z"
    )


@pytest.mark.parametrize(
    ("arguments", "expected_params"),
    [
        (
            {"ref": "main", "status": "failed", "source": "push"},
            {"ref": "main", "status": "failed", "source": "push"},
        ),
        (
            {"updated_after": "2026-10-01T12:30:00+02:00"},
            {"updated_after": "2026-10-01T10:30:00Z"},
        ),
    ],
    ids=["exact-filters", "offset-converted-to-utc"],
)
@respx.mock
async def test_project_scope_sends_filters(gitlab_env, arguments, expected_params):
    """Project filters go to GitLab as query parameters, and timestamps are sent in UTC."""
    # Arrange
    route = respx.get(PIPELINES_URL).mock(return_value=rest_page())

    # Act
    await call_tool("list_pipelines", arguments)

    # Assert
    assert sent_params(route) == {"order_by": "id", "sort": "desc"} | expected_params | {"per_page": "20", "page": "1"}


@respx.mock
async def test_schedule_scope_uses_schedule_route_with_allowed_filters(gitlab_env):
    """A schedule list uses the schedule route and sends only status and updated_after, newest first."""
    # Arrange
    route = respx.get(f"{API}/projects/{ENCODED_PROJECT}/pipeline_schedules/9/pipelines").mock(
        return_value=rest_page(rest_pipeline(901)),
    )

    # Act
    data = await call_tool(
        "list_pipelines",
        {"schedule_id": 9, "status": "success", "updated_after": "2026-10-01T00:00:00Z"},
    )

    # Assert
    assert sent_params(route) == {
        "sort": "desc",
        "status": "success",
        "updated_after": "2026-10-01T00:00:00Z",
        "per_page": "20",
        "page": "1",
    }
    assert data["pipelines"].split("\n")[0] == LIST_HEADER


@respx.mock
async def test_merge_request_scope_uses_mr_route_without_filters(gitlab_env):
    """A merge request list uses the MR route and sends paging parameters only."""
    # Arrange
    route = respx.get(f"{API}/projects/{ENCODED_PROJECT}/merge_requests/7/pipelines").mock(
        return_value=rest_page(rest_pipeline(901)),
    )

    # Act
    await call_tool("list_pipelines", {"mr_iid": 7, "limit": 5})

    # Assert
    assert sent_params(route) == {"per_page": "5", "page": "1"}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"mr_iid": 7, "schedule_id": 9}, "Pass either mr_iid or schedule_id, not both."),
        ({"mr_iid": 7, "ref": "main"}, "Not supported for this pipeline scope: ref."),
        ({"mr_iid": 7, "status": "failed"}, "Not supported for this pipeline scope: status."),
        ({"schedule_id": 9, "ref": "main"}, "Not supported for this pipeline scope: ref."),
        ({"schedule_id": 9, "source": "push"}, "Not supported for this pipeline scope: source."),
        ({"updated_after": "2026-10-01"}, "timezone-aware ISO 8601 datetime"),
        ({"updated_after": "2026-10-01T12:30:00"}, "timezone-aware ISO 8601 datetime"),
        ({"updated_after": "not-a-timestamp"}, "timezone-aware ISO 8601 datetime"),
    ],
    ids=[
        "mr-and-schedule",
        "mr-rejects-ref",
        "mr-rejects-status",
        "schedule-rejects-ref",
        "schedule-rejects-source",
        "date-only",
        "timezone-naive",
        "invalid",
    ],
)
@respx.mock
async def test_invalid_scope_or_filter_fails_before_network(gitlab_env, arguments, message):
    """Unsupported filter combinations and bad timestamps fail before any request is made."""
    # Arrange
    route = respx.get(PIPELINES_URL).mock(return_value=rest_page())

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("list_pipelines", arguments)
    assert route.call_count == 0


@respx.mock
async def test_empty_project_fails_before_network(gitlab_env):
    """A blank project path is rejected before any request is made."""
    # Arrange
    route = respx.get(PIPELINES_URL).mock(return_value=rest_page())

    # Act / Assert
    with pytest.raises(ToolError, match="Pass a project path"):
        await call_tool("list_pipelines", {"project": "  "})
    assert route.call_count == 0


@respx.mock
async def test_empty_list_returns_header_only_and_no_cursor(gitlab_env):
    """An empty page is a header row, and the result has no next_cursor key."""
    # Arrange
    respx.get(PIPELINES_URL).mock(return_value=rest_page())

    # Act
    data = await call_tool("list_pipelines")

    # Assert
    assert data == {
        "web_url_prefix": "https://gitlab.example.com/group/project/-/pipelines/",
        "pipelines": LIST_HEADER,
    }


def paged_pipelines(request: httpx.Request) -> httpx.Response:
    """Answer page 1 with one more page to come, and page 2 as the last page."""
    if request.url.params["page"] == "1":
        return rest_page(rest_pipeline(901), next_page="2")
    return rest_page(rest_pipeline(900))


@respx.mock
async def test_next_cursor_resumes_the_same_call_on_the_next_page(gitlab_env):
    """The cursor from one page fetches the next page with the same arguments, and the last page has no cursor."""
    # Arrange
    route = respx.get(PIPELINES_URL).mock(side_effect=paged_pipelines)
    first = await call_tool("list_pipelines", {"status": "failed", "limit": 1})

    # Act
    second = await call_tool("list_pipelines", {"status": "failed", "limit": 1, "cursor": first["next_cursor"]})

    # Assert
    assert first["next_cursor"]
    assert "901" in first["pipelines"]
    assert sent_params(route) == {
        "order_by": "id",
        "sort": "desc",
        "status": "failed",
        "per_page": "1",
        "page": "2",
    }
    assert second == {
        "web_url_prefix": "https://gitlab.example.com/group/project/-/pipelines/",
        "pipelines": (
            f"{LIST_HEADER}\n900\ttest\tfailed\tpush\tmain\tabc123\t2026-10-01T12:00:00Z\t2026-10-02T08:30:00Z"
        ),
    }


@pytest.mark.parametrize(
    "changed",
    [
        {"limit": 5},
        {"ref": "main"},
        {"status": "success"},
        {"source": "push"},
        {"updated_after": "2026-10-01T00:00:00Z"},
    ],
    ids=["limit", "ref", "status", "source", "updated_after"],
)
@respx.mock
async def test_cursor_is_rejected_when_any_argument_changes(gitlab_env, changed):
    """A cursor is bound to its arguments, so changing any of them fails before the next request."""
    # Arrange
    route = respx.get(PIPELINES_URL).mock(side_effect=paged_pipelines)
    first = await call_tool("list_pipelines", {"limit": 1})

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("list_pipelines", {"limit": 1} | changed | {"cursor": first["next_cursor"]})
    assert route.call_count == 1


@respx.mock
async def test_garbage_cursor_fails_before_network(gitlab_env):
    """Text that is not a cursor fails before any request is made."""
    # Arrange
    route = respx.get(PIPELINES_URL).mock(return_value=rest_page())

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("list_pipelines", {"cursor": "not-a-cursor!"})
    assert route.call_count == 0


@pytest.mark.parametrize("payload", [[], 42, "text", None], ids=["array", "number", "string", "null"])
@respx.mock
async def test_valid_base64_json_that_is_not_an_object_fails_before_network(gitlab_env, payload):
    """A cursor that decodes to JSON other than an object is an invalid cursor, not a crash."""
    # Arrange
    route = respx.get(PIPELINES_URL).mock(return_value=rest_page())

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("list_pipelines", {"cursor": encode_payload(payload)})
    assert route.call_count == 0


@pytest.mark.parametrize("page", [0, True, "2", None], ids=["zero", "boolean", "string", "null"])
@respx.mock
async def test_forged_page_number_is_rejected_before_next_request(gitlab_env, page):
    """A cursor with the right scope but a page that is not a positive integer fails before the next request."""
    # Arrange
    route = respx.get(PIPELINES_URL).mock(side_effect=paged_pipelines)
    first = await call_tool("list_pipelines", {"limit": 1})
    cursor = forged_cursor(first["next_cursor"], page=page)

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("list_pipelines", {"limit": 1, "cursor": cursor})
    assert route.call_count == 1


# ---------- get_pipeline ----------


@respx.mock
async def test_maps_pipeline_metadata_and_job_types(gitlab_env):
    """Pipeline fields are full timestamps, and each job type comes from its GID model."""
    # Arrange
    jobs = [
        job_node("Ci::Build", 11),
        job_node(
            "Ci::Bridge",
            12,
            name="deploy",
            status="FAILED",
            allowFailure=True,
            duration=None,
            stage={"name": "deploy"},
            downstreamPipeline={"id": "gid://gitlab/Ci::Pipeline/902", "project": {"fullPath": "group/child"}},
        ),
        job_node("GenericCommitStatus", 13, name="lint", stage=None),
        job_node("CommitStatus", 14, name="scan", status="CANCELED"),
    ]
    route = respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node(*jobs)))

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Assert
    query = json.loads(route.calls.last.request.content)["query"]
    assert "$pipelineId: CiPipelineID!" in query
    assert "failureMessage" not in query
    assert route.call_count == 1
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "pipelineId": "gid://gitlab/Ci::Pipeline/901",
        "first": 50,
        "after": None,
        "last": None,
        "before": None,
        "retried": False,
        "statuses": None,
    }
    expected_lines = [
        JOB_HEADER,
        "11\tbuild\tunit\ttest\tsuccess\tfalse\t12\t2026-10-01T12:00:00Z\t\t",
        "12\tbridge\tdeploy\tdeploy\tfailed\ttrue\t\t2026-10-01T12:00:00Z\tgroup/child\t902",
        "13\texternal\tlint\t\tsuccess\tfalse\t12\t2026-10-01T12:00:00Z\t\t",
        "14\texternal\tscan\ttest\tcanceled\tfalse\t12\t2026-10-01T12:00:00Z\t\t",
    ]
    expected_jobs = "\n".join(expected_lines)
    assert all(len(line.split("\t")) == len(JOB_HEADER.split("\t")) for line in data["jobs"].splitlines())
    assert data == {
        "pipeline_id": 901,
        "status": "failed",
        "source": "push",
        "ref": "main",
        "sha": "abc123",
        "created_at": "2026-10-01T12:00:00Z",
        "started_at": "2026-10-01T12:01:00Z",
        "duration_s": 300,
        "web_url": "https://gitlab.example.com/group/project/-/pipelines/901",
        "jobs": expected_jobs,
    }


@respx.mock
async def test_pipeline_merge_request_iid_is_returned_as_an_integer(gitlab_env):
    """A linked merge request IID is converted from GraphQL's string to an integer."""
    # Arrange
    expected_mr_iid = 7
    pipeline = pipeline_node(mergeRequest={"iid": "7"})
    route = respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline))

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Assert
    query = json.loads(route.calls.last.request.content)["query"]
    assert "mergeRequest { iid }" in query
    assert data["mr_iid"] == expected_mr_iid


@pytest.mark.parametrize(
    "pipeline_fields",
    [{}, {"mergeRequest": None}],
    ids=["absent", "null"],
)
@respx.mock
async def test_pipeline_without_merge_request_omits_mr_iid(gitlab_env, pipeline_fields):
    """An absent or null merge request does not add an `mr_iid` result field."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node(**pipeline_fields)))

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Assert
    assert "mr_iid" not in data


@pytest.mark.parametrize(
    ("downstream", "expected"),
    [
        (None, {"downstream_project": "", "downstream_pipeline_id": ""}),
        (
            {"id": "gid://gitlab/Ci::Pipeline/902", "project": None},
            {"downstream_project": "", "downstream_pipeline_id": "902"},
        ),
    ],
    ids=["no-downstream", "downstream-project-hidden"],
)
@respx.mock
async def test_missing_downstream_cells_stay_blank(gitlab_env, downstream, expected):
    """Null downstream fields leave blank cells, which mean unknown and never a healthy claim."""
    # Arrange
    job = job_node("Ci::Bridge", 12, downstreamPipeline=downstream)
    respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node(job)))

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Assert
    (row,) = rows(data["jobs"])
    assert {key: row[key] for key in expected} == expected


@respx.mock
async def test_long_downstream_project_path_is_copied_exactly(gitlab_env):
    """A downstream project path is never cut, so the agent can copy it and act on the child project."""
    # Arrange
    long_path = "g" * 120 + "/" + "p" * 120
    assert len(long_path) > TEXT_LIMIT
    job = job_node(
        "Ci::Bridge",
        12,
        downstreamPipeline={"id": "gid://gitlab/Ci::Pipeline/902", "project": {"fullPath": long_path}},
    )
    respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node(job)))

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Assert
    (row,) = rows(data["jobs"])
    assert row["downstream_project"] == long_path
    assert row["downstream_pipeline_id"] == "902"


@pytest.mark.parametrize("extra", [{"path": None}, {}], ids=["path-null-in-response", "path-absent-from-response"])
@respx.mock
async def test_web_url_is_built_from_project_and_pipeline_id(gitlab_env, extra):
    """The pipeline link comes from the project path and pipeline ID, so a null or absent `path` is accepted."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node() | extra))

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Assert
    assert data["web_url"] == "https://gitlab.example.com/group/project/-/pipelines/901"


@pytest.mark.parametrize(
    ("arguments", "expected_variables"),
    [
        ({"include_retried": True}, {"retried": None}),
        ({"statuses": ["success", "failed"]}, {"statuses": ["FAILED", "SUCCESS"]}),
        ({"limit": 10}, {"first": 10}),
    ],
    ids=["include-retried", "statuses-uppercased-and-sorted", "limit"],
)
@respx.mock
async def test_job_filters_map_to_graphql_variables(gitlab_env, arguments, expected_variables):
    """Retry and status options become GraphQL variables. Statuses are uppercased and sorted."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node()))

    # Act
    await call_tool("get_pipeline", {"pipeline_id": 901} | arguments)

    # Assert
    variables = sent_variables(route)
    assert {key: variables[key] for key in expected_variables} == expected_variables


@respx.mock
async def test_long_job_name_is_capped_and_name_and_stage_controls_are_flattened(gitlab_env):
    """Long names are capped, and tabs and line breaks in names and stages stay inside their TSV cells."""
    # Arrange
    long_name = "n\nx\t" + "x" * 250
    job = job_node("Ci::Build", 11, name=long_name, stage={"name": "test\nstage\tname"})
    respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node(job)))

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Assert
    (row,) = rows(data["jobs"])
    assert row["name"] == "n x " + "x" * (TEXT_LIMIT - 7) + "..."
    assert len(row["name"]) == TEXT_LIMIT
    assert row["stage"] == "test stage name"


@respx.mock
async def test_empty_job_page_is_header_only_and_has_no_cursor(gitlab_env):
    """A pipeline with no matching jobs returns the header row and no next_cursor."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node()))

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Assert
    assert data["jobs"] == JOB_HEADER
    assert "next_cursor" not in data


@respx.mock
async def test_failure_excerpts_are_disabled_without_changing_requests_or_output(gitlab_env):
    """The default call with a failed build job keeps the old output and sends no log request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=pipeline_response(
            pipeline_node(job_node("Ci::Build", 11, status="FAILED")),
        ),
    )

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Assert
    assert route.call_count == 1
    assert len(respx.calls) == 1
    assert set(data) == {
        "pipeline_id",
        "status",
        "source",
        "ref",
        "sha",
        "created_at",
        "started_at",
        "duration_s",
        "web_url",
        "jobs",
    }
    assert rows(data["jobs"])[0]["status"] == "failed"


@pytest.mark.parametrize("order", ["newest_first", "oldest_first"], ids=["newest", "oldest"])
@respx.mock
async def test_failure_excerpts_follow_displayed_order_and_only_select_failed_builds(gitlab_env, order):
    """Excerpt jobs follow the displayed page order and exclude failed bridges and external statuses."""
    # Arrange
    page = pipeline_node(
        job_node("Ci::Build", 12, status="FAILED"),
        job_node("Ci::Bridge", 13, status="FAILED"),
        job_node("Ci::Build", 14, status="SUCCESS"),
        job_node("GenericCommitStatus", 15, status="FAILED"),
        job_node("Ci::Build", 11, status="FAILED"),
        has_next=True,
        cursor="next-page",
        has_previous=True,
        start_cursor="previous-page",
    )
    route = respx.post(GRAPHQL).mock(
        side_effect=[pipeline_response(page), failure_log_response("error: first output", "error: second output")],
    )
    expected_ids = [12, 11] if order == "newest_first" else [11, 12]

    # Act
    data = await call_tool(
        "get_pipeline",
        {"pipeline_id": 901, "order": order, "include_failure_excerpts": True},
    )

    # Assert
    assert route.call_count == TWO_GRAPHQL_REQUESTS
    assert [entry["job_id"] for entry in data["failure_excerpts"]] == expected_ids
    assert [entry["text"] for entry in data["failure_excerpts"]] == ["error: first output", "error: second output"]
    assert sent_variables(route, 1) == {
        "project": PROJECT,
        "job0": f"gid://gitlab/Ci::Build/{expected_ids[0]}",
        "job1": f"gid://gitlab/Ci::Build/{expected_ids[1]}",
    }
    assert data["excerpt_safety_note"]
    assert "last 100 lines" in data["excerpt_safety_note"]
    assert data["next_cursor"]
    scope = json.loads(base64.urlsafe_b64decode(data["next_cursor"]))["scope"]
    assert scope["include_failure_excerpts"] is True
    assert set(scope) == {
        "tool",
        "project",
        "pipeline_id",
        "statuses",
        "include_retried",
        "order",
        "limit",
        "include_failure_excerpts",
    }
    assert "excerpt_warning" not in data
    assert "failure_excerpts_omitted" not in data


@respx.mock
async def test_failure_excerpt_cap_counts_only_extra_failed_builds_on_current_page(gitlab_env):
    """Only five failed builds are fetched, while failed bridges and external statuses do not count as omitted."""
    # Arrange
    builds = [job_node("Ci::Build", number, status="FAILED") for number in range(20, 14, -1)]
    page = pipeline_node(
        *builds,
        job_node("Ci::Bridge", 14, status="FAILED"),
        job_node("CommitStatus", 13, status="FAILED"),
        has_next=True,
        cursor="next-page",
    )
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            pipeline_response(page),
            failure_log_response(*(f"error: log {number}" for number in range(20, 15, -1))),
        ],
    )

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901, "include_failure_excerpts": True})

    # Assert
    assert route.call_count == TWO_GRAPHQL_REQUESTS
    assert [entry["job_id"] for entry in data["failure_excerpts"]] == [20, 19, 18, 17, 16]
    assert data["failure_excerpts_omitted"] == 1
    assert "job5" not in json.loads(route.calls[1].request.content)["variables"]


@respx.mock
async def test_pipeline_excerpts_scan_only_five_selected_jobs_without_metadata_requests(gitlab_env):
    """The shared fallback adds at most five trace reads to the pipeline and batched summary requests."""
    # Arrange
    builds = [job_node("Ci::Build", number, status="FAILED") for number in range(20, 14, -1)]
    graphql = respx.post(GRAPHQL).mock(
        side_effect=[pipeline_response(pipeline_node(*builds)), failure_log_response(*([None] * 5))],
    )
    traces = [
        respx.get(f"{API}/projects/{ENCODED_PROJECT}/jobs/{number}/trace").mock(
            return_value=httpx.Response(200, text=f"error: diagnostic for {number}\n"),
        )
        for number in range(20, 15, -1)
    ]

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901, "include_failure_excerpts": True})

    # Assert
    assert graphql.call_count == TWO_GRAPHQL_REQUESTS
    assert all(trace.call_count == 1 for trace in traces)
    assert len(respx.calls) == TWO_GRAPHQL_REQUESTS + len(traces)
    assert [item["job_id"] for item in data["failure_excerpts"]] == [20, 19, 18, 17, 16]
    assert all(item["source"] == "trace_prefix" for item in data["failure_excerpts"])
    assert data["failure_excerpts_omitted"] == 1


@respx.mock
async def test_failure_excerpts_are_masked_bounded_and_distinguish_missing_from_empty(gitlab_env):
    """Excerpts keep at most 1000 masked characters and distinguish unavailable from empty summaries."""
    # Arrange
    jobs = [
        job_node("Ci::Build", 11, status="FAILED"),
        job_node("Ci::Build", 12, status="FAILED"),
    ]
    long_summary = "x" * 1100 + " token=secretXYZ"
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            pipeline_response(pipeline_node(*jobs)),
            failure_log_response(long_summary, ""),
        ],
    )
    traces = [
        respx.get(f"{API}/projects/{ENCODED_PROJECT}/jobs/{number}/trace").mock(
            return_value=httpx.Response(200, text="no failure marker\n"),
        )
        for number in (11, 12)
    ]

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901, "include_failure_excerpts": True})

    # Assert
    assert route.call_count == TWO_GRAPHQL_REQUESTS
    first, second = data["failure_excerpts"]
    assert first["available"] is True
    assert first["chars"] == EXCERPT_CHAR_LIMIT
    assert first["truncated"] is True
    assert len(first["text"]) <= EXCERPT_CHAR_LIMIT
    assert "secretXYZ" not in first["text"]
    assert "[REDACTED]" in first["text"]
    assert second["available"] is True
    assert second["text"] == ""
    assert second["chars"] == 0
    assert second["truncated"] is False
    assert all(trace.call_count == 1 for trace in traces)
    assert len(respx.calls) == TWO_GRAPHQL_REQUESTS + len(traces)


@respx.mock
async def test_missing_failure_excerpt_is_unavailable_but_empty_excerpt_is_available(gitlab_env):
    """A missing aliased job result is unavailable, while an empty summary is a real empty excerpt."""
    # Arrange
    response = graphql_response(
        {
            "pipeline": pipeline_node(
                job_node("Ci::Build", 11, status="FAILED"),
                job_node("Ci::Build", 12, status="FAILED"),
            ),
        },
    )
    log_data = graphql_response({"job_0": None, "job_1": {"trace": {"htmlSummary": ""}}})
    route = respx.post(GRAPHQL).mock(side_effect=[response, log_data])
    traces = [
        respx.get(f"{API}/projects/{ENCODED_PROJECT}/jobs/{number}/trace").mock(
            return_value=httpx.Response(404),
        )
        for number in (11, 12)
    ]

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901, "include_failure_excerpts": True})

    # Assert
    assert route.call_count == TWO_GRAPHQL_REQUESTS
    unavailable, empty = data["failure_excerpts"]
    assert unavailable["available"] is False
    assert unavailable["text"] == ""
    assert empty["available"] is True
    assert empty["text"] == ""
    assert all(trace.call_count == 1 for trace in traces)


@respx.mock
async def test_log_graphql_error_keeps_job_table_and_adds_excerpt_warning(gitlab_env):
    """A log batch failure adds unavailable excerpts and a warning without losing pipeline metadata or jobs."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            pipeline_response(pipeline_node(job_node("Ci::Build", 11, status="FAILED"))),
            httpx.Response(200, json={"errors": [{"message": "Denied"}], "data": None}),
        ],
    )
    trace = respx.get(f"{API}/projects/{ENCODED_PROJECT}/jobs/11/trace").mock(
        return_value=httpx.Response(404),
    )

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901, "include_failure_excerpts": True})

    # Assert
    assert route.call_count == TWO_GRAPHQL_REQUESTS
    assert rows(data["jobs"])[0]["job_id"] == "11"
    assert data["failure_excerpts"][0]["available"] is False
    assert data["excerpt_warning"]
    assert "Denied" in data["excerpt_warning"]
    assert trace.call_count == 1


@pytest.mark.parametrize(
    "jobs",
    [
        (),
        (job_node("Ci::Build", 11, status="SUCCESS"),),
        (job_node("Ci::Bridge", 11, status="FAILED"), job_node("GenericCommitStatus", 12, status="FAILED")),
    ],
    ids=["empty", "no-failed-builds", "failed-non-builds"],
)
@respx.mock
async def test_empty_or_ineligible_page_does_not_request_failure_logs(gitlab_env, jobs):
    """No selected failed build jobs means no second request and an empty excerpt list."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node(*jobs)))

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901, "include_failure_excerpts": True})

    # Assert
    assert route.call_count == 1
    assert data["failure_excerpts"] == []
    assert data["excerpt_safety_note"]


@respx.mock
async def test_legacy_job_cursor_scope_is_accepted_when_excerpts_are_disabled(gitlab_env):
    """A cursor with the pre-excerpt scope remains valid for the default call."""
    # Arrange
    scope = {
        "tool": "get_pipeline",
        "project": PROJECT,
        "pipeline_id": 901,
        "statuses": None,
        "include_retried": False,
        "order": "newest_first",
        "limit": 50,
    }
    cursor = encode_payload({"scope": scope, "after": "legacy-cursor"})
    route = respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node()))

    # Act
    await call_tool("get_pipeline", {"pipeline_id": 901, "cursor": cursor})

    # Assert
    assert route.call_count == 1
    assert sent_variables(route)["after"] == "legacy-cursor"


@pytest.mark.parametrize(
    ("scope_with_excerpts", "requested_excerpts"),
    [(False, True), (True, False)],
    ids=["disabled-to-enabled", "enabled-to-disabled"],
)
@respx.mock
async def test_job_cursor_rejects_failure_excerpt_toggle(gitlab_env, scope_with_excerpts, requested_excerpts):
    """Changing the excerpt option invalidates cursors, while disabled legacy scope remains unchanged."""
    # Arrange
    scope = {
        "tool": "get_pipeline",
        "project": PROJECT,
        "pipeline_id": 901,
        "statuses": None,
        "include_retried": False,
        "order": "newest_first",
        "limit": 50,
    }
    if scope_with_excerpts:
        scope["include_failure_excerpts"] = True
    cursor = encode_payload({"scope": scope, "after": "cursor"})
    route = respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node()))

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool(
            "get_pipeline",
            {"pipeline_id": 901, "include_failure_excerpts": requested_excerpts, "cursor": cursor},
        )
    assert route.call_count == 0


@respx.mock
async def test_next_cursor_resumes_jobs_with_the_same_arguments(gitlab_env):
    """A job page with more jobs returns a cursor that requests the next page with the same arguments."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            pipeline_response(pipeline_node(job_node("Ci::Build", 11), has_next=True, cursor="cur-2")),
            pipeline_response(pipeline_node(job_node("Ci::Build", 12), has_next=False)),
        ],
    )
    first = await call_tool("get_pipeline", {"pipeline_id": 901, "statuses": ["failed"]})
    assert json.loads(base64.urlsafe_b64decode(first["next_cursor"]))["scope"] == {
        "tool": "get_pipeline",
        "project": PROJECT,
        "pipeline_id": 901,
        "statuses": ["failed"],
        "include_retried": False,
        "order": "newest_first",
        "limit": 50,
    }

    # Act
    second = await call_tool(
        "get_pipeline",
        {"pipeline_id": 901, "statuses": ["failed"], "cursor": first["next_cursor"]},
    )

    # Assert
    assert first["next_cursor"]
    assert sent_variables(route, 0) == {
        "fullPath": PROJECT,
        "pipelineId": "gid://gitlab/Ci::Pipeline/901",
        "first": 50,
        "after": None,
        "last": None,
        "before": None,
        "retried": False,
        "statuses": ["FAILED"],
    }
    assert sent_variables(route, 1)["after"] == "cur-2"
    assert rows(second["jobs"])[0]["job_id"] == "12"
    assert "next_cursor" not in second


@respx.mock
async def test_oldest_first_pages_jobs_by_numeric_id_and_keeps_mixed_types(gitlab_env):
    """Oldest-first pagination reverses each descending page and resumes before its start cursor."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        side_effect=[
            pipeline_response(
                pipeline_node(
                    job_node("Ci::Build", 12),
                    job_node("Ci::Bridge", 11),
                    has_previous=True,
                    start_cursor="cursor-12",
                ),
            ),
            pipeline_response(
                pipeline_node(
                    job_node("GenericCommitStatus", 14),
                    job_node("CommitStatus", 13),
                    has_previous=False,
                    start_cursor="cursor-14",
                ),
            ),
        ],
    )

    # Act
    first = await call_tool(
        "get_pipeline",
        {
            "pipeline_id": 901,
            "order": "oldest_first",
            "limit": 2,
            "statuses": ["failed", "success"],
            "include_retried": True,
        },
    )
    second = await call_tool(
        "get_pipeline",
        {
            "pipeline_id": 901,
            "order": "oldest_first",
            "limit": 2,
            "statuses": ["failed", "success"],
            "include_retried": True,
            "cursor": first["next_cursor"],
        },
    )

    # Assert
    assert route.call_count == len((first, second))
    assert sent_variables(route, 0) == {
        "fullPath": PROJECT,
        "pipelineId": "gid://gitlab/Ci::Pipeline/901",
        "first": None,
        "after": None,
        "last": 2,
        "before": None,
        "retried": None,
        "statuses": ["FAILED", "SUCCESS"],
    }
    assert sent_variables(route, 1) == {
        "fullPath": PROJECT,
        "pipelineId": "gid://gitlab/Ci::Pipeline/901",
        "first": None,
        "after": None,
        "last": 2,
        "before": "cursor-12",
        "retried": None,
        "statuses": ["FAILED", "SUCCESS"],
    }
    first_rows = rows(first["jobs"])
    second_rows = rows(second["jobs"])
    assert [(row["job_id"], row["type"]) for row in first_rows] == [("11", "bridge"), ("12", "build")]
    assert [(row["job_id"], row["type"]) for row in second_rows] == [("13", "external"), ("14", "external")]
    assert first["next_cursor"]
    assert "next_cursor" not in second


@respx.mock
async def test_job_cursor_is_rejected_when_order_changes(gitlab_env):
    """An oldest-first jobs cursor cannot resume the same pipeline in newest-first order."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=pipeline_response(
            pipeline_node(job_node("Ci::Build", 11), has_previous=True, start_cursor="cursor-11"),
        ),
    )
    first = await call_tool("get_pipeline", {"pipeline_id": 901, "order": "oldest_first"})

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("get_pipeline", {"pipeline_id": 901, "cursor": first["next_cursor"]})
    assert route.call_count == 1


@pytest.mark.parametrize("before", ["", None, 7, ["cursor-11"]], ids=["empty", "null", "number", "list"])
@respx.mock
async def test_forged_backward_job_cursor_is_rejected_before_next_request(gitlab_env, before):
    """A jobs cursor with the right scope but a bad `before` value fails before the next request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=pipeline_response(
            pipeline_node(job_node("Ci::Build", 11), has_previous=True, start_cursor="cursor-11"),
        ),
    )
    first = await call_tool("get_pipeline", {"pipeline_id": 901, "order": "oldest_first"})
    cursor = forged_cursor(first["next_cursor"], before=before)

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("get_pipeline", {"pipeline_id": 901, "order": "oldest_first", "cursor": cursor})
    assert route.call_count == 1


@respx.mock
async def test_backward_page_without_start_cursor_fails(gitlab_env):
    """GitLab reporting earlier jobs without a start cursor is an error, not a terminal page."""
    # Arrange
    route = respx.post(GRAPHQL).mock(
        return_value=pipeline_response(
            pipeline_node(job_node("Ci::Build", 11), has_previous=True, start_cursor=None),
        ),
    )

    # Act / Assert
    with pytest.raises(ToolError, match="GitLab reported more jobs but did not return a cursor"):
        await call_tool("get_pipeline", {"pipeline_id": 901, "order": "oldest_first"})
    assert route.call_count == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"pipeline_id": 902},
        {"statuses": ["success"]},
        {"include_retried": True},
        {"limit": 10},
        {"project": "other/project"},
    ],
    ids=["pipeline", "statuses", "include-retried", "limit", "project"],
)
@respx.mock
async def test_job_cursor_is_rejected_when_any_argument_changes(gitlab_env, changed):
    """A jobs cursor is bound to its pipeline, filters, limit, and project, so changes fail before the request."""
    # Arrange
    job = job_node("Ci::Build", 11)
    route = respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node(job, has_next=True, cursor="cur-2")))
    first = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("get_pipeline", {"pipeline_id": 901} | changed | {"cursor": first["next_cursor"]})
    assert route.call_count == 1


@pytest.mark.parametrize("after", ["", None, 7, ["cur-2"]], ids=["empty", "null", "number", "list"])
@respx.mock
async def test_forged_job_cursor_is_rejected_before_next_request(gitlab_env, after):
    """A jobs cursor with the right scope but a bad `after` value fails before the next request."""
    # Arrange
    job = job_node("Ci::Build", 11)
    route = respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node(job, has_next=True, cursor="cur-2")))
    first = await call_tool("get_pipeline", {"pipeline_id": 901})
    cursor = forged_cursor(first["next_cursor"], after=after)

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("get_pipeline", {"pipeline_id": 901, "cursor": cursor})
    assert route.call_count == 1


@respx.mock
async def test_list_cursor_is_rejected_by_get_pipeline(gitlab_env):
    """A cursor from list_pipelines cannot resume get_pipeline, because each tool binds its own scope."""
    # Arrange
    rest = respx.get(PIPELINES_URL).mock(side_effect=paged_pipelines)
    graphql = respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node()))
    first = await call_tool("list_pipelines", {"limit": 1})

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("get_pipeline", {"pipeline_id": 901, "cursor": first["next_cursor"]})
    assert rest.call_count == 1
    assert graphql.call_count == 0


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (graphql_response(None), "Project 'group/project' not found"),
        (pipeline_response(None), "Pipeline 901 not found in 'group/project'"),
        (graphql_response({"pipeline": pipeline_node() | {"jobs": None}}), "unavailable"),
        (httpx.Response(200, json={"errors": [{"message": "Forbidden"}]}), "GitLab GraphQL error: Forbidden"),
        (
            pipeline_response(pipeline_node(job_node("Ci::Unknown", 11))),
            "GitLab returned a job of an unknown type: Ci::Unknown",
        ),
        (
            pipeline_response(pipeline_node(job_node("Ci::Build", 0))),
            "GitLab returned an ID in an unexpected format.",
        ),
        (
            pipeline_response(
                pipeline_node(
                    job_node("Ci::Build", 11, downstreamPipeline={"id": "gid://gitlab/Ci::Build/5", "project": None}),
                ),
            ),
            "GitLab returned an ID of type Ci::Build, expected Ci::Pipeline.",
        ),
        (
            pipeline_response(pipeline_node(has_next=True, cursor=None)),
            "GitLab reported more jobs but did not return a cursor.",
        ),
    ],
    ids=[
        "project-null",
        "pipeline-null",
        "jobs-null",
        "graphql-error",
        "unknown-job-model",
        "malformed-job-gid",
        "downstream-wrong-model",
        "has-next-without-cursor",
    ],
)
@respx.mock
async def test_unusable_graphql_responses_raise_tool_errors(gitlab_env, response, message):
    """Null objects, unknown IDs, and missing cursors raise ToolErrors. Null jobs are never reported as empty."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=response)

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_pipeline", {"pipeline_id": 901})


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"project": ""}, "Pass a project path"),
        ({"pipeline_id": 0}, "greater than or equal to 1"),
        ({"pipeline_id": 901, "statuses": []}, "Pass at least one job status"),
    ],
    ids=["empty-project", "zero-pipeline", "empty-statuses"],
)
@respx.mock
async def test_invalid_get_pipeline_arguments_fail_before_network(gitlab_env, arguments, message):
    """Empty or invalid arguments fail before the GraphQL request is sent."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node()))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_pipeline", {"pipeline_id": 901} | arguments)
    assert route.call_count == 0
