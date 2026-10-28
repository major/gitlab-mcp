"""Behavioral regression tests for actionable pipeline failure excerpts 🧯."""

from typing import Any

import httpx
import pytest
import respx

from gitlab_mcp import ci_logs
from mcp_helpers import call_tool

pytestmark = pytest.mark.usefixtures("gitlab_env")

API = "https://gitlab.example.com/api/v4"
GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
JOBS_URL = f"{API}/projects/group%2Fproject/pipelines/901/jobs"
MAX_SCAN_BYTES = 1048576
MAX_EXCERPT_CHARS = 1000


def job(job_id: int) -> dict[str, Any]:
    """Build a failed REST job with a known ID."""
    return {"id": job_id, "name": f"job-{job_id}", "stage": "test", "status": "failed"}


def trace(job_id: int, response: httpx.Response) -> respx.Route:
    """Mock exactly one job's trace request."""
    return respx.get(f"{API}/projects/group%2Fproject/jobs/{job_id}/trace").mock(return_value=response)


def summaries(*values: str | None) -> dict[str, Any]:
    """Build a batched GraphQL response in job order."""
    return {
        "data": {
            "project": {f"job_{index}": {"trace": {"htmlSummary": value}} for index, value in enumerate(values)},
        },
    }


@pytest.mark.parametrize(
    "summary",
    [
        "ERROR: Job failed: exit code 1\n",
        "===== 145 passed, 2 warnings in 2.1s =====\nERROR: Job failed: exit code 1\n",
        "make: *** [check] Error 2.\nERROR: Job failed: exit code 1\n",
        None,
    ],
    ids=["runner", "passing-tests", "make-only", "missing-summary"],
)
@respx.mock
async def test_generic_tail_falls_back_to_early_actionable_errors(summary):
    """A runner footer, passing totals, make-only tail, or absent tail cannot hide early Pyright errors."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    graphql = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries(summary)))
    raw = (
        "make -k check\n"
        "/src/app.py:1:1 - error: bad type token=secret\n"
        "/src/app.py:2:1 - error: unknown name\n"
        "make: *** [typecheck] Error 1\n"
        "===== 145 passed in 2.1s =====\nUploading artifacts\nERROR: Job failed: exit code 1\n"
    )
    route = trace(11, httpx.Response(200, text=raw))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    log = data["logs"][0]
    assert "/src/app.py:1:1 - error: bad type token=[REDACTED]" in log["text"]
    assert "make: *** [typecheck] Error 1" in log["text"]
    assert "Uploading artifacts" not in log["text"]
    assert "secret" not in log["text"]
    assert log["source"] == ("trace_prefix_and_summary" if summary and "[check]" in summary else "trace_prefix")
    assert log["scanned_bytes"] == len(raw.encode())
    assert log["scan_truncated"] is False
    assert graphql.call_count == route.call_count == 1
    assert len(respx.calls) == 1 + graphql.call_count + route.call_count


@pytest.mark.parametrize(
    "summary",
    [
        "tests/test_parse.py::test_invalid[ValueError: expected] PASSED\n",
        "tests/test_parse.py::test_invalid[ValueError: expected] PASSED [ 42%]\n",
        "PASSED tests/test_parse.py::test_invalid[ValueError: expected]\n",
        "[gw0] [ 42%] PASSED tests/test_parse.py::test_invalid[ValueError: expected]\n",
        (
            "================ warnings summary ================\n"
            "tests/test_socket.py::test_read\n"
            "  SocketBlockedError: A test tried to use a socket\n"
            "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
            "================ 145 passed, 1 warning in 2.1s ================\n"
        ),
        (
            "================ warnings summary ================\n"
            "Traceback (most recent call last):\n"
            '  File "test_socket.py", line 1\n'
            "  SocketBlockedError: A test tried to use a socket\n"
            "================ 145 passed, 1 warning in 2.1s ================\n"
        ),
        (
            "================ warnings summary (final) ================\n"
            "  SocketBlockedError: A test tried to use a socket\n"
            "================ 145 passed, 1 warning in 2.1s ================\n"
        ),
        (
            "tests/test_socket.py::test_read\n"
            "  SocketBlockedError: A test tried to use a socket\n"
            "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
            "145 passed, 1 warning in 2.1s\n"
        ),
    ],
    ids=[
        "passed-row",
        "passed-row-progress",
        "passed-first",
        "passed-xdist",
        "warning",
        "warning-traceback",
        "warning-final-title",
        "warning-header-outside-window",
    ],
)
@respx.mock
async def test_pytest_success_and_warnings_do_not_hide_earlier_failure(summary):
    """Passing test IDs and warnings are not enough evidence to skip the raw trace."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    respx.post(GRAPHQL).mock(
        return_value=httpx.Response(200, json=summaries(summary + "ERROR: Job failed: exit code 1\n")),
    )
    raw = (
        "tests/test_parse.py::test_invalid[ValueError: expected] PASSED\n"
        "/src/app.py:1 - error: incompatible type\n"
        "make: *** [typecheck] Error 1\n"
        + ("================ warnings summary ================\n" if summary.startswith("tests/test_socket.py") else "")
        + summary
        + "ERROR: Job failed: exit code 1\n"
    )
    route = trace(11, httpx.Response(200, text=raw))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    log = data["logs"][0]
    assert route.call_count == 1
    assert log["source"] == "trace_prefix"
    assert "/src/app.py:1 - error: incompatible type" in log["text"]
    assert "make: *** [typecheck] Error 1" in log["text"]
    assert "SocketBlockedError" not in log["text"]
    assert "ValueError: expected" not in log["text"]


