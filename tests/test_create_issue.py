"""Behavioral tests for creating project issues through the in-memory MCP client ✨."""

import json
import os
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.app import mcp
from gitlab_mcp.tools import issues as _issues  # noqa: F401 - importing the module registers its tool.
from mcp_helpers import call_tool as call_mcp_tool

API = "https://gitlab.example.com/api/v4"
PROJECT = "group/project"
PROJECT_API = f"{API}/projects/group%2Fproject"
CREATE = f"{PROJECT_API}/issues"
LABELS = f"{PROJECT_API}/labels"
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


async def test_empty_inline_description_is_sent(gitlab_env, router):
    """An explicit empty description is distinct from an omitted description."""
    # Arrange
    mock_project(router)
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(title="Empty description", description="")

    # Assert
    assert json.loads(create.calls[0].request.content)["description"] == ""
    assert data["status"] == "created"


@pytest.mark.parametrize(
    "description",
    ["", "Details: café 🙂\n", "x" * 100000, "🙂" * 25000],
    ids=["empty", "utf8", "ascii-byte-limit", "multibyte-byte-limit"],
)
async def test_description_file_is_sent_in_issue_payload(gitlab_env, router, tmp_path, description):
    """A server-local UTF-8 file supplies the exact issue description, including at the byte limit."""
    # Arrange
    source = tmp_path / "description.md"
    source.write_text(description, encoding="utf-8")
    mock_project(router)
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(title="File description", description_file=str(source))

    # Assert
    assert json.loads(create.calls[0].request.content) == {
        "title": "File description",
        "issue_type": "issue",
        "description": description,
    }
    assert data["status"] == "created"
    assert [call.request.method for call in router.calls] == ["GET", "POST"]


async def test_validate_only_accepts_description_file_without_creating(gitlab_env, router, tmp_path):
    """Validation accepts a local description file and never sends a write."""
    # Arrange
    source = tmp_path / "description.md"
    source.write_text("Details 🙂", encoding="utf-8")
    project = mock_project(router)
    create = router.post(CREATE)

    # Act
    data = await call_tool(title="File description", description_file=str(source), validate_only=True)

    # Assert
    assert data == {"status": "validated", "title": "File description"}
    assert project.call_count == 1
    assert create.call_count == 0
    assert [call.request.method for call in router.calls] == ["GET"]


@pytest.mark.parametrize("validate_only", [False, True])
async def test_description_file_and_labels_work_together(gitlab_env, router, tmp_path, validate_only):
    """A local file and known labels work together in creation and validation modes."""
    # Arrange
    description = "Details from a local file 🙂\n"
    source = tmp_path / "description.md"
    source.write_text(description, encoding="utf-8")
    mock_project(router)
    lookup = router.get(LABELS).mock(
        return_value=httpx.Response(200, json=[{"name": "bug"}, {"name": "group::shared"}])
    )
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(
        title="File and labels",
        description_file=str(source),
        labels=["group::shared", "bug"],
        validate_only=validate_only,
    )

    # Assert
    assert lookup.call_count == 1
    if validate_only:
        assert data == {"status": "validated", "title": "File and labels"}
        assert create.call_count == 0
        assert [call.request.method for call in router.calls] == ["GET", "GET"]
    else:
        assert data == {
            "status": "created",
            "title": "File and labels",
            "id": CREATED_ID,
            "iid": CREATED_IID,
            "web_url": CREATED_URL,
        }
        assert create.call_count == 1
        assert json.loads(create.calls[0].request.content) == {
            "title": "File and labels",
            "issue_type": "issue",
            "description": description,
            "labels": "group::shared,bug",
        }
        assert [call.request.method for call in router.calls] == ["GET", "GET", "POST"]


@pytest.mark.parametrize("validate_only", [False, True])
@pytest.mark.parametrize(
    "file_case",
    [
        ("relative", "absolute server-local path"),
        ("missing", "Could not read description_file"),
        ("directory", "regular file"),
        ("fifo", "regular file"),
        ("invalid_utf8", "not valid UTF-8"),
        ("oversized", "100000 UTF-8 bytes"),
        ("oversized_multibyte", "100000 UTF-8 bytes"),
    ],
)
async def test_invalid_description_file_fails_before_network(gitlab_env, router, tmp_path, file_case, validate_only):
    """Invalid local files are rejected before all GitLab requests in both modes."""
    # Arrange
    kind, message = file_case
    source = tmp_path / "description.md"
    if kind == "relative":
        path = "description.md"
    elif kind == "directory":
        path = str(tmp_path)
    else:
        path = str(source)
        if kind == "fifo":
            os.mkfifo(source)
        elif kind == "invalid_utf8":
            source.write_bytes(b"\xff")
        elif kind == "oversized":
            source.write_bytes(b"x" * 100001)
        elif kind == "oversized_multibyte":
            source.write_text("🙂" * 25000 + "x", encoding="utf-8")

    # Act and assert
    with pytest.raises(ToolError, match=message):
        await call_tool(title="File description", description_file=path, validate_only=validate_only)
    assert not router.calls


