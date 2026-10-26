"""Job and pipeline schedule metadata readers for the CI tools 🧰."""

from datetime import datetime
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
    raise_tool_error,
)
from gitlab_mcp.client import GitLabAPIError
lazy from gitlab_mcp.client import GitLabClient

# Names, stages, failure reasons, descriptions, and refs are cut so one long value cannot dominate a reply.
TEXT_CAP = 200
MAX_PAGE_SIZE = 100  # GitLab's own limit for one GraphQL page.
NOT_FOUND = 404
SCHEDULE_MODELS = ("Ci::PipelineSchedule",)
PIPELINE_MODELS = ("Ci::Pipeline",)
SCHEDULE_COLUMNS = (
    "schedule_id",
    "description",
    "ref",
    "active",
    "cron",
    "timezone",
    "next_run",
    "owner",
    "last_pipeline_id",
    "last_status",
)
# A 404 here also happens for trigger jobs and external status checks, which have no job endpoint.
JOB_NOT_FOUND_MESSAGE = (
    "Job {job_id} was not found in {project!r}. Trigger jobs and external status checks have no job endpoint, "
    "so use get_pipeline to see them. Otherwise, the job does not exist, or the token cannot see it."
)

# One flat query per page. Variables and inputs are not selected, because they can hold secret values.
_SCHEDULES_QUERY = """
query($project: ID!, $first: Int!, $after: String, $status: PipelineScheduleStatus) {
  project(fullPath: $project) {
    pipelineSchedules(first: $first, after: $after, status: $status, sort: ID_DESC) {
      nodes {
        id description ref active cron cronTimezone nextRunAt
        owner { username }
        lastPipeline { id status }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


class JobRunner(BaseModel):
    """The runner that ran a job."""

    id: int
    description: str | None


class JobDetail(BaseModel):
    """One CI job's status, timing, failure reason, and runner. Logs, artifacts, commits, and users are not included."""

    job_id: int
    pipeline_id: int | None = Field(description="The pipeline this job belongs to.")
    name: str | None
    stage: str | None
    status: str
    allow_failure: bool | None = Field(description="True when a failure does not fail the pipeline.")
    failure_reason: str | None = Field(description="GitLab's short failure code, such as script_failure.")
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    erased_at: str | None
    duration_s: int | None = Field(description="Seconds the job ran, rounded to a whole second.")
    queued_duration_s: int | None = Field(description="Seconds the job waited for a runner, rounded to a whole second.")
    runner: JobRunner | None
    web_url: str | None


class ScheduleList(BaseModel):
    """One page of a project's pipeline schedules."""

    web_url_prefix: str = Field(description="Append a schedule_id to get the schedule link.")
    schedules: str = Field(
        description=(
            "TSV with a header row. Columns: schedule_id, description, ref, active, cron, timezone, next_run, "
            "owner, last_pipeline_id, last_status. next_run is a full ISO 8601 time. An empty last_pipeline_id "
            "means GitLab did not return a last pipeline, not proof that the schedule has never run."
        ),
    )
    next_cursor: str = Field(default="", exclude_if=lambda value: not value)  # Empty when there are no more pages.


def _require_project(project: str) -> None:
    """Reject an empty project path before any request is made."""
    if not project.strip():
        raise_tool_error("Pass a project path, like group/project.")


def _short_text(value: str | None) -> str | None:
    """Cap a short text field. A missing or empty value becomes None, so it shows as null."""
    return cap(value, TEXT_CAP) or None


def _job_path(project: str, job_id: int) -> str:
    """Return the REST path of one job, with the project path encoded as one segment."""
    quoted = quote(project, safe="")
    return f"/projects/{quoted}/jobs/{job_id}"


def _compact_timestamp(value: str | None) -> str | None:
    """Trim fractional seconds from a timestamp while preserving its timezone notation."""
    if value is None:
        return None
    timestamp = datetime.fromisoformat(value).isoformat(timespec="seconds")
    return timestamp.removesuffix("+00:00") + "Z" if value.endswith("Z") else timestamp


def _runner(body: dict[str, Any]) -> JobRunner | None:
    """Return the compact runner, or None when the job has no runner."""
    runner = body.get("runner")
    if runner is None:
        return None
    return JobRunner(id=runner["id"], description=_short_text(runner.get("description")))


