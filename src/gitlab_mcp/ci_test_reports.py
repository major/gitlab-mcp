"""Bounded pipeline test report readers for CI tools 🧪."""

from typing import Any

from pydantic import BaseModel, Field

from gitlab_mcp.ci_common import InvalidCursorError, cap, cell, decode_cursor, encode_cursor, raise_tool_error
lazy from gitlab_mcp.client import GitLabClient

TEXT_CAP = 200
MAX_BUILD_IDS = 20
SUMMARY_COLUMNS = (
    "name",
    "total",
    "success",
    "failed",
    "skipped",
    "error",
    "time_s",
    "build_ids",
    "build_ids_truncated",
)
CASE_COLUMNS = ("status", "name", "classname", "file")

_SUMMARY_QUERY = """
query($fullPath: ID!, $pipelineId: CiPipelineID!, $first: Int!, $after: String) {
  project(fullPath: $fullPath) {
    pipeline(id: $pipelineId) {
      testReportSummary {
        total { count success failed skipped error time }
        testSuites(first: $first, after: $after) {
          nodes { name totalTime totalCount successCount failedCount skippedCount errorCount buildIds }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
"""

_CASES_QUERY = """
query($fullPath: ID!, $pipelineId: CiPipelineID!, $buildIds: [ID!]!, $first: Int!, $after: String) {
  project(fullPath: $fullPath) {
    pipeline(id: $pipelineId) {
      testSuite(buildIds: $buildIds) {
        testCases(first: $first, after: $after) {
          nodes { status name classname file }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
"""


class PipelineTestReport(BaseModel):
    """One summary or one page of failed and errored test cases."""

    pipeline_id: int
    available: bool
    total_count: int | None = Field(default=None, exclude_if=lambda value: value is None)
    success_count: int | None = Field(default=None, exclude_if=lambda value: value is None)
    failed_count: int | None = Field(default=None, exclude_if=lambda value: value is None)
    skipped_count: int | None = Field(default=None, exclude_if=lambda value: value is None)
    error_count: int | None = Field(default=None, exclude_if=lambda value: value is None)
    time_s: float | None = Field(default=None, exclude_if=lambda value: value is None)
    suites: str | None = Field(default=None, exclude_if=lambda value: value is None)
    cases: str | None = Field(default=None, exclude_if=lambda value: value is None)
    cases_scanned: int | None = Field(default=None, exclude_if=lambda value: value is None)
    next_cursor: str = Field(default="", exclude_if=lambda value: not value)


def _scope(project: str, pipeline_id: int, build_ids: list[int] | None, limit: int) -> dict[str, Any]:
    """Build the exact cursor scope, including normalized optional build IDs."""
    return {
        "kind": "test_report",
        "project": project,
        "pipeline_id": pipeline_id,
        "build_ids": build_ids,
        "limit": limit,
    }


def _resume_after(cursor: str, scope: dict[str, Any]) -> str:
    """Return a non-empty GraphQL cursor only when the cursor matches this call's scope."""
    payload = decode_cursor(cursor)
    after = payload.get("after")
    if payload.get("scope") != scope or not isinstance(after, str) or not after:
        raise InvalidCursorError
    return after


def _next_cursor(page_info: dict[str, Any], scope: dict[str, Any]) -> str:
    """Return the next cursor or fail if GitLab claims another page without a position."""
    if not page_info["hasNextPage"]:
        return ""
    after = page_info["endCursor"]
    if not isinstance(after, str) or not after:
        raise_tool_error("GitLab reported more test report data but did not return a cursor.")
    return encode_cursor({"scope": scope, "after": after})


def _suite_row(node: dict[str, Any]) -> str:
    """Render one suite summary as a bounded, sanitized TSV row."""
    build_ids = node.get("buildIds") or []
    values = (
        cap(cell(node.get("name")), TEXT_CAP),
        node.get("totalCount"),
        node.get("successCount"),
        node.get("failedCount"),
        node.get("skippedCount"),
        node.get("errorCount"),
        node.get("totalTime"),
        ",".join(cell(value) for value in build_ids[:MAX_BUILD_IDS]),
        len(build_ids) > MAX_BUILD_IDS,
    )
    return "\t".join(cell(value) for value in values)


