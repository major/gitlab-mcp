"""Update-merge-request tool: fields, people, blocks, approval, and rebase in one call 🔧."""

import asyncio
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
lazy from collections.abc import Awaitable, Sequence

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
lazy from fastmcp import Context

from gitlab_mcp.app import get_client, mcp
from gitlab_mcp.client import GitLabAPIError, GitLabClient, GitLabConnectionError
from gitlab_mcp.common import (
    MAX_PAGES,
    REVIEW_NOTE_BODY_CAP,
    Iid,
    Project,
    has_quick_action,
    lower_or_empty,
    validate_project_labels,
)
from gitlab_mcp.mr_common import diff_refs_head_sha, fetch_blockers, fetch_merge_request, mr_rest_path, set_people

# Queries and mutations for update_merge_request. Each one changes one thing, so each step can fail on its own. 🔧
SET_DRAFT_MUTATION = """
mutation($projectPath: ID!, $iid: String!, $draft: Boolean!) {
  mergeRequestSetDraft(input: {projectPath: $projectPath, iid: $iid, draft: $draft}) {
    errors
  }
}
"""

# A small re-read for the final state, so the tool does not fetch discussions it does not need.
MERGE_REQUEST_STATE_QUERY = """
query($fullPath: ID!, $iid: String!) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      state draft detailedMergeStatus
      diffRefs { headSha }
      reviewers { nodes { username } }
      assignees { nodes { username } }
    }
  }
}
"""

# Reasons that replace the generic HTTP hint for one step. Seen live on GitLab 19.2:
# approving your own MR gives 401 (not a bad token), and unapproving when you never approved gives 404.
STEP_STATUS_REASONS = {
    ("approve", 401): "You may not approve this MR, for example because you are its author.",
    ("unapprove", 404): "You have not approved this MR.",
}

# Blocks per page on the REST blocks endpoint. The largest page GitLab serves.
BLOCKS_PAGE_SIZE = 100
# A rebase is watched for about 10 seconds: poll this many times, waiting this long before each poll. ⏱️
REBASE_POLL_ATTEMPTS = 5
# Tests set this to 0 so the poll loop runs instantly. ⚡
REBASE_POLL_DELAY = 2.0


class MergeRequestUpdate(BaseModel):
    """What one update did. Each step appears once, in the list that matches its outcome."""

    # GitLab confirmed these changes.
    applied: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    # GitLab accepted these, but they were still running when the wait ended. Check get_merge_request later.
    pending: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    # The write may have reached GitLab. Check with get_merge_request. Do not retry blindly.
    unknown: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    failed: dict[str, str] = Field(default_factory=dict, exclude_if=lambda v: not v)  # Step or item -> reason.
    state: str = Field(default="", exclude_if=lambda v: not v)  # opened, closed, merged, or locked.
    draft: bool = Field(default=False, exclude_if=lambda v: not v)
    merge_status: str = Field(default="", exclude_if=lambda v: not v)  # GitLab's detailed status, lowercased.
    head_sha: str = Field(default="", exclude_if=lambda v: not v)  # Full head SHA after the update.
    reviewers: list[str] | None = Field(default=None, exclude_if=lambda value: value is None)
    assignees: list[str] | None = Field(default=None, exclude_if=lambda value: value is None)
    # Full list of visible blockers after a blocker change. An empty list means none are left.
    blocked_by: list[int] | None = Field(default=None, exclude_if=lambda value: value is None)
    warning: str = Field(default="", exclude_if=lambda v: not v)  # Set when the final state could not be read.


def _verified_usernames(connection: Any) -> list[str] | None:
    """Return a complete username list from a valid GraphQL connection, or None when it cannot be verified."""
    if not isinstance(connection, dict):
        return None
    nodes = connection.get("nodes")
    if not isinstance(nodes, list) or any(
        not isinstance(node, dict) or not isinstance(node.get("username"), str) or not node["username"].strip()
        for node in nodes
    ):
        return None
    return [node["username"] for node in nodes]


