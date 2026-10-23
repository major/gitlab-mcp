"""Behavioral tests for exact-text description edits through an in-memory MCP client ✏️."""

import json
from typing import Any, Literal

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from gitlab_mcp.client import GitLabClient
from gitlab_mcp.config import Settings
from gitlab_mcp.description_edits import DIFF_CAP, edit_merge_request_description, edit_work_item_description

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 7
MR = "https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/7"
WORK_ITEM_ID = "gid://gitlab/WorkItem/123"
UPDATED = {"data": {"workItemUpdate": {"errors": [], "workItem": {"id": WORK_ITEM_ID}}}}
TEST_APP = FastMCP("description-edit-tests")
RESULT_BUDGET = 1000  # Characters. A big description must not inflate the result.


@TEST_APP.tool
async def edit_mr_description(project: str, iid: int, old_text: str, new_text: str) -> Any:
    """Expose the merge request edit helper through an in-memory test server."""
    client = GitLabClient(Settings.from_env())
    try:
        return await edit_merge_request_description(client, project, iid, old_text, new_text)
    finally:
        await client.aclose()


@TEST_APP.tool
async def edit_work_item(
    project: str,
    iid: int,
    old_text: str,
    new_text: str,
    scope: Literal["project", "group"] = "project",
) -> Any:
    """Expose the work-item edit helper through an in-memory test server."""
    client = GitLabClient(Settings.from_env())
    try:
        return await edit_work_item_description(client, project, iid, old_text, new_text, scope)
    finally:
        await client.aclose()


@pytest.fixture
def router(gitlab_env):
    """Give each test its own mock router, so no request reaches a real network."""
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


async def call_tool(name: str, **arguments: Any) -> dict[str, Any]:
    """Call one test tool through the MCP protocol and return its structured content."""
    async with Client(TEST_APP) as client:
        result = await client.call_tool(name, arguments)
    return result.structured_content


def operation(request: httpx.Request) -> str:
    """Return the GraphQL query text that a request carries."""
    return json.loads(request.content)["query"]


def serve_merge_request(
    router: respx.MockRouter,
    description: str | None,
    put: httpx.Response | Exception | None = None,
) -> respx.Route:
    """Serve one merge request and answer its description update with `put`. Returns the PUT route."""
    router.get(MR).mock(return_value=httpx.Response(200, json={"description": description}))
    return router.put(MR).mock(side_effect=[put if put is not None else httpx.Response(200, json={})])


def work_item_read(description: str | None) -> dict[str, Any]:
    """Build the edit-source GraphQL response for one project work item."""
    scope_namespace = {"__typename": "Project", "fullPath": PROJECT}
    item = {"id": WORK_ITEM_ID, "iid": str(IID), "description": description}
    namespace = {"__typename": "Namespace", "fullPath": PROJECT, "workItem": item}
    return {"data": {"scopeNamespace": scope_namespace, "namespace": namespace}}


def serve_work_item(
    router: respx.MockRouter,
    description: str | None,
    mutation: httpx.Response | Exception | None = None,
) -> respx.Route:
    """Serve one work item read, and answer its update mutation with `mutation`. Returns the GraphQL route."""
    reply = mutation if mutation is not None else httpx.Response(200, json=UPDATED)

    def handler(request: httpx.Request) -> httpx.Response:
        """Answer the mutation with `reply` and every other GraphQL query with the work item."""
        if "workItemUpdate" not in operation(request):
            return httpx.Response(200, json=work_item_read(description))
        if isinstance(reply, Exception):
            raise reply
        return reply

    return router.post(GRAPHQL).mock(side_effect=handler)


def mutation_variables(route: respx.Route) -> list[dict[str, Any]]:
    """Return the variables of each work item update mutation that the route received."""
    bodies = [json.loads(call.request.content) for call in route.calls]
    return [body["variables"] for body in bodies if "workItemUpdate" in body["query"]]


