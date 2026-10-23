"""Tests for the update_merge_request tool, run through a real in-memory MCP client ✏️."""

import json
from typing import Any
lazy from collections.abc import Callable

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import MAX_PAGES, mcp

GRAPHQL = "https://gitlab.example.com/api/graphql"
MR = "https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/2187"
PROJECT = "group/project"
PROJECT_ID = 1
IID = 2187
SHA = "6096f2d5a1b2c3d4e5f60718293a4b5c6d7e8f90"  # Full SHA, because approval needs it exact.
KNOWN_USERS = ("alice", "bob")
MUTATION_PAYLOADS = ("mergeRequestSetDraft", "mergeRequestSetReviewers", "mergeRequestSetAssignees")
GRAPHQL_MARKERS = ("users(", *MUTATION_PAYLOADS, "mergeRequest(")
READ_BACK = {"state": "opened", "draft": False, "detailedMergeStatus": "MERGEABLE", "diffRefs": {"headSha": SHA}}


@pytest.fixture
def router(gitlab_env):
    """Give each test its own mock router. Unused routes are fine, because each test checks what it needs."""
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


async def call_tool(**arguments: Any) -> dict[str, Any]:
    """Call update_merge_request through the MCP protocol and return its data."""
    async with Client(mcp) as client:
        result = await client.call_tool("update_merge_request", {"project": PROJECT, "iid": IID, **arguments})
    return result.structured_content


def operation(request: httpx.Request) -> str:
    """Return the GraphQL query text that a request carries."""
    return json.loads(request.content)["query"]


def graphql_calls(route: respx.Route, marker: str) -> list[dict[str, Any]]:
    """Return the variables of each GraphQL call whose query contains `marker`, in order."""
    bodies = [json.loads(call.request.content) for call in route.calls]
    return [body["variables"] for body in bodies if marker in body["query"]]


def requested_pages(route: respx.Route) -> list[int]:
    """Return the `page` query parameter of every request the route received, in order."""
    return [int(call.request.url.params["page"]) for call in route.calls]


def mr_body(**fields: Any) -> dict[str, Any]:
    """Wrap merge request fields in the response shape of the re-read query."""
    return {"data": {"project": {"mergeRequest": {**READ_BACK, **fields}}}}


def default_graphql_body(query: str) -> dict[str, Any]:
    """Answer like a healthy GitLab: every user exists, every mutation succeeds, and the MR is open."""
    if "users(" in query:
        return {"data": {"users": {"nodes": [{"username": name} for name in KNOWN_USERS]}}}
    for payload in MUTATION_PAYLOADS:
        if payload in query:
            return {"data": {payload: {"errors": []}}}
    return mr_body()