@pytest.mark.parametrize("description", ["", "Inline text"])
@pytest.mark.parametrize("validate_only", [False, True])
async def test_both_description_sources_are_rejected(gitlab_env, router, tmp_path, description, validate_only):
    """Both sources are rejected, even when the inline description is empty."""
    # Arrange
    source = tmp_path / "description.md"
    source.write_text("File text", encoding="utf-8")

    # Act and assert
    with pytest.raises(ToolError, match="description or description_file"):
        await call_tool(
            title="Two descriptions",
            description=description,
            description_file=str(source),
            validate_only=validate_only,
        )
    assert not router.calls


async def test_description_file_is_documented_in_tool_schema(gitlab_env):
    """The schema describes the optional and exclusive server-local file source."""
    # Arrange
    async with Client(mcp) as client:
        tools = await client.list_tools()

    # Act
    tool = next(tool for tool in tools if tool.name == "create_issue")
    schema = tool.input_schema
    description = schema["properties"]["description_file"]["description"]

    # Assert
    assert "description_file" not in schema["required"]
    assert "server-local" in description
    assert "regular" in description
    assert "100000" in description
    assert "description" in description


async def test_create_issue_checks_labels_and_preserves_requested_order(gitlab_env, router):
    """Only known project and ancestor labels are sent, with duplicates removed in requested order."""
    # Arrange
    project = "group/nested project"
    project_api = f"{API}/projects/group%2Fnested%20project"
    router.get(project_api).mock(return_value=httpx.Response(200, json={"issues_enabled": True}))
    labels = router.get(f"{project_api}/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "bug"}, {"name": "group::shared"}]),
    )
    create = router.post(f"{project_api}/issues").mock(return_value=create_response())

    # Act
    data = await call_tool(
        project=project,
        title="Labeled issue",
        labels=["group::shared", "missing", "bug", "group::shared", "missing", "bug"],
    )

    # Assert
    assert data == {
        "status": "created",
        "title": "Labeled issue",
        "id": CREATED_ID,
        "iid": CREATED_IID,
        "web_url": CREATED_URL,
        "failed": {"labels:missing": "unknown label"},
    }
    assert json.loads(create.calls[0].request.content) == {
        "title": "Labeled issue",
        "issue_type": "issue",
        "labels": "group::shared,bug",
    }
    assert dict(labels.calls[0].request.url.params) == {
        "include_ancestor_groups": "true",
        "per_page": "100",
        "page": "1",
    }
    assert labels.calls[0].request.url.raw_path.startswith(b"/api/v4/projects/group%2Fnested%20project/labels?")
    assert [call.request.method for call in router.calls] == ["GET", "GET", "POST"]


async def test_known_labels_accept_the_input_limit_without_failure_field(gitlab_env, router):
    """One hundred requested labels are accepted and repeated known labels are sent once."""
    # Arrange
    mock_project(router)
    router.get(LABELS).mock(return_value=httpx.Response(200, json=[{"name": "bug"}]))
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(title="At the limit", labels=["bug"] * 100)

    # Assert
    assert json.loads(create.calls[0].request.content)["labels"] == "bug"
    assert "failed" not in data


async def test_partial_label_lookup_failure_keeps_verified_labels(gitlab_env, router):
    """A later page failing does not discard labels already found on an earlier page."""
    # Arrange
    mock_project(router)
    lookup = router.get(LABELS).mock(
        side_effect=[
            httpx.Response(200, json=[{"name": "bug"}], headers={"X-Next-Page": "2"}),
            httpx.Response(403, json={"message": "Forbidden"}),
        ],
    )
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(title="Partial labels", labels=["missing", "bug"])

    # Assert
    assert data["status"] == "created"
    assert set(data["failed"]) == {"labels:missing"}
    assert "HTTP 403" in data["failed"]["labels:missing"]
    assert json.loads(create.calls[0].request.content)["labels"] == "bug"
    assert [int(call.request.url.params["page"]) for call in lookup.calls] == [1, 2]


