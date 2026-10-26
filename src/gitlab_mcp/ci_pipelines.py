"""Pipeline metadata readers for the CI tools 🚦."""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field

from gitlab_mcp.ci_common import (
    InvalidCursorError,
    cap,
    cell,
    decode_cursor,
    encode_cursor,
    numeric_gid,
    parse_gid,
    raise_tool_error,
)
lazy from gitlab_mcp.client import GitLabClient

# Names, stages, and failure messages are cut so one long value cannot dominate a page.
# Project paths are never cut, because an agent needs the exact path to act on a downstream pipeline.
TEXT_CAP = 200
PIPELINE_MODELS = ("Ci::Pipeline",)
# Each job GID model maps to the job type shown to the agent.
JOB_TYPES = {
    "Ci::Build": "build",
    "Ci::Bridge": "bridge",
    "GenericCommitStatus": "external",
    "CommitStatus": "external",
}
LIST_COLUMNS = ("pipeline_id", "name", "status", "source", "ref", "sha", "created", "updated")
JOB_COLUMNS = (
    "job_id",
    "type",
    "name",
    "stage",
    "status",
    "allow_failure",
    "duration_s",
    "scheduled_at",
    "downstream_project",
    "downstream_pipeline_id",
    "failure_message",
)
TIMESTAMP_MESSAGE = "updated_after must be a timezone-aware ISO 8601 datetime, such as 2026-10-01T12:30:00Z."

# One flat query per page. The job type comes from each job's GID model, so no kind field is selected.
_PIPELINE_QUERY = """
query(
  $fullPath: ID!, $pipelineId: CiPipelineID!, $first: Int!, $after: String,
  $retried: Boolean, $statuses: [CiJobStatus!]
) {
  project(fullPath: $fullPath) {
    pipeline(id: $pipelineId) {
      id status source ref sha createdAt startedAt finishedAt duration
      mergeRequest { iid }
      jobs(first: $first, after: $after, retried: $retried, statuses: $statuses) {
        nodes {
          id name status allowFailure duration scheduledAt failureMessage
          stage { name }
          downstreamPipeline { id project { fullPath } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


class PipelineList(BaseModel):
    """One page of pipelines from a merge request, a pipeline schedule, or a project."""

    web_url_prefix: str = Field(description="Append a pipeline_id to get the pipeline link.")
    pipelines: str = Field(
        description=(
            "TSV with a header row. Columns: pipeline_id, name, status, source, ref, sha, created, updated. "
            "Timestamps have second precision. An empty cell means unknown."
        ),
    )
    next_cursor: str = Field(default="", exclude_if=lambda value: not value)  # Empty when there are no more pages.


class PipelineDetail(BaseModel):
    """One pipeline's metadata and one page of its jobs."""

    pipeline_id: int
    mr_iid: int | None = Field(default=None, exclude_if=lambda value: value is None)
    status: str
    source: str = Field(default="", exclude_if=lambda value: not value)
    ref: str
    sha: str
    created_at: str
    started_at: str | None = Field(default=None, exclude_if=lambda value: value is None)
    finished_at: str | None = Field(default=None, exclude_if=lambda value: value is None)
    duration_s: int | None = Field(default=None, exclude_if=lambda value: value is None)
    web_url: str
    jobs: str = Field(
        description=(
            "TSV with a header row. Columns: job_id, type, name, stage, status, allow_failure, duration_s, "
            "scheduled_at, downstream_project, downstream_pipeline_id, failure_message. type is build, bridge, "
            "or external. Empty downstream cells mean there is no downstream pipeline or GitLab did not return it."
        ),
    )
    next_cursor: str = Field(default="", exclude_if=lambda value: not value)  # Empty when no matching jobs remain.


def _require_project(project: str) -> None:
    """Reject an empty project path before any request is made."""
    if not project.strip():
        raise_tool_error("Pass a project path, like group/project.")


def _utc_timestamp(value: str) -> str:
    """Validate a timezone-aware ISO 8601 datetime and return it in UTC, the form GitLab expects.

    Date-only and timezone-naive values fail, because they would depend on the server's time zone.
    """
    try:
        parsed = datetime.fromisoformat(value)
        utc = parsed.astimezone(UTC) if parsed.utcoffset() is not None else None
    except OverflowError, ValueError:
        utc = None
    if utc is None:
        raise_tool_error(TIMESTAMP_MESSAGE)
    return utc.isoformat().replace("+00:00", "Z")


def _resume_value(cursor: str, scope: dict[str, Any], key: str) -> Any:
    """Return the resume value stored under `key`. A cursor made for other arguments is an InvalidCursorError."""
    payload = decode_cursor(cursor)
    if payload.get("scope") != scope or key not in payload:
        raise InvalidCursorError
    return payload[key]