def graphql_mock(replies: dict[str, Any] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    """Return a GraphQL handler. `replies` maps a query marker to a JSON body, a response, or an exception to raise.

    Queries without a reply get the healthy default from `default_graphql_body`.
    """
    replies = replies or {}

    def handler(request: httpx.Request) -> httpx.Response:
        query = operation(request)
        for marker, reply in replies.items():
            if marker not in query:
                continue
            if isinstance(reply, Exception):
                raise reply
            if isinstance(reply, httpx.Response):
                return reply
            return httpx.Response(200, json=reply)
        return httpx.Response(200, json=default_graphql_body(query))

    return handler


def serve_writes(router: respx.MockRouter, replies: dict[str, Any] | None = None) -> dict[str, respx.Route]:
    """Register a success reply for every write endpoint and for GraphQL. Tests can swap any route later."""
    return {
        "fields": router.put(MR).mock(return_value=httpx.Response(200, json={})),
        "rebase": router.put(f"{MR}/rebase").mock(return_value=httpx.Response(202, json={"message": "202 Accepted"})),
        "approve": router.post(f"{MR}/approve").mock(return_value=httpx.Response(201, json={})),
        "unapprove": router.post(f"{MR}/unapprove").mock(return_value=httpx.Response(201, json={})),
        "add_block": router.post(f"{MR}/blocks").mock(return_value=httpx.Response(201, json={})),
        "list_blocks": router.get(f"{MR}/blocks").mock(return_value=httpx.Response(200, json=[])),
        "graphql": router.post(GRAPHQL).mock(side_effect=graphql_mock(replies)),
    }


def block(entry_id: int, blocker_iid: int | None, *, blocker_project: int = PROJECT_ID) -> dict[str, Any]:
    """Build one block entry from the blocks endpoint. A null blocker stands for a blocker that is gone."""
    blocker = None if blocker_iid is None else {"iid": blocker_iid, "project_id": blocker_project}
    return {"id": entry_id, "blocking_merge_request": blocker, "blocked_merge_request": {"project_id": PROJECT_ID}}


def serve_block_pages(pages: dict[int, list[dict[str, Any]]]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer each blocks request with the page it asks for. A page that exists sets X-Next-Page to the next one."""

    def handler(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params["page"])
        next_page = number + 1 if number + 1 in pages else None
        headers = {"X-Next-Page": "" if next_page is None else str(next_page)}
        return httpx.Response(200, json=pages[number], headers=headers)

    return handler


def step_label(request: httpx.Request) -> str:
    """Name one request by its GraphQL operation, or by its method and the path after the MR, to read the order."""
    if request.url.path == "/api/graphql":
        return next(marker for marker in GRAPHQL_MARKERS if marker in operation(request))
    suffix = request.url.path.partition(f"/merge_requests/{IID}")[2]
    return f"{request.method} {suffix}".strip()


@pytest.mark.parametrize(
    ("arguments", "body"),
    [
        ({"title": "New title"}, {"title": "New title"}),
        ({"description": "Fixed the bug."}, {"description": "Fixed the bug."}),
        ({"description": ""}, {"description": ""}),  # An empty description clears it, so it must be sent.
        ({"state": "close"}, {"state_event": "close"}),
        ({"state": "reopen"}, {"state_event": "reopen"}),
        ({"target_branch": "main"}, {"target_branch": "main"}),
        ({"add_labels": ["bug", "urgent"]}, {"add_labels": "bug,urgent"}),
        ({"remove_labels": ["wip"]}, {"remove_labels": "wip"}),
    ],
)
async def test_plain_fields_go_in_one_put(gitlab_env, router, arguments, body):
    """Plain fields are sent in one PUT, with the GitLab names for each field."""
    # Arrange
    routes = serve_writes(router)

    # Act
    data = await call_tool(**arguments)

    # Assert
    assert [json.loads(call.request.content) for call in routes["fields"].calls] == [body]
    assert data["applied"] == ["fields"]


async def test_all_plain_fields_share_one_put(gitlab_env, router):
    """Several plain fields and labels go in one PUT, and draft is not one of them."""
    # Arrange
    routes = serve_writes(router)

    # Act
    data = await call_tool(
        title="T",
        description="D",
        target_branch="main",
        state="close",
        add_labels=["a"],
        remove_labels=["b", "c"],
    )

    # Assert
    assert routes["fields"].call_count == 1
    assert json.loads(routes["fields"].calls[0].request.content) == {
        "title": "T",
        "description": "D",
        "target_branch": "main",
        "state_event": "close",
        "add_labels": "a",
        "remove_labels": "b,c",
    }
    assert data["applied"] == ["fields"]


@pytest.mark.parametrize("draft", [True, False])
async def test_draft_uses_the_graphql_mutation(gitlab_env, router, draft):
    """Draft status goes through the GraphQL mutation, for both true and false."""
    # Arrange
    routes = serve_writes(router)

    # Act
    data = await call_tool(draft=draft)

    # Assert
    expected = {"projectPath": PROJECT, "iid": str(IID), "draft": draft}
    assert graphql_calls(routes["graphql"], "mergeRequestSetDraft") == [expected]
    assert routes["fields"].call_count == 0
    assert data["applied"] == ["draft"]


@pytest.mark.parametrize(
    ("argument", "payload", "mode"),
    [
        ("add_reviewers", "mergeRequestSetReviewers", "APPEND"),
        ("remove_reviewers", "mergeRequestSetReviewers", "REMOVE"),
        ("add_assignees", "mergeRequestSetAssignees", "APPEND"),
        ("remove_assignees", "mergeRequestSetAssignees", "REMOVE"),
    ],
)
async def test_people_lists_send_one_mutation_with_the_mode(gitlab_env, router, argument, payload, mode):
    """Each reviewer or assignee list sends one call, with APPEND or REMOVE."""
    # Arrange
    routes = serve_writes(router)

    # Act
    data = await call_tool(**{argument: ["alice", "bob"]})

    # Assert
    expected = {"projectPath": PROJECT, "iid": str(IID), "usernames": ["alice", "bob"], "mode": mode}
    assert graphql_calls(routes["graphql"], payload) == [expected]
    assert data["applied"] == [argument]


async def test_usernames_are_checked_once_and_unknown_ones_are_left_out(gitlab_env, router):
    """One lookup covers every name. Matching ignores case, and GitLab's spelling is sent."""
    # Arrange
    routes = serve_writes(router)

    # Act
    data = await call_tool(add_reviewers=["Alice", "carol"], remove_assignees=["ghost"])

    # Assert
    assert graphql_calls(routes["graphql"], "users(") == [{"usernames": ["Alice", "carol", "ghost"]}]
    reviewers = graphql_calls(routes["graphql"], "mergeRequestSetReviewers")
    assert [variables["usernames"] for variables in reviewers] == [["alice"]]
    assert graphql_calls(routes["graphql"], "mergeRequestSetAssignees") == []
    assert data["applied"] == ["add_reviewers"]
    assert data["failed"] == {"reviewers:carol": "unknown user", "assignees:ghost": "unknown user"}


async def test_add_reviewers_uses_append_without_reading_or_replacing_reviewers(gitlab_env, router):
    """Adding reviewers uses APPEND and does not read or replace the reviewer list."""
    # Arrange
    routes = serve_writes(router)

    # Act
    data = await call_tool(add_reviewers=["alice", "alice", "bob"])

    # Assert
    assert graphql_calls(routes["graphql"], "users(") == [{"usernames": ["alice", "bob"]}]
    assert graphql_calls(routes["graphql"], "mergeRequestSetReviewers") == [
        {"projectPath": PROJECT, "iid": str(IID), "usernames": ["alice", "alice", "bob"], "mode": "APPEND"}
    ]
    assert graphql_calls(routes["graphql"], "mergeRequest(") == [{"fullPath": PROJECT, "iid": str(IID)}]
    assert [step_label(call.request) for call in router.calls] == [
        "users(",
        "mergeRequestSetReviewers",
        "mergeRequest(",
    ]
    assert routes["fields"].call_count == 0
    assert data["applied"] == ["add_reviewers"]


async def test_username_lookup_failure_skips_people_but_not_other_steps(gitlab_env, router):
    """A failed username lookup skips the people calls only."""
    # Arrange
    routes = serve_writes(router, replies={"users(": httpx.Response(500, json={"message": "Boom"})})

    # Act
    data = await call_tool(title="New", add_reviewers=["alice"], remove_assignees=["bob"])

    # Assert
    assert routes["fields"].call_count == 1
    assert graphql_calls(routes["graphql"], "mergeRequestSetReviewers") == []
    assert data["applied"] == ["fields"]
    assert "HTTP 500" in data["failed"]["usernames"]


async def test_add_blocked_by_posts_one_request_per_iid(gitlab_env, router):
    """Each blocker IID gets its own POST, with the IID as a query parameter."""
    # Arrange
    routes = serve_writes(router)

    # Act
    data = await call_tool(add_blocked_by=[5, 6])

    # Assert
    blocking = [call.request.url.params["blocking_merge_request_iid"] for call in routes["add_block"].calls]
    assert blocking == ["5", "6"]
    assert data["applied"] == ["blocked_by:5", "blocked_by:6"]


async def test_remove_blocked_by_deletes_the_same_project_block_across_pages(gitlab_env, router):
    """The match is found on page 2. Page 1 has a block from another project and an entry with no blocker."""
    # Arrange
    routes = serve_writes(router)
    pages = {
        1: [block(10, 5, blocker_project=99), block(12, None)],
        2: [block(11, 5), block(13, 6)],
    }
    routes["list_blocks"].mock(side_effect=serve_block_pages(pages))
    delete = router.delete(f"{MR}/blocks/11").mock(return_value=httpx.Response(204))

    # Act
    data = await call_tool(remove_blocked_by=[5, 7])

    # Assert
    assert requested_pages(routes["list_blocks"]) == [1, 2]
    assert delete.call_count == 1
    assert data["applied"] == ["blocked_by:5"]
    assert data["failed"] == {"blocked_by:7": "not a blocker"}


async def test_block_scan_stops_at_the_page_limit(gitlab_env, router):
    """When the blocks never end, the scan stops at MAX_PAGES and says the answer is incomplete."""
    # Arrange
    routes = serve_writes(router)
    routes["list_blocks"].mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=[],
            headers={"X-Next-Page": str(int(request.url.params["page"]) + 1)},
        ),
    )

    # Act
    data = await call_tool(remove_blocked_by=[5], title="New")

    # Assert
    assert requested_pages(routes["list_blocks"]) == list(range(1, MAX_PAGES + 1))
    assert data["failed"] == {"blocked_by:5": f"not found in the first {MAX_PAGES} pages of blocks"}


