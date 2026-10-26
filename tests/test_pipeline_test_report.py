"""Behavioral tests for bounded pipeline test report access 🧪."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from gitlab_mcp.ci_common import decode_cursor, encode_cursor
from mcp_helpers import call_tool

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
PIPELINE_ID = 901
SECOND_REQUEST_INDEX = 1
TWO_REQUESTS = 2
SUMMARY_HEADER = "name\ttotal\tsuccess\tfailed\tskipped\terror\ttime_s\tbuild_ids\tbuild_ids_truncated"
CASES_HEADER = "status\tname\tclassname\tfile"


async def report(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call get_pipeline_test_report through the in-memory MCP client."""
    defaults = {"project": PROJECT, "pipeline_id": PIPELINE_ID}
    return await call_tool("get_pipeline_test_report", defaults | (arguments or {}))


def summary_response(
    summary: dict[str, Any] | None,
    *,
    project_found: bool = True,
    pipeline_found: bool = True,
) -> httpx.Response:
    """Wrap a report summary in the project and pipeline GraphQL response shape."""
    pipeline = {"testReportSummary": summary} if pipeline_found else None
    project = {"pipeline": pipeline} if project_found else None
    return httpx.Response(200, json={"data": {"project": project}})


def summary_page(
    *suites: dict[str, Any],
    total: dict[str, Any] | None = None,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Build one test report summary and suite page."""
    return {
        "total": total,
        "testSuites": {"nodes": list(suites), "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}},
    }


def suite_node(**overrides: Any) -> dict[str, Any]:
    """Build one test suite row."""
    return {
        "name": "unit tests",
        "totalTime": 1.25,
        "totalCount": 5,
        "successCount": 3,
        "failedCount": 1,
        "skippedCount": 1,
        "errorCount": 0,
        "buildIds": ["11", "12"],
    } | overrides


def case_page(
    *cases: dict[str, Any],
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Build one test cases connection page."""
    return {"nodes": list(cases), "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}


def cases_response(
    cases: dict[str, Any] | None,
    *,
    project_found: bool = True,
    pipeline_found: bool = True,
) -> httpx.Response:
    """Wrap a test cases page in a project and pipeline GraphQL response."""
    pipeline = {"testSuite": {"testCases": cases}} if pipeline_found and cases is not None else None
    if pipeline_found and cases is None:
        pipeline = {"testSuite": None}
    project = {"pipeline": pipeline} if project_found else None
    return httpx.Response(200, json={"data": {"project": project}})


def sent_request(route: respx.Route, index: int = -1) -> tuple[str, dict[str, Any]]:
    """Return query text and variables from one captured GraphQL request."""
    body = json.loads(route.calls[index].request.content)
    return body["query"], body["variables"]


@respx.mock
async def test_summary_returns_totals_and_suite_rows_in_one_request(gitlab_env):
    """Summary mode maps totals and suite fields without requesting case details."""
    # Arrange
    total = {"count": 5, "success": 3, "failed": 1, "skipped": 1, "error": 0, "time": 1.25}
    summary = summary_page(suite_node(), total=total)
    route = respx.post(GRAPHQL).mock(return_value=summary_response(summary))

    # Act
    data = await report()

    # Assert
    query, variables = sent_request(route)
    assert route.call_count == 1
    assert variables == {
        "fullPath": PROJECT,
        "pipelineId": f"gid://gitlab/Ci::Pipeline/{PIPELINE_ID}",
        "first": 20,
        "after": None,
    }
    assert "$pipelineId: CiPipelineID!" in query
    assert "testReportSummary" in query
    assert "testSuites(first: $first, after: $after)" in query
    assert "testCases" not in query
    assert "buildIds" not in variables
    assert data == {
        "pipeline_id": PIPELINE_ID,
        "available": True,
        "total_count": 5,
        "success_count": 3,
        "failed_count": 1,
        "skipped_count": 1,
        "error_count": 0,
        "time_s": 1.25,
        "suites": f"{SUMMARY_HEADER}\nunit tests\t5\t3\t1\t1\t0\t1.25\t11,12\tfalse",
    }


@respx.mock
async def test_null_total_fields_stay_unknown(gitlab_env):
    """A report with unknown totals omits those fields instead of inventing zero counts."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=summary_response(summary_page(suite_node(), total=None)))

    # Act
    data = await report()

    # Assert
    assert route.call_count == 1
    assert data["available"] is True
    assert "total_count" not in data
    assert "success_count" not in data
    assert data["suites"].startswith(SUMMARY_HEADER)


@respx.mock
async def test_case_mode_sends_numeric_build_ids_and_only_returns_failed_cases(gitlab_env):
    """Case mode sorts and deduplicates build IDs, scans one page, and returns only failures and errors."""
    # Arrange
    connection = case_page(
        {"status": "PASSED", "name": "ok", "classname": "A", "file": "a.py"},
        {"status": "fAiLeD", "name": "bad\tcase\nagain", "classname": "B", "file": "b.py", "stackTrace": "secret"},
        {"status": "ERROR", "name": "broken", "classname": "C", "file": "c.py", "systemOutput": "secret"},
    )
    route = respx.post(GRAPHQL).mock(return_value=cases_response(connection))

    # Act
    data = await report({"build_ids": [22, 11, 22]})

    # Assert
    query, variables = sent_request(route)
    assert route.call_count == 1
    assert variables == {
        "fullPath": PROJECT,
        "pipelineId": f"gid://gitlab/Ci::Pipeline/{PIPELINE_ID}",
        "buildIds": ["11", "22"],
        "first": 20,
        "after": None,
    }
    assert "testSuite(buildIds: $buildIds)" in query
    assert "testCases(first: $first, after: $after)" in query
    assert "stackTrace" not in query
    assert "systemOutput" not in query
    assert data == {
        "pipeline_id": PIPELINE_ID,
        "available": True,
        "cases": f"{CASES_HEADER}\nfailed\tbad case again\tB\tb.py\nerror\tbroken\tC\tc.py",
        "cases_scanned": 3,
    }


@pytest.mark.parametrize("mode", ["summary", "cases"], ids=["summary", "cases"])
@respx.mock
async def test_empty_page_can_still_have_a_next_cursor(gitlab_env, mode):
    """An empty current page does not imply success when GitLab says another page exists."""
    # Arrange
    has_cases = mode == "cases"
    if has_cases:
        response = cases_response(case_page(has_next=True, cursor="cursor-2"))
    else:
        total = {"count": 0, "success": 0, "failed": 0, "skipped": 0, "error": 0, "time": 0}
        response = summary_response(summary_page(total=total, has_next=True, cursor="cursor-2"))
    route = respx.post(GRAPHQL).mock(return_value=response)

    # Act
    data = await report({"build_ids": [11]} if has_cases else None)

    # Assert
    assert route.call_count == 1
    assert data["next_cursor"]
    if has_cases:
        assert data["cases"] == CASES_HEADER
        assert data["cases_scanned"] == 0
    else:
        assert data["suites"] == SUMMARY_HEADER


def paged_response(request: httpx.Request) -> httpx.Response:
    """Return a summary or case page with a cursor, then a final page after it."""
    variables = json.loads(request.content)["variables"]
    has_builds = "buildIds" in variables
    if variables["after"] is None:
        if has_builds:
            return cases_response(case_page(has_next=True, cursor="case-page-2"))
        return summary_response(summary_page(total={}, has_next=True, cursor="summary-page-2"))
    if has_builds:
        connection = case_page({"status": "FAILED", "name": "later", "classname": "C", "file": "x.py"})
        return cases_response(connection)
    return summary_response(summary_page(suite_node(name="later"), total={}))


@pytest.mark.parametrize("mode", ["summary", "cases"], ids=["summary", "cases"])
@respx.mock
async def test_cursor_resumes_the_same_mode_on_the_next_page(gitlab_env, mode):
    """The next cursor requests another page in the same mode and keeps normalized build IDs."""
    # Arrange
    route = respx.post(GRAPHQL).mock(side_effect=paged_response)
    arguments = {"limit": 1} | ({"build_ids": [22, 11]} if mode == "cases" else {})
    first = await report(arguments)

    # Act
    repeated_arguments = {"limit": 1, "cursor": first["next_cursor"]}
    if mode == "cases":
        repeated_arguments["build_ids"] = [11, 22, 11]
    second = await report(repeated_arguments)

    # Assert
    assert route.call_count == TWO_REQUESTS
    expected_variables = {
        "fullPath": PROJECT,
        "pipelineId": f"gid://gitlab/Ci::Pipeline/{PIPELINE_ID}",
        "first": 1,
        "after": "case-page-2" if mode == "cases" else "summary-page-2",
    }
    if mode == "cases":
        expected_variables["buildIds"] = ["11", "22"]
        assert second["cases"].endswith("\nfailed\tlater\tC\tx.py")
    else:
        assert second["suites"].endswith("\nlater\t5\t3\t1\t1\t0\t1.25\t11,12\tfalse")
    assert sent_request(route, SECOND_REQUEST_INDEX)[1] == expected_variables
    assert "next_cursor" not in second


@pytest.mark.parametrize(
    "changed",
    [
        {"project": "other/project"},
        {"pipeline_id": 902},
        {"build_ids": [12]},
        {"limit": 2},
    ],
    ids=["project", "pipeline", "build-ids", "limit"],
)
@respx.mock
async def test_cursor_rejects_changed_scope_before_network(gitlab_env, changed):
    """A cursor cannot be reused after its project, pipeline, build IDs, or limit changes."""
    # Arrange
    route = respx.post(GRAPHQL).mock(side_effect=paged_response)
    first = await report({"build_ids": [11, 22], "limit": 1})

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await call_tool(
            "get_pipeline_test_report",
            {"project": PROJECT, "pipeline_id": PIPELINE_ID, "build_ids": [11, 22], "limit": 1}
            | changed
            | {"cursor": first["next_cursor"]},
        )
    assert route.call_count == 1


@pytest.mark.parametrize(
    "cursor",
    ["not-a-cursor!", encode_cursor({"scope": {}, "after": ""}), encode_cursor({"scope": {}, "after": 5})],
)
@respx.mock
async def test_malformed_cursor_fails_before_network(gitlab_env, cursor):
    """Malformed cursor text, a missing position, and a non-string position fail locally."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=cases_response(case_page()))

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await report({"build_ids": [11], "cursor": cursor})
    assert route.call_count == 0


@respx.mock
async def test_tampered_cursor_scope_fails_before_network(gitlab_env):
    """Changing a cursor's embedded scope cannot be used with the original arguments."""
    # Arrange
    route = respx.post(GRAPHQL).mock(side_effect=paged_response)
    first = await report({"build_ids": [11], "limit": 1})
    payload = decode_cursor(first["next_cursor"])
    payload["scope"]["project"] = "other/project"
    tampered = encode_cursor(payload)

    # Act / Assert
    with pytest.raises(ToolError, match="Invalid cursor"):
        await report({"build_ids": [11], "limit": 1, "cursor": tampered})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"project": " "}, "Pass a project path"),
        ({"pipeline_id": 0}, "greater than or equal to 1"),
        ({"build_ids": []}, "at least 1 item"),
        ({"build_ids": [1, 0]}, "greater than or equal to 1"),
        ({"build_ids": [True]}, "valid integer"),
        ({"build_ids": [1] * 21}, "at most 20 items"),
        ({"limit": 0}, "greater than or equal to 1"),
        ({"limit": 101}, "less than or equal to 100"),
    ],
    ids=[
        "blank-project",
        "bad-pipeline",
        "empty-builds",
        "nonpositive-build",
        "bool-build",
        "many-builds",
        "small-limit",
        "large-limit",
    ],
)
@respx.mock
async def test_invalid_inputs_fail_before_network(gitlab_env, arguments, message):
    """Invalid project and numeric arguments are rejected before a GraphQL request."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=summary_response(summary_page(total={})))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("get_pipeline_test_report", {"project": PROJECT, "pipeline_id": PIPELINE_ID} | arguments)
    assert route.call_count == 0


@respx.mock
async def test_suites_cap_text_and_build_ids(gitlab_env):
    """Suite text is flattened and capped, and only the first 20 build IDs are returned."""
    # Arrange
    node = suite_node(name="x" * 250 + "\tcontinued", buildIds=[str(value) for value in range(25)])
    respx.post(GRAPHQL).mock(return_value=summary_response(summary_page(node, total={})))

    # Act
    data = await report()

    # Assert
    cells = data["suites"].split("\n")[1].split("\t")
    assert len(cells) == len(SUMMARY_HEADER.split("\t"))
    assert cells[0] == "x" * 197 + "..."
    assert cells[7] == ",".join(str(value) for value in range(20))
    assert cells[8] == "true"


@pytest.mark.parametrize("mode", ["summary", "cases"], ids=["summary", "cases"])
@respx.mock
async def test_null_report_is_unavailable_with_header_only(gitlab_env, mode):
    """A null summary or suite is unavailable, not a fabricated empty report."""
    # Arrange
    response = summary_response(None) if mode == "summary" else cases_response(None)
    route = respx.post(GRAPHQL).mock(return_value=response)

    # Act
    data = await report({"build_ids": [11]} if mode == "cases" else None)

    # Assert
    assert route.call_count == 1
    assert data["available"] is False
    assert data["pipeline_id"] == PIPELINE_ID
    assert "next_cursor" not in data
    if mode == "summary":
        assert data == {"pipeline_id": PIPELINE_ID, "available": False, "suites": SUMMARY_HEADER}
        assert "total_count" not in data
    else:
        assert data == {"pipeline_id": PIPELINE_ID, "available": False, "cases": CASES_HEADER, "cases_scanned": 0}


@pytest.mark.parametrize("mode", ["summary", "cases"])
@respx.mock
async def test_null_connection_is_unavailable_with_header_only(gitlab_env, mode):
    """A null suites or cases connection is unavailable rather than an empty available report."""
    # Arrange
    pipeline = (
        {"testReportSummary": {"total": None, "testSuites": None}}
        if mode == "summary"
        else {"testSuite": {"testCases": None}}
    )
    response = httpx.Response(200, json={"data": {"project": {"pipeline": pipeline}}})
    route = respx.post(GRAPHQL).mock(return_value=response)

    # Act
    data = await report({"build_ids": [11]} if mode == "cases" else None)

    # Assert
    assert route.call_count == 1
    expected = {"pipeline_id": PIPELINE_ID, "available": False}
    if mode == "summary":
        expected["suites"] = SUMMARY_HEADER
    else:
        expected |= {"cases": CASES_HEADER, "cases_scanned": 0}
    assert data == expected


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (summary_response(None, project_found=False), "Project .* not found"),
        (summary_response(None, pipeline_found=False), "Pipeline 901 not found"),
    ],
    ids=["missing-project", "missing-pipeline"],
)
@respx.mock
async def test_missing_project_or_pipeline_is_an_error(gitlab_env, response, message):
    """A null project or pipeline is not reported as an unavailable test report."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=response)

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await report()


@pytest.mark.parametrize("mode", ["summary", "cases"], ids=["summary", "cases"])
@respx.mock
async def test_more_pages_without_end_cursor_is_an_error(gitlab_env, mode):
    """GitLab cannot report another page without giving a cursor."""
    # Arrange
    response = (
        summary_response(summary_page(total={}, has_next=True))
        if mode == "summary"
        else cases_response(case_page(has_next=True))
    )
    respx.post(GRAPHQL).mock(return_value=response)

    # Act / Assert
    with pytest.raises(ToolError, match="did not return a cursor"):
        await report({"build_ids": [11]} if mode == "cases" else None)


@respx.mock
async def test_graphql_errors_propagate(gitlab_env):
    """GraphQL errors propagate through the shared GitLab client."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"errors": [{"message": "Denied"}]}))

    # Act / Assert
    with pytest.raises(ToolError, match="GitLab GraphQL error: Denied"):
        await report()