@pytest.mark.parametrize(
    ("description", "old_text", "new_text", "expected_description", "expected_lines"),
    [
        pytest.param(
            "Fix typo here",
            "typo",
            "typo fixed",
            "Fix typo fixed here",
            "@@ -1 +1 @@\n-Fix typo here\n+Fix typo fixed here",
            id="single-line",
        ),
        pytest.param(
            "Intro 🦊\nLine two café\nEnd",
            "café\nEnd",
            "naïve\nDone ✅",
            "Intro 🦊\nLine two naïve\nDone ✅",
            "@@ -2,2 +2,2 @@\n-Line two café\n-End\n+Line two naïve\n+Done ✅",
            id="multiline-unicode",
        ),
        pytest.param(
            "Intro\nTwo\nThree\nFour\nTARGET here\nSix",
            "TARGET",
            "CHANGED",
            "Intro\nTwo\nThree\nFour\nCHANGED here\nSix",
            "@@ -5 +5 @@\n-TARGET here\n+CHANGED here",
            id="match-after-preceding-lines",
        ),
        pytest.param(
            "Keep this.\nDrop this line.\nKeep too.",
            "Drop this line.\n",
            "",
            "Keep this.\nKeep too.",
            "@@ -2 +1,0 @@\n-Drop this line.",
            id="delete-whole-line",
        ),
        pytest.param(
            "Keep this.\nDrop this line.\nKeep too.",
            "Drop this line.",
            "",
            "Keep this.\n\nKeep too.",
            "@@ -2 +2 @@\n-Drop this line.\n+",
            id="delete-text-leaves-blank-line",
        ),
        pytest.param(
            "Line one\nLine two",
            "\n",
            "",
            "Line oneLine two",
            "@@ -1,2 +1 @@\n-Line one\n-Line two\n+Line oneLine two",
            id="newline-removed-joins-lines",
        ),
        pytest.param(
            "Line one\nLine two",
            "Line one",
            "Line one\n",
            "Line one\n\nLine two",
            "@@ -1,0 +2 @@\n+",
            id="newline-added-makes-blank-line",
        ),
        pytest.param(
            "A\nB",
            "A",
            "A\nX",
            "A\nX\nB",
            "@@ -1,0 +2 @@\n+X",
            id="insert-new-line",
        ),
    ],
)
async def test_mr_edit_writes_full_description_and_returns_changed_lines(  # noqa: PLR0913, PLR0917
    router, description, old_text, new_text, expected_description, expected_lines
):
    """A unique match is replaced, the full description is written back, and only changed lines are returned."""
    # Arrange
    put = serve_merge_request(router, description)

    # Act
    result = await call_tool("edit_mr_description", project=PROJECT, iid=IID, old_text=old_text, new_text=new_text)

    # Assert
    assert result["status"] == "applied"
    assert result["changed_lines"] == expected_lines
    assert result["truncated"] is False
    assert json.loads(put.calls.last.request.content) == {"description": expected_description}


@pytest.mark.parametrize(
    ("description", "old_text", "new_text", "message"),
    [
        pytest.param("Some text", "missing", "x", "not found", id="no-match"),
        pytest.param("ab ab", "ab", "cd", "more than once", id="two-matches"),
        pytest.param("aaa", "aa", "b", "more than once", id="overlapping-matches"),
    ],
)
async def test_mr_edit_with_bad_match_raises_and_writes_nothing(router, description, old_text, new_text, message):
    """Zero or several matches raise a ToolError before any write, and the error does not echo the description."""
    # Arrange
    put = serve_merge_request(router, description)

    # Act / Assert
    with pytest.raises(ToolError, match=message) as error:
        await call_tool("edit_mr_description", project=PROJECT, iid=IID, old_text=old_text, new_text=new_text)
    assert description not in str(error.value)
    assert put.call_count == 0


@pytest.mark.parametrize(
    ("description", "old_text", "new_text", "message"),
    [
        pytest.param("Some text", "", "x", "must not be empty", id="empty-old-text"),
        pytest.param("same text", "same", "same", "identical", id="no-op"),
    ],
)
async def test_invalid_arguments_fail_before_any_request(router, description, old_text, new_text, message):
    """Empty and no-op edits are rejected without sending any request to GitLab."""
    # Arrange
    serve_merge_request(router, description)

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("edit_mr_description", project=PROJECT, iid=IID, old_text=old_text, new_text=new_text)
    assert len(router.calls) == 0


async def test_null_mr_description_is_treated_as_empty(router):
    """A merge request with a null description behaves like an empty one, so any non-empty match fails cleanly."""
    # Arrange
    put = serve_merge_request(router, None)

    # Act / Assert
    with pytest.raises(ToolError, match="not found"):
        await call_tool("edit_mr_description", project=PROJECT, iid=IID, old_text="anything", new_text="x")
    assert put.call_count == 0


async def test_long_changed_lines_are_capped_and_flagged(router):
    """A replacement whose diff exceeds the cap returns exactly the cap and sets `truncated`."""
    # Arrange
    old_text = "A" * 5000
    serve_merge_request(router, f"start {old_text} end")

    # Act
    result = await call_tool("edit_mr_description", project=PROJECT, iid=IID, old_text=old_text, new_text="B" * 5000)

    # Assert
    assert len(result["changed_lines"]) == DIFF_CAP
    assert result["changed_lines"].startswith("@@ -1 +1 @@\n-start AAA")
    assert result["truncated"] is True


async def test_large_description_is_written_whole_and_result_stays_small(router):
    """The write keeps every character of a big description, but the result only carries the changed line."""
    # Arrange
    description = "filler\n" * 3000 + "needle here\n" + "tail\n" * 3000
    put = serve_merge_request(router, description)

    # Act
    result = await call_tool(
        "edit_mr_description", project=PROJECT, iid=IID, old_text="needle here", new_text="pinned here"
    )

    # Assert
    assert json.loads(put.calls.last.request.content) == {"description": description.replace("needle", "pinned", 1)}
    assert result["changed_lines"] == "@@ -3001 +3001 @@\n-needle here\n+pinned here"
    assert len(json.dumps(result)) < RESULT_BUDGET


