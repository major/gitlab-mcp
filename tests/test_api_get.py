"""Behavioral tests for the bounded GitLab API read tool 🔒."""

import base64
import importlib
from typing import Any

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from mcp_helpers import call_tool as call_mcp_tool

# Importing the tool module ensures api_get is registered in the MCP app.
api_module = importlib.import_module("gitlab_mcp.tools.api")

GITLAB_API = "https://gitlab.example.com/api/v4"
PROJECT = "group/project"
PROJECT_API = f"{GITLAB_API}/projects/group%2Fproject"


async def call_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    """Call the registered api_get tool through the in-memory MCP client."""
    assert api_module is not None
    return await call_mcp_tool("api_get", arguments)


@pytest.mark.parametrize(
    ("project", "resource_args"),
    [
        ("", {"resource": "project", "fields": ["id"]}),
        ("/group/project", {"resource": "project", "fields": ["id"]}),
        ("group//project", {"resource": "project", "fields": ["id"]}),
        ("group/../project", {"resource": "project", "fields": ["id"]}),
        ("group/\x01/project", {"resource": "project", "fields": ["id"]}),
        ("group/project?x=1", {"resource": "project", "fields": ["id"]}),
        ("group/project%2Fother", {"resource": "project", "fields": ["id"]}),
        ("group/project", {"resource": "project", "fields": ["id"], "iid": 1}),
        ("group/project", {"resource": "project", "fields": ["id"], "page": 2}),
        ("group/project", {"resource": "members", "fields": ["id"], "iid": 1}),
        ("group/project", {"resource": "project", "fields": ["id"], "file_path": "config.yml"}),
        ("group/project", {"resource": "mr_commits", "fields": ["id"]}),
        ("group/project", {"resource": "mr_commits", "fields": ["id"], "iid": 2, "ref": "main"}),
        ("group/project", {"resource": "mr_commits", "fields": ["id"], "iid": 0}),
        (
            "group/project",
            {"resource": "file", "fields": ["file_path"], "file_path": "config.yml", "iid": 2, "ref": "main"},
        ),
        (
            "group/project",
            {"resource": "file", "fields": ["file_path"], "file_path": "config.yml", "page": 2, "ref": "main"},
        ),
        ("group/project", {"resource": "file", "fields": ["file_path"], "file_path": "config.yml", "ref": ""}),
        ("group/project", {"resource": "file", "fields": ["file_path"], "file_path": "../secret", "ref": "main"}),
        ("group/project", {"resource": "file", "fields": ["file_path"], "file_path": "dir\\secret", "ref": "main"}),
        ("group/project", {"resource": "file", "fields": ["file_path"], "file_path": "config%2Fsecret", "ref": "main"}),
        ("group/project", {"resource": "file", "fields": ["file_path"], "file_path": "config.yml", "ref": "main\n"}),
        ("group/project", {"resource": "file", "fields": ["file_path"], "file_path": ".gitlab-ci.yml"}),
    ],
)
@respx.mock
async def test_invalid_resource_arguments_fail_without_request(gitlab_env, project, resource_args):
    """Unsafe paths, missing required selectors, and irrelevant options are rejected before HTTP."""
    # Arrange
    route = respx.route().mock(return_value=httpx.Response(200, json={}))

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool({"project": project, **resource_args})
    assert route.call_count == 0


@pytest.mark.parametrize(
    "fields",
    [[], ["*"], ["id.secret"], ["email"], ["access_token"], ["id", "id"], ["id"] * 21],
)
@respx.mock
async def test_unapproved_or_invalid_fields_fail_without_request(gitlab_env, fields):
    """Only the exact per-resource flat allowlist is accepted."""
    # Arrange
    route = respx.route().mock(return_value=httpx.Response(200, json={"id": 1}))

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool({"project": PROJECT, "resource": "project", "fields": fields})
    assert route.call_count == 0


