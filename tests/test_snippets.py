"""Behavioral contracts for personal and project snippets."""

import json

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from mcp_helpers import call_tool

API = "https://gitlab.example.com/api/v4"
PERSONAL = f"{API}/snippets"
PROJECT = f"{API}/projects/group%2Fproject/snippets"
URL = "https://gitlab.example.com/-/snippets/42"
SNIPPET_ID = 42
PROJECT_ID = 7
NEXT_PAGE = 2


@pytest.fixture
def router(gitlab_env):
    """Provide an isolated GitLab router."""
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


@pytest.mark.parametrize(
    ("arguments", "endpoint"),
    [
        ({}, PERSONAL),
        ({"scope": "all"}, f"{PERSONAL}/all"),
        ({"scope": "public"}, f"{PERSONAL}/public"),
        ({"project": "group/project"}, PROJECT),
        ({"project": "123"}, f"{API}/projects/123/snippets"),
    ],
)
async def test_list_routes_and_formats_rows(router, arguments, endpoint):
    """List scopes route correctly and escape TSV cells without leaking fields."""
    # Arrange
    route = router.get(endpoint).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 42,
                    "title": "a\tb\nc\\d",
                    "visibility": "private",
                    "project_id": None,
                    "author": {"username": "alice"},
                    "updated_at": "2026-01-02T12:00:00Z",
                    "web_url": URL,
                    "secret": "hidden",
                }
            ],
            headers={"X-Next-Page": "2"},
        ),
    )
    # Act
    data = await call_tool("list_snippets", arguments)
    # Assert
    assert data["snippets"] == (
        "id\ttitle\tvisibility\tproject_id\tauthor\tupdated\tweb_url\n"
        f"42\ta\\tb\\nc\\\\d\tprivate\t\talice\t2026-01-02\t{URL}"
    )
    assert data["next_page"] == NEXT_PAGE
    assert dict(route.calls[0].request.url.params) == {"page": "1", "per_page": "20"}


async def test_list_last_page_and_pagination(router):
    """The last page has no next page and explicit pagination reaches GitLab."""
    # Arrange
    route = router.get(PERSONAL).mock(return_value=httpx.Response(200, json=[], headers={"X-Next-Page": ""}))
    # Act
    data = await call_tool("list_snippets", {"page": 3, "per_page": 100})
    # Assert
    assert data.get("next_page") is None
    assert dict(route.calls[0].request.url.params) == {"page": "3", "per_page": "100"}


@pytest.mark.parametrize(("project", "endpoint"), [(None, PERSONAL), ("group/project", PROJECT)])
async def test_get_metadata_and_description_limit(router, project, endpoint):
    """Metadata is reduced to the documented fields and long descriptions are clipped."""
    # Arrange
    router.get(f"{endpoint}/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "title": "Title",
                "description": "x" * 10001,
                "visibility": "private",
                "project_id": 7,
                "author": {"username": "alice"},
                "updated_at": "2026-01-02T12:34:56Z",
                "web_url": URL,
                "files": [
                    {"path": "src/a.py", "raw_url": "https://gitlab.example.com/raw/main/src/a.py"},
                    {"path": "b.txt"},
                ],
                "secret": "hidden",
            },
        )
    )
    # Act
    data = await call_tool("get_snippet", {"snippet_id": 42, "project": project})
    # Assert
    assert data["id"] == SNIPPET_ID
    assert data["title"] == "Title"
    assert data["description"] == "x" * 10000
    assert data["description_truncated"] is True
    assert data["visibility"] == "private"
    assert data["project_id"] == PROJECT_ID
    assert data["author"] == "alice"
    assert data["updated_at"] == "2026-01-02"
    assert data["web_url"] == URL
    assert data["files"] == [
        {"path": "src/a.py", "raw_url": "https://gitlab.example.com/raw/main/src/a.py"},
        {"path": "b.txt"},
    ]
    assert "secret" not in data


@pytest.mark.parametrize("description", [None, "x" * 10000])
async def test_legacy_metadata(router, description):
    """Legacy file_name and nullable metadata work without a truncation flag."""
    # Arrange
    router.get(f"{PERSONAL}/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "title": "Legacy",
                "description": description,
                "file_name": "old.txt",
                "author": {"username": "alice"},
                "updated_at": "2026-01-02T00:00:00Z",
                "web_url": URL,
                "project_id": None,
            },
        )
    )
    # Act
    data = await call_tool("get_snippet", {"snippet_id": 42})
    # Assert
    assert data["files"] == [{"path": "old.txt"}]
    assert data["description"] == (description or "")
    assert data.get("visibility", "") == ""
    assert data.get("description_truncated", False) is False
    assert data.get("project_id") is None


