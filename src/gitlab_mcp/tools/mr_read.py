"""Merge request read tools: list, merge train, and status views 🔀."""

import json
import re
from datetime import UTC, datetime
from itertools import batched
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.common import MAX_BATCH_MERGE_REQUESTS, MAX_PAGES, Iid, PositiveIid, Project, bare_id, lower_or_empty
from gitlab_mcp.errors import ProjectNotFoundError
from gitlab_mcp.mr_common import (
    ThreadIndex,
    ThreadRef,
    author_display,
    diff_refs_head_sha,
    fetch_merge_request,
    position_fields,
)
lazy from gitlab_mcp.client import GitLabClient

# One request per page. `open_threads` is computed from the two counts.
MERGE_REQUESTS_QUERY = """
query(
  $fullPath: ID!, $state: MergeRequestState, $authorUsername: String, $search: String,
  $createdAfter: Time, $updatedAfter: Time, $targetBranches: [String!],
  $includeSourceBranch: Boolean!, $includeLabels: Boolean!, $first: Int!, $after: String
) {
  project(fullPath: $fullPath) {
    mergeRequests(
      state: $state, authorUsername: $authorUsername, search: $search,
      createdAfter: $createdAfter, updatedAfter: $updatedAfter, targetBranches: $targetBranches,
      first: $first, after: $after, sort: UPDATED_DESC
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        iid title state draft updatedAt
        sourceBranch @include(if: $includeSourceBranch)
        labels(first: 100) @include(if: $includeLabels) {
          nodes { title }
          pageInfo { hasNextPage }
        }
        author { username }
        approved
        approvedBy { nodes { username } }
        resolvableDiscussionsCount resolvedDiscussionsCount
      }
    }
  }
}
"""

# One merge request with its status facts and the first page of discussions.
MERGE_REQUEST_QUERY = """
query($fullPath: ID!, $iid: String!, $includeDescription: Boolean!, $includeJobs: Boolean! = false) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      iid title description @include(if: $includeDescription) state draft sourceBranch targetBranch
      author { username }
      mergedAt mergeUser { username }
      diffRefs { headSha }
      detailedMergeStatus shouldBeRebased conflicts
      approved approvedBy { nodes { username } }
      reviewers { nodes { username mergeRequestInteraction { reviewState } } }
      headPipeline { status id @include(if: $includeJobs) }
      autoMergeStrategy
      diffStatsSummary { additions deletions fileCount }
      resolvableDiscussionsCount resolvedDiscussionsCount
      discussions(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id resolvable resolved
          notes(first: 1) {
            count
            nodes { system author { username bot name id } position { oldPath newPath oldLine newLine } }
          }
        }
      }
    }
  }
}
"""

# Shared fields for compact batch lookups. Keep discussion, reviewer, and change data out. 📦
COMPACT_MERGE_REQUEST_FRAGMENT = """
fragment CompactMergeRequest on MergeRequest {
  iid title description @include(if: $includeDescription) state draft sourceBranch targetBranch
  author { username }
  mergedAt mergeUser { username }
  diffRefs { headSha }
  detailedMergeStatus shouldBeRebased conflicts
  approved approvedBy { nodes { username } }
  headPipeline { status }
}
"""

