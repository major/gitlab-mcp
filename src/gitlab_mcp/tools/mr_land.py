"""Land tool: queue a merge, merge now, or cancel a queued merge 🚦."""

from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.client import GitLabAPIError, GitLabConnectionError
from gitlab_mcp.common import Iid, Project, lower_or_empty
from gitlab_mcp.mr_common import diff_refs_head_sha, fetch_merge_request, mr_rest_path

# How a land request is sent. "auto" queues the merge, "now" merges right away, "cancel" undoes a queued merge.
LandMode = Literal["auto", "now", "cancel"]
# What happened after a land request. "pending" means GitLab accepted it, but nothing has merged or queued yet.
LandResult = Literal["merge_train", "auto_merge", "merged", "cancelled", "pending"]
# GitLab's strategy names for an MR that waits in the merge train. Compared lowercased.
TRAIN_STRATEGIES = frozenset({"merge_train", "add_to_merge_train_when_checks_pass"})
# Statuses GitLab sends when it refuses a merge or cancel because of the MR's current state.
REFUSED_STATUSES = frozenset({405, 406, 409, 422})

# The state that a land request reads back. Only the fields that decide the result.
LAND_QUERY = """
query($fullPath: ID!, $iid: String!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      state detailedMergeStatus autoMergeStrategy mergeCommitSha
      diffRefs { headSha }
      headPipeline { status }
    }
  }
}
"""


class MergeRequestLanding(BaseModel):
    """What a land request did, read back from GitLab after the write."""

    result: LandResult
    state: str = Field(default="", exclude_if=lambda v: not v)  # For example "opened" or "merged".
    merge_status: str = Field(default="", exclude_if=lambda v: not v)  # GitLab's detailed status, lowercased.
    auto_merge_strategy: str = Field(default="", exclude_if=lambda v: not v)  # Lowercased.
    merge_commit_sha: str = Field(default="", exclude_if=lambda v: not v)
    # The head SHA GitLab queued or merged. Only auto and now fill it, so cancel keeps its shorter shape.
    head_sha: str = Field(default="", exclude_if=lambda v: not v)
    pipeline_status: str = Field(default="", exclude_if=lambda v: not v)  # Head pipeline status, lowercased.
    warning: str = Field(default="", exclude_if=lambda v: not v)  # Set when the write worked but the read did not.


class ShaRequiredError(ToolError):
    """A merge needs the head SHA, and none was given."""

    def __init__(self, mode: str) -> None:
        """Name the mode and point at where the SHA comes from."""
        super().__init__(f"Mode {mode!r} needs sha, the full head SHA from get_merge_request.")


class CancelOptionsError(ToolError):
    """A cancel was sent with merge options, which it does not take."""

    def __init__(self) -> None:
        """Tell the agent which arguments to drop."""
        super().__init__("Mode 'cancel' takes only project and iid. Drop sha, squash, and remove_source_branch.")


class OutcomeUnknownError(ToolError):
    """The connection failed during a write, so GitLab may or may not have acted."""

    def __init__(self, cause: GitLabConnectionError) -> None:
        """Keep the connection detail and say the outcome is unknown."""
        super().__init__(f"{cause} The outcome is unknown. Check get_merge_request before retrying.")


class MergeRefusedError(ToolError):
    """GitLab refused the merge or cancel because of the MR's current state."""

    def __init__(self, error: GitLabAPIError) -> None:
        """Keep GitLab's reason and point at the blockers tool."""
        super().__init__(f"{error} Check blockers with get_merge_request.")


def _check_land_args(mode: LandMode, sha: str | None, squash: bool | None, remove_source_branch: bool | None) -> None:
    """Refuse a bad land request before anything is sent to GitLab.

    `auto` and `now` need the head SHA. GitLab then merges only the commits
    that were reviewed. `cancel` takes no merge options, so any option given
    with it is a mistake.
    """
    if mode == "cancel":
        if sha is not None or squash is not None or remove_source_branch is not None:
            raise CancelOptionsError
    elif not sha:
        raise ShaRequiredError(mode)


def _merge_body(
    mode: LandMode, sha: str | None, squash: bool | None, remove_source_branch: bool | None
) -> dict[str, Any]:
    """Build the JSON body for the merge request. Unset options are left out, so GitLab uses the project defaults."""
    body: dict[str, Any] = {"sha": sha}
    if mode == "auto":
        body["auto_merge"] = True
    if squash is not None:
        body["squash"] = squash
    if remove_source_branch is not None:
        body["should_remove_source_branch"] = remove_source_branch
    return body


