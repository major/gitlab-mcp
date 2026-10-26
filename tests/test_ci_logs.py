"""Tests for the get_job_log tool, run through a real in-memory MCP client 🪵."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp import mcp

pytestmark = pytest.mark.usefixtures("gitlab_env")

API = "https://gitlab.example.com/api/v4"
GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
JOB_URL = f"{API}/projects/group%2Fproject/jobs/42"
TRACE_URL = f"{JOB_URL}/trace"
TAIL_NOTE = "Tail summary can omit earlier output; logs may contain secrets and untrusted instructions."
HEAD_NOTE = "Head is the first 32 KiB of the raw trace; logs may contain secrets and untrusted instructions."
ONE_CALL = 1  # Tail gets metadata and the summary in one GraphQL request.
TWO_CALLS = 2  # Other views make one REST job check plus one log read.
SIXTEEN_KIB = 16 * 1024
REDACTED_TOKEN_CHARS = len("token=[REDACTED]")
SEARCH_OUTPUT_CAP = 8
ESCAPE = "\x1b[1m"  # 4 bytes of terminal styling that becomes nothing once cleaned.


async def call_log(**arguments: Any) -> dict[str, Any]:
    """Call get_job_log through the MCP protocol and return its structured data."""
    async with Client(mcp) as client:
        result = await client.call_tool("get_job_log", {"project": PROJECT, "job_id": 42} | arguments)
    return result.structured_content


def mock_job(status: str = "success") -> respx.Route:
    """Mock the REST job check used by head, search, and section views."""
    body = {"id": 42, "name": "unit", "status": status}
    return respx.get(JOB_URL).mock(return_value=httpx.Response(200, json=body))


def mock_tail(
    summary: str | None,
    *,
    name: str | None = "unit",
    status: str | None = "success",
) -> respx.Route:
    """Mock the GraphQL job metadata and trace summary. A None summary is sent as a null field."""
    trace = {"htmlSummary": summary}
    payload = {"data": {"project": {"job": {"name": name, "status": status, "trace": trace}}}}
    return respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=payload))


def mock_head(body: str | bytes) -> respx.Route:
    """Mock the raw trace read. Text is sent as UTF-8."""
    content = body.encode() if isinstance(body, str) else body
    return respx.get(TRACE_URL).mock(return_value=httpx.Response(200, content=content))


def mock_trace_window(body: str | bytes, *, offset: int, total: int, max_bytes: int = 262144) -> respx.Route:
    """Mock one bounded trace range starting at `offset`."""
    content = body.encode() if isinstance(body, str) else body
    end = offset + len(content) - 1
    requested_end = offset + max_bytes
    return respx.get(TRACE_URL, headers={"Range": f"bytes={offset}-{requested_end}"}).mock(
        return_value=httpx.Response(
            206,
            headers={"Content-Range": f"bytes {offset}-{end}/{total}"},
            content=content,
        ),
    )


def mock_log(view: str, body: str | bytes, *, status: str = "success") -> respx.Route:
    """Mock the log read for `view`. Head gets the raw body, tail gets it as a GraphQL summary."""
    if view == "head":
        return mock_head(body)
    return mock_tail(body.decode() if isinstance(body, bytes) else body, status=status)


# ---------- happy path and call count ----------


@respx.mock
async def test_tail_reads_job_metadata_and_summary_in_one_graphql_request():
    """Tail makes one GraphQL request for metadata and summary, without a REST check or trace download."""
    # Arrange
    tail_route = mock_tail("first\nlast")
    trace_route = mock_head("not used")

    # Act
    data = await call_log()

    # Assert
    assert tail_route.call_count == 1
    assert trace_route.call_count == 0
    assert len(respx.calls) == ONE_CALL
    sent = json.loads(tail_route.calls.last.request.content)
    assert "htmlSummary(lastLines: 100)" in sent["query"]
    assert "name" in sent["query"]
    assert "status" in sent["query"]
    assert "$jobId: JobID!" in sent["query"]
    assert sent["variables"] == {"project": PROJECT, "jobId": "gid://gitlab/Ci::Build/42"}
    assert data == {
        "job_id": 42,
        "name": "unit",
        "status": "success",
        "view": "tail",
        "source": "graphql_html_summary",
        "text": "first\nlast",
        "chars": 10,
        "truncated": False,
        "complete": None,
        "note": TAIL_NOTE,
    }


@pytest.mark.parametrize(
    ("name", "status", "expected_name", "expected_status"),
    [(None, None, None, "unknown"), ("unit", "RUNNING", "unit", "running")],
    ids=["nullable-metadata", "lowercase-status"],
)
@respx.mock
async def test_tail_returns_graphql_metadata_with_nullable_fields(name, status, expected_name, expected_status):
    """Tail keeps a nullable name and reports a missing status as unknown."""
    # Arrange
    tail_route = mock_tail("partial output", name=name, status=status)

    # Act
    data = await call_log()

    # Assert
    assert data["name"] == expected_name
    assert data["status"] == expected_status
    assert tail_route.call_count == ONE_CALL
    assert len(respx.calls) == ONE_CALL


@respx.mock
async def test_head_reads_trace_prefix_after_one_rest_check():
    """Head makes exactly two calls: the REST job check, then one raw trace read with no GraphQL call."""
    # Arrange
    job_route = mock_job()
    trace_route = mock_head("first\r\nlast")
    tail_route = mock_tail("not used")

    # Act
    data = await call_log(view="head")

    # Assert
    assert job_route.call_count == 1
    assert trace_route.call_count == 1
    assert tail_route.call_count == 0
    assert len(respx.calls) == TWO_CALLS
    assert data == {
        "job_id": 42,
        "name": "unit",
        "status": "success",
        "view": "head",
        "source": "rest_trace_prefix",
        "text": "first\nlast",
        "chars": 10,
        "truncated": False,
        "complete": True,
        "note": HEAD_NOTE,
    }


@pytest.mark.parametrize("view", ["tail", "head"])
@pytest.mark.parametrize("status", ["running", "pending"])
@respx.mock
async def test_unfinished_job_still_reads_its_log(view, status):
    """A running or pending job is not cut short, because partial output is still useful."""
    # Arrange
    if view == "tail":
        log_route = mock_log(view, "partial output", status=status)
    else:
        mock_job(status=status)
        log_route = mock_log(view, "partial output")

    # Act
    data = await call_log(view=view)

    # Assert
    assert log_route.call_count == 1
    assert data["status"] == status
    assert data["text"] == "partial output"


@pytest.mark.parametrize(
    ("view", "arguments"),
    [
        ("tail", {}),
        ("head", {}),
        ("search", {"pattern": "output"}),
        ("section", {"section": "build"}),
    ],
)
@respx.mock
async def test_skipped_job_stops_before_any_log_read(view, arguments):
    """A skipped job never ran, so no log view makes a trace request."""
    # Arrange
    if view == "tail":
        job_route = None
        tail_route = mock_tail(None, status="skipped")
    else:
        job_route = mock_job(status="skipped")
        tail_route = mock_tail("should not be read")
    trace_route = mock_head("should not be read")

    # Act / Assert
    with pytest.raises(ToolError, match=r"skipped.*never ran.*no log"):
        await call_log(view=view, **arguments)
    assert (job_route.call_count if job_route is not None else 0) == (0 if view == "tail" else 1)
    assert trace_route.call_count == 0
    assert tail_route.call_count == (1 if view == "tail" else 0)
    assert len(respx.calls) == ONE_CALL


# ---------- missing job, missing log, and empty log ----------


@pytest.mark.parametrize("view", ["tail", "head"])
@respx.mock
async def test_missing_job_stops_before_any_log_read(view):
    """A missing job raises a clear error without requesting its log."""
    # Arrange
    if view == "tail":
        log_route = respx.post(GRAPHQL).mock(
            return_value=httpx.Response(200, json={"data": {"project": {"job": None}}}),
        )
    else:
        respx.get(JOB_URL).mock(return_value=httpx.Response(404, json={"message": "404 Not found"}))
        log_route = mock_log(view, "should not be read")

    # Act / Assert
    with pytest.raises(ToolError, match="was not found") as exc_info:
        await call_log(view=view)
    if view == "tail":
        message = str(exc_info.value).lower()
        assert "trigger jobs" in message
        assert "external status checks" in message
        assert log_route.call_count == 1
    else:
        assert log_route.call_count == 0
    assert len(respx.calls) == 1


@respx.mock
async def test_head_trace_404_is_reported_as_api_error():
    """A 404 on the raw trace is passed on as the API error, not guessed as erased or expired."""
    # Arrange
    mock_job()
    respx.get(TRACE_URL).mock(return_value=httpx.Response(404, json={"message": "404 Not Found"}))

    # Act / Assert
    with pytest.raises(ToolError, match="HTTP 404"):
        await call_log(view="head")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"project": None}, "not found"),
        ({"project": {"job": None}}, "was not found"),
        ({"project": {"job": {"trace": None}}}, "did not return a trace"),
        ({"project": {"job": {"trace": {"htmlSummary": None}}}}, "did not return a trace"),
    ],
    ids=["null-project", "null-job", "null-trace", "null-summary"],
)
@respx.mock
async def test_null_graphql_fields_are_unavailable_not_empty(payload, message):
    """A null project, job, trace, or summary is an error, so it never looks like an empty log."""
    # Arrange
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": payload}))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_log()


@pytest.mark.parametrize("view", ["tail", "head"])
@respx.mock
async def test_empty_log_is_empty_text_not_an_error(view):
    """An empty log gives empty text. Tail cannot prove completeness, so it stays null."""
    # Arrange
    if view == "head":
        mock_job()
    mock_log(view, "")

    # Act
    data = await call_log(view=view)

    # Assert
    assert data["text"] == ""
    assert data["chars"] == 0
    assert data["truncated"] is False
    assert data["complete"] is (True if view == "head" else None)


# ---------- GraphQL errors ----------


@respx.mock
async def test_missing_summary_field_suggests_head():
    """An old GitLab without the summary field gets a clear hint to use view='head'."""
    # Arrange
    error = "Field 'htmlSummary' doesn't exist on type 'CiTrace'"
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"errors": [{"message": error}], "data": None}))

    # Act / Assert
    with pytest.raises(ToolError, match="view='head'") as exc_info:
        await call_log()
    assert "doesn't exist on type" in str(exc_info.value)


@respx.mock
async def test_other_graphql_errors_are_not_hidden():
    """Any other GraphQL error is raised as it is, without the head hint."""
    # Arrange
    error = {"message": "Permission denied"}
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"errors": [error], "data": None}))

    # Act / Assert
    with pytest.raises(ToolError, match="Permission denied") as exc_info:
        await call_log()
    assert "view='head'" not in str(exc_info.value)


@respx.mock
async def test_error_bodies_do_not_leak_log_text():
    """An error from the raw trace read never includes the log text it was reading."""
    # Arrange
    mock_job()
    respx.get(TRACE_URL).mock(return_value=httpx.Response(500, text="SECRET-LOG-LINE token=abc"))

    # Act / Assert
    with pytest.raises(ToolError) as exc_info:
        await call_log(view="head")
    assert "HTTP 500" in str(exc_info.value)
    assert "SECRET-LOG-LINE" not in str(exc_info.value)


# ---------- arguments ----------


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_chars": 0},
        {"max_chars": 16001},
        {"project": "  "},
        {"job_id": 0},
    ],
    ids=["max-chars-low", "max-chars-high", "blank-project", "job-id-zero"],
)
@respx.mock
async def test_invalid_arguments_fail_before_any_request(arguments):
    """Bad limits, project paths, or job IDs are rejected before GitLab is called."""
    # Arrange
    job_route = mock_job()
    tail_route = mock_tail("unused")

    # Act / Assert
    with pytest.raises(ToolError):
        await call_log(**arguments)
    assert job_route.call_count == 0
    assert tail_route.call_count == 0
    assert len(respx.calls) == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"view": "search"},
        {"view": "search", "pattern": ""},
        {"view": "search", "pattern": "two\nlines"},
        {"view": "search", "pattern": "x" * 201},
        {"view": "search", "pattern": "match", "section": "build"},
        {"view": "search", "pattern": "match", "context": -1},
        {"view": "search", "pattern": "match", "context": 6},
        {"view": "search", "pattern": "match", "byte_offset": -1},
        {"view": "section"},
        {"view": "section", "section": "bad name"},
        {"view": "section", "section": "build", "pattern": "x"},
        {"view": "section", "section": "build", "context": 0},
        {"view": "section", "section": "build", "byte_offset": 0},
        {"view": "search", "pattern": "x", "scan_bytes": 0},
        {"view": "section", "section": "build", "scan_bytes": 1048577},
        {"view": "head", "pattern": "x"},
        {"view": "head", "byte_offset": 0},
        {"view": "tail", "section": "build"},
        {"view": "tail", "byte_offset": 0},
        {"view": "head", "context": 0},
        {"view": "tail", "scan_bytes": 10},
    ],
)
@respx.mock
async def test_advanced_options_are_validated_before_any_request(arguments):
    """Missing, incompatible, and out-of-range options fail before a GitLab request."""
    # Arrange
    job_route = mock_job()
    trace_route = mock_head("unused")
    tail_route = mock_tail("unused")

    # Act / Assert
    with pytest.raises(ToolError):
        await call_log(**arguments)
    assert job_route.call_count == 0
    assert trace_route.call_count == 0
    assert tail_route.call_count == 0
    assert len(respx.calls) == 0


# ---------- character limit ----------


@pytest.mark.parametrize(
    ("view", "body", "max_chars", "expected"),
    [
        ("tail", "0123456789", 4, {"text": "6789", "truncated": True, "complete": None}),
        ("head", "0123456789", 4, {"text": "0123", "truncated": True, "complete": False}),
        ("tail", "abc", 4, {"text": "abc", "truncated": False, "complete": None}),
        ("head", "abc", 4, {"text": "abc", "truncated": False, "complete": True}),
    ],
    ids=["tail-cut", "head-cut", "tail-fits", "head-fits"],
)
@respx.mock
async def test_max_chars_keeps_the_end_for_tail_and_the_start_for_head(view, body, max_chars, expected):
    """Tail keeps the last characters and head keeps the first. Both flags follow the cut."""
    # Arrange
    if view == "head":
        mock_job()
    mock_log(view, body)

    # Act
    data = await call_log(view=view, max_chars=max_chars)

    # Assert
    assert {key: data[key] for key in expected} == expected
    assert data["chars"] == len(expected["text"])
    assert len(respx.calls) == (ONE_CALL if view == "tail" else TWO_CALLS)


# ---------- head byte cap ----------


@pytest.mark.parametrize(
    ("body", "truncated", "complete"),
    [
        (ESCAPE * 8192, False, True),
        (ESCAPE * 8192 + "VISIBLE", True, False),
    ],
    ids=["exactly-at-cap", "one-byte-over-cap"],
)
@respx.mock
async def test_head_reads_at_most_32_kib_of_raw_bytes(body, truncated, complete):
    """Head reads at most 32 KiB of raw bytes. Text after that point is never seen, even if it is visible."""
    # Arrange
    mock_job()
    mock_head(body)

    # Act
    data = await call_log(view="head")

    # Assert
    assert data["text"] == ""
    assert "VISIBLE" not in data["text"]
    assert data["truncated"] is truncated
    assert data["complete"] is complete


@respx.mock
async def test_head_cut_inside_a_utf8_character_becomes_a_replacement_character():
    """A byte cut that splits a multi-byte character decodes to U+FFFD and does not fail."""
    # Arrange
    mock_job()
    # 8190 escapes (32760 bytes), 7 letters (32767 bytes), then "é" (2 bytes). The cut keeps only its first byte.
    mock_head((ESCAPE * 8190 + "a" * 7 + "é").encode())

    # Act
    data = await call_log(view="head")

    # Assert
    assert data["text"] == "a" * 7 + "\ufffd"
    assert data["truncated"] is True
    assert data["complete"] is False


# ---------- bounded prefix selection ----------


@respx.mock
async def test_search_finds_output_beyond_the_old_32_kib_head_limit():
    """Search scans its larger bounded prefix and reports the scanned byte count."""
    # Arrange
    body = "x\n" * 16500 + "target found here\n"
    mock_job()
    mock_head(body)

    # Act
    data = await call_log(view="search", pattern="target", context=0)

    # Assert
    assert data["text"] == "target found here\n"
    assert data["found"] is True
    assert data["scanned_bytes"] == len(body.encode())
    assert data["scan_truncated"] is False
    assert data["truncated"] is False
    assert data["complete"] is True
    assert data["source"] == "rest_trace_prefix"
    assert len(respx.calls) == TWO_CALLS


@respx.mock
async def test_search_continues_from_raw_offset_and_finds_a_later_match():
    """Search resumes after complete raw lines and finds a match beyond the initial prefix."""
    # Arrange
    body = b"first\nhit\n"
    job_route = mock_job()
    range_route = mock_trace_window(b"hit\n", offset=6, total=len(body), max_bytes=6)
    prefix_route = mock_head(body)

    # Act
    first = await call_log(view="search", pattern="hit", context=0, scan_bytes=6, byte_offset=0)
    second = await call_log(view="search", pattern="hit", context=0, scan_bytes=6, byte_offset=first["next_offset"])

    # Assert
    assert first["found"] is False
    assert first["next_offset"] == len(b"first\n")
    assert first["scan_truncated"] is True
    assert prefix_route.call_count == 1
    assert range_route.call_count == 1
    assert range_route.calls.last.request.headers["Range"] == "bytes=6-12"
    assert second["text"] == "hit\n"
    assert second["found"] is True
    assert second["scan_truncated"] is False
    assert "next_offset" not in second
    assert second["complete"] is False
    assert job_route.call_count == TWO_CALLS


@respx.mock
async def test_search_next_offset_uses_raw_bytes_before_cleaning_and_drops_partial_line():
    """A continuation offset counts raw bytes, including removed controls, not cleaned text characters."""
    # Arrange
    body = b"\x1b[31mone\n\xc3\xa9more\nhit\n"
    scan_bytes = len(b"\x1b[31mone\n\xc3")
    mock_job()
    mock_head(body)

    # Act
    data = await call_log(view="search", pattern="missing", context=0, scan_bytes=scan_bytes)

    # Assert
    assert data["text"] == ""
    assert data["found"] is False
    assert data["scanned_bytes"] == scan_bytes
    assert data["next_offset"] == len(b"\x1b[31mone\n")
    assert data["next_offset"] != len("one\n")
    assert data["scan_truncated"] is True


@respx.mock
async def test_search_rejects_cut_window_without_a_complete_line():
    """A cut window with no complete line fails instead of returning a stuck continuation cursor."""
    # Arrange
    mock_job()
    mock_head(b"partial line with more bytes")

    # Act / Assert
    with pytest.raises(ToolError, match=r"(?i)increase scan_bytes"):
        await call_log(view="search", pattern="partial", context=0, scan_bytes=7)


@respx.mock
async def test_search_nonzero_offset_errors_when_gitlab_ignores_range():
    """A server that ignores a nonzero Range request cannot silently return prefix search results."""
    # Arrange
    mock_job()
    ignored_range_response = mock_head("matching prefix\n")

    # Act / Assert
    with pytest.raises(ToolError, match="does not support bounded trace continuation"):
        await call_log(view="search", pattern="matching", byte_offset=10)
    assert ignored_range_response.call_count == 1
    assert len(respx.calls) == TWO_CALLS
    assert respx.calls.last.request.headers["Range"] == "bytes=10-262154"


@respx.mock
async def test_search_at_nonzero_offset_masks_credentials_before_matching():
    """A resumed search matches masked text and never returns the raw credential."""
    # Arrange
    mock_job()
    range_route = mock_trace_window("token=secret\n", offset=12, total=25)

    # Act
    data = await call_log(view="search", pattern="[REDACTED]", context=0, byte_offset=12)

    # Assert
    assert range_route.call_count == 1
    assert data["text"] == "token=[REDACTED]\n"
    assert "secret" not in data["text"]
    assert data["found"] is True
    assert data["complete"] is False


@respx.mock
async def test_search_at_nonzero_offset_at_eof_has_no_next_offset():
    """An uncut nonzero range at EOF has no continuation cursor but is not whole-trace complete."""
    # Arrange
    mock_job()
    range_route = mock_trace_window("last line\n", offset=12, total=22)

    # Act
    data = await call_log(view="search", pattern="last", context=0, byte_offset=12)

    # Assert
    assert range_route.call_count == 1
    assert data["text"] == "last line\n"
    assert data["found"] is True
    assert data["scan_truncated"] is False
    assert data["complete"] is False
    assert "next_offset" not in data


@pytest.mark.parametrize(
    ("body", "pattern", "context", "expected_text"),
    [
        (
            "zero\none\nA.* first\nthree\na.* second\nfive\nsix\n",
            "A.*",
            1,
            "one\nA.* first\nthree\na.* second\nfive\n",
        ),
        (
            (
                "first\nhit one\nmiddle\nunrelated gap one\nunrelated gap two\ncontext before second\n"
                "hit two\nafter\nlast\n"
            ),
            "hit",
            1,
            "first\nhit one\nmiddle\ncontext before second\nhit two\nafter\n",
        ),
    ],
    ids=["overlapping-casefold-literal-windows", "disjoint-context-windows"],
)
@respx.mock
async def test_search_merges_context_windows_in_source_order(body, pattern, context, expected_text):
    """Search handles literal case-folded patterns and keeps only selected context windows."""
    # Arrange
    mock_job()
    mock_head(body)

    # Act
    data = await call_log(view="search", pattern=pattern, context=context)

    # Assert
    assert data["text"] == expected_text
    assert data["found"] is True
    assert data["complete"] is True


@respx.mock
async def test_section_returns_first_exact_section_with_nested_content():
    """Section selection is exact, keeps nested output, and ends only at the selected outer marker."""
    # Arrange
    body = (
        "before\n"
        "section_start:1:build[collapsed=true]\x1b[0K\r"
        "first\r"
        "section_start:2:child\x1b[0K\n"
        "nested\n"
        "section_end:3:child\x1b[0K\n"
        "section_end:4:build\x1b[0K\n"
        "between\n"
        "section_start:5:build-more\nwrong\nsection_end:6:build-more\n"
        "section_start:7:build\nsecond occurrence\nsection_end:8:build\n"
        "after\n"
    )
    mock_job()
    mock_head(body)

    # Act
    data = await call_log(view="section", section="build")

    # Assert
    assert data["text"] == "first\nnested\n"
    assert data["found"] is True
    assert data["truncated"] is False
    assert data["complete"] is True
    assert len(respx.calls) == TWO_CALLS


@pytest.mark.parametrize(
    ("body", "arguments", "expected_text", "found", "truncated"),
    [
        ("some output\n", {"view": "search", "pattern": "absent"}, "", False, False),
        ("no section\n", {"view": "section", "section": "build"}, "", False, False),
        (
            "section_start:1:Build\nwrong case\nsection_end:2:Build\n",
            {"view": "section", "section": "build"},
            "",
            False,
            False,
        ),
        (
            "section_start:1:build-extra\nwrong prefix\nsection_end:2:build-extra\n",
            {"view": "section", "section": "build"},
            "",
            False,
            False,
        ),
        (
            "section_start:1:build\npartial output\n",
            {"view": "section", "section": "build"},
            "partial output\n",
            True,
            True,
        ),
        (
            "section_start:1:build\navailable content\nsection_end:2:build\n",
            {
                "view": "section",
                "section": "build",
                "scan_bytes": len(b"section_start:1:build\navailable content\n"),
            },
            "available content\n",
            True,
            True,
        ),
        (
            "unterminated target",
            {"view": "search", "pattern": "target", "context": 0},
            "unterminated target",
            True,
            False,
        ),
    ],
    ids=[
        "search-missing",
        "section-missing",
        "section-case-mismatch",
        "section-prefix-mismatch",
        "section-unclosed",
        "section-cut-before-end",
        "unterminated-eof-line",
    ],
)
@respx.mock
async def test_missing_and_unclosed_selection_results(body, arguments, expected_text, found, truncated):
    """Missing selections are empty, while an opened unclosed section is marked incomplete."""
    # Arrange
    mock_job()
    mock_head(body)

    # Act
    data = await call_log(**arguments)

    # Assert
    assert data["text"] == expected_text
    assert data["found"] is found
    assert data["truncated"] is truncated
    assert data["complete"] is (not truncated)


@pytest.mark.parametrize(
    ("body", "scan_bytes", "arguments", "expected_text", "found"),
    [
        (
            b"complete line\npartial target",
            len(b"complete line\npartial"),
            {"view": "search", "pattern": "partial", "context": 0},
            "",
            False,
        ),
        (
            b"complete line\nsection_start:1:build",
            len(b"complete line\nsection_start:1:bu"),
            {"view": "section", "section": "build"},
            "",
            False,
        ),
        (
            "kept\né".encode(),
            len("kept\né".encode()) - 1,
            {"view": "search", "pattern": "replacement", "context": 0},
            "",
            False,
        ),
    ],
    ids=[
        "partial-search-line",
        "partial-section-marker",
        "partial-utf8-line",
    ],
)
@respx.mock
async def test_cut_prefix_drops_incomplete_final_raw_line(body, scan_bytes, arguments, expected_text, found):
    """A byte-cut partial line cannot become a match, marker, or replacement-character output."""
    # Arrange
    mock_job()
    mock_head(body)

    # Act
    data = await call_log(**arguments, scan_bytes=scan_bytes)

    # Assert
    assert data["text"] == expected_text
    assert data["found"] is found
    assert data["scanned_bytes"] == scan_bytes
    assert data["scan_truncated"] is True
    assert data["truncated"] is True
    assert data["complete"] is False


@respx.mock
async def test_search_matches_redacted_display_text_before_keeping_earliest_max_chars():
    """Search sees cleaned redacted text, and max_chars keeps its earliest selected characters."""
    # Arrange
    mock_job()
    mock_head("token=secretXYZ\nsecond line\n")

    # Act
    data = await call_log(view="search", pattern="[REDACTED]", context=1, max_chars=SEARCH_OUTPUT_CAP)

    # Assert
    assert data["text"] == "token=[R"
    assert data["chars"] == SEARCH_OUTPUT_CAP
    assert data["found"] is True
    assert data["truncated"] is True
    assert data["complete"] is False


# ---------- cleaning ----------


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("\x1b[31mred\x1b[0m", "red"),
        ("\x1b]8;;https://example.com\x07link\x1b]8;;\x07", "link"),
        ("\x1b]0;title\x1b\\text", "text"),
        ("a\x00b\x07c\x7fd\x85e", "abcde"),
        ("one\r\ntwo\rthree", "one\ntwo\nthree"),
        ("a\tb", "a\tb"),
        ("abc\u202ed\u2066e\u200fF", "abcdeF"),
        ("x\x1b[31mred", "xred"),
        ("x&#13;y", "x\ny"),
    ],
    ids=[
        "csi-color",
        "osc-bel",
        "osc-st",
        "c0-c1-controls",
        "crlf-and-cr",
        "tab-kept",
        "bidi-controls",
        "raw-escape-in-html",
        "cr-from-entity",
    ],
)
@respx.mock
async def test_tail_removes_terminal_codes_and_controls(summary, expected):
    """Terminal codes and control characters are removed. Tabs and line breaks are kept as LF."""
    # Arrange
    mock_tail(summary)

    # Act
    data = await call_log()

    # Assert
    assert data["text"] == expected
    assert len(respx.calls) == ONE_CALL


@pytest.mark.parametrize("view", ["tail", "head"])
@respx.mock
async def test_runner_prefixes_and_section_markers_are_removed_after_ansi_cleanup(view):
    """Runner prefixes and marker lines are removed, while ordinary timestamps and embedded markers stay."""
    # Arrange
    log = (
        ESCAPE
        + "2026-10-23T21:41:17.313023Z 01O Running job\x1b[0m\n"
        + "section_start:1761255677:prepare_script\x1b[0K\n"
        + "ordinary timestamp 2026-10-23T21:41:17Z is text\n"
        + "literal section_end: stays when embedded\n"
        + "section_end:1761255678:prepare_script\x1b[0K\n"
        + "2026-10-23T21:41:18.000001Z 02E runner error\n"
    )
    if view == "head":
        mock_job()
    mock_log(view, log)

    # Act
    data = await call_log(view=view)

    # Assert
    assert data["text"] == (
        "Running job\n"
        "ordinary timestamp 2026-10-23T21:41:17Z is text\n"
        "literal section_end: stays when embedded\n"
        "runner error\n"
    )
    assert len(respx.calls) == (ONE_CALL if view == "tail" else TWO_CALLS)


@pytest.mark.parametrize("view", ["tail", "head"])
@pytest.mark.parametrize(
    ("credential", "expected"),
    [
        ("glpat-abc123XYZ", "[REDACTED]"),
        ("Bearer AbCd123._-", "Bearer [REDACTED]"),
        ("bEaReR abc/def+ghi==", "bEaReR [REDACTED]"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_123", "[REDACTED]"),
        ("ToKeN=abc123XYZ&next=ok", "ToKeN=[REDACTED]&next=ok"),
    ],
    ids=["gitlab-token", "bearer-token", "bearer-rfc6750", "jwt", "token-parameter"],
)
@respx.mock
async def test_common_credentials_are_masked_in_both_views(view, credential, expected):
    """Common credential forms are masked before either bounded view is returned."""
    # Arrange
    if view == "head":
        mock_job()
    mock_log(view, f"before {credential} after")

    # Act
    data = await call_log(view=view)

    # Assert
    assert data["text"] == f"before {expected} after"
    assert len(respx.calls) == (ONE_CALL if view == "tail" else TWO_CALLS)


@pytest.mark.parametrize("view", ["tail", "head"])
@pytest.mark.parametrize("text", ["v1.2.3", "registry.example.com"])
@respx.mock
async def test_jwt_cleanup_preserves_versions_and_hostnames(view, text):
    """Dot-separated versions and hostnames are not JWTs without the JSON-header prefix."""
    # Arrange
    if view == "head":
        mock_job()
    mock_log(view, text)

    # Act
    data = await call_log(view=view)

    # Assert
    assert data["text"] == text
    assert len(respx.calls) == (ONE_CALL if view == "tail" else TWO_CALLS)


@pytest.mark.parametrize("view", ["tail", "head"])
@respx.mock
async def test_ordinary_text_and_punctuation_are_not_masked(view):
    """Cleanup keeps normal timestamps, noncredential bearer text, and punctuation around parameters."""
    # Arrange
    body = "at 2026-10-23T21:41:17.313023Z; Bearer: is a label; token=, and glpat is plain text."
    if view == "head":
        mock_job()
    mock_log(view, body)

    # Act
    data = await call_log(view=view)

    # Assert
    assert data["text"] == body
    assert len(respx.calls) == (ONE_CALL if view == "tail" else TWO_CALLS)


@pytest.mark.parametrize(
    ("view", "body"),
    [
        ("head", "token=secretXYZ tail"),
        ("tail", "prefix token=secretXYZ"),
    ],
    ids=["head-redaction-before-cap", "tail-redaction-before-cap"],
)
@respx.mock
async def test_redaction_happens_before_character_cap(view, body):
    """Character limits and truncation reflect cleaned text, not the longer unmasked credential."""
    # Arrange
    if view == "head":
        mock_job()
    mock_log(view, body)

    # Act
    data = await call_log(view=view, max_chars=REDACTED_TOKEN_CHARS)

    # Assert
    assert data["text"] == "token=[REDACTED]"
    assert data["chars"] == REDACTED_TOKEN_CHARS
    assert data["truncated"] is True
    assert data["complete"] is (False if view == "head" else None)
    assert len(respx.calls) == (ONE_CALL if view == "tail" else TWO_CALLS)


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ('<span class="a">one <span>two</span></span><br>three', "one two\nthree"),
        ("a<br/>b<BR>c", "a\nb\nc"),
        ("&lt;b&gt;bold&lt;/b&gt; &amp;lt;", "<b>bold</b> &lt;"),
        ("&lt;script&gt;alert(1)&lt;/script&gt;", "<script>alert(1)</script>"),
        ("<script>alert(1)</script>after<style>p{}</style>!", "after!"),
        ("before<!-- hidden -->after", "beforeafter"),
    ],
    ids=[
        "nested-spans-and-br",
        "br-variants",
        "entities-decoded-once",
        "escaped-script-stays-text",
        "script-removed",
        "comment",
    ],
)
@respx.mock
async def test_tail_turns_markup_into_plain_text(summary, expected):
    """Tags are dropped, `<br>` becomes a newline, and entities are decoded once without being parsed again."""
    # Arrange
    mock_tail(summary)

    # Act
    data = await call_log()

    # Assert
    assert data["text"] == expected
    assert len(respx.calls) == ONE_CALL


def markup_summary(line_count: int) -> tuple[str, str]:
    """Build a summary of `line_count` lines of 100 characters each. Return `(html, visible_text)`.

    Each line is wrapped in a span, so the markup is much longer than the text it shows.
    """
    lines = [f"L{number:04d} " + "x" * 94 for number in range(line_count)]
    html = "<br>".join(f'<span class="log-line">{line}</span>' for line in lines)
    return html, "\n".join(lines)


@pytest.mark.parametrize(
    ("line_count", "expect_truncated"),
    [(130, False), (200, True)],
    ids=["text-fits-under-16000", "text-over-16000"],
)
@respx.mock
async def test_long_markup_is_parsed_in_full_before_the_final_cut(line_count, expect_truncated):
    """Markup over 16 KiB keeps all its visible text. Only the final character cut can drop text."""
    # Arrange
    html, visible = markup_summary(line_count)
    assert len(html.encode()) > SIXTEEN_KIB  # The raw markup alone is larger than 16 KiB.
    mock_tail(html)

    # Act
    data = await call_log(max_chars=16000)

    # Assert
    assert data["text"] == visible[-16000:]
    assert data["truncated"] is expect_truncated
    assert data["complete"] is None
    assert len(respx.calls) == ONE_CALL
