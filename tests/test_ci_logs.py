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
TWO_CALLS = 2  # One REST job check plus one log read.
SIXTEEN_KIB = 16 * 1024
ESCAPE = "\x1b[1m"  # 4 bytes of terminal styling that becomes nothing once cleaned.


async def call_log(**arguments: Any) -> dict[str, Any]:
    """Call get_job_log through the MCP protocol and return its structured data."""
    async with Client(mcp) as client:
        result = await client.call_tool("get_job_log", {"project": PROJECT, "job_id": 42} | arguments)
    return result.structured_content


def mock_job(status: str = "success") -> respx.Route:
    """Mock the REST job check that runs before every log read."""
    body = {"id": 42, "name": "unit", "status": status}
    return respx.get(JOB_URL).mock(return_value=httpx.Response(200, json=body))


def mock_tail(summary: str | None) -> respx.Route:
    """Mock the GraphQL trace summary. A None summary is sent as a null field."""
    trace = {"htmlSummary": summary}
    payload = {"data": {"project": {"job": {"trace": trace}}}}
    return respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=payload))


def mock_head(body: str | bytes) -> respx.Route:
    """Mock the raw trace read. Text is sent as UTF-8."""
    content = body.encode() if isinstance(body, str) else body
    return respx.get(TRACE_URL).mock(return_value=httpx.Response(200, content=content))


def mock_log(view: str, body: str | bytes) -> respx.Route:
    """Mock the log read for `view`. Head gets the raw body, tail gets the same text as a summary."""
    if view == "head":
        return mock_head(body)
    return mock_tail(body.decode() if isinstance(body, bytes) else body)


# ---------- happy path and call count ----------


@respx.mock
async def test_tail_reads_one_graphql_summary_after_one_rest_check():
    """Tail makes exactly two calls: the REST job check, then one GraphQL summary read with no trace download."""
    # Arrange
    job_route = mock_job()
    tail_route = mock_tail("first\nlast")
    trace_route = mock_head("not used")

    # Act
    data = await call_log()

    # Assert
    assert job_route.call_count == 1
    assert tail_route.call_count == 1
    assert trace_route.call_count == 0
    assert len(respx.calls) == TWO_CALLS
    sent = json.loads(tail_route.calls.last.request.content)
    assert "htmlSummary(lastLines: 100)" in sent["query"]
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
    mock_job(status=status)
    log_route = mock_log(view, "partial output")

    # Act
    data = await call_log(view=view)

    # Assert
    assert log_route.call_count == 1
    assert data["status"] == status
    assert data["text"] == "partial output"


# ---------- missing job, missing log, and empty log ----------


@pytest.mark.parametrize("view", ["tail", "head"])
@respx.mock
async def test_missing_job_stops_before_any_log_read(view):
    """A 404 on the job check raises the job error and never reaches the log read."""
    # Arrange
    respx.get(JOB_URL).mock(return_value=httpx.Response(404, json={"message": "404 Not found"}))
    log_route = mock_log(view, "should not be read")

    # Act / Assert
    with pytest.raises(ToolError, match="was not found"):
        await call_log(view=view)
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
        ({"project": {"job": None}}, "did not return a trace"),
        ({"project": {"job": {"trace": None}}}, "did not return a trace"),
        ({"project": {"job": {"trace": {"htmlSummary": None}}}}, "did not return a trace"),
    ],
    ids=["null-project", "null-job", "null-trace", "null-summary"],
)
@respx.mock
async def test_null_graphql_fields_are_unavailable_not_empty(payload, message):
    """A null project, job, trace, or summary is an error, so it never looks like an empty log."""
    # Arrange
    mock_job()
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": payload}))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_log()


@pytest.mark.parametrize("view", ["tail", "head"])
@respx.mock
async def test_empty_log_is_empty_text_not_an_error(view):
    """An empty log gives empty text. Tail cannot prove completeness, so it stays null."""
    # Arrange
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
    mock_job()
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
    mock_job()
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
    mock_job()
    mock_log(view, body)

    # Act
    data = await call_log(view=view, max_chars=max_chars)

    # Assert
    assert {key: data[key] for key in expected} == expected
    assert data["chars"] == len(expected["text"])


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
    mock_job()
    mock_tail(summary)

    # Act
    data = await call_log()

    # Assert
    assert data["text"] == expected


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
    mock_job()
    mock_tail(summary)

    # Act
    data = await call_log()

    # Assert
    assert data["text"] == expected


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
    mock_job()
    mock_tail(html)

    # Act
    data = await call_log(max_chars=16000)

    # Assert
    assert data["text"] == visible[-16000:]
    assert data["truncated"] is expect_truncated
    assert data["complete"] is None