@pytest.mark.parametrize("labels", [None, []])
async def test_no_labels_skip_lookup_and_result_fields(gitlab_env, router, labels):
    """Missing or empty labels add no lookup, POST field, or failure result."""
    # Arrange
    mock_project(router)
    lookup = router.get(LABELS)
    create = router.post(CREATE).mock(return_value=create_response())
    arguments = {} if labels is None else {"labels": labels}

    # Act
    data = await call_tool(title="No labels", **arguments)

    # Assert
    assert lookup.call_count == 0
    assert json.loads(create.calls[0].request.content) == {"title": "No labels", "issue_type": "issue"}
    assert "failed" not in data
    assert [call.request.method for call in router.calls] == ["GET", "POST"]


async def test_validate_only_checks_labels_without_posting(gitlab_env, router):
    """Validation checks requested labels and reports missing ones without attempting a write."""
    # Arrange
    mock_project(router)
    lookup = router.get(LABELS).mock(return_value=httpx.Response(200, json=[{"name": "bug"}]))
    create = router.post(CREATE)

    # Act
    data = await call_tool(title="Validate labels", labels=["bug", "missing"], validate_only=True)

    # Assert
    assert data == {"status": "validated", "title": "Validate labels", "failed": {"labels:missing": "unknown label"}}
    assert lookup.call_count == 1
    assert create.call_count == 0
    assert [call.request.method for call in router.calls] == ["GET", "GET"]


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(403, json={"message": "Forbidden"}), "HTTP 403"),
        (httpx.Response(200, json={"unexpected": "object"}), "invalid response"),
        (httpx.Response(200, content=b"not JSON"), "invalid response"),
        (httpx.Response(200, json=[], headers={"X-Next-Page": "invalid"}), "invalid response"),
    ],
)
async def test_failed_label_lookup_omits_labels_and_continues(gitlab_env, router, response, reason):
    """Failed or invalid label checks are reported while the issue is still created."""
    # Arrange
    mock_project(router)
    lookup = router.get(LABELS).mock(return_value=response)
    create = router.post(CREATE).mock(return_value=create_response())

    # Act
    data = await call_tool(title="Still create", labels=["bug", "bug", "group::shared"])

    # Assert
    assert data["status"] == "created"
    assert set(data["failed"]) == {"labels:bug", "labels:group::shared"}
    assert all(reason in failure for failure in data["failed"].values())
    assert json.loads(create.calls[0].request.content) == {"title": "Still create", "issue_type": "issue"}
    assert lookup.call_count == 1
    assert create.call_count == 1


async def test_too_many_labels_are_rejected_before_http(gitlab_env, router):
    """The 100-label input cap is enforced before any request, even for duplicates."""
    # Arrange
    labels = ["bug"] * 101

    # Act / Assert
    with pytest.raises(ToolError, match="at most 100"):
        await call_tool(title="Too many labels", labels=labels)
    assert not router.calls


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(408, json={"message": "timeout"}),
        httpx.Response(503, json={"message": "unavailable"}),
        httpx.Response(201, content=b"not JSON"),
        httpx.Response(201, json=[]),
        httpx.ConnectError("connection lost"),
    ],
)
async def test_unknown_creation_retains_label_failures(gitlab_env, router, response):
    """Uncertain write outcomes retain label diagnostics and never retry."""
    # Arrange
    mock_project(router)
    router.get(LABELS).mock(return_value=httpx.Response(200, json=[{"name": "bug"}]))
    create = router.post(CREATE).mock(side_effect=response if isinstance(response, Exception) else [response])

    # Act
    data = await call_tool(title="Potential issue", labels=["bug", "missing"])

    # Assert
    assert data["status"] == "unknown"
    assert data["failed"] == {"labels:missing": "unknown label"}
    assert json.loads(create.calls[0].request.content)["labels"] == "bug"
    assert create.call_count == 1


async def test_create_issue_schema_documents_labels_and_validation(gitlab_env):
    """The tool schema describes label limits, omitted failures, and validation limitations."""
    # Arrange / Act
    async with Client(mcp) as client:
        tools = await client.list_tools()
    tool = next(item for item in tools if item.name == "create_issue")

    # Assert
    label_limit = 100
    label_schema = tool.input_schema["properties"]["labels"]
    assert any(option.get("maxItems") == label_limit for option in label_schema["anyOf"])
    assert "ancestor" in label_schema["description"]
    assert "unknown" in tool.description
    assert "permission" in tool.description


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