async def test_block_lookup_failure_skips_the_deletes(gitlab_env, router):
    """A failed blocks lookup records one failure and sends no deletes."""
    # Arrange
    routes = serve_writes(router)
    routes["list_blocks"].mock(return_value=httpx.Response(500, json={"message": "Boom"}))

    # Act
    data = await call_tool(remove_blocked_by=[5], title="New")

    # Assert
    assert data["applied"] == ["fields"]
    assert "HTTP 500" in data["failed"]["remove_blocked_by"]


async def test_approve_sends_the_head_sha(gitlab_env, router):
    """Approve sends the full head SHA as the guard."""
    # Arrange
    routes = serve_writes(router)

    # Act
    data = await call_tool(approve=True, sha=SHA)

    # Assert
    assert [json.loads(call.request.content) for call in routes["approve"].calls] == [{"sha": SHA}]
    assert data["applied"] == ["approve"]


async def test_unapprove_posts_to_the_unapprove_endpoint(gitlab_env, router):
    """approve=false posts to the unapprove endpoint, not approve."""
    # Arrange
    routes = serve_writes(router)

    # Act
    data = await call_tool(approve=False)

    # Assert
    assert routes["unapprove"].call_count == 1
    assert routes["approve"].call_count == 0
    assert data["applied"] == ["unapprove"]