@pytest.mark.parametrize(
    "boundary",
    [
        "145 passed, 1 warning in 2.1s\n",
        "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n",
    ],
    ids=["quiet-counts", "docs-footer"],
)
@pytest.mark.parametrize("source", ["summary", "trace_prefix"])
@respx.mock
async def test_diagnostics_after_warning_boundary_remain_actionable(boundary, source):
    """Quiet result counts and the docs footer end warnings before later real diagnostics."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    diagnostic = "/src/app.py:1 - error: incompatible type\nmake: *** [typecheck] Error 1\n"
    output = (
        "================ warnings summary ================\n"
        "  SocketBlockedError: A test tried to use a socket\n" + boundary + diagnostic
    )
    summary = output if source == "summary" else "ERROR: Job failed: exit code 1\n"
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries(summary)))
    route = trace(11, httpx.Response(200, text=output))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    log = data["logs"][0]
    assert route.call_count == (1 if source == "trace_prefix" else 0)
    assert log["source"] == source
    assert diagnostic == log["text"]
    assert "SocketBlockedError" not in log["text"]


@respx.mock
async def test_clipped_warning_header_only_excludes_lines_since_latest_known_boundary():
    """A docs footer excludes a clipped warning block without hiding causes on either side."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    before = "/src/early.py:1 - error: early type error\n"
    after = "/src/later.py:1 - error: later type error\n"
    target = "make: *** [typecheck] Error 1\n"
    summary = (
        before + "================ 12 passed in 0.1s ================\n"
        "SocketBlockedError: A test tried to use a socket\n"
        "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n" + after + target
    )
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries(summary)))
    route = trace(11, httpx.Response(200, text="not requested"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    log = data["logs"][0]
    assert route.call_count == 0
    assert log["source"] == "summary"
    assert log["text"] == before + after + target
    assert "SocketBlockedError" not in log["text"]


@pytest.mark.parametrize(
    "next_heading",
    [
        "================ short test summary info ================\n",
        "================ 145 passed, 1 warning in 2.1s ================\n",
    ],
    ids=["short-summary", "result-counts"],
)
@respx.mock
async def test_pytest_failure_after_warning_section_stays_on_fast_path(next_heading):
    """A new summary heading ends the warning block so later actual failures remain actionable."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    summary = (
        "================ warnings summary ================\n"
        "SocketBlockedError: A test tried to use a socket\n"
        + next_heading
        + "FAILED tests/test_parse.py::test_failure - AssertionError\n"
        "================ 1 failed, 1 warning in 2.1s ================\n"
    )
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries(summary)))
    route = trace(11, httpx.Response(200, text="not requested"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    log = data["logs"][0]
    assert route.call_count == 0
    assert log["source"] == "summary"
    assert "FAILED tests/test_parse.py::test_failure" in log["text"]
    assert "SocketBlockedError" not in log["text"]


@respx.mock
async def test_standalone_failures_view_keeps_its_existing_broad_marker_selection():
    """The raw failures view still matches broad error markers in their original order."""
    # Arrange
    url = f"{API}/projects/group%2Fproject/jobs/11"
    respx.get(url).mock(return_value=httpx.Response(200, json=job(11)))
    passed = "tests/test_parse.py::test_invalid[ValueError: expected] PASSED\n"
    warning = "SocketBlockedError: A test tried to use a socket\n"
    trace(11, httpx.Response(200, text=passed + warning))

    # Act
    data = await call_tool(
        "get_job_log",
        {"project": PROJECT, "job_id": 11, "view": "failures", "context": 0},
    )

    # Assert
    assert data["text"] == passed + warning
    assert data["found"] is True
    assert len(respx.calls) == 1 + 1


@respx.mock
async def test_fast_path_keeps_actionable_tail_without_trace():
    """An actual error in the last-100-line summary avoids a raw trace request."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    respx.post(GRAPHQL).mock(
        return_value=httpx.Response(200, json=summaries("artifact\nFAILED tests/test_x.py::test_a\ncleanup\n")),
    )
    route = trace(11, httpx.Response(200, text="not requested"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    assert data["logs"][0]["text"] == "FAILED tests/test_x.py::test_a\n"
    assert data["logs"][0]["source"] == "summary"
    assert "scanned_bytes" not in data["logs"][0]
    assert route.call_count == 0
    assert len(respx.calls) == 1 + 1


@respx.mock
async def test_traceback_summary_keeps_context_and_make_target_without_trace():
    """Traceback context and a failed make target accompany a real tail diagnostic."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    summary = (
        "Traceback (most recent call last):\n"
        '  File "test.py", line 1, in run\n'
        "    raise ValueError('bad')\n"
        "ValueError: bad\n"
        "make: *** [test] Error 1\n"
        "artifact upload\n"
    )
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries(summary)))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    log = data["logs"][0]
    assert log["source"] == "summary"
    assert 'File "test.py", line 1' in log["text"]
    assert "ValueError: bad" in log["text"]
    assert "make: *** [test]" in log["text"]
    assert "artifact upload" not in log["text"]
    assert len(respx.calls) == 1 + 1


@respx.mock
async def test_excerpt_budget_preserves_make_target_and_early_diagnostics():
    """A long selection keeps useful cause lines and the failed target inside the 1000-character cap."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries("ERROR: Job failed: exit code 1")))
    diagnostics = "".join(f"/src/file{i}.py:1 - error: " + "details " * 40 + "\n" for i in range(4))
    target = "make: *** [typecheck] Error 1\n"
    trace(11, httpx.Response(200, text=diagnostics + target + "ordinary success output\n"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    log = data["logs"][0]
    assert "/src/file0.py:1 - error:" in log["text"]
    assert target in log["text"]
    assert log["chars"] <= MAX_EXCERPT_CHARS
    assert log["truncated"] is True
    assert log["scan_truncated"] is False


@respx.mock
async def test_cut_prefix_drops_partial_line_and_returns_raw_continuation():
    """A partial last line is excluded and the next offset counts the complete raw lines only."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    tail = "ERROR: Job failed: token=secret\n"
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries(tail)))
    prefix = b"setup\n" + b"error: " + b"x" * (MAX_SCAN_BYTES - len(b"setup\nerror: "))
    trace(11, httpx.Response(200, content=prefix + b" more\n"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    log = data["logs"][0]
    assert log["text"] == "ERROR: Job failed: token=[REDACTED]\n"
    assert log["source"] == "summary"
    assert log["truncated"] is False
    assert log["scan_truncated"] is True
    assert log["scanned_bytes"] == MAX_SCAN_BYTES
    assert log["next_offset"] == len(b"setup\n")
    assert "view=failures" in log["hint"]


@respx.mock
async def test_cut_prefix_cause_keeps_later_make_target_from_summary():
    """A cut prefix and the last-100-line summary can provide complementary failure anchors."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    target = "make: *** [typecheck] Error 1"
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries(f"{target}\nERROR: Job failed\n")))
    cause = b"/src/app.py:1 - error: bad type\n"
    trace(11, httpx.Response(200, content=cause + b"x" * MAX_SCAN_BYTES))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    log = data["logs"][0]
    assert "/src/app.py:1 - error: bad type" in log["text"]
    assert target in log["text"]
    assert log["source"] == "trace_prefix_and_summary"
    assert log["scan_truncated"] is True
    assert log["next_offset"] == len(cause)
    assert log["truncated"] is False


@respx.mock
async def test_prefix_next_offset_can_resume_with_the_job_log_tool(monkeypatch):
    """The returned raw offset resumes at a cut line and finds its complete diagnostic."""
    # Arrange
    monkeypatch.setattr(ci_logs, "MAX_SCAN_BYTES", 24)
    body = b"\x1b[31msetup\nerror: continuation cause\n"
    offset = len(b"\x1b[31msetup\n")
    url = f"{API}/projects/group%2Fproject/jobs/11"
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries("ERROR: Job failed\n")))
    respx.get(url).mock(return_value=httpx.Response(200, json=job(11)))
    ranged = respx.get(f"{url}/trace", headers={"Range": f"bytes={offset}-{offset + 262144}"}).mock(
        return_value=httpx.Response(
            206,
            headers={"Content-Range": f"bytes {offset}-{len(body) - 1}/{len(body)}"},
            content=body[offset:],
        ),
    )
    prefix = trace(11, httpx.Response(200, content=body))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})
    log = data["logs"][0]
    resumed = await call_tool(
        "get_job_log",
        {"project": PROJECT, "job_id": 11, "view": "failures", "context": 0, "byte_offset": log["next_offset"]},
    )

    # Assert
    assert log["next_offset"] == offset
    assert log["scan_truncated"] is True
    assert log["source"] == "summary"
    assert resumed["text"] == "error: continuation cause\n"
    assert resumed["scan_truncated"] is False
    assert prefix.call_count == ranged.call_count == 1


