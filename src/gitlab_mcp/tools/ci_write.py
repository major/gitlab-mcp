"""Guarded write tools for retrying CI and creating pipelines 🚦."""

import re
import unicodedata
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, WithJsonSchema
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError
from gitlab_mcp.common import HTTP_REQUEST_TIMEOUT, HTTP_SERVER_ERROR
from gitlab_mcp.mr_common import full_sha, mr_rest_path
lazy from gitlab_mcp.common import Iid, PositiveIid, Project

HTTP_CLIENT_ERROR = 400
MAX_PIPELINE_VARIABLES = 20
MAX_VARIABLE_NAME_LENGTH = 255
MAX_VARIABLE_VALUE_LENGTH = 4096
# A head pipeline in one of these states is still going, so a new MR pipeline would be a duplicate. ⏳
ACTIVE_PIPELINE_STATUSES = frozenset({"created", "pending", "running", "preparing", "waiting_for_resource"})
# GitLab answers 400 or 405 when the CI config makes no MR pipeline, for example with no merge_request_event rules.
MR_PIPELINE_REJECTED_HINT = (
    'The CI config may not define merge request pipelines (rules: if: $CI_PIPELINE_SOURCE == "merge_request_event").'
)

CiId = Annotated[int, Field(strict=True, ge=1, description="Positive numeric GitLab job or pipeline ID.")]
FullSha = Annotated[
    str,
    Field(strict=True, pattern=r"^[0-9a-fA-F]{40}$", description="Full 40-character hexadecimal commit SHA."),
]
Ref = Annotated[str, Field(strict=True, min_length=1, max_length=1024, description="Branch or tag name.")]
PIPELINE_VARIABLES_SCHEMA = {
    "type": "object",
    "maxProperties": MAX_PIPELINE_VARIABLES,
    "propertyNames": {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_VARIABLE_NAME_LENGTH,
        "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
    },
    "additionalProperties": {"type": "string", "maxLength": MAX_VARIABLE_VALUE_LENGTH},
}
PipelineVariables = Annotated[
    Any,
    Field(description="Up to 20 environment variables. Names and values are checked strictly and never returned."),
    WithJsonSchema(PIPELINE_VARIABLES_SCHEMA),
]


class CiWriteResult(BaseModel):
    """Compact result of a validated or started CI write."""

    status: Literal["validated", "started"]
    kind: Literal["job", "pipeline"]
    id: int | None = Field(default=None, exclude_if=lambda value: value is None)
    web_url: str | None = Field(default=None, exclude_if=lambda value: value is None)
    sha: str | None = Field(default=None, exclude_if=lambda value: value is None)
    ref: str | None = Field(default=None, exclude_if=lambda value: value is None)
    # GitLab's status of a new pipeline, like "created". Only create_merge_request_pipeline sets it.
    pipeline_status: str | None = Field(default=None, exclude_if=lambda value: value is None)
    warning: str = Field(default="", exclude_if=lambda value: not value)


def _project_component(project: str) -> str:
    """Validate a project path, then encode it as one REST path component."""
    forbidden = set("\\%?#")
    invalid_segment = any(segment in {"", ".", ".."} for segment in project.split("/"))
    has_control = any(unicodedata.category(character) == "Cc" for character in project)
    if not project.strip() or invalid_segment or has_control or any(char in forbidden for char in project):
        raise ToolError(  # noqa: TRY003
            "Pass a valid project path with nonempty path segments and no URL control characters."
        )
    return quote(project, safe="")


def _validate_ref(ref: str) -> str:
    """Validate a slash-separated branch or tag ref, then encode it as one REST path component."""
    has_control = any(unicodedata.category(character) == "Cc" for character in ref)
    if has_control or any(segment in {".", ".."} for segment in ref.split("/")):
        raise ToolError("ref must not contain control characters or dot path segments.")  # noqa: TRY003
    return quote(ref, safe="")