@pytest.mark.parametrize(
    ("arguments", "route", "status", "reason"),
    [
        ({"approve": True, "sha": SHA}, "approve", 401, "you are its author"),
        ({"approve": False}, "unapprove", 404, "You have not approved this MR."),
    ],
)
async def test_approval_refusals_get_a_clear_reason(router, arguments, route, status, reason):
    """GitLab's 401 on approve and 404 on unapprove are normal refusals, not a bad token or a missing MR."""
    # Arrange
    routes = serve_writes(router)
    routes[route].mock(return_value=httpx.Response(status, json={"message": f"{status} error"}))

    # Act
    data = await call_tool(title="New", **arguments)

    # Assert
    message = data["failed"][route]
    assert message.startswith(f"GitLab returned HTTP {status}. ")
    assert reason in message
    assert "token" not in message


@pytest.mark.parametrize("marker", ["mergeRequestSetReviewers", "mergeRequestSetAssignees"])
async def test_people_mutations_declare_a_non_null_username_list(gitlab_env, router, marker):
    """GitLab rejects a nullable `[String!]` variable for the username argument, so it must be `[String!]!`."""
    # Arrange
    routes = serve_writes(router)
    kind = "reviewers" if "Reviewers" in marker else "assignees"

    # Act
    await call_tool(**{f"add_{kind}": ["alice"]})

    # Assert
    queries = [operation(call.request) for call in routes["graphql"].calls if marker in operation(call.request)]
    assert len(queries) == 1
    assert "$usernames: [String!]!" in queries[0]


async def test_rebase_is_pending_not_applied(gitlab_env, router):
    """A rebase is accepted, so it is pending, and never applied."""
    # Arrange
    routes = serve_writes(router)

    # Act
    data = await call_tool(rebase=True)

    # Assert
    assert routes["rebase"].call_count == 1
    assert data["pending"] == ["rebase"]
    assert "applied" not in data


async def test_steps_run_in_the_documented_order(gitlab_env, router):
    """Steps run in order: fields, draft, people, blocks, approval, then the re-read."""
    """Each request is labeled by what it does, so the order reads at a glance."""
    # Arrange
    serve_writes(router)

    # Act
    await call_tool(
        title="New",
        draft=True,
        add_reviewers=["alice"],
        remove_assignees=["bob"],
        add_blocked_by=[5],
        approve=True,
        sha=SHA,
    )

    # Assert
    assert [step_label(call.request) for call in router.calls] == [
        "PUT",
        "mergeRequestSetDraft",
        "users(",
        "mergeRequestSetReviewers",
        "mergeRequestSetAssignees",
        "POST /blocks",
        "POST /approve",
        "mergeRequest(",
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "Nothing to update"),
        ({"rebase": False}, "Nothing to update"),
        ({"add_labels": []}, "Nothing to update"),
        ({"add_labels": ["bug"], "remove_labels": ["bug"]}, "same label in one call: bug"),
        ({"add_reviewers": ["alice"], "remove_reviewers": ["alice"]}, "same reviewer in one call: alice"),
        ({"add_assignees": ["bob"], "remove_assignees": ["bob"]}, "same assignee in one call: bob"),
        ({"add_blocked_by": [5], "remove_blocked_by": [5]}, "same blocking merge request in one call: 5"),
        ({"approve": True}, "needs sha"),
        ({"approve": True, "sha": SHA, "rebase": True}, "Approve in a separate call"),
        ({"approve": False, "target_branch": "main"}, "Approve in a separate call"),
    ],
)
async def test_invalid_requests_are_rejected_before_any_write(gitlab_env, router, arguments, message):
    """Invalid requests raise a ToolError before any request is sent."""
    # Arrange
    serve_writes(router)

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool(**arguments)
    assert router.calls.call_count == 0


