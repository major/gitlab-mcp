"""Behavioral tests for bounded job artifact text reads 📦."""

from typing import Any

import httpx
import pytest
import respx

from gitlab_mcp.ci_common import decode_cursor, encode_cursor
from mcp_helpers import call_tool_result

pytestmark = pytest.mark.usefixtures("gitlab_env")

API = "https://gitlab.example.com/api/v4"
PROJECT = "group/project"
JOB_ID = 42
ARTIFACT_URL = f"{API}/projects/group%2Fproject/jobs/{JOB_ID}/artifacts"
JOB_URL = f"{API}/projects/group%2Fproject/jobs/{JOB_ID}"
TREE_URL = f"{API}/projects/group%2Fproject/jobs/{JOB_ID}/artifacts/tree"
TWO_CALLS = 2
TSV_SAFE_PATH_LENGTH = 791
TSV_SAFE_ENTRY_LENGTH = 15994
TWENTY_ENTRIES = 20
HTTP_NOT_FOUND = 404


async def call_artifact(**arguments: Any) -> Any:
    """Call get_job_artifact through the in-memory MCP client."""
    return await call_tool_result(
        "get_job_artifact",
        {"project": PROJECT, "job_id": JOB_ID, "artifact_path": "report.txt", **arguments},
        raise_on_error=False,
    )


def error_text(result: Any) -> str:
    """Return the text content from an error tool result."""
    return " ".join(item.text for item in result.content if hasattr(item, "text"))


async def call_artifact_list(**arguments: Any) -> Any:
    """Call list_job_artifacts through the in-memory MCP client."""
    return await call_tool_result(
        "list_job_artifacts",
        {"project": PROJECT, "job_id": JOB_ID, **arguments},
        raise_on_error=False,
    )


@pytest.mark.parametrize(
    ("body", "max_bytes", "expected_content", "expected_truncated"),
    [
        pytest.param(b"one\ntwo", 20, "one\ntwo", False, id="text"),
        pytest.param(b"", 20, "", False, id="empty"),
        pytest.param(b"four", 4, "four", False, id="exact-limit"),
        pytest.param(b"five!", 4, "five", True, id="over-limit"),
    ],
)
@respx.mock
async def test_returns_bounded_artifact_text(body, max_bytes, expected_content, expected_truncated):
    """Return all available text or the byte-capped prefix without changing it."""
    # Arrange
    artifact = respx.get(f"{ARTIFACT_URL}/report.txt").mock(return_value=httpx.Response(200, content=body))
    job = respx.get(JOB_URL).mock(return_value=httpx.Response(200, json={"status": "success"}))

    # Act
    result = await call_artifact(max_bytes=max_bytes)

    # Assert
    assert result.structured_content == {
        "content": expected_content,
        "bytes_returned": len(expected_content.encode("utf-8")),
        "truncated": expected_truncated,
    }
    assert artifact.call_count == 1
    assert job.call_count == 0
    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    ("body", "max_bytes", "expected_content"),
    [
        pytest.param("aé!".encode(), 2, "a", id="cut-before-multibyte"),
        pytest.param("aé!".encode(), 3, "aé", id="cut-after-multibyte"),
        pytest.param("aé!".encode(), 4, "aé!", id="complete-multibyte"),
    ],
)
@respx.mock
async def test_multibyte_text_is_decoded_strictly_at_byte_limit(body, max_bytes, expected_content):
    """Decode complete UTF-8 characters and do not replace content around a byte cut."""
    # Arrange
    route = respx.get(f"{ARTIFACT_URL}/report.txt").mock(return_value=httpx.Response(200, content=body))

    # Act
    result = await call_artifact(max_bytes=max_bytes)

    # Assert
    assert result.structured_content == {
        "content": expected_content,
        "bytes_returned": len(expected_content.encode("utf-8")),
        "truncated": len(body) > max_bytes,
    }
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("body", "max_bytes", "expect_error"),
    [
        pytest.param(b"ok\xe2\x82more", 3, False, id="incomplete-trailing-sequence-is-omitted-when-cut"),
        pytest.param(b"ok\xff", 10, True, id="invalid-at-eof-errors"),
        pytest.param(b"ok\xffmore", 3, True, id="invalid-before-cut-errors"),
        pytest.param(b"ok\xe2", 10, True, id="incomplete-at-eof-errors"),
    ],
)
@respx.mock
async def test_invalid_utf8_is_an_error_except_for_incomplete_cut_suffix(body, max_bytes, expect_error):
    """Only an incomplete final code point caused by transport truncation may be dropped."""
    # Arrange
    route = respx.get(f"{ARTIFACT_URL}/report.txt").mock(return_value=httpx.Response(200, content=body))

    # Act
    result = await call_artifact(max_bytes=max_bytes)

    # Assert
    if not expect_error:
        assert result.structured_content == {"content": "ok", "bytes_returned": 2, "truncated": True}
    else:
        assert result.is_error
        assert "valid UTF-8" in error_text(result)
    assert route.call_count == 1


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x08", "\x0b", "\x1f", "\x7f"])
@respx.mock
async def test_rejects_disallowed_content_controls(control):
    """NUL, C0 controls other than tab and line breaks, and DEL are rejected."""
    # Arrange
    route = respx.get(f"{ARTIFACT_URL}/report.txt").mock(
        return_value=httpx.Response(200, content=f"safe{control}text".encode())
    )

    # Act
    result = await call_artifact()

    # Assert
    assert result.is_error
    assert "control character" in error_text(result)
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        pytest.param("application/octet-stream", b"<script>token=secret</script>\tline\r\n", id="mime-spoof-ignored"),
        pytest.param("text/plain", b"<b>ordinary text</b>", id="markup-is-preserved"),
    ],
)
@respx.mock
async def test_content_is_data_and_mime_does_not_change_decoding(content_type, body):
    """Accept valid UTF-8 regardless of MIME and preserve every allowed character."""
    # Arrange
    route = respx.get(f"{ARTIFACT_URL}/report.txt").mock(
        return_value=httpx.Response(200, headers={"Content-Type": content_type}, content=body)
    )

    # Act
    result = await call_artifact()

    # Assert
    content = body.decode("utf-8")
    assert result.structured_content == {
        "content": content,
        "bytes_returned": len(body),
        "truncated": False,
    }
    assert route.call_count == 1