def _land_result(mode: LandMode, node: dict[str, Any]) -> LandResult:
    """Name what GitLab did, from the MR state read back after the write.

    A merged MR wins over any strategy still set on it. A cancel counts as
    cancelled only when no strategy is left.
    """
    strategy = lower_or_empty(node["autoMergeStrategy"])
    if node["state"] == "merged":
        return "merged"
    if strategy in TRAIN_STRATEGIES:
        return "merge_train"
    if strategy:
        return "auto_merge"
    # 🚦 Nothing is queued and nothing merged, so a cancel went through.
    return "cancelled" if mode == "cancel" else "pending"


def _to_landing(mode: LandMode, node: dict[str, Any]) -> MergeRequestLanding:
    """Convert the read-back state into the landing result.

    Cancel leaves out the head SHA and pipeline status. Its result only says what the queue looks like now.
    """
    fields: dict[str, Any] = {
        "result": _land_result(mode, node),
        "state": node["state"],
        "merge_status": lower_or_empty(node["detailedMergeStatus"]),
        "auto_merge_strategy": lower_or_empty(node["autoMergeStrategy"]),
        "merge_commit_sha": node["mergeCommitSha"] or "",
    }
    if mode != "cancel":
        # A null headPipeline means the head has no pipeline, so the status stays empty and is left out.
        pipeline = node["headPipeline"] or {}
        fields["head_sha"] = diff_refs_head_sha(node["diffRefs"])
        fields["pipeline_status"] = lower_or_empty(pipeline.get("status"))
    return MergeRequestLanding(**fields)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
async def land_merge_request(  # noqa: PLR0913, PLR0917 - each argument is a user-facing option.
    ctx: Context,
    project: Project,
    iid: Iid,
    sha: Annotated[
        str | None,
        Field(description="Full head SHA from get_merge_request. Required for auto and now."),
    ] = None,
    mode: Annotated[LandMode, Field(description="auto (default), now, or cancel.")] = "auto",
    squash: Annotated[bool | None, Field(description="Squash the commits. Omit for the project default.")] = None,
    remove_source_branch: Annotated[
        bool | None,
        Field(description="Delete the source branch after merging. Omit for the project default."),
    ] = None,
) -> MergeRequestLanding:
    """Merge a merge request, or cancel a queued merge. Like `glab mr merge`.

    - `auto` (default): join the merge train when the project has one, otherwise merge when checks pass.
    - `now`: merge right away and skip the train. Use it only when the user asks for it.
    - `cancel`: leave the merge train or cancel auto-merge. Takes no `sha`, `squash`, or `remove_source_branch`.
    - Get `sha` from get_merge_request. GitLab refuses the merge with 409 when the head has moved.
    - `result`: `merge_train`, `auto_merge`, `merged`, `cancelled`, or `pending`. Use get_merge_request to see blockers.
    - `auto` and `now` also return `head_sha` and `pipeline_status`, so you can confirm which commit was queued.
    - A refused merge (405, 406, 409, 422) says to check blockers. Never retry a failed write without checking first.
    """
    _check_land_args(mode, sha, squash, remove_source_branch)
    client = get_client(ctx)
    path = mr_rest_path(project, iid)
    try:
        if mode == "cancel":
            await client.request("POST", f"{path}/cancel_merge_when_pipeline_succeeds")
        else:
            await client.request("PUT", f"{path}/merge", json=_merge_body(mode, sha, squash, remove_source_branch))
    except GitLabConnectionError as exc:
        # The request may have reached GitLab before the connection failed, so the outcome is unknown.
        raise OutcomeUnknownError(exc) from exc
    except GitLabAPIError as exc:
        if exc.status not in REFUSED_STATUSES:
            raise
        raise MergeRefusedError(exc) from exc

    # The write was accepted, so a failed read must not look like a failed merge. 🧾
    try:
        node = await fetch_merge_request(client, LAND_QUERY, project, iid)
    except ToolError:
        return MergeRequestLanding(
            result="pending",
            warning="GitLab accepted the request, but its state could not be read. Check get_merge_request.",
        )
    return _to_landing(mode, node)