class NothingToUpdateError(ToolError):
    """The call did not ask for any change."""

    def __init__(self) -> None:
        """Tell the agent to pass at least one change."""
        super().__init__("Nothing to update. Pass at least one change.")


class OverlapError(ToolError):
    """One item is both added and removed in the same call."""

    def __init__(self, label: str, names: list[str]) -> None:
        """Name the list type and the items, so the agent can fix the call."""
        super().__init__(f"Cannot add and remove the same {label} in one call: {', '.join(names)}.")


class ApprovalShaError(ToolError):
    """Approve was requested without the head SHA that guards it."""

    def __init__(self) -> None:
        """Point the agent to the SHA source."""
        super().__init__("approve=true needs sha, the full head SHA from get_merge_request.")


class ApprovalSeparateError(ToolError):
    """Approve was combined with a change that can undo it."""

    def __init__(self) -> None:
        """Ask the agent to make the approval its own call."""
        super().__init__("Approve in a separate call. Approval may not survive a rebase or a target branch change.")


class CommentBodyError(ToolError):
    """The comment is blank, too long, or may run a GitLab quick action."""

    def __init__(self) -> None:
        """Explain the comment rules, so the agent can fix the text."""
        super().__init__(
            f"comment must be nonblank, at most {REVIEW_NOTE_BODY_CAP} characters, and have no line that starts "
            "like a quick action (/close). Use state for close or reopen. No write was made."
        )


def _check_overlap(add: Sequence[Any], remove: Sequence[Any], label: str) -> None:
    """Raise OverlapError when one item is both added and removed in the same call."""
    overlap = sorted(str(item) for item in set(add) & set(remove))
    if overlap:
        raise OverlapError(label, overlap)


@dataclass
class _Changes:
    """The changes that one update asks for. Checked before any write is sent."""

    approve: bool | None
    sha: str | None
    comment: str | None
    state: Literal["close", "reopen"] | None
    draft: bool | None
    title: str | None
    description: str | None
    target_branch: str | None
    add_labels: list[str]
    remove_labels: list[str]
    add_reviewers: list[str]
    remove_reviewers: list[str]
    add_assignees: list[str]
    remove_assignees: list[str]
    add_blocked_by: list[int]
    remove_blocked_by: list[int]
    rebase: bool

    def check(self) -> None:
        """Raise ToolError when the request asks for nothing, contradicts itself, or cannot be sent safely.

        Runs before any write, so a rejected request changes nothing.
        """
        if not self._has_change():
            raise NothingToUpdateError
        _check_overlap(self.add_labels, self.remove_labels, "label")
        _check_overlap(self.add_reviewers, self.remove_reviewers, "reviewer")
        _check_overlap(self.add_assignees, self.remove_assignees, "assignee")
        _check_overlap(self.add_blocked_by, self.remove_blocked_by, "blocking merge request")
        if self.comment is not None and (
            not self.comment.strip() or len(self.comment) > REVIEW_NOTE_BODY_CAP or has_quick_action(self.comment)
        ):
            raise CommentBodyError
        if self.approve and not self.sha:
            raise ApprovalShaError
        # Approval may not survive a rebase or a target branch change, so GitLab needs them in separate calls.
        if self.approve is not None and (self.rebase or self.target_branch is not None):
            raise ApprovalSeparateError

    def fields_body(self) -> dict[str, Any]:
        """Return the body of the one PUT for the plain fields. Only the fields that were given are included.

        An empty description is kept on purpose, because it clears the description.
        """
        given = {
            "state_event": self.state,
            "title": self.title,
            "description": self.description,
            "target_branch": self.target_branch,
        }
        body: dict[str, Any] = {key: value for key, value in given.items() if value is not None}
        if self.add_labels:
            body["add_labels"] = ",".join(self.add_labels)
        if self.remove_labels:
            body["remove_labels"] = ",".join(self.remove_labels)
        return body

    def _has_change(self) -> bool:
        """Return True when at least one field, list, or rebase was requested."""
        given = (self.approve, self.comment, self.state, self.draft, self.title, self.description, self.target_branch)
        lists = (
            self.add_labels,
            self.remove_labels,
            self.add_reviewers,
            self.remove_reviewers,
            self.add_assignees,
            self.remove_assignees,
            self.add_blocked_by,
            self.remove_blocked_by,
        )
        return any(value is not None for value in given) or any(lists) or self.rebase


