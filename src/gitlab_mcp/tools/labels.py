"""Project label names, plus checks that labels exist before a merge request uses them 🏷️."""

from typing import Annotated, Any
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.common import MAX_PAGES, PROJECT_LABELS_PAGE_SIZE, Project
lazy from gitlab_mcp.client import GitLabClient


class ProjectLabels(BaseModel):
    """Project label names, either one compact page or the result of a names check.

    A normal call fills `labels`. A names check fills `existing` and `missing` instead.
    """

    labels: str = Field(default="", exclude_if=lambda v: not v)
    has_more: bool
    existing: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    missing: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)


class SearchAndNamesError(ToolError):
    """list_labels got both a search and a names check."""

    def __init__(self) -> None:
        """Tell the agent to pass only one of the two filters."""
        super().__init__("Pass either search or names to list_labels, not both.")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def list_labels(
    ctx: Context,
    project: Project,
    search: str | None = None,
    names: Annotated[
        list[str] | None,
        Field(description="Exact label names to check. Pass either this or search, not both."),
    ] = None,
) -> ProjectLabels:
    """List the first 100 project and ancestor label names, like `glab api projects/:id/labels`.

    The result contains names only. If `has_more` is true, refine the search to find labels beyond the first page.
    Pass `names` to check that labels exist before create_merge_request(labels=...). The result then lists
    `existing` and `missing` names. `has_more` is true only when the page limit ended the check
    while names were still missing, so `missing` may be incomplete.
    """
    if names is not None:
        if search is not None:
            raise SearchAndNamesError
        return await _check_label_names(get_client(ctx), project, names)

    params: dict[str, Any] = {
        "per_page": PROJECT_LABELS_PAGE_SIZE,
        "include_ancestor_groups": True,
        "page": 1,
    }
    if search is not None:
        params["search"] = search

    path = f"/projects/{quote(project, safe='')}/labels"
    labels, next_page = await get_client(ctx).get_page(path, **params)
    return ProjectLabels(
        labels="\n".join(label["name"] for label in labels),
        has_more=next_page is not None,
    )


async def _check_label_names(client: GitLabClient, project: str, names: list[str]) -> ProjectLabels:
    """Return which requested label names exist in the project, reading at most MAX_PAGES label pages.

    Names are matched exactly and case-sensitively. The scan stops early once every name is found.
    `existing` and `missing` keep the order of the request, without duplicates.
    """
    wanted = list(dict.fromkeys(names))
    path = f"/projects/{quote(project, safe='')}/labels"
    found: set[str] = set()
    page = 1
    for _ in range(MAX_PAGES):
        labels, next_page = await client.get_page(
            path,
            include_ancestor_groups=True,
            per_page=PROJECT_LABELS_PAGE_SIZE,
            page=page,
        )
        found.update(label["name"] for label in labels)
        if found.issuperset(wanted) or next_page is None:
            return _label_check_result(wanted, found, has_more=False)
        page = next_page
    return _label_check_result(wanted, found, has_more=True)


def _label_check_result(wanted: list[str], found: set[str], *, has_more: bool) -> ProjectLabels:
    """Split the requested names into existing and missing, keeping the request order."""
    return ProjectLabels(
        has_more=has_more,
        existing=[name for name in wanted if name in found],
        missing=[name for name in wanted if name not in found],
    )