def _is_page_number(value: object) -> bool:
    """Return True for an int page number of at least 1. Booleans are ints in Python, so they are rejected too."""
    return type(value) is int and value >= 1


def _list_request(
    project: str,
    mr_iid: int | None,
    schedule_id: int | None,
    filters: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Return the REST path and query parameters for one list scope. Unset filters are left out.

    The caller has already rejected filters that the scope does not support.
    """
    quoted = quote(project, safe="")
    present = {name: value for name, value in filters.items() if value is not None}
    if mr_iid is not None:
        return f"/projects/{quoted}/merge_requests/{mr_iid}/pipelines", {}
    if schedule_id is not None:
        return f"/projects/{quoted}/pipeline_schedules/{schedule_id}/pipelines", {"sort": "desc"} | present
    return f"/projects/{quoted}/pipelines", {"order_by": "id", "sort": "desc"} | present


def _list_row(pipeline: dict[str, Any]) -> str:
    """Render one REST pipeline as a TSV row with timestamps at second precision."""
    values = (
        pipeline["id"],
        pipeline.get("name"),
        pipeline["status"],
        pipeline.get("source"),
        pipeline["ref"],
        pipeline["sha"],
        _iso_seconds(pipeline["created_at"]),
        _iso_seconds(pipeline["updated_at"]),
    )
    return "\t".join(cell(value) for value in values)


def _iso_seconds(value: str) -> str:
    """Format an ISO 8601 timestamp to seconds without changing its timezone."""
    formatted = datetime.fromisoformat(value).isoformat(timespec="seconds")
    return formatted[:-6] + "Z" if value.endswith("Z") else formatted


async def list_pipelines(  # noqa: PLR0913 - each argument is one filter or paging input.
    client: GitLabClient,
    project: str,
    *,
    mr_iid: int | None = None,
    schedule_id: int | None = None,
    ref: str | None = None,
    status: str | None = None,
    source: str | None = None,
    updated_after: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> PipelineList:
    """Return one page of pipelines, newest first, like `glab api projects/:id/pipelines`.

    Pass at most one of `mr_iid` and `schedule_id`. A merge request list takes no filters. A schedule list
    takes `status` and `updated_after`. A project list takes every filter. `updated_after` must be a
    timezone-aware ISO 8601 datetime. `next_cursor` fetches the next page of the same call and is empty on the
    last page. The cursor is rejected when any argument changes.
    """
    _require_project(project)
    if mr_iid is not None and schedule_id is not None:
        raise_tool_error("Pass either mr_iid or schedule_id, not both.")

    updated_after = _utc_timestamp(updated_after) if updated_after is not None else None
    filters = {"ref": ref, "status": status, "source": source, "updated_after": updated_after}
    if mr_iid is not None:
        allowed: set[str] = set()
    elif schedule_id is not None:
        allowed = {"status", "updated_after"}
    else:
        allowed = set(filters)
    unsupported = sorted(name for name, value in filters.items() if value is not None and name not in allowed)
    if unsupported:
        raise_tool_error(f"Not supported for this pipeline scope: {', '.join(unsupported)}.")

    scope = {
        "tool": "list_pipelines",
        "project": project,
        "mr_iid": mr_iid,
        "schedule_id": schedule_id,
        "ref": ref,
        "status": status,
        "source": source,
        "updated_after": updated_after,
        "limit": limit,
    }
    page = 1
    if cursor is not None:
        page = _resume_value(cursor, scope, "page")
        if not _is_page_number(page):
            raise InvalidCursorError

    path, params = _list_request(project, mr_iid, schedule_id, filters)
    rows, next_page = await client.get_page(path, **params, per_page=limit, page=page)
    header = "\t".join(LIST_COLUMNS)
    return PipelineList(
        web_url_prefix=f"{client.settings.url}/{project}/-/pipelines/",
        pipelines="\n".join([header, *(_list_row(row) for row in rows)]),
        next_cursor=encode_cursor({"scope": scope, "page": next_page}) if next_page is not None else "",
    )


def _job_type(model: str) -> str:
    """Return the job type for a job GID model. An unknown model is a ToolError, never a guess."""
    job_type = JOB_TYPES.get(model)
    if job_type is None:
        raise_tool_error(f"GitLab returned a job of an unknown type: {model}.")
    return job_type


def _downstream(node: dict[str, Any]) -> tuple[int | None, str | None]:
    """Return the downstream pipeline ID and project path. Both are None when the job has no downstream pipeline."""
    downstream = node["downstreamPipeline"]
    if downstream is None:
        return None, None
    project = downstream["project"] or {}
    return numeric_gid(downstream["id"], PIPELINE_MODELS), project.get("fullPath")


def _job_row(node: dict[str, Any]) -> str:
    """Render one GraphQL job as a TSV row. Long text is capped, and downstream cells can be empty."""
    model, job_id = parse_gid(node["id"])
    stage = node["stage"] or {}
    downstream_id, downstream_project = _downstream(node)
    values = (
        job_id,
        _job_type(model),
        cap(node["name"], TEXT_CAP),
        cap(stage.get("name"), TEXT_CAP),
        node["status"].lower(),
        node["allowFailure"],
        node["duration"],
        node["scheduledAt"],
        downstream_project,
        downstream_id,
        cap(node["failureMessage"], TEXT_CAP),
    )
    return "\t".join(cell(value) for value in values)


def _job_statuses(statuses: list[str] | None) -> list[str] | None:
    """Return the sorted, unique status names, or None when the caller did not filter by status."""
    if statuses is None:
        return None
    if not statuses:
        raise_tool_error("Pass at least one job status, or leave statuses unset.")
    return sorted(set(statuses))


async def get_pipeline(  # noqa: PLR0913 - each argument is one filter or paging input.
    client: GitLabClient,
    project: str,
    pipeline_id: int,
    *,
    statuses: list[str] | None = None,
    include_retried: bool = False,
    limit: int = 50,
    cursor: str | None = None,
) -> PipelineDetail:
    """Return one pipeline's metadata and one page of its jobs.

    Jobs are the latest attempt of each job by default. Set `include_retried` to include older attempts.
    `statuses` keeps only jobs with those statuses. `next_cursor` fetches the next page of matching jobs and
    is empty when none remain. The cursor is rejected when any argument changes. Timestamps are full ISO 8601
    values. An empty `jobs` table means no matching jobs, and a missing job list raises an error.
    """
    _require_project(project)
    if pipeline_id < 1:
        raise_tool_error("Pass a positive pipeline ID.")
    job_statuses = _job_statuses(statuses)
    scope = {
        "tool": "get_pipeline",
        "project": project,
        "pipeline_id": pipeline_id,
        "statuses": job_statuses,
        "include_retried": include_retried,
        "limit": limit,
    }
    after: str | None = None
    if cursor is not None:
        after = _resume_value(cursor, scope, "after")
        if not isinstance(after, str) or not after:
            raise InvalidCursorError

    variables = {
        "fullPath": project,
        "pipelineId": f"gid://gitlab/Ci::Pipeline/{pipeline_id}",
        "first": limit,
        "after": after,
        "retried": None if include_retried else False,  # None asks GitLab for both latest and retried jobs.
        "statuses": None if job_statuses is None else [status.upper() for status in job_statuses],
    }
    data = await client.graphql(_PIPELINE_QUERY, variables)
    project_data = data["project"]
    if project_data is None:
        raise_tool_error(f"Project {project!r} not found, or the token cannot see it.")
    pipeline = project_data["pipeline"]
    if pipeline is None:
        raise_tool_error(f"Pipeline {pipeline_id} not found in {project!r}, or the token cannot see it.")
    jobs = pipeline["jobs"]
    if jobs is None:  # GitLab sends null when it cannot read the jobs. That is not the same as no jobs.
        raise_tool_error(f"GitLab did not return the jobs for pipeline {pipeline_id}, so they are unavailable.")

    page_info = jobs["pageInfo"]
    if page_info["hasNextPage"] and not page_info["endCursor"]:
        raise_tool_error("GitLab reported more jobs but did not return a cursor.")
    job_rows = [_job_row(node) for node in jobs["nodes"]]
    merge_request = pipeline.get("mergeRequest")
    return PipelineDetail(
        pipeline_id=numeric_gid(pipeline["id"], PIPELINE_MODELS),
        mr_iid=int(merge_request["iid"]) if merge_request is not None else None,  # GraphQL sends IIDs as strings.
        status=pipeline["status"].lower(),
        source=(pipeline["source"] or "").lower(),
        ref=pipeline["ref"],
        sha=pipeline["sha"],
        created_at=pipeline["createdAt"],
        started_at=pipeline["startedAt"],
        finished_at=pipeline["finishedAt"],
        duration_s=pipeline["duration"],
        web_url=f"{client.settings.url}/{project}/-/pipelines/{pipeline_id}",
        jobs="\n".join(["\t".join(JOB_COLUMNS), *job_rows]),
        next_cursor=(
            encode_cursor({"scope": scope, "after": page_info["endCursor"]}) if page_info["hasNextPage"] else ""
        ),
    )