async def _list_blocks(client: GitLabClient, mr_path: str) -> tuple[list[dict[str, Any]], bool]:
    """Return the block entries of a merge request, and whether the scan stopped before the last page.

    The scan is bounded by MAX_PAGES, like the other list scans in this file.
    """
    entries: list[dict[str, Any]] = []
    page = 1
    for _ in range(MAX_PAGES):
        data, next_page = await client.get_page(f"{mr_path}/blocks", per_page=BLOCKS_PAGE_SIZE, page=page)
        entries.extend(data)
        if next_page is None:
            return entries, False
        page = next_page
    return entries, True


def _find_block(entries: list[dict[str, Any]], iid: int) -> dict[str, Any] | None:
    """Return the entry where merge request `iid` blocks this merge request, or None.

    The blocker must be in the same project. GitLab can list an IID from another
    project too, so the project IDs have to match. Entries with no blocker are skipped.
    """
    for entry in entries:
        blocker = entry["blocking_merge_request"]
        if blocker is None:  # The blocking MR is gone, so there is nothing to match.
            continue
        same_project = blocker["project_id"] == entry["blocked_merge_request"]["project_id"]
        if same_project and blocker["iid"] == iid:
            return entry
    return None


@dataclass
class _Update:
    """Runs the steps of one update_merge_request call. Steps are independent, so one failure never stops the rest."""

    client: GitLabClient
    project: str
    iid: int
    changes: _Changes
    applied: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def path(self) -> str:
        """Return the REST path of this merge request."""
        return mr_rest_path(self.project, self.iid)

    async def run(self) -> None:
        """Run every requested step, in the order the docs describe.

        The comment goes first, so a note like "replaced by !12" lands before the MR closes. 💬
        """
        await self._comment()
        await self._labels()
        await self._fields()
        await self._draft()
        await self._people()
        await self._blocks()
        await self._approval()
        await self._rebase()

    async def attempt(self, name: str, action: Awaitable[Any], accepted: list[str] | None = None) -> None:
        """Await one write and record how it ended under `name`.

        A connection error means the write may have reached GitLab, so it goes to
        `unknown`. A refusal from GitLab goes to `failed`, with its reason. A success
        goes to `accepted`, which defaults to `applied`.
        """
        try:
            await action
        except GitLabConnectionError:
            self.unknown.append(name)
        except GitLabAPIError as exc:
            # Some steps reuse a status for a common, harmless case. The generic hint would mislead there. 🧭
            reason = STEP_STATUS_REASONS.get((name, exc.status))
            self.failed[name] = f"GitLab returned HTTP {exc.status}. {reason}" if reason else str(exc)
        except ToolError as exc:
            self.failed[name] = str(exc)
        else:
            (self.applied if accepted is None else accepted).append(name)

    async def read_back(self) -> dict[str, Any]:
        """Re-read the merge request and return its final fields, or a warning when the read fails."""
        try:
            node = await fetch_merge_request(self.client, MERGE_REQUEST_STATE_QUERY, self.project, self.iid)
        except ToolError as exc:
            return {"warning": f"Could not read the merge request back. Check it with get_merge_request. {exc}"}
        final: dict[str, Any] = {
            "state": node["state"],
            "draft": node["draft"],
            "merge_status": lower_or_empty(node["detailedMergeStatus"]),
            "head_sha": diff_refs_head_sha(node["diffRefs"]),
        }
        if self.changes.add_reviewers or self.changes.remove_reviewers:
            reviewers = _verified_usernames(node.get("reviewers"))
            if reviewers is not None:
                final["reviewers"] = reviewers
        if self.changes.add_assignees or self.changes.remove_assignees:
            assignees = _verified_usernames(node.get("assignees"))
            if assignees is not None:
                final["assignees"] = assignees
        return final

    async def read_blockers(self) -> tuple[list[int] | None, str]:
        """Re-read the blockers after a blocker change. Returns the IIDs, or a warning when the read fails.

        A failed read gives `None` for the IIDs, so the result does not claim a list that was never checked.
        """
        if not (self.changes.add_blocked_by or self.changes.remove_blocked_by):
            return None, ""
        try:
            blockers = await fetch_blockers(self.client, self.project, [self.iid])
        except ToolError as exc:
            return None, f"Could not read the blockers back. Check them with get_merge_request. {exc}"
        return blockers[self.iid].iids, ""

    async def _comment(self) -> None:
        """Post one top-level MR comment, like `glab mr note`."""
        if self.changes.comment is not None:
            request = self.client.request("POST", f"{self.path}/notes", json={"body": self.changes.comment})
            await self.attempt("comment", request)

    async def _fields(self) -> None:
        """Send one PUT with every plain field that was given."""
        body = self.changes.fields_body()
        if body:
            await self.attempt("fields", self.client.request("PUT", self.path, json=body))

    async def _labels(self) -> None:
        """Check project and ancestor-group labels before including them in a fields PUT."""
        changes = self.changes
        requested = [*changes.add_labels, *changes.remove_labels]
        if not requested:
            return
        valid, failures = await validate_project_labels(self.client, self.project, requested)
        known = set(valid)
        changes.add_labels = [name for name in changes.add_labels if name in known]
        changes.remove_labels = [name for name in changes.remove_labels if name in known]
        self.failed.update({f"labels:{name}": reason for name, reason in failures.items()})

    async def _draft(self) -> None:
        """Set or clear draft status. GitLab only accepts this change through GraphQL."""
        if self.changes.draft is not None:
            variables = {"projectPath": self.project, "iid": str(self.iid), "draft": self.changes.draft}
            await self.attempt("draft", self.client.mutate(SET_DRAFT_MUTATION, variables, "mergeRequestSetDraft"))

    async def _people(self) -> None:
        """Add or remove reviewers and assignees, using one shared username lookup and one call per list."""
        changes = self.changes
        jobs = [
            ("reviewers", "add_reviewers", "APPEND", changes.add_reviewers),
            ("reviewers", "remove_reviewers", "REMOVE", changes.remove_reviewers),
            ("assignees", "add_assignees", "APPEND", changes.add_assignees),
            ("assignees", "remove_assignees", "REMOVE", changes.remove_assignees),
        ]
        outcome = await set_people(self.client, self.project, self.iid, jobs)
        self.applied.extend(outcome.applied)
        self.unknown.extend(outcome.unknown)
        self.failed.update(outcome.failed)

    async def _blocks(self) -> None:
        """Add or remove blocks. Each blocker IID gets its own request, so one failure stays on its own item."""
        for iid in self.changes.add_blocked_by:
            request = self.client.request("POST", f"{self.path}/blocks", blocking_merge_request_iid=iid)
            await self.attempt(f"blocked_by:{iid}", request)
        if self.changes.remove_blocked_by:
            await self._remove_blocks(self.changes.remove_blocked_by)

    async def _remove_blocks(self, iids: list[int]) -> None:
        """Delete the block that each IID has on this merge request. A missing block is a failure."""
        try:
            entries, truncated = await _list_blocks(self.client, self.path)
        except ToolError as exc:
            self.failed["remove_blocked_by"] = str(exc)
            return
        for iid in iids:
            key = f"blocked_by:{iid}"
            entry = _find_block(entries, iid)
            if entry is None:
                missing = f"not found in the first {MAX_PAGES} pages of blocks" if truncated else "not a blocker"
                self.failed[key] = missing
            else:
                await self.attempt(key, self.client.request("DELETE", f"{self.path}/blocks/{entry['id']}"))

    async def _approval(self) -> None:
        """Approve or unapprove. The SHA guards the approval, so GitLab refuses it when new commits arrived."""
        if self.changes.approve is True:
            body = {"sha": self.changes.sha}
            await self.attempt("approve", self.client.request("POST", f"{self.path}/approve", json=body))
        elif self.changes.approve is False:
            await self.attempt("unapprove", self.client.request("POST", f"{self.path}/unapprove"))

    async def _rebase(self) -> None:
        """Ask GitLab to rebase, then watch the job for about 10 seconds.

        GitLab accepts a rebase even when it will fail later, so the conflicts flag is
        checked first. An MR with conflicts is never sent to the rebase endpoint. 🧯
        """
        if not self.changes.rebase:
            return
        try:
            mr = await self.client.get(self.path)
        except ToolError as exc:
            self.failed["rebase"] = str(exc)
            return
        if mr.get("has_conflicts"):
            source_branch = mr.get("source_branch")
            target_branch = mr.get("target_branch")
            head_sha = mr.get("sha")
            details = [
                f"{name} '{value}'"
                for name, value in (
                    ("source branch", source_branch),
                    ("target branch", target_branch),
                    ("head SHA", head_sha),
                )
                if isinstance(value, str) and value
            ]
            message = "GitLab cannot rebase this MR because it has conflicts."
            if details:
                message += f" MR details: {', '.join(details)}."
            if isinstance(source_branch, str) and source_branch and isinstance(head_sha, str) and head_sha:
                message += f" Rebase locally, then push with `--force-with-lease={source_branch}:{head_sha}`."
            else:
                message += (
                    " Rebase locally, then use `--force-with-lease` when pushing "
                    "after verifying the expected remote SHA."
                )
            self.failed["rebase"] = message
            return
        await self.attempt("rebase", self.client.request("PUT", f"{self.path}/rebase"), accepted=self.pending)
        if "rebase" in self.pending:
            await self._watch_rebase()

    async def _watch_rebase(self) -> None:
        """Poll until GitLab finishes the rebase, or until the wait runs out.

        A finished rebase moves from `pending` to `applied`, or to `failed` with GitLab's
        `merge_error`. A failed poll leaves it pending, because GitLab already accepted the write.
        """
        for _ in range(REBASE_POLL_ATTEMPTS):
            await asyncio.sleep(REBASE_POLL_DELAY)
            try:
                mr = await self.client.get(self.path, include_rebase_in_progress=True)
            except ToolError:
                return
            # A missing flag counts as still running, so an unclear answer never reads as success.
            if mr.get("rebase_in_progress", True):
                continue
            self.pending.remove("rebase")
            if mr.get("merge_error"):
                self.failed["rebase"] = mr["merge_error"]
            else:
                self.applied.append("rebase")
            return


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def update_merge_request(  # noqa: PLR0913, PLR0917 - each argument is one user-facing change.
    ctx: Context,
    project: Project,
    iid: Iid,
    approve: Annotated[bool | None, Field(description="Approve or remove your approval.")] = None,
    sha: Annotated[
        str | None,
        Field(description="Full head SHA, required to approve."),
    ] = None,
    comment: Annotated[
        str | None,
        Field(description="Top-level MR comment, posted before the other changes. Needs the user's APPROVE."),
    ] = None,
    state: Annotated[Literal["close", "reopen"] | None, Field(description="Close or reopen.")] = None,
    draft: Annotated[bool | None, Field(description="Set draft status.")] = None,
    title: Annotated[str | None, Field(description="New title.")] = None,
    description: Annotated[str | None, Field(description="New description; empty clears it.")] = None,
    target_branch: Annotated[str | None, Field(description="New target branch.")] = None,
    add_labels: Annotated[list[str] | None, Field(description="Labels to add.")] = None,
    remove_labels: Annotated[list[str] | None, Field(description="Labels to remove.")] = None,
    add_reviewers: Annotated[list[str] | None, Field(description="Reviewer usernames to add.")] = None,
    remove_reviewers: Annotated[list[str] | None, Field(description="Reviewer usernames to remove.")] = None,
    add_assignees: Annotated[list[str] | None, Field(description="Assignee usernames to add.")] = None,
    remove_assignees: Annotated[list[str] | None, Field(description="Assignee usernames to remove.")] = None,
    add_blocked_by: Annotated[
        list[int] | None,
        Field(description="Same-project blocker IIDs to add."),
    ] = None,
    remove_blocked_by: Annotated[
        list[int] | None,
        Field(description="Same-project blocker IIDs to remove."),
    ] = None,
    rebase: Annotated[bool, Field(description="Ask GitLab to rebase.")] = False,
) -> MergeRequestUpdate:
    """Reviewer and assignee changes for merge request review. Like `glab mr update`.

    Also like `glab mr note`, `glab mr close`, `glab mr reopen`, `glab mr approve`, and `glab mr rebase`.

    Lower risk than a merge, but not always reversible. A rebase rewrites the
    source branch. An approval can let an auto-merge that is already enabled go through.

    - Several changes go in one call. Each step runs even when an earlier step failed.
    - `comment` posts one general MR comment first, so "comment + close" is one call. Show the user the final
      text and get explicit APPROVE before posting. A line that starts like a quick action is rejected.
    - `applied`: GitLab confirmed the change. `pending`: the rebase was still running after about 10 seconds.
      Check get_merge_request later. A rebase reports `applied` with the new `head_sha`,
      or `failed` with GitLab's reason.
    - A rebase on an MR with conflicts fails right away and is never sent, because GitLab cannot rebase it.
      Rebase locally and push instead.
    - `unknown`: the request may have reached GitLab. Check with get_merge_request. Do not retry blindly.
    - `failed` maps each step or item to a reason. Items look like `reviewers:bob` (unknown user) or `blocked_by:12`.
    - `approve` needs `sha`, the full `head_sha` from get_merge_request. Approve in its own call, because
      approval may not survive a rebase or a target_branch change.
    - Usernames must be real users. Blockers are IIDs of MRs in this same project.
    - After a blocker change, `blocked_by` lists the visible blocker IIDs. An empty list means none are left.
    - An invalid request sends nothing: no changes, a bad comment, the same item in an add and a remove list,
      or approve with rebase or target_branch.
    - Needs a token with the `api` scope.
    """
    client = get_client(ctx)
    changes = _Changes(
        approve=approve,
        sha=sha,
        comment=comment,
        state=state,
        draft=draft,
        title=title,
        description=description,
        target_branch=target_branch,
        add_labels=add_labels or [],
        remove_labels=remove_labels or [],
        add_reviewers=add_reviewers or [],
        remove_reviewers=remove_reviewers or [],
        add_assignees=add_assignees or [],
        remove_assignees=remove_assignees or [],
        add_blocked_by=add_blocked_by or [],
        remove_blocked_by=remove_blocked_by or [],
        rebase=rebase,
    )
    changes.check()
    update = _Update(client=client, project=project, iid=iid, changes=changes)
    await update.run()
    if not (update.applied or update.pending or update.unknown):
        raise ToolError("; ".join(f"{name}: {reason}" for name, reason in update.failed.items()))
    final = await update.read_back()
    blocked_by, blockers_warning = await update.read_blockers()
    if blockers_warning:
        # Both re-reads can fail. Join the warnings so neither one is lost.
        final["warning"] = " ".join(filter(None, (final.get("warning"), blockers_warning)))
    return MergeRequestUpdate(
        applied=update.applied,
        pending=update.pending,
        unknown=update.unknown,
        failed=update.failed,
        blocked_by=blocked_by,
        **final,
    )