@respx.mock
async def test_cut_prefix_with_no_complete_line_has_no_stuck_offset(monkeypatch):
    """A prefix cut within its first line cannot advertise a continuation that makes no progress."""
    # Arrange
    monkeypatch.setattr(ci_logs, "MAX_SCAN_BYTES", 8)
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries(None)))
    trace(11, httpx.Response(200, text="error: an incomplete line\n"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    log = data["logs"][0]
    assert log["available"] is True
    assert log["text"] == ""
    assert log["scan_truncated"] is True
    assert log["truncated"] is False
    assert "next_offset" not in log
    assert "outside this tool" in log["hint"]


@respx.mock
async def test_unavailable_summary_with_no_cause_returns_available_prefix():
    """A successful raw read remains available even when it contains no recognized cause."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries(None)))
    trace(11, httpx.Response(200, text="ordinary output\n"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    log = data["logs"][0]
    assert log["available"] is True
    assert log["source"] == "trace_prefix"
    assert log["text"] == "ordinary output\n"
    assert "more diagnostics" in log["hint"]


@respx.mock
async def test_small_excerpt_cap_is_respected_when_both_anchors_cannot_fit():
    """A tiny caller budget remains a hard limit even if it cannot hold both make and cause."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries("ERROR: Job failed\n")))
    trace(11, httpx.Response(200, text="error: missing file\nmake: *** [lint] Error 1\n"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901, "log_chars": 1})

    # Assert
    log = data["logs"][0]
    assert log["chars"] == len(log["text"]) == 1
    assert log["truncated"] is True
    assert log["scan_truncated"] is False


@respx.mock
async def test_graphql_error_and_per_job_trace_failures_keep_other_results():
    """A failed batch still scans each trace, and one trace error does not lose another job's cause."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11), job(12)]))
    respx.post(GRAPHQL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "Denied"}], "data": None}),
    )
    trace(11, httpx.Response(403, text="private log token=secret"))
    trace(12, httpx.Response(200, text="error: missing config token=secret\n"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    first, second = data["logs"]
    assert not first["available"]
    assert second["text"] == "error: missing config token=[REDACTED]\n"
    assert second["source"] == "trace_prefix"
    assert "Denied" in data["warning"]
    assert "11" in data["warning"]
    assert "token=secret" not in str(data)
    assert len(respx.calls) == 2 + len(data["logs"])


@respx.mock
async def test_trace_failure_preserves_available_masked_tail_and_warns():
    """An inaccessible trace retains the cleaned summary and reports the affected job."""
    # Arrange
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[job(11)]))
    respx.post(GRAPHQL).mock(
        return_value=httpx.Response(200, json=summaries("ERROR: Job failed: token=secret\n")),
    )
    trace(11, httpx.Response(404, text="private token=secret"))

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901})

    # Assert
    assert data["logs"][0]["text"] == "ERROR: Job failed: token=[REDACTED]\n"
    assert data["logs"][0]["source"] == "summary"
    assert data["logs"][0]["available"] is True
    assert "11" in data["warning"]
    assert "token=secret" not in str(data)


@respx.mock
async def test_ten_missing_causes_have_at_most_twelve_requests():
    """The largest failure page makes one REST list, one GraphQL batch, and ten trace reads."""
    # Arrange
    jobs = [job(number) for number in range(1, 11)]
    respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=jobs))
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=summaries(*([None] * 10))))
    routes = [trace(number, httpx.Response(200, text="error: missing config\n")) for number in range(1, 11)]

    # Act
    data = await call_tool("get_pipeline_failures", {"project": PROJECT, "pipeline_id": 901, "limit": 10})

    # Assert
    assert len(respx.calls) == 2 + len(jobs)
    assert all(route.call_count == 1 for route in routes)
    assert all(log["source"] == "trace_prefix" for log in data["logs"])