async def get_job(client: GitLabClient, project: str, job_id: int) -> JobDetail:
    """Return one CI job's status, timing, failure reason, and runner, like `glab api projects/:id/jobs/:job_id`.

    Only runner jobs have a job endpoint. Trigger jobs and external status checks answer "not found", so
    the error points to `get_pipeline` for them. Logs, artifacts, commits, and users are never returned.
    Missing fields are null, not healthy defaults.
    """
    _require_project(project)
    if job_id < 1:
        raise_tool_error("Pass a positive job ID.")
    try:
        body = await client.get(_job_path(project, job_id))
    except GitLabAPIError as exc:
        if exc.status != NOT_FOUND:
            raise
        raise_tool_error(JOB_NOT_FOUND_MESSAGE.format(job_id=job_id, project=project))

    pipeline = body.get("pipeline") or {}
    return JobDetail(
        job_id=body["id"],
        pipeline_id=pipeline.get("id"),
        name=_short_text(body.get("name")),
        stage=_short_text(body.get("stage")),
        status=body["status"].lower(),
        allow_failure=body.get("allow_failure"),
        failure_reason=_short_text(body.get("failure_reason")),
        created_at=_compact_timestamp(body.get("created_at")),
        started_at=_compact_timestamp(body.get("started_at")),
        finished_at=_compact_timestamp(body.get("finished_at")),
        erased_at=_compact_timestamp(body.get("erased_at")),
        duration_s=round(body["duration"]) if body.get("duration") is not None else None,
        queued_duration_s=round(body["queued_duration"]) if body.get("queued_duration") is not None else None,
        runner=_runner(body),
        web_url=body.get("web_url"),
    )


def _status_filter(active: bool | None) -> str | None:
    """Map the `active` argument to GitLab's schedule status. None means no filter."""
    if active is None:
        return None
    return "ACTIVE" if active else "INACTIVE"


def _last_pipeline(node: dict[str, Any]) -> tuple[int | None, str | None]:
    """Return the last pipeline ID and status, or None when GitLab did not return a last pipeline."""
    last = node["lastPipeline"]
    if last is None:
        return None, None
    return numeric_gid(last["id"], PIPELINE_MODELS), last["status"].lower()


def _schedule_row(node: dict[str, Any]) -> str:
    """Render one GraphQL pipeline schedule as a TSV row. Free text is flattened and capped."""
    last_pipeline_id, last_status = _last_pipeline(node)
    owner = node["owner"] or {}  # A deleted owner is null, which shows as a blank cell.
    values = (
        numeric_gid(node["id"], SCHEDULE_MODELS),
        cap(cell(node["description"]), TEXT_CAP),
        cap(cell(node["ref"]), TEXT_CAP),
        node["active"],
        node["cron"],
        node["cronTimezone"],
        node["nextRunAt"],
        owner.get("username"),
        last_pipeline_id,
        last_status,
    )
    return "\t".join(cell(value) for value in values)


def _resume_after(cursor: str, scope: dict[str, Any]) -> str:
    """Return the resume position stored in a cursor. A cursor made for other arguments is an InvalidCursorError."""
    payload = decode_cursor(cursor)
    after = payload.get("after")
    if payload.get("scope") != scope or not isinstance(after, str) or not after:
        raise InvalidCursorError
    return after


async def list_pipeline_schedules(
    client: GitLabClient,
    project: str,
    *,
    active: bool | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> ScheduleList:
    """Return one page of a project's pipeline schedules, newest first.

    `active` keeps only active (True) or inactive (False) schedules. Leave it unset for both. `limit` is 1 to
    100. `next_cursor` fetches the next page of the same call and is empty on the last page. The cursor is
    rejected when the project, `active`, or `limit` changes. A null project or schedule list is an error.
    """
    _require_project(project)
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise_tool_error(f"Pass a limit from 1 to {MAX_PAGE_SIZE}.")

    scope = {"tool": "list_pipeline_schedules", "project": project, "active": active, "limit": limit}
    after = _resume_after(cursor, scope) if cursor is not None else None
    variables = {"project": project, "first": limit, "after": after, "status": _status_filter(active)}
    data = await client.graphql(_SCHEDULES_QUERY, variables)

    project_data = data["project"]
    if project_data is None:
        raise_tool_error(f"Project {project!r} not found, or the token cannot see it.")
    connection = project_data["pipelineSchedules"]
    if connection is None:  # GitLab sends null when it cannot read the schedules. That is not the same as none.
        raise_tool_error(f"GitLab did not return the pipeline schedules for {project!r}, so they are unavailable.")

    page_info = connection["pageInfo"]
    if page_info["hasNextPage"] and not page_info["endCursor"]:
        raise_tool_error("GitLab reported more schedules but did not return a cursor.")
    rows = [_schedule_row(node) for node in connection["nodes"]]
    return ScheduleList(
        web_url_prefix=f"{client.settings.url}/{project}/-/pipeline_schedules/",
        schedules="\n".join(["\t".join(SCHEDULE_COLUMNS), *rows]),
        next_cursor=(
            encode_cursor({"scope": scope, "after": page_info["endCursor"]}) if page_info["hasNextPage"] else ""
        ),
    )