def _validate_variables(variables: Any) -> dict[str, str] | None:
    """Validate variable limits without allowing validation errors to echo secret values."""
    if variables is None:
        return None
    if not isinstance(variables, dict) or len(variables) > MAX_PIPELINE_VARIABLES:
        raise ToolError("variables must be an object with at most 20 environment variables.")  # noqa: TRY003
    for key, value in variables.items():
        if (
            not isinstance(key, str)
            or len(key) > MAX_VARIABLE_NAME_LENGTH
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None
        ):
            raise ToolError("Variable names must be ASCII environment variable names of at most 255 characters.")  # noqa: TRY003
        if not isinstance(value, str) or len(value) > MAX_VARIABLE_VALUE_LENGTH:
            raise ToolError("Variable values must be strings of at most 4096 characters.")  # noqa: TRY003
    return variables


def _resource_sha(kind: Literal["job", "pipeline"], resource: Any) -> str | None:
    """Get a full commit SHA from a GitLab job or pipeline response, if present."""
    if not isinstance(resource, dict):
        return None
    if kind == "pipeline":
        sha = resource.get("sha")
    else:
        commit = resource.get("commit")
        sha = commit.get("id") if isinstance(commit, dict) else None
        if not full_sha(sha):
            pipeline = resource.get("pipeline")
            sha = pipeline.get("sha") if isinstance(pipeline, dict) else None
    return sha if full_sha(sha) else None


def _response_id(resource: Any) -> int | None:
    """Return a valid positive numeric ID from a GitLab response."""
    if not isinstance(resource, dict):
        return None
    resource_id = resource.get("id")
    return resource_id if type(resource_id) is int and resource_id > 0 else None


def _unverified_write() -> ToolError:
    """Build the safe message used when GitLab may have accepted a write but its result is unusable."""
    return ToolError("GitLab may have accepted the write, but the outcome is unverified. Check GitLab before retrying.")


async def _send_write(
    client: GitLabClient, path: str, body: dict[str, Any] | None = None, rejected_hint: str = ""
) -> Any:
    """Send one CI write without retrying and hide request data from error messages.

    GitLab's reason for a 4xx is hidden by default, because it may echo pipeline variables. A write with no
    secret input passes `rejected_hint`, so the agent sees GitLab's reason and the hint instead.
    """
    try:
        return await client.request("POST", path, json=body)
    except GitLabConnectionError as exc:
        raise ToolError(  # noqa: TRY003
            "The CI write outcome is unknown after a connection error. Check GitLab before retrying."
        ) from exc
    except GitLabAPIError as exc:
        if exc.status == HTTP_REQUEST_TIMEOUT or exc.status >= HTTP_SERVER_ERROR:
            raise ToolError(  # noqa: TRY003
                f"The CI write outcome is unknown after HTTP {exc.status}. Check GitLab before retrying."
            ) from exc
        if HTTP_CLIENT_ERROR <= exc.status < HTTP_SERVER_ERROR:
            reason = f" {exc.detail}. {rejected_hint}" if rejected_hint and exc.detail else ""
            raise ToolError(f"GitLab rejected the CI write with HTTP {exc.status}.{reason}") from exc  # noqa: TRY003
        raise ToolError(f"GitLab returned an unexpected HTTP {exc.status} for the CI write.") from exc  # noqa: TRY003
    except ValueError as exc:
        # A successful status with invalid JSON can still mean GitLab applied the write.
        raise _unverified_write() from exc