@pytest.mark.parametrize(
    ("arguments", "endpoint"),
    [
        ({}, f"{PERSONAL}/42/raw"),
        ({"project": "group/project"}, f"{PROJECT}/42/raw"),
        (
            {"file_path": "dir/a b%z.txt", "ref": "feature/a b%z"},
            f"{PERSONAL}/42/files/feature%2Fa%20b%25z/dir%2Fa%20b%25z.txt/raw",
        ),
    ],
)
async def test_get_file_routes_and_encoding(router, arguments, endpoint):
    """Raw content routes encode ref and path as individual URL components."""
    # Arrange
    route = router.get(endpoint).mock(return_value=httpx.Response(200, content="café\n".encode()))
    # Act
    data = await call_tool("get_snippet_file", {"snippet_id": 42, **arguments})
    # Assert
    assert data == {"content": "café\n"}
    assert route.call_count == 1
    assert route.calls[0].request.url.raw_path == endpoint.removeprefix("https://gitlab.example.com").encode()


@pytest.mark.parametrize("ref", ["", " ", ".", "..", "a\x00b"])
async def test_invalid_refs_fail_before_requests(router, ref):
    """Invalid refs cannot become dot segments that change the requested API path."""
    # Arrange
    route = router.get(url__regex=rf"{API}/.*").mock(return_value=httpx.Response(200, content=b"text"))

    # Act / Assert
    with pytest.raises(ToolError, match="ref must"):
        await call_tool("get_snippet_file", {"snippet_id": 42, "file_path": "a.txt", "ref": ref})
    assert not route.called


@pytest.mark.parametrize("content", [b"x" * 100001, b"\xff", b"a\x00b"])
async def test_invalid_raw_content_is_rejected(router, content):
    """Oversized, non-UTF-8, and binary content are not returned as text."""
    # Arrange
    router.get(f"{PERSONAL}/42/raw").mock(return_value=httpx.Response(200, content=content))
    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool("get_snippet_file", {"snippet_id": 42})


async def test_raw_content_accepts_byte_limit(router):
    """Exactly 100000 UTF-8 bytes are accepted."""
    # Arrange
    content = "🙂" * 25000
    router.get(f"{PERSONAL}/42/raw").mock(return_value=httpx.Response(200, content=content.encode()))
    # Act
    data = await call_tool("get_snippet_file", {"snippet_id": 42})
    # Assert
    assert data == {"content": content}


@pytest.mark.parametrize(("project", "endpoint"), [(None, PERSONAL), ("group/project", PROJECT)])
async def test_create_with_local_file(router, tmp_path, project, endpoint):
    """Create resolves local files, posts requested fields, and returns only identifiers."""
    # Arrange
    source = tmp_path / "source.txt"
    source.write_text("café", encoding="utf-8")
    route = router.post(endpoint).mock(return_value=httpx.Response(201, json={"id": 42, "web_url": URL}))
    # Act
    data = await call_tool(
        "create_snippet",
        {
            "project": project,
            "title": "Title",
            "description": "Details",
            "visibility": "internal",
            "files": [{"file_path": "a.txt", "content_file": str(source)}, {"file_path": "b", "content": "text"}],
        },
    )
    # Assert
    assert json.loads(route.calls[0].request.content) == {
        "title": "Title",
        "description": "Details",
        "visibility": "internal",
        "files": [{"file_path": "a.txt", "content": "café"}, {"file_path": "b", "content": "text"}],
    }
    assert data == {"status": "created", "id": 42, "web_url": URL}


async def test_create_defaults_private_and_accepts_exact_byte_limit(router):
    """Creation stays private unless requested otherwise and accepts exactly the content byte cap."""
    # Arrange
    route = router.post(PERSONAL).mock(return_value=httpx.Response(201, json={"id": 42, "web_url": URL}))
    files = [{"file_path": "a", "content": "🙂" * 12500}, {"file_path": "b", "content": "x" * 50000}]

    # Act
    data = await call_tool("create_snippet", {"title": "Private", "files": files})

    # Assert
    assert data["status"] == "created"
    assert json.loads(route.calls[0].request.content) == {"title": "Private", "visibility": "private", "files": files}


