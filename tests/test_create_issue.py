"""Behavioral tests for creating project issues through the in-memory MCP client ✨."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from gitlab_mcp.tools import issues as _issues  # noqa: F401 - importing the module registers its tool.
from mcp_helpers import call_tool as call_mcp_tool

API = "https://gitlab.example.com/api/v4"
PROJECT = "group/project"
PROJECT_API = f"{API}/projects/group%2Fproject"
CREATE = f"{PROJECT_API}/issues"
CREATED_ID = 4200
CREATED_IID = 42
CREATED_URL = f"https://gitlab.example.com/{PROJECT}/-/issues/{CREATED_IID}"


@pytest.fixture
def router(gitlab_env):
    """Give each test an isolated mock router."""
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


async def call_tool(**arguments: Any) -> dict[str, Any]:
    """Call create_issue with the default project and return structured data."""
    return await call_mcp_tool("create_issue", {"project": PROJECT, **arguments})


def mock_project(router: respx.MockRouter, response: httpx.Response | None = None) -> respx.Route:
    """Mock the project settings preflight response."""
    return router.get(PROJECT_API).mock(
        return_value=response or httpx.Response(200, json={"issues_enabled": True, "issues_access_level": "enabled"}),
    )


def create_response(**overrides: Any) -> httpx.Response:
    """Return a successful issue creation response."""
    body = {"id": CREATED_ID, "iid": CREATED_IID, "web_url": CREATED_URL} | overrides
    return httpx.Response(201, json=body)


async def test_validate_only_checks_project_settings_without_creating(gitlab_env, router):
    """Validation checks local data and server issue settings but never sends a write."""
    # Arrange
    project = mock_project(router)
    create = router.post(CREATE)

    # Act
    data = await call_tool(title="Plan a change", description="Details", validate_only=True)

    # Assert
    assert data == {"status": "validated", "title": "Plan a change"}
    assert project.call_count == 1
    assert create.call_count == 0
    assert [call.request.method for call in router.calls] == ["GET"]


async def test_create_issue_posts_only_requested_fields(gitlab_env, router):
    """Issue creation sends the ordinary REST payload and returns verified identifiers."""
    # Arrange
    mock_project(router)
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(title="Add a setting", description="Keep this private")

    # Assert
    assert json.loads(create.calls[0].request.content) == {
        "title": "Add a setting",
        "issue_type": "issue",
        "description": "Keep this private",
    }
    assert data == {
        "status": "created",
        "title": "Add a setting",
        "id": CREATED_ID,
        "iid": CREATED_IID,
        "web_url": CREATED_URL,
    }
    assert [call.request.method for call in router.calls] == ["GET", "POST"]


async def test_omitted_description_is_not_sent(gitlab_env, router):
    """An omitted description stays omitted from the REST payload."""
    # Arrange
    mock_project(router)
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(title="No description")

    # Assert
    assert json.loads(create.calls[0].request.content) == {"title": "No description", "issue_type": "issue"}
    assert data["status"] == "created"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project", ""),
        ("project", "group//project"),
        ("project", "group/../project"),
        ("project", "group/project%2Fother"),
        ("project", "group\\project"),
        ("project", "group/project?search"),
        ("project", "group/project#issue"),
        ("project", "group/\x00project"),
        ("title", " \t"),
        ("title", "line one\nline two"),
        ("title", "line\u2028break"),
        ("title", "x" * 256),
        ("description", "🙂" * 25001),
    ],
)
async def test_invalid_input_is_rejected_before_any_http_request(gitlab_env, router, field, value):
    """Invalid project paths, titles, and UTF-8 byte sizes make no GitLab requests."""
    # Arrange
    arguments = {"title": "A valid title", field: value}

    # Act and assert
    with pytest.raises(ToolError):
        await call_tool(**arguments)
    assert not router.calls


@pytest.mark.parametrize(
    ("project_settings", "expected_message"),
    [
        ({"issues_enabled": False}, "disabled"),
        ({"issues_enabled": True, "issues_access_level": "disabled"}, "disabled"),
    ],
)
async def test_disabled_project_issues_stop_before_creation(gitlab_env, router, project_settings, expected_message):
    """Disabled issue settings prevent POST requests in both modes."""
    # Arrange
    project = mock_project(router, httpx.Response(200, json=project_settings))
    create = router.post(CREATE)

    # Act and assert
    with pytest.raises(ToolError, match=expected_message):
        await call_tool(title="Blocked", validate_only=True)
    assert project.call_count == 1
    assert create.call_count == 0


@pytest.mark.parametrize("status", [403, 404])
async def test_project_preflight_errors_propagate_without_creation(gitlab_env, router, status):
    """Permission and not-found errors from the preflight are not hidden."""
    # Arrange
    project = mock_project(router, httpx.Response(status, json={"message": "Project lookup failed"}))
    create = router.post(CREATE)

    # Act and assert
    with pytest.raises(ToolError, match=f"HTTP {status}"):
        await call_tool(title="Should not be created")
    assert project.call_count == 1
    assert create.call_count == 0


@pytest.mark.parametrize("status", [408, 500, 503])
async def test_uncertain_http_write_returns_unknown_once(gitlab_env, router, status):
    """Timeout and server errors are reported as unknown and are never retried."""
    # Arrange
    mock_project(router)
    create = router.post(CREATE).mock(return_value=httpx.Response(status, json={"message": "private response detail"}))

    # Act
    data = await call_tool(title="Potential issue")

    # Assert
    assert data["status"] == "unknown"
    assert "Check GitLab before retrying" in data["message"]
    assert "private response detail" not in json.dumps(data)
    assert create.call_count == 1


async def test_connection_error_returns_unknown_without_retry(gitlab_env, router):
    """A connection error during the POST does not claim failure or retry."""
    # Arrange
    mock_project(router)
    create = router.post(CREATE).mock(side_effect=httpx.ConnectError("secret network detail"))

    # Act
    data = await call_tool(title="Potential issue")

    # Assert
    assert data["status"] == "unknown"
    assert "secret network detail" not in json.dumps(data)
    assert create.call_count == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201, content=b"not JSON"),
        httpx.Response(201, json=[]),
        create_response(id=0),
        create_response(iid="42"),
        create_response(web_url=""),
    ],
)
async def test_malformed_success_is_unknown_without_retry(gitlab_env, router, response):
    """Invalid JSON or incomplete success data is not treated as a created issue."""
    # Arrange
    mock_project(router)
    create = router.post(CREATE).mock(return_value=response)

    # Act
    data = await call_tool(title="Potential issue")

    # Assert
    assert data["status"] == "unknown"
    assert create.call_count == 1


async def test_rejected_write_does_not_echo_issue_content(gitlab_env, router):
    """A definite 4xx rejection has a friendly error without title or description details."""
    # Arrange
    title = "SECRET TITLE"
    description = "SECRET DESCRIPTION"
    mock_project(router)
    create = router.post(CREATE).mock(
        return_value=httpx.Response(422, json={"message": f"{title}: {description}"}),
    )

    # Act and assert
    with pytest.raises(ToolError) as error:
        await call_tool(title=title, description=description)
    assert "HTTP 422" in str(error.value)
    assert title not in str(error.value)
    assert description not in str(error.value)
    assert create.call_count == 1