def _case_row(node: dict[str, Any]) -> str | None:
    """Render a failed or errored case and ignore every other status."""
    status = str(node.get("status") or "").casefold()
    if status not in {"failed", "error"}:
        return None
    values = (status, *(cap(cell(node.get(key)), TEXT_CAP) for key in ("name", "classname", "file")))
    return "\t".join(cell(value) for value in values)


def _summary_result(
    pipeline_id: int,
    summary: dict[str, Any],
    scope: dict[str, Any],
) -> PipelineTestReport:
    """Map summary totals and suites into the compact report result."""
    suites = summary["testSuites"]
    if suites is None:
        return PipelineTestReport(pipeline_id=pipeline_id, available=False, suites="\t".join(SUMMARY_COLUMNS))
    total = summary.get("total") or {}
    page_info = suites["pageInfo"]
    return PipelineTestReport(
        pipeline_id=pipeline_id,
        available=True,
        total_count=total.get("count"),
        success_count=total.get("success"),
        failed_count=total.get("failed"),
        skipped_count=total.get("skipped"),
        error_count=total.get("error"),
        time_s=total.get("time"),
        suites="\n".join(["\t".join(SUMMARY_COLUMNS), *(_suite_row(node) for node in suites["nodes"])]),
        next_cursor=_next_cursor(page_info, scope),
    )


def _cases_result(pipeline_id: int, suite: dict[str, Any] | None, scope: dict[str, Any]) -> PipelineTestReport:
    """Map one test case page, retaining only failed and errored cases."""
    cases = None if suite is None else suite["testCases"]
    if cases is None:
        return PipelineTestReport(
            pipeline_id=pipeline_id,
            available=False,
            cases="\t".join(CASE_COLUMNS),
            cases_scanned=0,
        )
    page_info = cases["pageInfo"]
    rows = (_case_row(node) for node in cases["nodes"])
    return PipelineTestReport(
        pipeline_id=pipeline_id,
        available=True,
        cases="\n".join(["\t".join(CASE_COLUMNS), *(row for row in rows if row is not None)]),
        cases_scanned=len(cases["nodes"]),
        next_cursor=_next_cursor(page_info, scope),
    )


async def get_pipeline_test_report(  # noqa: PLR0913 - each argument is one filter or paging input.
    client: GitLabClient,
    project: str,
    pipeline_id: int,
    *,
    build_ids: list[int] | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> PipelineTestReport:
    """Return a pipeline summary or one bounded page of failed and errored test cases."""
    if not project.strip():
        raise_tool_error("Pass a project path, like group/project.")
    normalized_ids = None if build_ids is None else sorted(set(build_ids))
    scope = _scope(project, pipeline_id, normalized_ids, limit)
    after = _resume_after(cursor, scope) if cursor is not None else None
    variables: dict[str, Any] = {
        "fullPath": project,
        "pipelineId": f"gid://gitlab/Ci::Pipeline/{pipeline_id}",
        "first": limit,
        "after": after,
    }
    if normalized_ids is None:
        query = _SUMMARY_QUERY
    else:
        query = _CASES_QUERY
        variables["buildIds"] = [str(job_id) for job_id in normalized_ids]

    data = await client.graphql(query, variables)
    project_data = data["project"]
    if project_data is None:
        raise_tool_error(f"Project {project!r} not found, or the token cannot see it.")
    pipeline = project_data["pipeline"]
    if pipeline is None:
        raise_tool_error(f"Pipeline {pipeline_id} not found in {project!r}, or the token cannot see it.")

    if normalized_ids is None:
        summary = pipeline["testReportSummary"]
        if summary is None:
            return PipelineTestReport(pipeline_id=pipeline_id, available=False, suites="\t".join(SUMMARY_COLUMNS))
        return _summary_result(pipeline_id, summary, scope)
    return _cases_result(pipeline_id, pipeline["testSuite"], scope)