# One active train and one page of its active cars per request, limiting nested overfetch.
# GraphQL's car index includes historical cars, so positions come from active car order instead.
MERGE_TRAIN_QUERY = """
query($fullPath: ID!, $targetBranches: [String!], $after: String, $carAfter: String, $first: Int!) {
  project(fullPath: $fullPath) {
    mergeTrains(status: ACTIVE, targetBranches: $targetBranches, first: 1, after: $after) {
      nodes {
        targetBranch
        cars(activityStatus: ACTIVE, first: $first, after: $carAfter) {
          nodes {
            status
            mergeRequest { iid title author { username } }
            pipeline { id status }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

# GitLab rejects a GraphQL query above complexity 250. Each aliased merge request costs about 23 points,
# so 8 per query (about 184) leaves headroom. Larger batches run as several sequential queries.
GRAPHQL_MR_CHUNK = 8

# Long descriptions are cut to keep one tool result small. The flag tells the agent it was cut.
DESCRIPTION_CAP = 4000
# The open-thread list is capped. Counts stay exact, and `index_complete` says whether the list is whole.
THREAD_INDEX_CAP = 20

# A Literal is sent to the agent as an enum, so this type replaces a separate
# "list states" tool.
MergeRequestState = Literal["opened", "closed", "locked", "merged", "all"]

# Column order of the TSV output. Every row has every column, so agents
# never have to guess whether a missing key means "empty" or "not sent".
TSV_COLUMNS = ("iid", "title", "author", "state", "draft", "approved", "approved_by", "open_threads", "updated")


class MergeRequestSummary(BaseModel):
    """One merge request row, with every field the TSV output needs.

    Values are kept as-is here. `_to_tsv` turns them into cells.
    """

    iid: int
    title: str
    source_branch: str | None = None
    labels: list[str] = Field(default_factory=list)
    labels_truncated: bool = False
    author: str | None  # Null when the author was deleted or banned.
    state: str
    draft: bool
    approved: bool
    approved_by: list[str]
    open_threads: int
    updated: str  # Date only, YYYY-MM-DD.


class MergeRequestList(BaseModel):
    """A page of merge requests."""

    web_url_prefix: str = Field(description="Append an iid to get the MR link.")
    merge_requests: str = Field(
        description=(
            "TSV with a header row. Columns: iid, title, author, state, draft, approved, "
            "approved_by, open_threads, updated. approved_by is comma-separated usernames. "
            "An empty cell means none. Booleans are true/false. source_branch and labels are appended only when asked; "
            "labels is a JSON array in one cell, and labels_complete reports whether all returned labels were included."
        ),
    )
    has_more: bool
    labels_complete: bool | None = Field(default=None, exclude_if=lambda value: value is None)


class MergeTrainList(BaseModel):
    """A page of active merge train entries."""

    web_url_prefix: str = Field(description="Append an iid to get the MR link.")
    entries: str = Field(
        description=(
            "TSV with a header row. Columns: target_branch, position, iid, title, author, status, "
            "pipeline_id, pipeline_status. Empty cells mean unknown or none."
        ),
    )
    has_more: bool


class Reviewer(BaseModel):
    """A requested reviewer."""

    username: str
    review_state: str = Field(default="", exclude_if=lambda v: not v)  # Lowercased, like "reviewed".


class DiffSummary(BaseModel):
    """Diff size totals."""

    files: int
    additions: int
    deletions: int


class MergeRequestDetail(BaseModel):
    """One merge request's status facts."""

    iid: int
    title: str
    state: str
    draft: bool = Field(default=False, exclude_if=lambda v: not v)
    author: str = Field(default="", exclude_if=lambda v: not v)
    merged_at: str | None = Field(default=None, exclude_if=lambda value: value is None)
    merged_by: str | None = Field(default=None, exclude_if=lambda value: value is None)
    source_branch: str
    target_branch: str
    head_sha: str = Field(default="", exclude_if=lambda v: not v)
    description: str = Field(default="", exclude_if=lambda v: not v)
    description_truncated: bool = Field(default=False, exclude_if=lambda v: not v)
    merge_status: str = Field(default="", exclude_if=lambda v: not v)  # GitLab's detailed status.
    should_be_rebased: bool = Field(default=False, exclude_if=lambda v: not v)
    conflicts: bool = Field(default=False, exclude_if=lambda v: not v)
    approved: bool
    approved_by: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    reviewers: list[Reviewer] = Field(default_factory=list, exclude_if=lambda v: not v)
    pipeline_status: str = Field(default="", exclude_if=lambda v: not v)  # Head pipeline, lowercased.
    failed_jobs: str = Field(default="", exclude_if=lambda value: not value)
    jobs_complete: bool = Field(default=True, exclude_if=lambda value: value)
    auto_merge_strategy: str = Field(default="", exclude_if=lambda v: not v)
    changes: DiffSummary | None = Field(default=None, exclude_if=lambda v: v is None)
    threads: ThreadIndex


