"""Behavioral tests for creating merge requests through an in-memory MCP client ✨."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import MAX_CREATE_ASSIGNEES, mcp
from mcp_helpers import call_tool as call_mcp_tool

API = "https://gitlab.example.com/api/v4"
GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
PROJECT_API = f"{API}/projects/group%2Fproject"
PROJECT_LABELS = f"{PROJECT_API}/labels"
CREATE = f"{PROJECT_API}/merge_requests"
CREATED_IID = 42
BLOCKER_ONE = 5
BLOCKER_TWO = 6
BLOCKER_GLOBAL_ID = 5005
DEPENDENCY_COUNT = 2
CREATED_MR = f"{PROJECT_API}/merge_requests/{CREATED_IID}"
CREATED_BLOCKS = f"{CREATED_MR}/blocks"
CREATED_URL = f"https://gitlab.example.com/group/project/-/merge_requests/{CREATED_IID}"


@pytest.fixture
def router(gitlab_env):
    """Give each test an isolated mock router."""
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


async def call_tool(**arguments: Any) -> dict[str, Any]:
    """Call create_merge_request with the default project and return structured data."""
    return await call_mcp_tool("create_merge_request", {"project": PROJECT, **arguments})


def create_response(**overrides: Any) -> httpx.Response:
    """Return a successful merge request creation response."""
    body = {"iid": CREATED_IID, "web_url": CREATED_URL, "id": 4200} | overrides
    return httpx.Response(201, json=body)


def mock_assignee_lookup(router: respx.MockRouter, nodes: list[dict[str, Any]]) -> respx.Route:
    """Return the requested users from the create-assignee GraphQL lookup."""
    return router.post(GRAPHQL).mock(
        return_value=httpx.Response(200, json={"data": {"users": {"nodes": nodes}}}),
    )


def mock_blocker(router: respx.MockRouter, iid: int, *, global_id: int | None = None) -> respx.Route:
    """Mock one project-scoped blocker lookup with its numeric global ID."""
    return router.get(f"{PROJECT_API}/merge_requests/{iid}").mock(
        return_value=httpx.Response(200, json={"iid": iid, "id": global_id or iid * 100}),
    )


async def test_create_with_fields_labels_and_blockers(gitlab_env, router):
    """Create posts the requested fields, then adds each deduplicated dependency by global ID."""
    # Arrange
    labels = router.get(PROJECT_LABELS).mock(
        return_value=httpx.Response(200, json=[{"name": "bug"}, {"name": "Group::Urgent"}]),
    )
    blocker = mock_blocker(router, BLOCKER_ONE, global_id=BLOCKER_GLOBAL_ID)
    create = router.post(CREATE).mock(return_value=create_response())
    dependencies = router.post(CREATED_BLOCKS).mock(return_value=httpx.Response(201, json={}))

    # Act
    data = await call_tool(
        source_branch="feature/login",
        target_branch="main",
        title="Fix login",
        description="Details",
        labels=["bug", "Group::Urgent"],
        remove_source_branch=False,
        blocked_by=[BLOCKER_ONE, BLOCKER_ONE],
    )

    # Assert
    assert json.loads(create.calls[0].request.content) == {
        "source_branch": "feature/login",
        "target_branch": "main",
        "title": "Fix login",
        "description": "Details",
        "labels": "bug,Group::Urgent",
        "remove_source_branch": False,
    }
    assert dict(labels.calls[0].request.url.params) == {
        "include_ancestor_groups": "true",
        "per_page": "100",
        "page": "1",
    }
    assert blocker.call_count == 1
    assert dependencies.call_count == 1
    assert dependencies.calls[0].request.url.params["blocking_merge_request_id"] == str(BLOCKER_GLOBAL_ID)
    assert [call.request.method for call in router.calls] == ["GET", "GET", "POST", "POST"]
    assert data == {
        "iid": CREATED_IID,
        "url": CREATED_URL,
        "applied": ["create", "blocked_by:5"],
    }


@pytest.mark.parametrize("arguments", [{}, {"assignees": []}])
async def test_omitted_or_empty_assignees_skip_lookup_and_body_field(gitlab_env, router, arguments):
    """Omitted and empty assignees do not add lookup or assignment requests."""
    # Arrange
    lookup = router.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"users": {"nodes": []}}}))
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(
        source_branch="feature", target_branch="main", title="New MR", description="Details", **arguments
    )

    # Assert
    assert lookup.call_count == 0
    assert "assignee_ids" not in json.loads(create.calls[0].request.content)
    assert data["applied"] == ["create"]


async def test_known_assignee_is_resolved_before_creation_and_reported_applied(gitlab_env, router):
    """A known username is resolved by GraphQL and its numeric ID is included in creation."""
    # Arrange
    lookup = mock_assignee_lookup(router, [{"id": "gid://gitlab/User/19", "username": "alice"}])
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(
        source_branch="feature", target_branch="main", title="New MR", description="Details", assignees=["alice"]
    )

    # Assert
    assert json.loads(lookup.calls[0].request.content)["variables"] == {"usernames": ["alice"]}
    assert json.loads(create.calls[0].request.content)["assignee_ids"] == [19]
    assert data["applied"] == ["create", "assignees:alice"]


async def test_unknown_assignee_is_omitted_and_reported(gitlab_env, router):
    """A missing GraphQL user is reported without blocking merge request creation."""
    # Arrange
    mock_assignee_lookup(router, [])
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(
        source_branch="feature", target_branch="main", title="New MR", description="Details", assignees=["missing"]
    )

    # Assert
    assert "assignee_ids" not in json.loads(create.calls[0].request.content)
    assert data["failed"] == {"assignees:missing": "unknown user"}
    assert data["applied"] == ["create"]


async def test_mixed_known_and_unknown_assignees(gitlab_env, router):
    """Known assignees are included while unknown users are reported independently."""
    # Arrange
    mock_assignee_lookup(router, [{"id": "gid://gitlab/User/19", "username": "alice"}])
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(
        source_branch="feature",
        target_branch="main",
        title="New MR",
        description="Details",
        assignees=["alice", "missing"],
    )

    # Assert
    assert json.loads(create.calls[0].request.content)["assignee_ids"] == [19]
    assert data["failed"] == {"assignees:missing": "unknown user"}
    assert data["applied"] == ["create", "assignees:alice"]


async def test_assignee_lookup_normalizes_case_and_deduplicates(gitlab_env, router):
    """Case variants are queried and assigned once, preserving the first requested spelling in results."""
    # Arrange
    lookup = mock_assignee_lookup(router, [{"id": "gid://gitlab/User/19", "username": "Alice"}])
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(
        source_branch="feature",
        target_branch="main",
        title="New MR",
        description="Details",
        assignees=["Alice", "alice", "ALICE"],
    )

    # Assert
    assert json.loads(lookup.calls[0].request.content)["variables"] == {"usernames": ["alice"]}
    assert json.loads(create.calls[0].request.content)["assignee_ids"] == [19]
    assert data["applied"] == ["create", "assignees:Alice"]


async def test_assignee_lookup_failure_reports_each_user_and_creation_continues(gitlab_env, router):
    """A failed user lookup is item-scoped and does not prevent creation."""
    # Arrange
    lookup = router.post(GRAPHQL).mock(return_value=httpx.Response(500, json={"message": "Boom"}))
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(
        source_branch="feature",
        target_branch="main",
        title="New MR",
        description="Details",
        assignees=["Alice", "bob", "alice"],
    )

    # Assert
    assert lookup.call_count == 1
    assert "assignee_ids" not in json.loads(create.calls[0].request.content)
    assert set(data["failed"]) == {"assignees:Alice", "assignees:bob"}
    assert all("HTTP 500" in message for message in data["failed"].values())
    assert data["applied"] == ["create"]


@pytest.mark.parametrize(
    "global_id",
    ["gid://gitlab/Group/19", "gid://gitlab/User/0", "not-a-gid", f"gid://gitlab/User/{'9' * 5000}"],
)
async def test_invalid_assignee_global_id_is_reported_and_omitted(gitlab_env, router, global_id):
    """Only positive numeric GitLab User global IDs can be included in creation."""
    # Arrange
    mock_assignee_lookup(router, [{"id": global_id, "username": "alice"}])
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(
        source_branch="feature", target_branch="main", title="New MR", description="Details", assignees=["alice"]
    )

    # Assert
    assert "assignee_ids" not in json.loads(create.calls[0].request.content)
    assert data["failed"] == {"assignees:alice": "could not verify user: invalid GitLab user ID"}
    assert data["applied"] == ["create"]


async def test_failed_creation_does_not_report_assignees_applied(gitlab_env, router):
    """A definite creation rejection does not claim that pre-resolved assignees were applied."""
    # Arrange
    mock_assignee_lookup(router, [{"id": "gid://gitlab/User/19", "username": "alice"}])
    create = router.post(CREATE).mock(return_value=httpx.Response(400, json={"message": "Bad request"}))

    # Act
    data = await call_tool(
        source_branch="feature", target_branch="main", title="New MR", description="Details", assignees=["alice"]
    )

    # Assert
    assert create.call_count == 1
    assert data["failed"]["create"]
    assert not any(key.startswith("assignees:") for key in data.get("applied", []))


async def test_unknown_creation_does_not_report_assignees_applied(gitlab_env, router):
    """An uncertain creation does not claim any assignee assignment was applied."""
    # Arrange
    mock_assignee_lookup(router, [{"id": "gid://gitlab/User/19", "username": "alice"}])
    create = router.post(CREATE).mock(side_effect=httpx.ConnectError("connection reset"))

    # Act
    data = await call_tool(
        source_branch="feature", target_branch="main", title="New MR", description="Details", assignees=["alice"]
    )

    # Assert
    assert create.call_count == 1
    assert data["unknown"] == ["create"]
    assert not any(key.startswith("assignees:") for key in data.get("applied", []))


async def test_assignees_schema_allows_up_to_100_names(gitlab_env):
    """The tool schema exposes the optional assignees list with its 100-item limit."""
    # Arrange
    async with Client(mcp) as client:
        tools = await client.list_tools()

    # Act
    tool = next(tool for tool in tools if tool.name == "create_merge_request")

    # Assert
    assignees = tool.input_schema["properties"]["assignees"]
    array_schema = next(option for option in assignees["anyOf"] if option.get("type") == "array")
    assert array_schema["maxItems"] == MAX_CREATE_ASSIGNEES


async def test_description_file_is_read_as_utf8_and_empty_content_is_valid(gitlab_env, router, tmp_path):
    """A description file supplies the body, and an empty file is still a valid source."""
    # Arrange
    source = tmp_path / "description.md"
    source.write_bytes(b"")
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(
        source_branch="feature",
        target_branch="main",
        title="Update docs",
        description_file=str(source),
    )

    # Assert
    assert json.loads(create.calls[0].request.content) == {
        "source_branch": "feature",
        "target_branch": "main",
        "title": "Update docs",
        "description": "",
    }
    assert data["applied"] == ["create"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"description": None}, "Set exactly one of description or description_file"),
        ({"description": "inline", "description_file": "description.md"}, "Set exactly one"),
        ({"source_branch": " \t"}, "source_branch must not be blank"),
        ({"target_branch": " \t"}, "target_branch must not be blank"),
        ({"title": "  "}, "title must not be blank"),
    ],
)
async def test_invalid_create_arguments_fail_before_network(gitlab_env, router, arguments, message):
    """Required description sources and nonblank branch/title values are checked before requests."""
    # Arrange
    route = router.post(CREATE).mock(return_value=create_response())
    required = {
        "source_branch": "feature",
        "target_branch": "main",
        "title": "New MR",
        "description": "Details",
    }

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool(**(required | arguments))
    assert router.calls.call_count == 0
    assert route.call_count == 0


@pytest.mark.parametrize("kind", ["relative", "missing", "directory", "invalid_utf8", "oversized"])
async def test_invalid_description_file_fails_before_network(gitlab_env, router, tmp_path, kind):
    """Description files must be absolute regular UTF-8 files no larger than 100000 bytes."""
    # Arrange
    if kind == "relative":
        path = "description.md"
    elif kind == "missing":
        path = str(tmp_path / "missing.md")
    elif kind == "directory":
        path = str(tmp_path)
    else:
        source = tmp_path / "description.md"
        source.write_bytes(b"\xff" if kind == "invalid_utf8" else b"x" * 100001)
        path = str(source)
    create = router.post(CREATE).mock(return_value=create_response())

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(
            source_branch="feature",
            target_branch="main",
            title="New MR",
            description_file=path,
        )
    assert router.calls.call_count == 0
    assert create.call_count == 0


async def test_unknown_labels_are_skipped_but_creation_continues(gitlab_env, router):
    """Known labels are sent, unknown names are reported, and creation still succeeds."""
    # Arrange
    router.get(PROJECT_LABELS).mock(return_value=httpx.Response(200, json=[{"name": "bug"}]))
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(
        source_branch="feature",
        target_branch="main",
        title="New MR",
        description="Details",
        labels=["bug", "unknown"],
    )

    # Assert
    assert json.loads(create.calls[0].request.content)["labels"] == "bug"
    assert data["iid"] == CREATED_IID
    assert data["failed"] == {"labels:unknown": "unknown label"}
    assert data["applied"] == ["create"]


async def test_label_lookup_failure_skips_labels_but_creation_continues(gitlab_env, router):
    """A lookup failure leaves labels out of the body without blocking the merge request."""
    # Arrange
    router.get(PROJECT_LABELS).mock(return_value=httpx.Response(500, json={"message": "Boom"}))
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(
        source_branch="feature",
        target_branch="main",
        title="New MR",
        description="Details",
        labels=["bug", "urgent"],
    )

    # Assert
    assert "labels" not in json.loads(create.calls[0].request.content)
    assert data["applied"] == ["create"]
    assert "HTTP 500" in data["failed"]["labels:bug"]
    assert "HTTP 500" in data["failed"]["labels:urgent"]


async def test_missing_blocker_prevents_creation(gitlab_env, router):
    """Every blocker is resolved before creation, and a missing one stops all writes."""
    # Arrange
    first = mock_blocker(router, BLOCKER_ONE)
    missing = router.get(f"{PROJECT_API}/merge_requests/{BLOCKER_TWO}").mock(
        return_value=httpx.Response(404, json={"message": "Not found"}),
    )
    create = router.post(CREATE).mock(return_value=create_response())
    router.post(CREATED_BLOCKS).mock(return_value=httpx.Response(201, json={}))

    # Act
    data = await call_tool(
        source_branch="feature",
        target_branch="main",
        title="New MR",
        description="Details",
        blocked_by=[BLOCKER_ONE, BLOCKER_TWO],
    )

    # Assert
    assert first.call_count == missing.call_count == 1
    assert create.call_count == 0
    assert data["failed"][f"blocked_by:{BLOCKER_TWO}"] == "not found in this project or inaccessible"
    assert "iid" not in data
    assert not data.get("applied")


@pytest.mark.parametrize("blocked_by", [[0], [-1], list(range(1, 22))])
async def test_invalid_blockers_fail_before_network(gitlab_env, router, blocked_by):
    """Blocker IIDs must be positive and the input list is capped at 20."""
    # Arrange
    create = router.post(CREATE).mock(return_value=create_response())

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(
            source_branch="feature",
            target_branch="main",
            title="New MR",
            description="Details",
            blocked_by=blocked_by,
        )
    assert router.calls.call_count == 0
    assert create.call_count == 0


async def test_dependency_failure_keeps_created_merge_request(gitlab_env, router):
    """A definite dependency rejection does not hide the successfully created merge request."""
    # Arrange
    mock_blocker(router, BLOCKER_ONE)
    mock_blocker(router, BLOCKER_TWO)
    create = router.post(CREATE).mock(return_value=create_response())
    dependencies = router.post(CREATED_BLOCKS).mock(
        side_effect=[httpx.Response(201, json={}), httpx.Response(409, json={"message": "Conflict"})],
    )

    # Act
    data = await call_tool(
        source_branch="feature",
        target_branch="main",
        title="New MR",
        description="Details",
        blocked_by=[BLOCKER_ONE, BLOCKER_TWO],
    )

    # Assert
    assert create.call_count == 1
    assert dependencies.call_count == DEPENDENCY_COUNT
    assert data["iid"] == CREATED_IID
    assert data["url"] == CREATED_URL
    assert data["applied"] == ["create", f"blocked_by:{BLOCKER_ONE}"]
    assert "HTTP 409" in data["failed"][f"blocked_by:{BLOCKER_TWO}"]
    assert "not confirmed" in data["failed"][f"blocked_by:{BLOCKER_TWO}"]


async def test_uncertain_creation_does_not_post_dependencies(gitlab_env, router):
    """A connection failure during creation is unknown, so no dependency write follows."""
    # Arrange
    mock_blocker(router, BLOCKER_ONE)
    create = router.post(CREATE).mock(side_effect=httpx.ConnectError("connection reset"))
    dependencies = router.post(CREATED_BLOCKS).mock(return_value=httpx.Response(201, json={}))

    # Act
    data = await call_tool(
        source_branch="feature",
        target_branch="main",
        title="New MR",
        description="Details",
        blocked_by=[BLOCKER_ONE],
    )

    # Assert
    assert create.call_count == 1
    assert dependencies.call_count == 0
    assert data["unknown"] == ["create"]
    assert "iid" not in data


@pytest.mark.parametrize("status", [502, 504])
async def test_server_error_during_creation_is_unknown_and_stops_dependencies(gitlab_env, router, status):
    """A proxy or server error can hide a successful create, so do not retry or post dependencies."""
    # Arrange
    mock_blocker(router, BLOCKER_ONE)
    create = router.post(CREATE).mock(return_value=httpx.Response(status, json={"message": "Proxy error"}))
    dependencies = router.post(CREATED_BLOCKS).mock(return_value=httpx.Response(201, json={}))

    # Act
    data = await call_tool(
        source_branch="feature",
        target_branch="main",
        title="New MR",
        description="Details",
        blocked_by=[BLOCKER_ONE],
    )

    # Assert
    assert create.call_count == 1
    assert dependencies.call_count == 0
    assert data["unknown"] == ["create"]
    assert "failed" not in data
    assert "iid" not in data


async def test_uncertain_dependency_stops_later_dependency_writes(gitlab_env, router):
    """An unknown dependency outcome stops later dependencies and retains the created MR details."""
    # Arrange
    mock_blocker(router, BLOCKER_ONE)
    mock_blocker(router, BLOCKER_TWO)
    router.post(CREATE).mock(return_value=create_response())
    dependencies = router.post(CREATED_BLOCKS).mock(side_effect=httpx.ConnectError("connection reset"))

    # Act
    data = await call_tool(
        source_branch="feature",
        target_branch="main",
        title="New MR",
        description="Details",
        blocked_by=[BLOCKER_ONE, BLOCKER_TWO],
    )

    # Assert
    assert dependencies.call_count == 1
    assert data["iid"] == CREATED_IID
    assert data["unknown"] == [f"blocked_by:{BLOCKER_ONE}"]
    assert data["failed"][f"blocked_by:{BLOCKER_TWO}"] == "not attempted after an unknown dependency outcome"


async def test_create_tool_is_a_non_idempotent_write(gitlab_env):
    """The tool annotations make clear that creation is a non-idempotent write."""
    # Arrange
    async with Client(mcp) as client:
        tools = await client.list_tools()

    # Act
    tool = next(tool for tool in tools if tool.name == "create_merge_request")

    # Assert
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is True
    assert "glab mr create" in tool.description
    assert "Do not retry blindly" in tool.description