def _started_result(kind: Literal["job", "pipeline"], response: Any) -> CiWriteResult:
    """Validate the minimum GitLab response needed to confirm a CI write."""
    response_id = _response_id(response)
    sha = _resource_sha(kind, response)
    if response_id is None or sha is None:
        raise _unverified_write()
    web_url = response.get("web_url")
    return CiWriteResult(
        status="started",
        kind=kind,
        id=response_id,
        web_url=web_url if isinstance(web_url, str) else None,
        sha=sha,
    )


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
async def retry_ci(  # noqa: PLR0913, PLR0917 - each argument is one guarded retry input.
    ctx: Context,
    project: Project,
    kind: Literal["job", "pipeline"],
    id: CiId,  # noqa: A002 - GitLab API names this argument `id`.
    expected_sha: FullSha,
    validate_only: bool = False,
) -> CiWriteResult:
    """Retry one failed or canceled CI job or pipeline, like `glab ci retry`.

    Get `expected_sha` from the target and provide its full 40-character SHA. The preflight checks the current
    status and SHA, but GitLab's retry request does not accept a SHA, so the check is not atomic. Only failed and
    canceled resources are eligible. `validate_only` runs the preflight without a POST. Writes are never retried
    automatically; check GitLab before retrying when the outcome is unknown or unverified.
    """
    encoded_project = _project_component(project)
    client = get_client(ctx)
    resource_path = f"/projects/{encoded_project}/{'jobs' if kind == 'job' else 'pipelines'}/{id}"
    resource = await client.get(resource_path)
    if not isinstance(resource, dict) or resource.get("status") not in {"failed", "canceled"}:
        raise ToolError("Only a failed or canceled CI job or pipeline can be retried.")  # noqa: TRY003
    sha = _resource_sha(kind, resource)
    if sha is None:
        raise ToolError(  # noqa: TRY003
            "GitLab did not return a full commit SHA for this CI resource. Refresh it before retrying."
        )
    if sha.lower() != expected_sha.lower():
        raise ToolError("The CI resource SHA does not match expected_sha. Refresh the resource before retrying.")  # noqa: TRY003
    if validate_only:
        return CiWriteResult(status="validated", kind=kind, id=id, sha=sha)

    response = await _send_write(client, f"{resource_path}/retry")
    return _started_result(kind, response)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
async def create_pipeline(  # noqa: PLR0913, PLR0917 - each argument is one user-facing pipeline option.
    ctx: Context,
    project: Project,
    ref: Ref,
    expected_sha: FullSha,
    variables: PipelineVariables | None = None,
    validate_only: bool = False,
) -> CiWriteResult:
    """Create a pipeline for a ref, like `glab ci run`.

    `expected_sha` must be the full 40-character SHA returned by the ref preflight. GitLab's POST accepts the ref,
    not a SHA, so this check is not atomic. If the ref moves, the result reports the actual SHA and a warning.
    `variables` accepts up to 20 `env_var` entries with ASCII environment variable names of at most 255 characters
    and strict string values of at most 4096 characters. Values are never returned or included in error messages.
    `validate_only` checks the ref and variables without a POST. Writes are never retried automatically; check GitLab
    before retrying when the outcome is unknown or unverified.
    """
    encoded_project = _project_component(project)
    encoded_ref = _validate_ref(ref)
    checked_variables = _validate_variables(variables)
    client = get_client(ctx)
    commit = await client.get(f"/projects/{encoded_project}/repository/commits/{encoded_ref}")
    sha = commit.get("id") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or not full_sha(sha):
        raise ToolError(  # noqa: TRY003
            "GitLab did not return a full commit SHA for this ref. Refresh the ref before creating a pipeline."
        )
    if sha.lower() != expected_sha.lower():
        raise ToolError("The ref SHA does not match expected_sha. Refresh the ref before creating a pipeline.")  # noqa: TRY003
    if validate_only:
        return CiWriteResult(status="validated", kind="pipeline", sha=sha, ref=ref)

    body = {
        "ref": ref,
        "variables": [
            {"key": key, "value": value, "variable_type": "env_var"}
            for key, value in sorted((checked_variables or {}).items())
        ],
    }
    response = await _send_write(client, f"/projects/{encoded_project}/pipeline", body)
    result = _started_result("pipeline", response)
    warning = ""
    if isinstance(result.sha, str) and result.sha.lower() != sha.lower():
        warning = (
            "The ref moved after preflight. GitLab started the pipeline at a different SHA; check GitLab before acting."
        )
    return result.model_copy(update={"ref": ref, "warning": warning})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