@respx.mock
async def test_project_result_contains_only_selected_known_fields_and_missing_fields_are_null(gitlab_env):
    """The project response is flattened to selected values and absent allowed fields become null."""
    # Arrange
    route = respx.get(f"{PROJECT_API}").mock(
        return_value=httpx.Response(
            200,
            json={"id": 8, "name": "Platform", "archived": False, "email": "private@example.com", "secrets": {"x": 1}},
        ),
    )

    # Act
    result = await call_tool(
        {"project": PROJECT, "resource": "project", "fields": ["id", "name", "default_branch", "archived"]},
    )

    # Assert
    assert result == {"data": {"id": 8, "name": "Platform", "default_branch": None, "archived": False}}
    assert route.call_count == 1
    assert dict(route.calls[0].request.url.params) == {}


@respx.mock
async def test_members_fetches_one_requested_page_and_returns_next_page(gitlab_env):
    """List calls use only bounded pagination parameters and expose the response page header."""
    # Arrange
    route = respx.get(f"{PROJECT_API}/members/all").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 2, "username": "dev", "email": "private@example.com"}],
            headers={"X-Next-Page": "3"},
        ),
    )

    # Act
    result = await call_tool(
        {"project": PROJECT, "resource": "members", "fields": ["id", "username", "state"], "page": 2, "per_page": 5},
    )

    # Assert
    assert result == {"data": [{"id": 2, "username": "dev", "state": None}], "next_page": 3}
    assert route.call_count == 1
    assert dict(route.calls[0].request.url.params) == {"page": "2", "per_page": "5"}


@respx.mock
async def test_mr_commits_uses_iid_pagination_and_flat_selected_fields(gitlab_env):
    """Commit pages use the fixed merge-request route and keep parent IDs as a bounded flat list."""
    # Arrange
    route = respx.get(f"{PROJECT_API}/merge_requests/9/commits").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "sha1", "parent_ids": ["parent"], "message": "Commit", "author_email": "private"}],
        ),
    )

    # Act
    result = await call_tool(
        {
            "project": PROJECT,
            "resource": "mr_commits",
            "fields": ["id", "parent_ids", "message"],
            "iid": 9,
            "page": 2,
            "per_page": 4,
        },
    )

    # Assert
    assert result == {"data": [{"id": "sha1", "parent_ids": ["parent"], "message": "Commit"}]}
    assert route.call_count == 1
    assert dict(route.calls[0].request.url.params) == {"page": "2", "per_page": "4"}


@pytest.mark.parametrize(
    ("resource", "extra", "body"),
    [
        ("members", {}, {"id": 1}),
        ("members", {}, [{"id": 1}, "bad"]),
        ("mr_commits", {"iid": 2}, [{"id": 1}, {"id": 2}, {"id": 3}]),
    ],
)
@respx.mock
async def test_unexpected_response_shapes_and_oversized_pages_fail(gitlab_env, resource, extra, body):
    """Expected object/list shapes and the requested list size are enforced."""
    # Arrange
    endpoint = f"{PROJECT_API}/members/all" if resource == "members" else f"{PROJECT_API}/merge_requests/2/commits"
    route = respx.get(endpoint).mock(return_value=httpx.Response(200, json=body))

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(
            {"project": PROJECT, "resource": resource, "fields": ["id"], "per_page": 2, **extra},
        )
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("id", True, "integer"), ("archived", "false", "boolean")],
)
@respx.mock
async def test_selected_fields_require_their_expected_scalar_types(gitlab_env, field, value, message):
    """Selected API values are not coerced between JSON types."""
    # Arrange
    route = respx.get(PROJECT_API).mock(return_value=httpx.Response(200, json={field: value}))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool({"project": PROJECT, "resource": "project", "fields": [field]})
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("parent_ids", "message"),
    [(["parent", 7], "list"), (["p" * 257], "list"), (["parent"] * 101, "list")],
)
@respx.mock
async def test_parent_id_lists_are_string_only_and_bounded(gitlab_env, parent_ids, message):
    """Commit parent IDs must be a small list of bounded strings."""
    # Arrange
    route = respx.get(f"{PROJECT_API}/merge_requests/9/commits").mock(
        return_value=httpx.Response(200, json=[{"parent_ids": parent_ids}]),
    )

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool(
            {"project": PROJECT, "resource": "mr_commits", "fields": ["parent_ids"], "iid": 9},
        )
    assert route.call_count == 1