@pytest.mark.parametrize(
    ("put_reply", "status", "warning"),
    [
        pytest.param(httpx.ConnectError("connection reset"), "unknown", "connection reset", id="network-error"),
        pytest.param(httpx.Response(502, text="<html>Bad Gateway</html>"), "unknown", "HTTP 502", id="gateway-5xx"),
        pytest.param(
            httpx.Response(422, json={"message": {"description": ["is too long"]}}),
            "failed",
            "description: is too long",
            id="api-refusal",
        ),
        pytest.param(httpx.Response(403, json={"message": "403 Forbidden"}), "failed", "403", id="permission"),
    ],
)
async def test_mr_write_outcome_is_applied_failed_or_unknown_and_never_retried(router, put_reply, status, warning):
    """A network error or 5xx on the write is `unknown`, a 4xx refusal is `failed`, and the write is sent only once."""
    # Arrange
    put = serve_merge_request(router, "Plan: ship", put=put_reply)

    # Act
    result = await call_tool("edit_mr_description", project=PROJECT, iid=IID, old_text="ship", new_text="launch")

    # Assert
    assert result["status"] == status
    assert warning in result["warning"]
    assert "changed_lines" not in result
    assert result["truncated"] is False
    assert put.call_count == 1


async def test_mr_read_error_raises_and_writes_nothing(router):
    """A failed read is a normal ToolError, and no write is sent."""
    # Arrange
    router.get(MR).mock(return_value=httpx.Response(404, json={"message": "404 Not found"}))
    put = router.put(MR).mock(return_value=httpx.Response(200, json={}))

    # Act / Assert
    with pytest.raises(ToolError, match="HTTP 404"):
        await call_tool("edit_mr_description", project=PROJECT, iid=IID, old_text="a", new_text="b")
    assert put.call_count == 0


async def test_work_item_edit_writes_exact_global_id_and_full_description(router):
    """The work-item write sends the global ID and the whole new description in one mutation."""
    # Arrange
    route = serve_work_item(router, "Plan 🦊\nKeep café\nold line")

    # Act
    result = await call_tool(
        "edit_work_item",
        project=PROJECT,
        iid=IID,
        old_text="old line",
        new_text="new line ✅",
    )

    # Assert
    assert result["status"] == "applied"
    assert result["changed_lines"] == "@@ -3 +3 @@\n-old line\n+new line ✅"
    assert mutation_variables(route) == [
        {"input": {"id": WORK_ITEM_ID, "descriptionWidget": {"description": "Plan 🦊\nKeep café\nnew line ✅"}}}
    ]


@pytest.mark.parametrize(
    ("mutation", "status", "warning"),
    [
        pytest.param(
            httpx.Response(
                200, json={"data": {"workItemUpdate": {"errors": ["Description is locked"], "workItem": None}}}
            ),
            "failed",
            "Description is locked",
            id="payload-errors",
        ),
        pytest.param(
            httpx.Response(200, json={"errors": [{"message": "Denied"}], "data": {"workItemUpdate": None}}),
            "failed",
            "Denied",
            id="top-level-graphql-error",
        ),
        pytest.param(
            httpx.Response(200, json={"data": {"workItemUpdate": None}}),
            "failed",
            "returned no payload",
            id="null-payload",
        ),
        pytest.param(httpx.ConnectError("reset"), "unknown", "reset", id="network-error"),
        pytest.param(httpx.Response(504, text="timeout"), "unknown", "HTTP 504", id="gateway-timeout"),
    ],
)
async def test_work_item_write_outcome_is_failed_or_unknown_and_never_retried(router, mutation, status, warning):
    """GitLab refusals are `failed`. Lost connections and gateway timeouts are `unknown`. Never retried."""
    # Arrange
    route = serve_work_item(router, "old line", mutation=mutation)

    # Act
    result = await call_tool("edit_work_item", project=PROJECT, iid=IID, old_text="old", new_text="new")

    # Assert
    assert result["status"] == status
    assert warning in result["warning"]
    assert len(mutation_variables(route)) == 1


@pytest.mark.parametrize(
    ("description", "old_text", "message"),
    [
        pytest.param("old line", "missing", "not found", id="no-match"),
        pytest.param("aaa", "aa", "more than once", id="overlapping-matches"),
    ],
)
async def test_work_item_bad_match_writes_nothing(router, description, old_text, message):
    """A bad match raises before the mutation, so GitLab never receives a write."""
    # Arrange
    route = serve_work_item(router, description)

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool("edit_work_item", project=PROJECT, iid=IID, old_text=old_text, new_text="x")
    assert mutation_variables(route) == []


async def test_work_item_not_found_raises_and_writes_nothing(router):
    """A missing work item is a read error, so no mutation is sent."""
    # Arrange
    router.post(GRAPHQL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "scopeNamespace": {"__typename": "Project", "fullPath": PROJECT},
                    "namespace": {"__typename": "Namespace", "fullPath": PROJECT, "workItem": None},
                }
            },
        )
    )

    # Act / Assert
    with pytest.raises(ToolError, match="Work item #7 not found"):
        await call_tool("edit_work_item", project=PROJECT, iid=IID, old_text="a", new_text="b")