@respx.mock
async def test_encodes_project_and_each_path_segment_once():
    """Preserve separators while escaping reserved and Unicode filename characters exactly once."""
    # Arrange
    project = "group/sub group%"
    artifact_path = "reports/a b%?#é.txt"
    expected_url = f"{API}/projects/group%2Fsub%20group%25/jobs/{JOB_ID}/artifacts/reports/a%20b%25%3F%23%C3%A9.txt"
    route = respx.get(expected_url).mock(return_value=httpx.Response(200, content=b"ok"))

    # Act
    result = await call_artifact(project=project, artifact_path=artifact_path)

    # Assert
    assert result.structured_content["content"] == "ok"
    assert route.call_count == 1
    assert route.calls.last.request.url.raw_path.decode() == (
        "/api/v4/projects/group%2Fsub%20group%25/jobs/42/artifacts/reports/a%20b%25%3F%23%C3%A9.txt"
    )
    assert route.calls.last.request.url.query == b""


@pytest.mark.parametrize("job_status", ["success", "failed"])
@respx.mock
async def test_artifact_endpoint_is_called_directly_for_any_job_status(job_status):
    """Do not preflight job status before requesting its artifact endpoint."""
    # Arrange
    artifact = respx.get(f"{ARTIFACT_URL}/report.txt").mock(return_value=httpx.Response(200, content=b"text"))
    job = respx.get(JOB_URL).mock(return_value=httpx.Response(200, json={"status": job_status}))

    # Act
    result = await call_artifact()

    # Assert
    assert result.structured_content["content"] == "text"
    assert artifact.call_count == 1
    assert job.call_count == 0
    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    "artifact_path",
    [
        pytest.param("", id="empty"),
        pytest.param("/absolute.txt", id="absolute"),
        pytest.param("folder\\file.txt", id="backslash"),
        pytest.param("folder//file.txt", id="empty-segment"),
        pytest.param("./file.txt", id="dot-segment"),
        pytest.param("folder/../file.txt", id="dotdot-segment"),
        pytest.param("folder/", id="trailing-empty-segment"),
        pytest.param("bad\nname.txt", id="control-character"),
        pytest.param("x" * 1025, id="over-1024-characters"),
    ],
)
@respx.mock
async def test_rejects_invalid_artifact_paths_before_request(artifact_path):
    """Reject absolute, ambiguous, or oversized logical paths before downloading."""
    # Arrange
    route = respx.get(f"{ARTIFACT_URL}/report.txt").mock(return_value=httpx.Response(200, content=b"unused"))

    # Act
    result = await call_artifact(artifact_path=artifact_path)

    # Assert
    assert result.is_error
    assert route.call_count == 0
    assert len(respx.calls) == 0


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"project": ""}, id="empty-project"),
        pytest.param({"project": " \t"}, id="blank-project"),
        pytest.param({"job_id": 0}, id="zero-job-id"),
        pytest.param({"job_id": True}, id="boolean-job-id"),
        pytest.param({"max_bytes": 0}, id="max-bytes-low"),
        pytest.param({"max_bytes": 65537}, id="max-bytes-high"),
        pytest.param({"max_bytes": True}, id="max-bytes-boolean"),
        pytest.param({"max_bytes": 1.5}, id="max-bytes-float"),
    ],
)
@respx.mock
async def test_rejects_invalid_arguments_before_request(arguments):
    """Reject invalid project, identifier, and byte-limit arguments before calling GitLab."""
    # Arrange
    route = respx.get(f"{ARTIFACT_URL}/report.txt").mock(return_value=httpx.Response(200, content=b"unused"))

    # Act
    result = await call_artifact(**arguments)

    # Assert
    assert result.is_error
    assert route.call_count == 0
    assert len(respx.calls) == 0