class CompactMergeRequest(BaseModel):
    """One merge request's compact status facts, without reviewers or threads."""

    iid: int
    title: str
    state: str
    draft: bool = Field(default=False, exclude_if=lambda v: not v)
    author: str = Field(default="", exclude_if=lambda v: not v)
    merged_at: str | None = Field(default=None, exclude_if=lambda value: value is None)
    merged_by: str | None = Field(default=None, exclude_if=lambda value: value is None)
    source_branch: str
    target_branch: str
    head_sha: str = Field(default="", exclude_if=lambda v: not v)
    merge_status: str = Field(default="", exclude_if=lambda v: not v)
    should_be_rebased: bool = Field(default=False, exclude_if=lambda v: not v)
    conflicts: bool = Field(default=False, exclude_if=lambda v: not v)
    approved: bool
    approved_by: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    pipeline_status: str = Field(default="", exclude_if=lambda v: not v)
    description: str = Field(default="", exclude_if=lambda v: not v)
    description_truncated: bool = Field(default=False, exclude_if=lambda v: not v)


class MergeRequestBatch(BaseModel):
    """Compact results and the requested IIDs GitLab did not return."""

    merge_requests: list[CompactMergeRequest]
    missing_iids: list[int] = Field(default_factory=list, exclude_if=lambda v: not v)


