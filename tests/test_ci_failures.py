"""Behavioral tests for the batched pipeline failure reader 🧯."""

import base64
import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from mcp_helpers import call_tool

pytestmark = pytest.mark.usefixtures("gitlab_env")

API = "https://gitlab.example.com/api/v4"
GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
ENCODED_PROJECT = "group%2Fproject"
PIPELINE_ID = 901
JOBS_URL = f"{API}/projects/{ENCODED_PROJECT}/pipelines/{PIPELINE_ID}/jobs"
JOB_HEADER = "job_id\tname\tstage\tstatus\tallow_failure\tfailure_reason\tduration_s"
TWO_HTTP_REQUESTS = 2
TWO_FAILED_ROWS = 2
WARNING_CAP = 200
TEXT_CAP = 200
LOG_CHAR_LIMIT = 20
MAX_BATCH_JOBS = 10
MAX_BATCH_LOG_CHARS = 10_000
DEFAULT_LOG_CHARS = 1000


def rest_job(job_id: int, **overrides: Any) -> dict[str, Any]:
    """Build a failed build-job row like the REST pipeline jobs endpoint returns."""
    return {
        "id": job_id,
        "name": f"job-{job_id}",
        "stage": "test",
        "status": "failed",
        "allow_failure": False,
        "failure_reason": "script_failure",
        "duration": 12.5,
    } | overrides


def jobs_response(*jobs: dict[str, Any], next_page: str = "") -> httpx.Response:
    """Return one REST jobs page with its next-page header."""
    return httpx.Response(200, json=list(jobs), headers={"X-Next-Page": next_page})


def graphql_route(payload: dict[str, Any]) -> respx.Route:
    """Mock a successful GraphQL response containing the supplied data object."""
    return respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": payload}))


def graph_jobs(*summaries: str | None) -> dict[str, Any]:
    """Build aliased project job results for summaries in input order."""
    return {
        "project": {
            f"job_{index}": {"trace": {"htmlSummary": summary}} if summary is not None else None
            for index, summary in enumerate(summaries)
        },
    }


def sent_params(route: respx.Route) -> dict[str, str]:
    """Return the query parameters from a REST request received by `route`."""
    return dict(route.calls.last.request.url.params)


def sent_graphql(route: respx.Route) -> dict[str, Any]:
    """Decode the GraphQL request body received by `route`."""
    return json.loads(route.calls.last.request.content)


def decode_cursor(cursor: str) -> dict[str, Any]:
    """Decode a cursor payload for assertions about its bound scope and page."""
    return json.loads(base64.urlsafe_b64decode(cursor))