@pytest.mark.parametrize(
    ("status", "message"),
    [
        pytest.param(404, "missing, expired, or inaccessible", id="not-found-is-ambiguous"),
        pytest.param(403, "HTTP 403", id="forbidden"),
        pytest.param(429, "HTTP 429", id="rate-limited"),
    ],
)
@respx.mock
async def test_reports_artifact_http_errors_without_metadata_preflight(status, message):
    """Handle 404 as ambiguous while preserving actionable permission and rate-limit errors."""
    # Arrange
    route = respx.get(f"{ARTIFACT_URL}/report.txt").mock(
        return_value=httpx.Response(status, json={"message": "details are not safe to repeat"})
    )
    job = respx.get(JOB_URL).mock(return_value=httpx.Response(200, json={"status": "success"}))

    # Act
    result = await call_artifact()

    # Assert
    assert result.is_error
    assert message in error_text(result)
    assert "details are not safe to repeat" not in error_text(result)
    assert route.call_count == 1
    assert job.call_count == 0
    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    ("path", "recursive", "limit", "expected_params"),
    [
        pytest.param("", False, 20, {"recursive": "false", "per_page": "20", "page": "1"}, id="root-defaults"),
        pytest.param(
            "nested path/",
            True,
            7,
            {"path": "nested path", "recursive": "true", "per_page": "7", "page": "1"},
            id="path-canonicalized",
        ),
    ],
)
@respx.mock
async def test_lists_one_page_with_bounded_api_query(path, recursive, limit, expected_params):
    """Use one bounded tree request and omit the root path query parameter."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, json=[]))

    # Act
    result = await call_artifact_list(path=path, recursive=recursive, limit=limit)

    # Assert
    assert result.structured_content == {"entries": "path\ttype\tsize"}
    assert route.call_count == 1
    assert dict(route.calls.last.request.url.params) == expected_params
    assert len(respx.calls) == 1


@respx.mock
async def test_lists_files_and_directories_as_untrusted_tsv_cells():
    """Keep the fixed TSV columns, preserve directory size as empty, and flatten untrusted path controls."""
    # Arrange
    route = respx.get(TREE_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"path": "folder", "type": "directory", "ignored": "extra"},
                {"path": "folder/a\tfile.txt", "type": "file", "size": 7},
                {"path": "unknown-size.txt", "type": "file"},
            ],
        )
    )

    # Act
    result = await call_artifact_list()

    # Assert
    assert result.structured_content == {
        "entries": "path\ttype\tsize\nfolder\tdirectory\t\nfolder/a file.txt\tfile\t7\nunknown-size.txt\tfile\t"
    }
    assert route.call_count == 1
    assert len(respx.calls) == 1


@respx.mock
async def test_empty_tree_is_header_only_and_omits_empty_cursor():
    """An empty response is a header-only page and has no next cursor field."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, json=[], headers={"X-Next-Page": ""}))

    # Act
    result = await call_artifact_list()

    # Assert
    assert result.structured_content == {"entries": "path\ttype\tsize"}
    assert "next_cursor" not in result.structured_content
    assert route.call_count == 1


@respx.mock
async def test_next_page_cursor_preserves_scope_and_requests_exactly_one_page():
    """Return an opaque cursor for the next page and use it for one subsequent request."""
    # Arrange
    route = respx.get(TREE_URL).mock(
        side_effect=[
            httpx.Response(200, json=[{"path": "first", "type": "file", "size": 1}], headers={"X-Next-Page": "2"}),
            httpx.Response(200, json=[{"path": "second", "type": "file", "size": 2}]),
        ]
    )

    # Act
    first = await call_artifact_list(path="reports/", recursive=True, limit=5)
    second = await call_artifact_list(
        path="reports",
        recursive=True,
        limit=5,
        cursor=first.structured_content["next_cursor"],
    )

    # Assert
    assert first.structured_content["entries"] == "path\ttype\tsize\nfirst\tfile\t1"
    assert second.structured_content == {"entries": "path\ttype\tsize\nsecond\tfile\t2"}
    assert route.call_count == TWO_CALLS
    assert route.calls[0].request.url.params["page"] == "1"
    assert route.calls[1].request.url.params["page"] == "2"
    assert "next_cursor" not in second.structured_content
    assert len(respx.calls) == TWO_CALLS