def _cell(value: object) -> str:
    """Render one value as a TSV cell.

    None becomes an empty cell, booleans become true/false, and lists become
    comma-separated. Tabs and line breaks in text would split the row, so each
    run of them becomes one space. Titles can contain them.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(value)
    return re.sub(r"[\t\r\n]+", " ", str(value))


def _to_tsv(
    mrs: list[MergeRequestSummary],
    *,
    include_source_branch: bool = False,
    include_labels: bool = False,
) -> str:
    """Render merge requests as TSV, appending optional data columns when requested."""
    columns = list(TSV_COLUMNS)
    if include_source_branch:
        columns.append("source_branch")
    if include_labels:
        columns.append("labels")
    lines = ["\t".join(columns)]
    for mr in mrs:
        values = [_cell(getattr(mr, column)) for column in TSV_COLUMNS]
        if include_source_branch:
            values.append(_cell(mr.source_branch))
        if include_labels:
            # JSON escaping keeps tabs and newlines inside one TSV cell.
            values.append(json.dumps(mr.labels, ensure_ascii=False, separators=(",", ":")))
        lines.append("\t".join(values))
    return "\n".join(lines)


def _to_summary(
    node: dict[str, Any],
    *,
    include_source_branch: bool = False,
    include_labels: bool = False,
) -> MergeRequestSummary:
    """Convert one GraphQL merge request node into a summary."""
    # A deleted or banned author comes back as null.
    author = node["author"]
    label_connection = node.get("labels") if include_labels else None
    return MergeRequestSummary(
        iid=int(node["iid"]),  # GraphQL sends IIDs as strings.
        title=node["title"],
        source_branch=node.get("sourceBranch") if include_source_branch else None,
        labels=[label["title"] for label in label_connection["nodes"]] if label_connection else [],
        labels_truncated=label_connection["pageInfo"]["hasNextPage"] if label_connection else False,
        author=author["username"] if author else None,
        state=node["state"],
        draft=node["draft"],
        approved=node["approved"],
        approved_by=[user["username"] for user in node["approvedBy"]["nodes"]],
        open_threads=(node["resolvableDiscussionsCount"] or 0) - (node["resolvedDiscussionsCount"] or 0),
        updated=node["updatedAt"][:10],  # ISO 8601 timestamp, keep the date part.
    )


def _matches(mr: MergeRequestSummary, approved: bool | None, has_open_threads: bool | None) -> bool:
    """Return True when `mr` passes the client-side filters. `None` means no filter."""
    if approved is not None and mr.approved != approved:
        return False
    return has_open_threads is None or (mr.open_threads > 0) == has_open_threads


def _to_merge_train_entry(target_branch: str, position: int, car: dict[str, Any]) -> str:
    """Render one merge train car as a sanitized TSV row."""
    merge_request = car["mergeRequest"]
    author = merge_request["author"]
    pipeline = car["pipeline"]
    pipeline_id = pipeline["id"] if pipeline else None
    if pipeline_id:
        pipeline_id = pipeline_id.rsplit("/", 1)[-1]
    values = (
        target_branch,
        position,
        merge_request["iid"],
        merge_request["title"],
        author["username"] if author else None,
        lower_or_empty(car["status"]),
        pipeline_id,
        lower_or_empty(pipeline["status"]) if pipeline else "",
    )
    return "\t".join(_cell(value) for value in values)


def _normalize_timestamp(value: str | None, name: str) -> str | None:
    """Validate a timezone-aware ISO 8601 datetime and normalize it to UTC for GraphQL."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except OverflowError, ValueError:
        raise ToolError(  # noqa: TRY003 - the parameter name makes the validation error actionable.
            f"{name} must be a timezone-aware ISO 8601 datetime, such as 2026-10-01T12:30:00Z."
        ) from None
    if "T" not in value.upper() or parsed.utcoffset() is None:
        raise ToolError(  # noqa: TRY003 - the parameter name makes the validation error actionable.
            f"{name} must be a timezone-aware ISO 8601 datetime, such as 2026-10-01T12:30:00Z."
        )
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_merge_requests(  # noqa: PLR0913, PLR0917 - each argument is a user-facing filter.
    ctx: Context,
    project: Project,
    state: MergeRequestState = "opened",
    author: Annotated[str | None, Field(description="Author username.")] = None,
    search: Annotated[
        str | None,
        Field(description="Search merge request titles or descriptions."),
    ] = None,
    approved: Annotated[
        bool | None,
        Field(description="True: only fully approved. False: only not fully approved."),
    ] = None,
    has_open_threads: Annotated[
        bool | None,
        Field(description="True: only MRs with unresolved threads. False: only MRs without."),
    ] = None,
    created_after: Annotated[
        str | None,
        Field(description="Timezone-aware ISO 8601 datetime. Date-only and timezone-naive values are not accepted."),
    ] = None,
    updated_after: Annotated[
        str | None,
        Field(description="Timezone-aware ISO 8601 datetime. Date-only and timezone-naive values are not accepted."),
    ] = None,
    target_branch: Annotated[str | None, Field(description="Only merge requests targeting this branch.")] = None,
    include_source_branch: Annotated[
        bool,
        Field(description="Append the source branch column. Defaults to false to keep output compact."),
    ] = False,
    include_labels: Annotated[
        bool,
        Field(description="Append labels as a JSON array in one TSV cell. Defaults to false."),
    ] = False,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> MergeRequestList:
    """List merge requests in a project, newest-updated first.

    Equivalent to `glab mr list` with filters. The token needs the `read_api`
    scope. Results are TSV with a header row, and every row has every column.
    An empty cell means none, like no author or no approvers. Booleans are
    true or false. `updated` is a date. For a link, append an MR's `iid` to
    `web_url_prefix`.

    `approved` follows the instance rules: on Free it means at least one
    approval, on Premium and Ultimate it means all approval rules are met.
    `search` uses GitLab's `Project.mergeRequests(search:)` filter, which matches
    text in the title or description.
    `created_after` and `updated_after` accept timezone-aware ISO 8601 datetimes.
    `target_branch` filters by target branch. `include_source_branch` and `include_labels`
    append columns only when requested. Labels are JSON arrays inside a TSV cell;
    `labels_complete` is false when any returned label list exceeds its first 100 labels.
    `approved` and `has_open_threads` are filtered after fetching, so the tool
    scans at most 5 pages. If `has_more` is true but fewer than `limit` MRs
    came back, the scan stopped early and more matches may exist.
    """
    created_after = _normalize_timestamp(created_after, "created_after")
    updated_after = _normalize_timestamp(updated_after, "updated_after")
    client = get_client(ctx)
    # Client-side filters can drop nodes, so fetch big pages to find matches faster.
    filtered = approved is not None or has_open_threads is not None
    variables: dict[str, Any] = {
        "fullPath": project,
        "state": state,
        "authorUsername": author,
        "search": search,
        "createdAfter": created_after,
        "updatedAfter": updated_after,
        "targetBranches": [target_branch] if target_branch is not None else None,
        "includeSourceBranch": include_source_branch,
        "includeLabels": include_labels,
        "after": None,
        "first": 100 if filtered else limit,
    }

    collected: list[MergeRequestSummary] = []
    has_next = False
    for _ in range(MAX_PAGES):
        data = await client.graphql(MERGE_REQUESTS_QUERY, variables)
        if data["project"] is None:
            raise ProjectNotFoundError(project)
        connection = data["project"]["mergeRequests"]
        summaries = [
            _to_summary(node, include_source_branch=include_source_branch, include_labels=include_labels)
            for node in connection["nodes"]
        ]
        collected.extend(mr for mr in summaries if _matches(mr, approved, has_open_threads))
        page_info = connection["pageInfo"]
        has_next = page_info["hasNextPage"]
        if len(collected) >= limit or not has_next:
            break
        variables["after"] = page_info["endCursor"]

    displayed = collected[:limit]
    return MergeRequestList(
        web_url_prefix=f"{client.settings.url}/{project}/-/merge_requests/",
        merge_requests=_to_tsv(
            displayed,
            include_source_branch=include_source_branch,
            include_labels=include_labels,
        ),
        has_more=len(collected) > limit or has_next,
        labels_complete=not any(mr.labels_truncated for mr in displayed) if include_labels else None,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_merge_train(
    ctx: Context,
    project: Project,
    target_branch: Annotated[
        str | None,
        Field(description="Only this target branch. Omit for all active trains."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> MergeTrainList:
    """List active merge train entries, like `glab api projects/:id/merge_trains?scope=active&sort=asc`.

    Requires the `read_api` scope and GitLab Premium or Ultimate. Active trains include cars that are waiting,
    not only cars currently running. TSV columns are `target_branch`, `position`, `iid`, `title`, `author`, `status`,
    `pipeline_id`, and `pipeline_status`; positions are one-based within each target branch, not a global queue.
    Empty cells mean unknown or none. The scan reads at most 5 pages; `has_more` means entries or trains remain,
    or the page cap stopped the scan. Set `target_branch` to narrow the scan, or increase `limit` for more entries.
    """
    client = get_client(ctx)
    entries: list[str] = []
    outer_after: str | None = None
    car_after: str | None = None
    position = 1
    has_more = False

    for _ in range(MAX_PAGES):
        variables = {
            "fullPath": project,
            "targetBranches": [target_branch] if target_branch is not None else None,
            "after": outer_after,
            "carAfter": car_after,
            "first": limit - len(entries),
        }
        data = await client.graphql(MERGE_TRAIN_QUERY, variables)
        if data["project"] is None:
            raise ProjectNotFoundError(project)
        trains = data["project"]["mergeTrains"]
        train_nodes = trains["nodes"]
        if not train_nodes:
            # The connection can drain between pages. An empty page is complete even if pageInfo is stale.
            has_more = False
            break

        train = train_nodes[0]
        cars = train["cars"]
        car_nodes = cars["nodes"]
        entries.extend(
            _to_merge_train_entry(train["targetBranch"], car_position, car)
            for car_position, car in enumerate(car_nodes, start=position)
        )
        position += len(car_nodes)

        cars_page_info = cars["pageInfo"]
        trains_page_info = trains["pageInfo"]
        has_more = cars_page_info["hasNextPage"] or trains_page_info["hasNextPage"]
        if len(entries) >= limit or not has_more:
            break
        if cars_page_info["hasNextPage"]:
            car_after = cars_page_info["endCursor"]
        else:
            outer_after = trains_page_info["endCursor"]
            car_after = None
            position = 1

    header = "target_branch\tposition\tiid\ttitle\tauthor\tstatus\tpipeline_id\tpipeline_status"
    return MergeTrainList(
        web_url_prefix=f"{client.settings.url}/{project}/-/merge_requests/",
        entries="\n".join([header, *entries]),
        has_more=has_more,
    )


def _cap_description(description: str | None, limit: int = DESCRIPTION_CAP) -> tuple[str, bool]:
    """Cut a description to the requested number of characters.

    Returns the text and whether it was cut. Empty text stays empty, so it is left out.
    """
    if not description or limit == 0:
        return "", False
    return description[:limit], len(description) > limit


def _to_reviewer(node: dict[str, Any]) -> Reviewer:
    """Convert one reviewer node. A null interaction means GitLab has no review state for them."""
    interaction = node["mergeRequestInteraction"]
    review_state = interaction["reviewState"] if interaction else None
    return Reviewer(username=node["username"], review_state=lower_or_empty(review_state))


def _to_diff_summary(stats: dict[str, Any] | None) -> DiffSummary | None:
    """Convert the diff stats. The stats can be null, so check before reading them."""
    if stats is None:
        return None
    return DiffSummary(files=stats["fileCount"], additions=stats["additions"], deletions=stats["deletions"])


def _is_open_thread(discussion: dict[str, Any]) -> bool:
    """Return True for an unresolved, resolvable thread that starts with a human note.

    System notes, like "changed the title", are skipped because agents act on
    human feedback.
    """
    first = discussion["notes"]["nodes"][0]
    return discussion["resolvable"] and not discussion["resolved"] and not first["system"]


def _to_thread_ref(discussion: dict[str, Any]) -> ThreadRef:
    """Convert one open discussion into a thread ref, using its first note for the details."""
    notes = discussion["notes"]
    first = notes["nodes"][0]
    author = first["author"]  # A deleted or banned author comes back as null.
    return ThreadRef(
        id=bare_id(discussion["id"]),
        by=author_display(author),
        notes=notes["count"],
        **position_fields(first["position"]),
    )


def _to_thread_index(node: dict[str, Any]) -> ThreadIndex:
    """Build the thread index from the exact counts and the first page of discussions.

    Only one page of discussions is read, so `index_complete` is false when
    GitLab has another page.
    """
    # Null counts mean zero. Only the list below can be partial, never the counts.
    resolvable = node["resolvableDiscussionsCount"] or 0
    resolved = node["resolvedDiscussionsCount"] or 0
    discussions = node["discussions"]
    open_discussions = [discussion for discussion in discussions["nodes"] if _is_open_thread(discussion)]
    return ThreadIndex(
        open_count=resolvable - resolved,
        resolved_count=resolved,
        open=[_to_thread_ref(discussion) for discussion in open_discussions[:THREAD_INDEX_CAP]],
        index_complete=len(open_discussions) <= THREAD_INDEX_CAP and not discussions["pageInfo"]["hasNextPage"],
    )


def _merged_metadata(node: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return date-only merge metadata for merged requests with available values."""
    if node["state"] != "merged":
        return None, None
    merged_at = node.get("mergedAt")
    merge_user = node.get("mergeUser")
    return merged_at[:10] if merged_at else None, merge_user.get("username") if merge_user else None


def _to_detail(
    node: dict[str, Any],
    *,
    include_description: bool,
    description_limit: int,
) -> MergeRequestDetail:
    """Convert one GraphQL merge request node into the detail shape.

    When the description was skipped or is absent from the query result, it and its flag are left out.
    """
    # A deleted or banned author comes back as null.
    author = node["author"]
    # headPipeline can be null, so check before reading it.
    pipeline = node["headPipeline"]
    merged_at, merged_by = _merged_metadata(node)
    description, description_truncated = _cap_description(
        node.get("description") if include_description else None,
        description_limit,
    )
    return MergeRequestDetail(
        iid=int(node["iid"]),  # GraphQL sends IIDs as strings.
        title=node["title"],
        state=node["state"],
        merged_at=merged_at,
        merged_by=merged_by,
        draft=node["draft"],
        author=author["username"] if author else "",
        source_branch=node["sourceBranch"],
        target_branch=node["targetBranch"],
        head_sha=diff_refs_head_sha(node["diffRefs"]),
        description=description,
        description_truncated=description_truncated,
        merge_status=lower_or_empty(node["detailedMergeStatus"]),
        should_be_rebased=node["shouldBeRebased"],
        conflicts=node["conflicts"],
        approved=node["approved"],
        approved_by=[user["username"] for user in node["approvedBy"]["nodes"]],
        reviewers=[_to_reviewer(reviewer) for reviewer in node["reviewers"]["nodes"]],
        pipeline_status=lower_or_empty(pipeline["status"]) if pipeline else "",
        auto_merge_strategy=node["autoMergeStrategy"] or "",
        changes=_to_diff_summary(node["diffStatsSummary"]),
        threads=_to_thread_index(node),
    )


def _to_compact_detail(
    node: dict[str, Any],
    *,
    include_description: bool,
    description_limit: int,
) -> CompactMergeRequest:
    """Convert one compact GraphQL merge request node to its output model."""
    # A deleted or banned author comes back as null.
    author = node["author"]
    # headPipeline can be null, so check before reading it.
    pipeline = node["headPipeline"]
    merged_at, merged_by = _merged_metadata(node)
    description, description_truncated = _cap_description(
        node.get("description") if include_description else None,
        description_limit,
    )
    return CompactMergeRequest(
        iid=int(node["iid"]),  # GraphQL sends IIDs as strings.
        title=node["title"],
        state=node["state"],
        merged_at=merged_at,
        merged_by=merged_by,
        draft=node["draft"],
        author=author["username"] if author else "",
        source_branch=node["sourceBranch"],
        target_branch=node["targetBranch"],
        head_sha=diff_refs_head_sha(node["diffRefs"]),
        merge_status=lower_or_empty(node["detailedMergeStatus"]),
        should_be_rebased=node["shouldBeRebased"],
        conflicts=node["conflicts"],
        approved=node["approved"],
        approved_by=[user["username"] for user in node["approvedBy"]["nodes"]],
        pipeline_status=lower_or_empty(pipeline["status"]) if pipeline else "",
        description=description,
        description_truncated=description_truncated,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_merge_request(  # noqa: PLR0913, PLR0917 - each argument controls one optional detail.
    ctx: Context,
    project: Project,
    iid: Iid,
    include_jobs: Annotated[
        bool,
        Field(description="Include failed job IDs and names as TSV. Defaults to false to keep output compact."),
    ] = False,
    include_description: Annotated[
        bool,
        Field(description="True includes the description. Defaults to false to save tokens."),
    ] = False,
    description_limit: Annotated[
        int,
        Field(ge=0, le=DESCRIPTION_CAP, description="Maximum description length in characters, from 0 to 4000."),
    ] = DESCRIPTION_CAP,
) -> MergeRequestDetail:
    """Review merge request status, reviewers, approvals, and CI. Like `glab mr view`.

    - `merge_status` is the source of truth for mergeability. Open threads and failed CI do not always block a merge.
    - `approved`: Free means one approval. Premium and Ultimate mean all approval rules pass.
    - `auto_merge_strategy`: `merge_when_checks_pass`, `merge_train`, `add_to_merge_train_when_checks_pass` (waiting).
    - `threads.open` IDs go to get_merge_request_threads. `index_complete` false means some are missing.
    - `line` is the new-file line. `old_line` replaces it on a deleted line.
    - Empty fields are left out: a missing `draft` means false, a missing list is empty.
    - The description is skipped by default. Set `include_description=true` to read it when you need it.
    - `description_limit` controls description length when the description is included. Zero skips it; the default is
      4000 characters.
    - `include_jobs` adds failed job IDs and names as a compact TSV only when requested. `jobs_complete` is false when
      the failed-job scan reaches its page cap. No jobs are fetched when the merge request has no head pipeline.
    """
    include_description = include_description and description_limit > 0
    client = get_client(ctx)
    node = await fetch_merge_request(
        client,
        MERGE_REQUEST_QUERY,
        project,
        iid,
        includeDescription=include_description,
        includeJobs=include_jobs,
    )
    detail = _to_detail(node, include_description=include_description, description_limit=description_limit)
    pipeline = node["headPipeline"]
    if not include_jobs or pipeline is None:
        return detail

    pipeline_gid = pipeline.get("id")
    pipeline_match = (
        re.fullmatch(r"gid://gitlab/Ci::Pipeline/([0-9]+)", pipeline_gid) if isinstance(pipeline_gid, str) else None
    )
    if pipeline_match is None:
        raise ToolError("The pipeline ID format is invalid; expected a numeric pipeline GID.")  # noqa: TRY003

    pipeline_id = pipeline_match.group(1)
    jobs: list[dict[str, Any]] = []
    page = 1
    for _ in range(MAX_PAGES):
        page_jobs, next_page = await client.get_page(
            f"/projects/{quote(project, safe='')}/pipelines/{pipeline_id}/jobs",
            **{"scope[]": "failed", "per_page": 100, "page": page},
        )
        jobs.extend(page_jobs)
        if next_page is None:
            break
        page = next_page
    else:
        detail.jobs_complete = False

    if jobs:
        detail.failed_jobs = "\n".join(
            ["id\tname", *("\t".join((_cell(job["id"]), _cell(job["name"]))) for job in jobs)],
        )
    return detail


async def _fetch_compact_chunk(
    client: GitLabClient,
    project: str,
    iids: list[int],
    include_description: bool,
) -> list[dict[str, Any] | None]:
    """Fetch compact merge request nodes for one chunk of IIDs in one GraphQL query.

    Each IID gets its own alias (`mr_0`, `mr_1`, ...) inside the chunk. Returns one entry per IID in chunk
    order, where `None` means the merge request does not exist. Raises `ProjectNotFoundError` when the project
    is missing.
    """
    declarations = ["$fullPath: ID!", "$includeDescription: Boolean!"]
    variables: dict[str, Any] = {
        "fullPath": project,
        "includeDescription": include_description,
    }
    selections = []
    for index, iid in enumerate(iids):
        variable = f"iid{index}"
        declarations.append(f"${variable}: String!")
        variables[variable] = str(iid)
        selections.append(f"mr_{index}: mergeRequest(iid: ${variable}) {{ ...CompactMergeRequest }}")

    query = (
        f"query({', '.join(declarations)}) {{\n"
        "  project(fullPath: $fullPath) {\n"
        f"    {' '.join(selections)}\n"
        "  }\n"
        "}\n"
        f"{COMPACT_MERGE_REQUEST_FRAGMENT}"
    )
    data = await client.graphql(query, variables)
    if data["project"] is None:
        raise ProjectNotFoundError(project)
    return [data["project"][f"mr_{index}"] for index in range(len(iids))]


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_merge_requests(
    ctx: Context,
    project: Project,
    iids: Annotated[list[PositiveIid], Field(description="Positive merge request IIDs. Up to 20 unique IIDs.")],
    include_description: Annotated[bool, Field(description="True includes descriptions. Defaults to false.")] = False,
    description_limit: Annotated[
        int,
        Field(ge=0, le=DESCRIPTION_CAP, description="Maximum description length in characters, from 0 to 4000."),
    ] = DESCRIPTION_CAP,
) -> MergeRequestBatch:
    """Return compact status for up to 20 merge requests, similar to `glab mr view`.

    IIDs are deduplicated in requested order. More than 20 raw IIDs are accepted when there are at most 20 unique
    values. Missing IIDs are listed separately. This compact result has no reviewers, changes, or discussion/thread
    indexes. Descriptions are skipped by default; set `include_description` to true to include up to 4000 characters.
    Set `description_limit` to zero to skip descriptions even when requested. Batches above 8 IIDs run as several
    sequential GraphQL queries.
    """
    unique_iids = list(dict.fromkeys(iids))
    if not unique_iids:
        raise ToolError("Pass at least one merge request IID.")  # noqa: TRY003
    if len(unique_iids) > MAX_BATCH_MERGE_REQUESTS:
        raise ToolError("Pass no more than 20 unique merge request IIDs.")  # noqa: TRY003

    include_description = include_description and description_limit > 0
    client = get_client(ctx)
    nodes: list[dict[str, Any] | None] = []
    for chunk in batched(unique_iids, GRAPHQL_MR_CHUNK, strict=False):
        nodes.extend(await _fetch_compact_chunk(client, project, list(chunk), include_description))

    merge_requests = []
    missing_iids = []
    for iid, node in zip(unique_iids, nodes, strict=True):
        if node is None:
            missing_iids.append(iid)
            continue
        merge_requests.append(
            _to_compact_detail(
                node,
                include_description=include_description,
                description_limit=description_limit,
            ),
        )
    return MergeRequestBatch(merge_requests=merge_requests, missing_iids=missing_iids)
