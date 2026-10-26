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
TEXT_LIMIT = 200  # Matches the cap for names, stages, and failure messages in ci_pipelines.
LIST_HEADER = "pipeline_id\tname\tstatus\tsource\tref\tsha\tcreated\tupdated"
JOB_HEADER = (
    "job_id\ttype\tname\tstage\tstatus\tallow_failure\tduration_s\tscheduled_at"
    "\tdownstream_project\tdownstream_pipeline_id\tfailure_message"
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
        "failureMessage": None,
        "stage": {"name": "test"},
        "downstreamPipeline": None,
    }
    return node | overrides


def pipeline_node(
    *jobs: dict[str, Any],
    has_next: bool = False,
    cursor: str | None = None,
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
        "jobs": {"nodes": list(jobs), "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}},
    }
    return node | overrides


def graphql_response(project: dict[str, Any] | None) -> httpx.Response:
    """Wrap one project object in a successful GraphQL response."""
    return httpx.Response(200, json={"data": {"project": project}})


def pipeline_response(pipeline: dict[str, Any] | None) -> httpx.Response:
    """Answer a `project.pipeline` query with one pipeline, or with null when it does not exist."""
    return graphql_response({"pipeline": pipeline})


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
            failureMessage="Downstream failed",
            downstreamPipeline={"id": "gid://gitlab/Ci::Pipeline/902", "project": {"fullPath": "group/child"}},
        ),
        job_node("GenericCommitStatus", 13, name="lint", stage=None),
        job_node("CommitStatus", 14, name="scan", status="CANCELED"),
    ]
    route = respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node(*jobs)))

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Assert
    assert "$pipelineId: CiPipelineID!" in json.loads(route.calls.last.request.content)["query"]
    assert sent_variables(route) == {
        "fullPath": PROJECT,
        "pipelineId": "gid://gitlab/Ci::Pipeline/901",
        "first": 50,
        "after": None,
        "retried": False,
        "statuses": None,
    }
    expected_lines = [
        JOB_HEADER,
        "11\tbuild\tunit\ttest\tsuccess\tfalse\t12\t2026-10-01T12:00:00Z\t\t\t",
        "12\tbridge\tdeploy\tdeploy\tfailed\ttrue\t\t2026-10-01T12:00:00Z\tgroup/child\t902\tDownstream failed",
        "13\texternal\tlint\t\tsuccess\tfalse\t12\t2026-10-01T12:00:00Z\t\t\t",
        "14\texternal\tscan\ttest\tcanceled\tfalse\t12\t2026-10-01T12:00:00Z\t\t\t",
    ]
    expected_jobs = "\n".join(expected_lines)
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
async def test_long_text_is_capped_and_newlines_are_flattened(gitlab_env):
    """Names and failure messages are capped at 200 characters with "...", and line breaks become spaces."""
    # Arrange
    long_name = "n" * 250
    failure = "boom\nline two\t" + "x" * 250
    job = job_node("Ci::Build", 11, name=long_name, failureMessage=failure)
    respx.post(GRAPHQL).mock(return_value=pipeline_response(pipeline_node(job)))

    # Act
    data = await call_tool("get_pipeline", {"pipeline_id": 901})

    # Assert
    (row,) = rows(data["jobs"])
    assert row["name"] == "n" * (TEXT_LIMIT - 3) + "..."
    assert row["failure_message"].startswith("boom line two ")
    assert len(row["failure_message"]) == TEXT_LIMIT
    assert row["failure_message"].endswith("...")


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
        "retried": False,
        "statuses": ["FAILED"],
    }
    assert sent_variables(route, 1)["after"] == "cur-2"
    assert rows(second["jobs"])[0]["job_id"] == "12"
    assert "next_cursor" not in second


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