@pytest.mark.parametrize(
    "changed_arguments",
    [
        pytest.param({"project": "other/project"}, id="project"),
        pytest.param({"job_id": 43}, id="job-id"),
        pytest.param({"path": "other"}, id="path"),
        pytest.param({"recursive": True}, id="recursive"),
        pytest.param({"limit": 21}, id="limit"),
    ],
)
@respx.mock
async def test_cursor_rejects_changed_scope_before_request(changed_arguments):
    """Reject cursors reused with any changed project, job, path, recursion, or limit argument."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, json=[], headers={"X-Next-Page": "2"}))
    first = await call_artifact_list(path="reports", recursive=False, limit=20)
    cursor = first.structured_content["next_cursor"]

    # Act
    arguments = {"path": "reports", "recursive": False, "limit": 20, "cursor": cursor} | changed_arguments
    result = await call_artifact_list(**arguments)

    # Assert
    assert result.is_error
    assert "Invalid cursor" in error_text(result)
    assert route.call_count == 1
    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    "bad_cursor",
    [
        pytest.param("not-a-cursor", id="malformed"),
        pytest.param("", id="empty-string"),
    ],
)
@respx.mock
async def test_malformed_cursor_is_rejected_before_request(bad_cursor):
    """Reject malformed and empty cursors instead of treating them as the first page."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, json=[]))

    # Act
    result = await call_artifact_list(cursor=bad_cursor)

    # Assert
    assert result.is_error
    assert "Invalid cursor" in error_text(result)
    assert route.call_count == 0
    assert len(respx.calls) == 0


@pytest.mark.parametrize(
    "bad_scope",
    [
        pytest.param(None, id="null"),
        pytest.param([], id="list"),
        pytest.param("scope", id="string"),
    ],
)
@respx.mock
async def test_cursor_rejects_non_object_scope_before_request(bad_scope):
    """Reject encoded cursors whose scope is not an object before making a tree request."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, json=[]))
    cursor = encode_cursor({"scope": bad_scope, "page": 2})

    # Act
    result = await call_artifact_list(cursor=cursor)

    # Assert
    assert result.is_error
    assert "Invalid cursor" in error_text(result)
    assert route.call_count == 0
    assert len(respx.calls) == 0


@pytest.mark.parametrize(
    "bad_page",
    [pytest.param(True, id="boolean"), pytest.param(0, id="zero"), pytest.param(1_000_001, id="too-large")],
)
@respx.mock
async def test_cursor_rejects_invalid_page_before_request(bad_page):
    """Require cursor pages to be positive bounded integers, excluding booleans."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, json=[], headers={"X-Next-Page": "2"}))
    initial = await call_artifact_list()
    payload = decode_cursor(initial.structured_content["next_cursor"])
    payload["page"] = bad_page
    cursor = encode_cursor(payload)

    # Act
    result = await call_artifact_list(cursor=cursor)

    # Assert
    assert result.is_error
    assert "Invalid cursor" in error_text(result)
    assert route.call_count == 1
    assert len(respx.calls) == 1