async def create_merge_request_pipeline(
    ctx: Context,
    project: Project,
    iid: Iid,
    expected_sha: FullSha,
    validate_only: bool = False,
) -> CiWriteResult:
    """Start a merge request pipeline, like `glab api --method POST projects/:id/merge_requests/:iid/pipelines`.

    Use this, not create_pipeline, when an MR needs a pipeline for "pipeline must succeed" or a merge train. A
    branch pipeline does not count there. A common case is a retarget with no new push: the old pipeline ran
    against the old target, so the MR waits in `ci_must_pass` until a new MR pipeline runs.

    `expected_sha` must be the full MR `head_sha` from get_merge_request. GitLab's POST takes no SHA, so the check
    is not atomic. If the head moves, the result reports the actual SHA and a warning. `ref` is GitLab's pipeline
    ref, like `refs/merge-requests/12/merge` for merged results. Only open MRs qualify, and an MR whose head
    pipeline is still active is refused, so no duplicate pipeline starts. `validate_only` checks all of this
    without a POST.
    Writes are never retried automatically; check GitLab before retrying when the outcome is unknown or unverified.
    """
    _project_component(project)
    client = get_client(ctx)
    path = mr_rest_path(project, iid)
    merge_request = await client.get(path)
    sha = merge_request.get("sha") if isinstance(merge_request, dict) else None
    if not isinstance(sha, str) or not full_sha(sha):
        raise ToolError("GitLab did not return a full head SHA for this MR. Refresh it before starting a pipeline.")  # noqa: TRY003
    if sha.lower() != expected_sha.lower():
        raise ToolError("The MR head SHA does not match expected_sha. Refresh the MR before starting a pipeline.")  # noqa: TRY003
    if merge_request.get("state") != "opened":
        raise ToolError("Only an open MR can get a new pipeline.")  # noqa: TRY003
    head_pipeline = merge_request.get("head_pipeline") or {}
    if head_pipeline.get("status") in ACTIVE_PIPELINE_STATUSES:
        raise ToolError(  # noqa: TRY003
            f"The MR head pipeline {head_pipeline.get('id')} is still {head_pipeline['status']}. Wait for it first."
        )
    if validate_only:
        return CiWriteResult(status="validated", kind="pipeline", sha=sha)

    response = await _send_write(client, f"{path}/pipelines", rejected_hint=MR_PIPELINE_REJECTED_HINT)
    result = _started_result("pipeline", response)
    ref, status = response.get("ref"), response.get("status")
    ref = ref if isinstance(ref, str) else None
    # Merged-results pipelines test a merge commit, so their SHA never matches the head. 🔀
    tests_merge_commit = ref is not None and ref.endswith("/merge")
    warning = ""
    if not tests_merge_commit and isinstance(result.sha, str) and result.sha.lower() != sha.lower():
        warning = "The MR head moved after preflight. GitLab started the pipeline at a different SHA; check GitLab."
    return result.model_copy(
        update={
            "ref": ref,
            "pipeline_status": status if isinstance(status, str) else None,
            "warning": warning,
        }
    )


async def start_pipeline(  # noqa: PLR0913, PLR0917
    ctx: Context,
    project: Project,
    target: Literal["ref", "merge_request"],
    expected_sha: FullSha,
    ref: Ref | None = None,
    iid: PositiveIid | None = None,
    variables: PipelineVariables | None = None,
    validate_only: bool = False,
) -> CiWriteResult:
    """Start a ref or merge request pipeline.

    Use `glab ci run` for a ref, or for an MR use
    `glab api --method POST projects/:id/merge_requests/:iid/pipelines`.

    Set target to ref with ref and optional variables, or merge_request with iid only. A branch pipeline does not
    satisfy the MR pipeline gate. expected_sha is checked before the POST, but this preflight is not atomic.
    validate_only skips the POST. An observed active MR pipeline is refused, not a concurrency guarantee.
    For unknown or unverified writes, check GitLab before retrying; writes are never retried automatically.
    """
    if target == "ref":
        if ref is None or iid is not None:
            raise ToolError("The ref target requires ref and does not accept iid.")  # noqa: TRY003
        return await create_pipeline(ctx, project, ref, expected_sha, variables, validate_only)
    if iid is None or ref is not None or variables is not None:
        raise ToolError("The merge_request target requires iid and does not accept ref or variables.")  # noqa: TRY003
    return await create_merge_request_pipeline(ctx, project, iid, expected_sha, validate_only)