async def test_one_failed_step_does_not_stop_the_others(gitlab_env, router):
    """A refused step is reported under its name, and the other steps still run."""
    # Arrange
    routes = serve_writes(router)
    routes["fields"].mock(return_value=httpx.Response(409, json={"message": "SHA does not match"}))

    # Act
    data = await call_tool(title="New", draft=True)

    # Assert
    assert data["applied"] == ["draft"]
    assert data["failed"]["fields"].startswith("GitLab returned HTTP 409")
    assert "SHA does not match" in data["failed"]["fields"]


async def test_all_steps_failing_raises_one_error_with_every_reason(gitlab_env, router):
    """When no step succeeds, the error lists every reason."""
    # Arrange
    routes = serve_writes(
        router, replies={"mergeRequestSetDraft": {"data": {"mergeRequestSetDraft": {"errors": ["Locked"]}}}}
    )
    routes["fields"].mock(return_value=httpx.Response(409, json={"message": "Conflict"}))

    # Act / Assert
    with pytest.raises(ToolError, match=r"fields: GitLab returned HTTP 409.*; draft: GitLab GraphQL error: Locked"):
        await call_tool(title="New", draft=True)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"data": {"mergeRequestSetDraft": {"errors": ["Draft is locked"]}}}, "GitLab GraphQL error: Draft is locked"),
        ({"errors": [{"message": "Not allowed"}], "data": None}, "GitLab GraphQL error: Not allowed"),
        ({"data": {"mergeRequestSetDraft": None}}, "GitLab GraphQL error: mergeRequestSetDraft returned no payload"),
    ],
    ids=["payload-errors", "top-level-errors", "null-payload"],
)
async def test_graphql_errors_become_the_step_reason(gitlab_env, router, body, reason):
    """GraphQL payload errors, top-level errors, and null payloads all become the step reason."""
    # Arrange
    serve_writes(router, replies={"mergeRequestSetDraft": body})

    # Act
    data = await call_tool(title="New", draft=True)

    # Assert
    assert data["failed"] == {"draft": reason}
    assert data["applied"] == ["fields"]


async def test_connection_error_on_a_write_goes_to_unknown(gitlab_env, router):
    """A connection error on a write goes to unknown, so the write is not retried."""
    """The write may have reached GitLab, so the step is unknown and not failed."""
    # Arrange
    routes = serve_writes(router)
    routes["fields"].mock(side_effect=httpx.ConnectError("boom"))

    # Act
    data = await call_tool(title="New", draft=True)

    # Assert
    assert data["unknown"] == ["fields"]
    assert data["applied"] == ["draft"]
    assert "failed" not in data


async def test_final_state_comes_from_a_reread(gitlab_env, router):
    """The final state comes from a re-read after the writes."""
    # Arrange
    serve_writes(
        router, replies={"mergeRequest(": mr_body(state="closed", draft=True, detailedMergeStatus="CI_MUST_PASS")}
    )

    # Act
    data = await call_tool(state="close", draft=True)

    # Assert
    assert data == {
        "applied": ["fields", "draft"],
        "state": "closed",
        "draft": True,
        "merge_status": "ci_must_pass",
        "head_sha": SHA,
    }


async def test_failed_reread_keeps_the_outcomes_and_adds_a_warning(gitlab_env, router):
    """A failed re-read keeps the outcomes and adds a warning."""
    # Arrange
    serve_writes(router, replies={"mergeRequest(": httpx.Response(500, json={"message": "Boom"})})

    # Act
    data = await call_tool(title="New")

    # Assert
    assert data["applied"] == ["fields"]
    assert "state" not in data
    assert "head_sha" not in data
    assert data["warning"].startswith("Could not read the merge request back")