@respx.mock
async def test_cursor_rejects_changed_tool_version_before_request():
    """Reject cursors from another tool version before making a tree request."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, json=[], headers={"X-Next-Page": "2"}))
    initial = await call_artifact_list()
    payload = decode_cursor(initial.structured_content["next_cursor"])
    payload["scope"]["tool"] = "list_job_artifacts:v0"
    cursor = encode_cursor(payload)

    # Act
    result = await call_artifact_list(cursor=cursor)

    # Assert
    assert result.is_error
    assert "Invalid cursor" in error_text(result)
    assert route.call_count == 1
    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/absolute", id="absolute"),
        pytest.param("nested//empty", id="empty-segment"),
        pytest.param("./dot", id="dot-segment"),
        pytest.param("nested/../parent", id="dotdot-segment"),
        pytest.param("back\\slash", id="backslash"),
        pytest.param("control\nchar", id="control-character"),
        pytest.param("x" * 1025, id="too-long"),
    ],
)
@respx.mock
async def test_invalid_browse_path_is_rejected_before_request(path):
    """Reject invalid tree paths before the GitLab request."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, json=[]))

    # Act
    result = await call_artifact_list(path=path)

    # Assert
    assert result.is_error
    assert route.call_count == 0
    assert len(respx.calls) == 0


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"recursive": 1}, id="non-boolean-recursive"),
        pytest.param({"recursive": "true"}, id="string-recursive"),
        pytest.param({"limit": True}, id="boolean-limit"),
        pytest.param({"limit": 0}, id="limit-low"),
        pytest.param({"limit": 101}, id="limit-high"),
    ],
)
@respx.mock
async def test_invalid_list_arguments_are_rejected_before_request(arguments):
    """Reject non-strict booleans, booleans-as-integers, and out-of-range limits."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, json=[]))

    # Act
    result = await call_artifact_list(**arguments)

    # Assert
    assert result.is_error
    assert route.call_count == 0
    assert len(respx.calls) == 0


@pytest.mark.parametrize(
    ("body", "limit", "message"),
    [
        pytest.param({"not": "a list"}, 20, "list", id="not-a-list"),
        pytest.param([{"path": "a", "type": "file", "size": 0}] * 2, 1, "limit", id="too-many-rows"),
        pytest.param(["not a row"], 20, "object", id="row-not-object"),
        pytest.param([{"path": 1, "type": "file", "size": 0}], 20, "path", id="path-not-string"),
        pytest.param([{"path": "x" * 1025, "type": "file", "size": 0}], 20, "path", id="path-too-long"),
        pytest.param([{"path": "x", "type": "unknown", "size": 0}], 20, "type", id="invalid-type"),
        pytest.param([{"path": "x", "type": "file", "size": -1}], 20, "size", id="negative-size"),
        pytest.param([{"path": "x", "type": "file", "size": True}], 20, "size", id="boolean-size"),
        pytest.param([{"path": "x", "type": "file", "size": "1"}], 20, "size", id="string-size"),
    ],
)
@respx.mock
async def test_invalid_tree_response_is_an_error_not_a_partial_list(body, limit, message):
    """Reject malformed response shapes or fields instead of silently dropping entries."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, json=body))

    # Act
    result = await call_artifact_list(limit=limit)

    # Assert
    assert result.is_error
    assert message in error_text(result)
    assert route.call_count == 1
    assert len(respx.calls) == 1


@pytest.mark.parametrize("path_length", [791, 792], ids=["under-tsv-limit", "over-tsv-limit"])
@respx.mock
async def test_rendered_tsv_is_bounded_without_dropping_rows(path_length):
    """Keep a rendered page under 16000 characters or fail with a smaller-limit hint."""
    # Arrange
    body = [{"path": "x" * path_length, "type": "file", "size": 0} for _ in range(20)]
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, json=body))

    # Act
    result = await call_artifact_list(limit=20)

    # Assert
    assert route.call_count == 1
    assert len(respx.calls) == 1
    if path_length == TSV_SAFE_PATH_LENGTH:
        assert len(result.structured_content["entries"]) == TSV_SAFE_ENTRY_LENGTH
        assert result.structured_content["entries"].count("\n") == TWENTY_ENTRIES
    else:
        assert result.is_error
        assert "reduce the limit" in error_text(result).casefold()


@respx.mock
async def test_oversized_json_body_is_rejected_by_bounded_reader():
    """Refuse a tree response over the fixed 128 KiB transport limit."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(200, content=b" " * 131073))

    # Act
    result = await call_artifact_list()

    # Assert
    assert result.is_error
    assert "131072-byte limit" in error_text(result)
    assert route.call_count == 1
    assert len(respx.calls) == 1


@pytest.mark.parametrize(
    ("status", "message"),
    [
        pytest.param(HTTP_NOT_FOUND, "may be missing or inaccessible", id="ambiguous-404"),
        pytest.param(403, "HTTP 403", id="forbidden"),
        pytest.param(429, "HTTP 429", id="rate-limited"),
    ],
)
@respx.mock
async def test_tree_http_errors_are_clear_and_never_fall_back(status, message):
    """Describe ambiguous tree 404s and preserve other errors without retry or ZIP fallback."""
    # Arrange
    route = respx.get(TREE_URL).mock(return_value=httpx.Response(status, json={"message": "private detail"}))

    # Act
    result = await call_artifact_list()

    # Assert
    assert result.is_error
    assert message in error_text(result)
    if status == HTTP_NOT_FOUND:
        assert "get_job_artifact" in error_text(result)
        assert "private detail" not in error_text(result)
    assert route.call_count == 1
    assert len(respx.calls) == 1
