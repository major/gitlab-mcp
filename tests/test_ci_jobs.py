"""Tests for the get_job tool, run through a real in-memory MCP client 🧰."""

from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp import mcp

API = "https://gitlab.example.com/api/v4"
PROJECT = "group/project"
JOB_URL = f"{API}/projects/group%2Fproject/jobs/42"
TEXT_LIMIT = 200  # Matches the cap for names, stages, and failure reasons in ci_metadata.


async def call_tool(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call get_job through the MCP protocol and return its structured data."""
    async with Client(mcp) as client:
        result = await client.call_tool("get_job", {"project": PROJECT, "job_id": 42} | (arguments or {}))
    return result.structured_content


def rest_job(**overrides: Any) -> dict[str, Any]:
    """Build one REST job with the fields the tool reads, plus large or private fields it must leave out."""
    job = {
        "id": 42,
        "name": "unit",
        "stage": "test",
        "status": "failed",
        "allow_failure": False,
        "failure_reason": "script_failure",
        "created_at": "2026-10-01T12:00:00.678Z",
        "started_at": "2026-10-01T12:01:00.789Z",
        "finished_at": "2026-10-01T12:02:30.500Z",
        "erased_at": None,
        "duration": 89.6,
        "queued_duration": 0.6,
        "web_url": "https://gitlab.example.com/group/project/-/jobs/42",
        "pipeline": {"id": 901, "project_id": 1, "ref": "main", "sha": "abc123", "status": "failed"},
        "runner": {"id": 7, "description": "shared-runner", "runner_type": "instance_type", "active": True},
        # Left out of the output: commits, users, artifacts, and tags are large or private.
        "ref": "main",
        "tag": False,
        "commit": {"id": "abc123", "message": "a long commit message"},
        "user": {"username": "jdoe", "email": "jdoe@example.com"},
        "artifacts": [{"file_type": "archive", "size": 1024}],
    }
    return job | overrides


# ---------- get_job ----------


@respx.mock
async def test_returns_timing_failure_and_runner_in_one_request(gitlab_env):
    """One REST call gives status, full timestamps, failure reason, and runner, with no log or heavy fields."""
    # Arrange
    route = respx.get(JOB_URL).mock(return_value=httpx.Response(200, json=rest_job()))

    # Act
    data = await call_tool()

    # Assert
    assert route.call_count == 1
    assert data == {
        "job_id": 42,
        "pipeline_id": 901,
        "name": "unit",
        "stage": "test",
        "status": "failed",
        "allow_failure": False,
        "failure_reason": "script_failure",
        "created_at": "2026-10-01T12:00:00Z",
        "started_at": "2026-10-01T12:01:00Z",
        "finished_at": "2026-10-01T12:02:30Z",
        "erased_at": None,
        "duration_s": 90,
        "queued_duration_s": 1,
        "runner": {"id": 7, "description": "shared-runner"},
        "web_url": "https://gitlab.example.com/group/project/-/jobs/42",
    }


@respx.mock
async def test_compacts_timezone_offsets_and_preserves_zero_and_null_durations(gitlab_env):
    """Timestamp precision and timezone offsets stay clear, while zero and null durations remain distinct."""
    # Arrange
    job = rest_job(
        created_at="2026-10-01T12:00:00.987+05:30",
        started_at="2026-10-01T12:01:00.654-04:00",
        finished_at="2026-10-01T12:02:30.321Z",
        erased_at="2026-10-02T09:00:00.123+00:00",
        duration=0,
        queued_duration=None,
    )
    respx.get(JOB_URL).mock(return_value=httpx.Response(200, json=job))

    # Act
    data = await call_tool()

    # Assert
    assert data["created_at"] == "2026-10-01T12:00:00+05:30"
    assert data["started_at"] == "2026-10-01T12:01:00-04:00"
    assert data["finished_at"] == "2026-10-01T12:02:30Z"
    assert data["erased_at"] == "2026-10-02T09:00:00+00:00"
    assert data["duration_s"] == 0
    assert data["queued_duration_s"] is None


@respx.mock
async def test_missing_optional_fields_stay_null_not_healthy(gitlab_env):
    """Fields GitLab leaves out are null. A missing allow_failure is not False, and a missing runner is not healthy."""
    # Arrange
    respx.get(JOB_URL).mock(return_value=httpx.Response(200, json={"id": 42, "status": "success"}))

    # Act
    data = await call_tool()

    # Assert
    assert data == {
        "job_id": 42,
        "pipeline_id": None,
        "name": None,
        "stage": None,
        "status": "success",
        "allow_failure": None,
        "failure_reason": None,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
        "erased_at": None,
        "duration_s": None,
        "queued_duration_s": None,
        "runner": None,
        "web_url": None,
    }


@respx.mock
async def test_long_text_is_capped_and_runner_description_too(gitlab_env):
    """Names, stages, failure reasons, and runner descriptions are cut to the text cap, with a marker."""
    # Arrange
    long_text = "x" * 300
    job = rest_job(
        name=long_text,
        stage=long_text,
        failure_reason=long_text,
        runner={"id": 7, "description": long_text},
    )
    respx.get(JOB_URL).mock(return_value=httpx.Response(200, json=job))
    capped = "x" * (TEXT_LIMIT - 3) + "..."

    # Act
    data = await call_tool()

    # Assert
    assert data["name"] == capped
    assert data["stage"] == capped
    assert data["failure_reason"] == capped
    assert data["runner"] == {"id": 7, "description": capped}


@respx.mock
async def test_bridge_or_external_job_not_found_points_to_get_pipeline(gitlab_env):
    """A 404 for a trigger or external status job explains the limit, and still mentions missing permission."""
    # Arrange
    respx.get(JOB_URL).mock(return_value=httpx.Response(404, json={"message": "404 Not found"}))

    # Act / Assert
    with pytest.raises(ToolError, match="get_pipeline") as error:
        await call_tool()
    assert "token cannot see it" in str(error.value)


@respx.mock
async def test_other_http_errors_pass_through_with_their_hint(gitlab_env):
    """Errors other than 404 keep the client's status and hint, so a bad token is not reported as a missing job."""
    # Arrange
    respx.get(JOB_URL).mock(return_value=httpx.Response(403, json={"message": "403 Forbidden"}))

    # Act / Assert
    with pytest.raises(ToolError, match="HTTP 403") as error:
        await call_tool()
    assert "get_pipeline" not in str(error.value)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"job_id": 0}, "greater than or equal to 1"),
        ({"job_id": -3}, "greater than or equal to 1"),
        ({"project": "  "}, "Pass a project path"),
    ],
    ids=["zero-id", "negative-id", "blank-project"],
)
@respx.mock
async def test_invalid_arguments_fail_before_network(gitlab_env, arguments, message):
    """Bad job IDs and blank project paths are rejected before any request is made."""
    # Arrange
    route = respx.get(JOB_URL).mock(return_value=httpx.Response(200, json=rest_job()))

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_tool(arguments)
    assert route.call_count == 0