async def test_update_metadata_preserves_omitted_fields(router):
    """Metadata-only edits do not send file actions or replace an omitted description."""
    # Arrange
    route = router.put(f"{PERSONAL}/42").mock(return_value=httpx.Response(200, json={"id": 42, "web_url": URL}))

    # Act
    data = await call_tool("update_snippet", {"snippet_id": 42, "title": "Renamed", "visibility": "public"})

    # Assert
    assert data == {"status": "updated", "id": 42, "web_url": URL}
    assert json.loads(route.calls[0].request.content) == {"title": "Renamed", "visibility": "public"}


async def test_update_actions_and_empty_description(router, tmp_path):
    """Update sends only requested fields and preserves each file action's semantics."""
    # Arrange
    source = tmp_path / "content"
    source.write_text("new", encoding="utf-8")
    route = router.put(f"{PROJECT}/42").mock(return_value=httpx.Response(200, json={"id": 42, "web_url": URL}))
    files = [
        {"action": "create", "file_path": "a", "content_file": str(source)},
        {"action": "update", "file_path": "b", "content": "updated"},
        {"action": "move", "file_path": "c", "previous_path": "old"},
        {"action": "delete", "file_path": "d"},
    ]
    # Act
    data = await call_tool(
        "update_snippet", {"snippet_id": 42, "project": "group/project", "description": "", "files": files}
    )
    # Assert
    assert json.loads(route.calls[0].request.content) == {
        "description": "",
        "files": [
            {"action": "create", "file_path": "a", "content": "new"},
            *files[1:],
        ],
    }
    assert data == {"status": "updated", "id": 42, "web_url": URL}


@pytest.mark.parametrize(("project", "endpoint"), [(None, PERSONAL), ("group/project", PROJECT)])
async def test_delete_requires_204(router, project, endpoint):
    """A 204 response confirms deletion of the requested snippet."""
    # Arrange
    route = router.delete(f"{endpoint}/42").mock(return_value=httpx.Response(204))
    # Act
    data = await call_tool("delete_snippet", {"snippet_id": 42, "project": project})
    # Assert
    assert data == {"status": "deleted", "id": 42}
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_snippets", {"project": "group/project", "scope": "public"}),
        ("list_snippets", {"project": " "}),
        ("list_snippets", {"page": 0}),
        ("list_snippets", {"per_page": 101}),
        ("get_snippet", {"snippet_id": True}),
        ("get_snippet", {"snippet_id": 0}),
        ("get_snippet_file", {"snippet_id": 42, "ref": "main"}),
        ("get_snippet_file", {"snippet_id": 42, "file_path": "a"}),
        ("create_snippet", {"title": " ", "files": [{"file_path": "a", "content": "x"}]}),
        ("create_snippet", {"title": "x" * 256, "files": [{"file_path": "a", "content": "x"}]}),
        ("create_snippet", {"title": "x", "description": "x" * 10001, "files": [{"file_path": "a", "content": "x"}]}),
        ("create_snippet", {"title": "x", "files": []}),
        ("create_snippet", {"title": "x", "files": [{"file_path": "a", "content": " "}]}),
        (
            "create_snippet",
            {"title": "x", "files": [{"file_path": "a", "content": "x", "content_file": "/home/example/x"}]},
        ),
        ("create_snippet", {"title": "x", "files": [{"file_path": "a", "content": "🙂" * 25001}]}),
        (
            "create_snippet",
            {
                "title": "x",
                "files": [{"file_path": "a", "content": "x" * 50001}, {"file_path": "b", "content": "x" * 50000}],
            },
        ),
        ("update_snippet", {"snippet_id": 42}),
        ("update_snippet", {"snippet_id": 42, "files": [{"action": "create", "file_path": "a"}]}),
        ("update_snippet", {"snippet_id": 42, "files": [{"action": "update", "file_path": "a"}]}),
        ("update_snippet", {"snippet_id": 42, "files": [{"action": "move", "file_path": "a"}]}),
        ("update_snippet", {"snippet_id": 42, "files": [{"action": "delete", "file_path": "a", "content": "x"}]}),
        (
            "update_snippet",
            {"snippet_id": 42, "files": [{"action": "update", "file_path": "a", "content": "x", "previous_path": "b"}]},
        ),
        ("update_snippet", {"snippet_id": 42, "files": [{"action": "delete", "file_path": "a"}] * 2}),
        ("delete_snippet", {"snippet_id": -1}),
    ],
)
async def test_invalid_inputs_never_call_gitlab(router, tool, arguments):
    """Invalid inputs fail locally before a GitLab request."""
    # Arrange / Act / Assert
    with pytest.raises(ToolError) as error:
        await call_tool(tool, arguments)
    assert "Unknown tool" not in str(error.value)
    assert not router.calls