@respx.mock
async def test_project_requires_an_object_response(gitlab_env):
    """A singleton resource rejects list-shaped GitLab responses."""
    # Arrange
    route = respx.get(PROJECT_API).mock(return_value=httpx.Response(200, json=[]))

    # Act / Assert
    with pytest.raises(ToolError, match="object shape"):
        await call_tool({"project": PROJECT, "resource": "project", "fields": ["id"]})
    assert route.call_count == 1


@respx.mock
async def test_file_result_decodes_complete_utf8_content_without_truncation(gitlab_env):
    """File content is decoded only after strict base64, size, UTF-8, and control checks pass."""
    # Arrange
    raw_content = b"# pipeline\nhello\tworld\r\n"
    route = respx.get(f"{PROJECT_API}/repository/files/.gitlab-ci.yml").mock(
        return_value=httpx.Response(
            200,
            json={
                "file_name": ".gitlab-ci.yml",
                "file_path": ".gitlab-ci.yml",
                "size": len(raw_content),
                "encoding": "base64",
                "content": base64.b64encode(raw_content).decode(),
                "ref": "feature/ci docs",
                "blob_id": "blob",
                "private_token": "never return",
            },
        ),
    )

    # Act
    result = await call_tool(
        {
            "project": PROJECT,
            "resource": "file",
            "fields": ["file_name", "content", "ref"],
            "file_path": ".gitlab-ci.yml",
            "ref": "feature/ci docs",
        },
    )

    # Assert
    assert result == {
        "data": {"file_name": ".gitlab-ci.yml", "content": "# pipeline\nhello\tworld\r\n", "ref": "feature/ci docs"}
    }
    assert route.call_count == 1
    assert dict(route.calls[0].request.url.params) == {"ref": "feature/ci docs"}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"encoding": "text"}, "encoding"),
        ({"content": "%%%"}, "base64"),
        ({"size": 99}, "size"),
        ({"size": 8193}, "size"),
        ({"size": True}, "size"),
        ({"content": "A" * 10925}, "encoded content size"),
        ({"content": 3}, "encoded content size"),
        ({"content": base64.b64encode(b"\x00").decode(), "size": 1}, "control"),
        ({"content": base64.b64encode(b"\xff").decode(), "size": 1}, "UTF-8"),
    ],
)
@respx.mock
async def test_invalid_selected_file_content_is_rejected(gitlab_env, overrides, message):
    """Malformed or unsafe file content never reaches tool output."""
    # Arrange
    response_body = {"encoding": "base64", "size": 1, "content": base64.b64encode(b"x").decode()} | overrides
    route = respx.get(f"{PROJECT_API}/repository/files/config.yml").mock(
        return_value=httpx.Response(200, json=response_body),
    )

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool(
            {
                "project": PROJECT,
                "resource": "file",
                "fields": ["content"],
                "file_path": "config.yml",
                "ref": "main",
            },
        )
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"page": 0}, "greater than or equal to 1"),
        ({"page": 1_000_001}, "less than or equal to 1000000"),
        ({"per_page": 21}, "less than or equal to 20"),
        ({"max_chars": 1023}, "greater than or equal to 1024"),
        ({"max_chars": 16001}, "less than or equal to 16000"),
        ({"iid": True}, "valid integer"),
    ],
)
@respx.mock
async def test_numeric_bounds_and_strict_ids_reject_before_request(gitlab_env, arguments, message):
    """Paging, output limits, and IDs reject values outside their exact integer bounds."""
    # Arrange
    route = respx.route().mock(return_value=httpx.Response(200, json={}))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool({"project": PROJECT, "resource": "members", "fields": ["id"], **arguments})
    assert route.call_count == 0


@respx.mock
async def test_output_over_limit_is_refused_with_selection_guidance(gitlab_env):
    """The tool refuses an oversized complete envelope and suggests reducing selection or page size."""
    # Arrange
    route = respx.get(f"{PROJECT_API}/members/all").mock(
        return_value=httpx.Response(200, json=[{"name": "x" * 2000}]),
    )

    # Act / Assert
    with pytest.raises(ToolError, match="fewer fields or a smaller per_page"):
        await call_tool(
            {"project": PROJECT, "resource": "members", "fields": ["name"], "max_chars": 1024},
        )
    assert route.call_count == 1