@respx.mock
async def test_multi_job_page_uses_rest_order_and_one_batched_graphql_request():
    """REST keeps job order and failure details while all summaries share one aliased query."""
    # Arrange
    rest_route = respx.get(JOBS_URL).mock(
        return_value=jobs_response(
            rest_job(11, allow_failure=True),
            rest_job(12, name="job\n12", failure_reason="runner_system_failure"),
            rest_job(13, status="success"),
        ),
    )
    graphql = graphql_route(graph_jobs("first log", "second log"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": PIPELINE_ID})

    # Assert
    assert len(respx.calls) == TWO_HTTP_REQUESTS
    assert sent_params(rest_route) == {
        "scope[]": "failed",
        "include_retried": "false",
        "per_page": "5",
        "page": "1",
    }
    sent = sent_graphql(graphql)
    assert "job_0: job(id: $job0)" in sent["query"]
    assert "job_1: job(id: $job1)" in sent["query"]
    assert "htmlSummary(lastLines: 100)" in sent["query"]
    assert sent["variables"] == {
        "project": PROJECT,
        "job0": "gid://gitlab/Ci::Build/11",
        "job1": "gid://gitlab/Ci::Build/12",
    }
    assert data["jobs"] == (
        f"{JOB_HEADER}\n"
        "11\tjob-11\ttest\tfailed\ttrue\tscript_failure\t12\n"
        "12\tjob 12\ttest\tfailed\tfalse\trunner_system_failure\t12"
    )
    assert [entry["job_id"] for entry in data["logs"]] == [11, 12]
    assert [entry["text"] for entry in data["logs"]] == ["first log", "second log"]
    assert "safety_note" in data


@respx.mock
async def test_job_name_stage_and_failure_reason_are_sanitized_and_capped():
    """TSV strings are flattened to one cell and capped at 200 characters."""
    # Arrange
    job = rest_job(
        11,
        name="n" * 250,
        stage="build\nphase",
        failure_reason="failure\t" + "r" * 220,
    )
    respx.get(JOBS_URL).mock(return_value=jobs_response(job))

    # Act
    data = await call_tool(
        "get_pipeline_failures",
        {"project": PROJECT, "pipeline_id": PIPELINE_ID, "include_logs": False},
    )

    # Assert
    header, row = data["jobs"].splitlines()
    assert header == JOB_HEADER
    cells = row.split("\t")
    assert cells[1] == "n" * (TEXT_CAP - 3) + "..."
    assert cells[2] == "build phase"
    assert len(cells[5]) == TEXT_CAP
    assert cells[5].startswith("failure ")
    assert cells[5].endswith("...")


@respx.mock
async def test_zero_duration_is_preserved_and_null_duration_stays_blank():
    """A real zero duration remains visible while a null duration renders as an empty TSV cell."""
    # Arrange
    jobs = [rest_job(11, duration=12.6), rest_job(12, duration=0), rest_job(13, duration=None)]
    respx.get(JOBS_URL).mock(return_value=jobs_response(*jobs))

    # Act
    data = await call_tool(
        "get_pipeline_failures",
        {"project": PROJECT, "pipeline_id": PIPELINE_ID, "include_logs": False},
    )

    # Assert
    lines = data["jobs"].splitlines()
    assert lines[1].endswith("\t13")
    assert lines[2].endswith("\t0")
    assert lines[3].endswith("\t")


@respx.mock
async def test_uncut_log_summary_does_not_claim_full_trace_completeness():
    """An uncut summary is still explicitly limited to GitLab's last 100 lines."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=jobs_response(rest_job(11)))
    graphql_route(graph_jobs("latest summary output"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": PIPELINE_ID})

    # Assert
    assert data["logs"][0]["truncated"] is False
    assert "last 100 lines" in data["safety_note"]
    assert "even when truncated is false" in data["safety_note"]


@pytest.mark.parametrize(
    ("include_logs", "jobs"),
    [(False, (rest_job(11),)), (True, ())],
    ids=["logs-disabled", "no-jobs"],
)
@respx.mock
async def test_no_log_batch_makes_only_the_rest_request(include_logs, jobs):
    """Disabled summaries and empty pages do not make a GraphQL request."""
    # Arrange
    rest_route = respx.get(JOBS_URL).mock(return_value=jobs_response(*jobs))
    graphql = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": graph_jobs()}))

    # Act
    data = await call_tool(
        "get_pipeline_failures",
        {"project": PROJECT, "pipeline_id": PIPELINE_ID, "include_logs": include_logs},
    )

    # Assert
    assert rest_route.call_count == 1
    assert graphql.call_count == 0
    assert len(respx.calls) == 1
    assert data["logs"] == []
    if not jobs:
        assert data["jobs"] == JOB_HEADER


@respx.mock
async def test_null_summaries_are_unavailable_but_empty_summary_is_available():
    """Null job, trace, and summary values differ from a real empty summary."""
    # Arrange
    rest_route = respx.get(JOBS_URL).mock(return_value=jobs_response(*(rest_job(number) for number in range(1, 5))))
    graphql = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "project": {
                        "job_0": None,
                        "job_1": {"trace": None},
                        "job_2": {"trace": {"htmlSummary": None}},
                        "job_3": {"trace": {"htmlSummary": ""}},
                    },
                },
            },
        ),
    )

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": PIPELINE_ID})

    # Assert
    assert rest_route.call_count == graphql.call_count == 1
    assert [log["available"] for log in data["logs"]] == [False, False, False, True]
    assert [log["text"] for log in data["logs"]] == ["", "", "", ""]
    assert [log["chars"] for log in data["logs"]] == [0, 0, 0, 0]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"errors": [{"message": "Denied"}], "data": None}),
        httpx.Response(503, json={"message": "X" * 300}),
    ],
    ids=["graphql-error", "graphql-http-error"],
)
@respx.mock
async def test_graphql_error_keeps_rest_rows_and_marks_logs_unavailable(response):
    """Any GraphQL ToolError is surfaced once as a capped warning without losing REST rows."""
    # Arrange
    jobs = [rest_job(11), rest_job(12)]
    rest_route = respx.get(JOBS_URL).mock(return_value=jobs_response(*jobs))
    graphql = respx.post(GRAPHQL).mock(return_value=response)

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": PIPELINE_ID})

    # Assert
    assert rest_route.call_count == graphql.call_count == 1
    assert data["jobs"].count("\n") == TWO_FAILED_ROWS
    assert data["warning"]
    assert len(data["warning"]) <= WARNING_CAP
    assert [log["available"] for log in data["logs"]] == [False, False]


@respx.mock
async def test_rest_errors_propagate_without_a_graphql_fallback():
    """REST errors stay visible and stop before the optional summary request."""
    # Arrange
    rest = respx.get(JOBS_URL).mock(return_value=httpx.Response(403, json={"message": "forbidden"}))
    graphql = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": graph_jobs()}))

    # Act / Assert
    with pytest.raises(ToolError, match="HTTP 403"):
        await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": PIPELINE_ID})
    assert rest.call_count == 1
    assert graphql.call_count == 0


@respx.mock
async def test_html_cleanup_redaction_and_tail_cut_happen_before_log_character_cap():
    """HTML is decoded, credentials are masked, then only the bounded tail is returned."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=jobs_response(rest_job(11)))
    graphql_route(graph_jobs("<span>before</span><br>token=secretXYZ after"))

    # Act
    data = await call_tool(
        "get_pipeline_failures",
        {"project": PROJECT, "pipeline_id": PIPELINE_ID, "log_chars": LOG_CHAR_LIMIT},
    )

    # Assert
    log = data["logs"][0]
    assert log == {
        "job_id": 11,
        "text": "ken=[REDACTED] after",
        "chars": 20,
        "truncated": True,
        "available": True,
    }
    assert "secretXYZ" not in log["text"]
    assert len(log["text"]) <= LOG_CHAR_LIMIT


@respx.mock
async def test_ten_job_batch_stays_within_total_log_character_bound():
    """The largest page has one summary request and at most ten thousand output log characters."""
    # Arrange
    jobs = [rest_job(number) for number in range(1, 11)]
    respx.get(JOBS_URL).mock(return_value=jobs_response(*jobs))
    graphql_route(graph_jobs(*(f"<span>{'x' * 1000}</span>" for _ in jobs)))

    # Act
    data = await call_tool(
        "get_pipeline_failures",
        {"project": PROJECT, "pipeline_id": PIPELINE_ID, "limit": MAX_BATCH_JOBS},
    )

    # Assert
    assert len(respx.calls) == TWO_HTTP_REQUESTS
    assert len(data["logs"]) == MAX_BATCH_JOBS
    assert sum(log["chars"] for log in data["logs"]) <= MAX_BATCH_LOG_CHARS
    assert all(log["chars"] == DEFAULT_LOG_CHARS and not log["truncated"] for log in data["logs"])


def paged_jobs(request: httpx.Request) -> httpx.Response:
    """Return the first page with a next-page header, then an empty final page."""
    if request.url.params["page"] == "1":
        return jobs_response(rest_job(11), next_page="2")
    return jobs_response(rest_job(12))


@respx.mock
async def test_next_cursor_binds_failure_scope_and_resumes_rest_page():
    """The cursor records every scope argument and resumes the exact next REST page."""
    # Arrange
    route = respx.get(JOBS_URL).mock(side_effect=paged_jobs)
    first = await call_tool(
        "get_pipeline_failures",
        {
            "project": PROJECT,
            "pipeline_id": PIPELINE_ID,
            "limit": 1,
            "include_logs": False,
            "log_chars": 55,
            "include_retried": True,
        },
    )

    # Act
    second = await call_tool(
        "get_pipeline_failures",
        {
            "project": PROJECT,
            "pipeline_id": PIPELINE_ID,
            "limit": 1,
            "include_logs": False,
            "log_chars": 55,
            "include_retried": True,
            "cursor": first["next_cursor"],
        },
    )

    # Assert
    assert decode_cursor(first["next_cursor"]) == {
        "scope": {
            "kind": "failures",
            "project": PROJECT,
            "pipeline_id": PIPELINE_ID,
            "limit": 1,
            "include_logs": False,
            "log_chars": 55,
            "include_retried": True,
        },
        "page": 2,
    }
    assert sent_params(route)["page"] == "2"
    assert sent_params(route)["include_retried"] == "true"
    assert "12\tjob-12" in second["jobs"]
    assert "next_cursor" not in second
    assert len(respx.calls) == TWO_HTTP_REQUESTS


@pytest.mark.parametrize(
    "changed",
    [
        {"project": "other/project"},
        {"pipeline_id": 902},
        {"limit": 2},
        {"include_logs": True},
        {"log_chars": 56},
        {"include_retried": False},
    ],
    ids=["project", "pipeline", "limit", "include-logs", "log-chars", "include-retried"],
)
@respx.mock
async def test_cursor_is_rejected_locally_when_call_scope_changes(changed):
    """A cursor cannot resume when any bound argument changes."""
    # Arrange
    route = respx.get(JOBS_URL).mock(return_value=jobs_response(rest_job(11), next_page="2"))
    args = {
        "project": PROJECT,
        "pipeline_id": PIPELINE_ID,
        "limit": 1,
        "include_logs": False,
        "log_chars": 55,
        "include_retried": True,
    }
    first = await call_tool("get_pipeline_failures", args)

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool("get_pipeline_failures", args | changed | {"cursor": first["next_cursor"]})
    assert route.call_count == 1


@pytest.mark.parametrize("page", [0, True, "2", None], ids=["zero", "boolean", "string", "null"])
@respx.mock
async def test_forged_cursor_page_must_be_a_positive_integer(page):
    """Malformed cursor positions, including booleans, fail before a second REST request."""
    # Arrange
    route = respx.get(JOBS_URL).mock(return_value=jobs_response(rest_job(11), next_page="2"))
    first = await call_tool(
        "get_pipeline_failures",
        {"project": PROJECT, "pipeline_id": PIPELINE_ID, "limit": 1, "include_logs": False},
    )
    payload = decode_cursor(first["next_cursor"])
    payload["page"] = page
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool(
            "get_pipeline_failures",
            {"project": PROJECT, "pipeline_id": PIPELINE_ID, "limit": 1, "include_logs": False, "cursor": cursor},
        )
    assert route.call_count == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"project": "  "},
        {"pipeline_id": 0},
        {"limit": 0},
        {"limit": 11},
        {"log_chars": 0},
        {"log_chars": 1001},
    ],
    ids=["blank-project", "pipeline-id", "limit-low", "limit-high", "log-chars-low", "log-chars-high"],
)
@respx.mock
async def test_invalid_project_and_schema_bounds_fail_before_rest(arguments):
    """Blank paths and schema-invalid IDs or bounds are rejected without network requests."""
    # Arrange
    rest = respx.get(JOBS_URL).mock(return_value=jobs_response())

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": PIPELINE_ID} | arguments)
    assert rest.call_count == 0


@respx.mock
async def test_invalid_cursor_text_fails_before_rest():
    """Malformed cursor text fails locally without a REST call."""
    # Arrange
    rest = respx.get(JOBS_URL).mock(return_value=jobs_response())

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool(
            "get_pipeline_failures",
            {"project": PROJECT, "pipeline_id": PIPELINE_ID, "cursor": "not-a-cursor!"},
        )
    assert rest.call_count == 0