@pytest.mark.parametrize("path", ["", " ", "/absolute", "../parent", "a/./b", "a/../b", "a\x00b"])
async def test_invalid_file_paths_fail_locally(router, path):
    """Blank, absolute, control, and dot paths are never sent to GitLab."""
    # Arrange / Act / Assert
    with pytest.raises(ToolError):
        await call_tool("create_snippet", {"title": "x", "files": [{"file_path": path, "content": "x"}]})
    assert not router.calls


@pytest.mark.parametrize("kind", ["relative", "directory", "missing", "invalid_utf8"])
async def test_invalid_content_file_fails_locally(router, tmp_path, kind):
    """Local content must be an absolute path to an existing regular UTF-8 file."""
    # Arrange
    source = tmp_path / "file"
    if kind == "invalid_utf8":
        source.write_bytes(b"\xff")
    path = "relative" if kind == "relative" else str(tmp_path if kind == "directory" else source)
    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool("create_snippet", {"title": "x", "files": [{"file_path": "a", "content_file": path}]})
    assert not router.calls


@pytest.mark.parametrize(
    ("tool", "method", "endpoint", "arguments"),
    [
        ("create_snippet", "POST", PERSONAL, {"title": "x", "files": [{"file_path": "a", "content": "x"}]}),
        ("update_snippet", "PUT", f"{PERSONAL}/42", {"snippet_id": 42, "title": "x"}),
        ("delete_snippet", "DELETE", f"{PERSONAL}/42", {"snippet_id": 42}),
    ],
)
@pytest.mark.parametrize("outcome", [httpx.ConnectError("secret"), httpx.ReadTimeout("secret"), 408, 503])
async def test_uncertain_writes_never_retry(router, tool, method, endpoint, arguments, outcome):  # noqa: PLR0913, PLR0917
    """Transport, timeout, and server failures report an unknown outcome without retry."""
    # Arrange
    route = router.route(method=method, url=endpoint)
    if isinstance(outcome, Exception):
        route.mock(side_effect=outcome)
    else:
        route.mock(return_value=httpx.Response(outcome, json={"message": "secret"}))
    # Act
    data = await call_tool(tool, arguments)
    # Assert
    assert data["status"] == "unknown"
    assert "check gitlab before retry" in data["message"].lower()
    assert "secret" not in json.dumps(data)
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("tool", "method", "endpoint", "arguments", "response"),
    [
        (
            "create_snippet",
            "POST",
            PERSONAL,
            {"title": "x", "files": [{"file_path": "a", "content": "x"}]},
            httpx.Response(201, content=b"bad"),
        ),
        (
            "create_snippet",
            "POST",
            PERSONAL,
            {"title": "x", "files": [{"file_path": "a", "content": "x"}]},
            httpx.Response(201, json={"id": 42}),
        ),
        (
            "update_snippet",
            "PUT",
            f"{PERSONAL}/42",
            {"snippet_id": 42, "title": "x"},
            httpx.Response(200, json={"id": 43, "web_url": URL}),
        ),
        (
            "update_snippet",
            "PUT",
            f"{PERSONAL}/42",
            {"snippet_id": 42, "title": "x"},
            httpx.Response(200, json={"web_url": URL}),
        ),
        ("delete_snippet", "DELETE", f"{PERSONAL}/42", {"snippet_id": 42}, httpx.Response(200)),
        ("delete_snippet", "DELETE", f"{PERSONAL}/42", {"snippet_id": 42}, httpx.Response(302)),
    ],
)
async def test_unverified_success_is_unknown(router, tool, method, endpoint, arguments, response):  # noqa: PLR0913, PLR0917
    """Malformed or unexpected success responses never claim a verified write."""
    # Arrange
    route = router.route(method=method, url=endpoint).mock(return_value=response)
    # Act
    data = await call_tool(tool, arguments)
    # Assert
    assert data["status"] == "unknown"
    assert "check gitlab before retry" in data["message"].lower()
    assert route.call_count == 1


async def test_rejected_write_is_sanitized(router):
    """A definite client rejection does not leak GitLab's response or snippet content."""
    # Arrange
    route = router.post(PERSONAL).mock(return_value=httpx.Response(422, json={"message": "SECRET CONTENT"}))
    # Act / Assert
    with pytest.raises(ToolError) as error:
        await call_tool("create_snippet", {"title": "x", "files": [{"file_path": "a", "content": "SECRET CONTENT"}]})
    assert "422" in str(error.value)
    assert "SECRET CONTENT" not in str(error.value)
    assert route.call_count == 1
