"""Land tool: queue a merge, merge now, or cancel a queued merge 🚦."""

from collections import Counter
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError
from gitlab_mcp.common import Iid, Project, lower_or_empty
from gitlab_mcp.mr_common import (
    PIPELINE_STALL_HINT,
    diff_refs_head_sha,
    fetch_merge_request,
    mr_rest_path,
    needs_mr_pipeline,
)

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
      state detailedMergeStatus autoMergeStrategy mergeCommitSha targetBranch
      diffRefs { headSha }
      headPipeline { status }
    }
  }
}
"""

# GraphQL's car index includes historical cars, so the position is the 1-based order among active cars,
# the same approach as list_merge_train. Bounded to the first 100 active cars.
TRAIN_POSITION_QUERY = """
query($fullPath: ID!, $targetBranches: [String!]) {
  project(fullPath: $fullPath) {
    mergeTrains(status: ACTIVE, targetBranches: $targetBranches, first: 1) {
      nodes { cars(activityStatus: ACTIVE, first: 100) { nodes { status mergeRequest { iid } pipeline { id } } } }
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
    # Only for merge_train results. Empty when the MR is not among the first 100 active cars.
    train_position: int | None = Field(default=None, exclude_if=lambda v: v is None)  # 1-based, among active cars.
    train_status: str = Field(default="", exclude_if=lambda v: not v)  # The car's status, lowercased, like "idle".
    train_pipeline_id: int | None = Field(default=None, exclude_if=lambda v: v is None)  # None until a pipeline runs.
    # Set when the write worked but a read did not, or when the MR waits for a pipeline that never started.
    warning: str = Field(default="", exclude_if=lambda v: not v)


class LandItem(BaseModel):
    """One merge request in a batch land."""

    iid: Iid = Field(description="Merge request IID to land.")
    sha: str | None = Field(
        default=None, description="Full head SHA from get_merge_requests. Required for auto and now."
    )


LandBatchStatus = Literal["applied", "failed", "unknown"]


class LandBatchItem(BaseModel):
    """The outcome for one merge request in a batch land."""

    iid: int
    status: LandBatchStatus
    landing: MergeRequestLanding | None = Field(default=None, exclude_if=lambda v: v is None)
    warning: str = Field(default="", exclude_if=lambda v: not v)  # The error message when the land did not apply.


class LandBatchResult(BaseModel):
    """Ordered outcomes for a batch land."""

    results: list[LandBatchItem]


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


class DuplicateIidError(ToolError):
    """The same merge request appeared more than once in one batch."""

    def __init__(self, iids: list[int]) -> None:
        """Name the repeated IIDs, so the agent can drop the extra entries."""
        repeated = ", ".join(str(iid) for iid in iids)
        super().__init__(f"Each merge request may appear once in a batch. Repeated IIDs: {repeated}.")


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


def _check_batch_args(
    mode: LandMode,
    items: list[LandItem],
    squash: bool | None,
    remove_source_branch: bool | None,
) -> None:
    """Refuse a whole batch before its first write.

    A repeated IID is refused too, because two writes to one MR in a batch would be unclear.
    """
    repeated = sorted(iid for iid, count in Counter(item.iid for item in items).items() if count > 1)
    if repeated:
        raise DuplicateIidError(repeated)
    for item in items:
        _check_land_args(mode, item.sha, squash, remove_source_branch)


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
        if needs_mr_pipeline(node["state"], fields["merge_status"], fields["pipeline_status"]):
            fields["warning"] = PIPELINE_STALL_HINT
    return MergeRequestLanding(**fields)


def _train_fields(data: dict[str, Any], iid: int) -> dict[str, Any]:
    """Find this MR's car in a TRAIN_POSITION_QUERY response and return its train fields.

    Returns an empty dict when the MR is not among the active cars of the response. A response with no
    train list counts as no train, so the fields are left out.
    """
    project = data["project"] or {}
    trains = (project.get("mergeTrains") or {}).get("nodes") or []
    cars = trains[0]["cars"]["nodes"] if trains else []
    # GraphQL IIDs are strings, so the comparison uses str(iid).
    for position, car in enumerate(cars, start=1):
        if car["mergeRequest"]["iid"] == str(iid):
            pipeline = car["pipeline"]
            return {
                "train_position": position,
                "train_status": lower_or_empty(car["status"]),
                # The pipeline GID ends in its number, like gid://gitlab/Ci::Pipeline/731.
                "train_pipeline_id": int(pipeline["id"].rsplit("/", 1)[-1]) if pipeline else None,
            }
    return {}


async def _with_train_position(
    client: GitLabClient,
    project: str,
    iid: int,
    target_branch: str,
    landing: MergeRequestLanding,
) -> MergeRequestLanding:
    """Add where the MR sits in its merge train. The land already worked, so a failed read only adds a warning."""
    try:
        data = await client.graphql(TRAIN_POSITION_QUERY, {"fullPath": project, "targetBranches": [target_branch]})
    except ToolError:
        # Keep any stall hint, so neither warning is lost.
        warning = " ".join(filter(None, (landing.warning, "The merge train position could not be read.")))
        return landing.model_copy(update={"warning": f"{warning} Check list_merge_train."})
    return landing.model_copy(update=_train_fields(data, iid))


async def _land_one(  # noqa: PLR0913, PLR0917 - each argument is one land option.
    client: GitLabClient,
    project: str,
    iid: int,
    sha: str | None,
    mode: LandMode,
    squash: bool | None,
    remove_source_branch: bool | None,
) -> MergeRequestLanding:
    """Send one land write, then read the MR back. Raises the write's errors, and never retries a write.

    The caller checks the arguments first. A failed read after an accepted write returns `pending` with a warning,
    so a read problem never looks like a failed merge. 🧾
    """
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

    try:
        node = await fetch_merge_request(client, LAND_QUERY, project, iid)
    except ToolError:
        return MergeRequestLanding(
            result="pending",
            warning="GitLab accepted the request, but its state could not be read. Check get_merge_request.",
        )
    landing = _to_landing(mode, node)
    if landing.result == "merge_train":
        return await _with_train_position(client, project, iid, node["targetBranch"], landing)
    return landing


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
    - For `merge_train` results, `train_position` (1-based among active cars), `train_status`, and
      `train_pipeline_id` say where the MR sits in its train. They are left out when the MR is not found there.
    - A `warning` says when GitLab waits for CI (`ci_must_pass`) but the MR has no head pipeline, for example
      after a retarget with no new push. Start one with create_merge_request_pipeline.
    - A refused merge (405, 406, 409, 422) says to check blockers. Never retry a failed write without checking first.
    """
    _check_land_args(mode, sha, squash, remove_source_branch)
    return await _land_one(get_client(ctx), project, iid, sha, mode, squash, remove_source_branch)


async def _land_batch_item(  # noqa: PLR0913, PLR0917 - each argument is one land option.
    client: GitLabClient,
    project: str,
    item: LandItem,
    mode: LandMode,
    squash: bool | None,
    remove_source_branch: bool | None,
) -> LandBatchItem:
    """Land one batch item and turn its outcome or error into a compact row. Errors never stop the batch."""
    try:
        landing = await _land_one(client, project, item.iid, item.sha, mode, squash, remove_source_branch)
    except OutcomeUnknownError as exc:
        return LandBatchItem(iid=item.iid, status="unknown", warning=str(exc))
    except ToolError as exc:
        return LandBatchItem(iid=item.iid, status="failed", warning=str(exc))
    return LandBatchItem(iid=item.iid, status="applied", landing=landing)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
async def land_merge_requests(  # noqa: PLR0913, PLR0917 - each argument is a user-facing option.
    ctx: Context,
    project: Project,
    items: Annotated[
        list[LandItem],
        Field(
            min_length=1, max_length=20, description="One to 20 merge requests with their full head SHA, in land order."
        ),
    ],
    mode: Annotated[LandMode, Field(description="auto (default), now, or cancel. Applies to every item.")] = "auto",
    squash: Annotated[bool | None, Field(description="Squash the commits. Omit for the project default.")] = None,
    remove_source_branch: Annotated[
        bool | None,
        Field(description="Delete the source branch after merging. Omit for the project default."),
    ] = None,
) -> LandBatchResult:
    """Merge or cancel up to 20 merge requests, in order. Like `glab mr merge` for several MRs.

    - Get each `sha` from get_merge_requests. `mode`, `squash`, and `remove_source_branch` apply to every item.
    - Items run in order. A failed item does not stop later items, and writes are never retried.
    - Check each `status` before you retry. Never retry an `unknown` outcome without checking get_merge_request first.
    - Bad arguments and repeated IIDs are refused before the first write, so nothing is sent.
    """
    _check_batch_args(mode, items, squash, remove_source_branch)
    client = get_client(ctx)
    results = [await _land_batch_item(client, project, item, mode, squash, remove_source_branch) for item in items]
    return LandBatchResult(results=results)
