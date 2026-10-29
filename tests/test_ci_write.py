"""Tests for guarded CI write tools through the in-memory MCP client 🚦."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp import mcp
from gitlab_mcp.tools import ci_write as _ci_write  # noqa: F401 - importing registers the tools for direct tests.

API = "https://gitlab.example.com/api/v4"
PROJECT = "group/project"
ENCODED_PROJECT = "group%2Fproject"
SHA = "a" * 40
NEW_SHA = "b" * 40
SAFE_VALIDATION_HINT = (
    "Invalid create_pipeline arguments. Provide project, ref, expected_sha, and valid variables; see the tool schema."
)


async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one CI write tool through the in-memory MCP protocol."""
    async with Client(mcp) as client:
        result = await client.call_tool(name, {"project": PROJECT, **arguments})
    return result.structured_content


def ci_resource(kind: str, *, status: str = "failed", sha: str = SHA) -> dict[str, Any]:
    """Build a GitLab job or pipeline preflight response."""
    if kind == "job":
        return {"id": 42, "status": status, "commit": {"id": sha}, "pipeline": {"sha": NEW_SHA}}
    return {"id": 901, "status": status, "sha": sha}


def retry_args(kind: str, **overrides: Any) -> dict[str, Any]:
    """Return the common arguments for one retry request."""
    return {"kind": kind, "id": 42 if kind == "job" else 901, "expected_sha": SHA, **overrides}


@pytest.mark.parametrize("kind", ["job", "pipeline"])
@respx.mock
async def test_retries_failed_resources_with_empty_post_body(gitlab_env, kind):
    """A matching failed resource is retried at its fixed endpoint with an empty request body."""
    # Arrange
    resource_path = "jobs/42" if kind == "job" else "pipelines/901"
    preflight = respx.get(f"{API}/projects/{ENCODED_PROJECT}/{resource_path}").mock(
        return_value=httpx.Response(200, json=ci_resource(kind))
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/{resource_path}/retry").mock(
        return_value=httpx.Response(
            200,
            json=(
                {"id": 43, "commit": {"id": SHA}, "pipeline": {"sha": NEW_SHA}}
                if kind == "job"
                else {"id": 902, "sha": SHA, "web_url": "https://gitlab.example.com/pipelines/902"}
            ),
        )
    )

    # Act
    result = await call_tool("retry_ci", retry_args(kind))

    # Assert
    assert preflight.call_count == write.call_count == 1
    assert write.calls.last.request.content == b""
    assert result["status"] == "started"
    assert result["kind"] == kind
    assert result["id"] == (43 if kind == "job" else 902)
    assert result["sha"] == SHA


@pytest.mark.parametrize("kind", ["job", "pipeline"])
@pytest.mark.parametrize("status", ["failed", "canceled"])
@respx.mock
async def test_validate_only_checks_eligible_ci_without_post(gitlab_env, kind, status):
    """Validate-only confirms the target and SHA without sending a retry request."""
    # Arrange
    resource_path = "jobs/42" if kind == "job" else "pipelines/901"
    preflight = respx.get(f"{API}/projects/{ENCODED_PROJECT}/{resource_path}").mock(
        return_value=httpx.Response(200, json=ci_resource(kind, status=status))
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/{resource_path}/retry").mock(
        return_value=httpx.Response(200, json={})
    )

    # Act
    result = await call_tool("retry_ci", retry_args(kind, validate_only=True))

    # Assert
    assert result == {"status": "validated", "kind": kind, "id": 42 if kind == "job" else 901, "sha": SHA}
    assert preflight.call_count == 1
    assert write.call_count == 0


@pytest.mark.parametrize("kind", ["job", "pipeline"])
@respx.mock
async def test_rejects_stale_sha_before_retry(gitlab_env, kind):
    """A changed commit SHA stops the retry before its POST."""
    # Arrange
    resource_path = "jobs/42" if kind == "job" else "pipelines/901"
    respx.get(f"{API}/projects/{ENCODED_PROJECT}/{resource_path}").mock(
        return_value=httpx.Response(200, json=ci_resource(kind, sha=NEW_SHA))
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/{resource_path}/retry").mock(
        return_value=httpx.Response(200, json={})
    )

    # Act / Assert
    with pytest.raises(ToolError, match="does not match expected_sha"):
        await call_tool("retry_ci", retry_args(kind))
    assert write.call_count == 0


@pytest.mark.parametrize("status", ["running", "success", "skipped", None])
@respx.mock
async def test_rejects_ineligible_or_unknown_status(gitlab_env, status):
    """Only failed and canceled statuses are eligible for a retry."""
    # Arrange
    resource = ci_resource("pipeline") | {"status": status}
    preflight = respx.get(f"{API}/projects/{ENCODED_PROJECT}/pipelines/901").mock(
        return_value=httpx.Response(200, json=resource)
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/pipelines/901/retry").mock(
        return_value=httpx.Response(200, json={})
    )

    # Act / Assert
    with pytest.raises(ToolError, match="Only a failed or canceled"):
        await call_tool("retry_ci", retry_args("pipeline"))
    assert preflight.call_count == 1
    assert write.call_count == 0


@pytest.mark.parametrize("status", [408, 500, 503])
@respx.mock
async def test_retry_http_write_errors_are_unknown_and_not_repeated(gitlab_env, status):
    """Timeout and server errors report unknown outcome and never repeat the write."""
    # Arrange
    respx.get(f"{API}/projects/{ENCODED_PROJECT}/pipelines/901").mock(
        return_value=httpx.Response(200, json=ci_resource("pipeline"))
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/pipelines/901/retry").mock(
        return_value=httpx.Response(status, json={"message": "secret detail"})
    )

    # Act / Assert
    with pytest.raises(ToolError, match="outcome is unknown") as error:
        await call_tool("retry_ci", retry_args("pipeline"))
    assert "secret detail" not in str(error.value)
    assert write.call_count == 1


@respx.mock
async def test_create_pipeline_uses_encoded_ref_and_reports_race_sha(gitlab_env):
    """Pipeline creation sends bounded variables and reports the actual SHA if the ref moves."""
    # Arrange
    ref = "feature/one"
    encoded_ref = "feature%2Fone"
    commit = respx.get(f"{API}/projects/{ENCODED_PROJECT}/repository/commits/{encoded_ref}").mock(
        return_value=httpx.Response(200, json={"id": SHA})
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/pipeline").mock(
        return_value=httpx.Response(
            201,
            json={"id": 902, "sha": NEW_SHA, "web_url": "https://gitlab.example.com/pipelines/902"},
        )
    )
    variable_value = "do-not-return-this-value"

    # Act
    result = await call_tool(
        "create_pipeline",
        {"ref": ref, "expected_sha": SHA, "variables": {"TOKEN": variable_value, "MODE": "fast"}},
    )

    # Assert
    assert commit.call_count == write.call_count == 1
    assert json.loads(write.calls.last.request.content) == {
        "ref": ref,
        "variables": [
            {"key": "MODE", "value": "fast", "variable_type": "env_var"},
            {"key": "TOKEN", "value": variable_value, "variable_type": "env_var"},
        ],
    }
    assert result == {
        "status": "started",
        "kind": "pipeline",
        "id": 902,
        "web_url": "https://gitlab.example.com/pipelines/902",
        "sha": NEW_SHA,
        "ref": ref,
        "warning": (
            "The ref moved after preflight. GitLab started the pipeline at a different SHA; check GitLab before acting."
        ),
    }
    assert variable_value not in str(result)


@respx.mock
async def test_create_pipeline_validate_only_omits_variable_values_and_post(gitlab_env):
    """Validate-only checks a commit and returns no variable values or pipeline write."""
    # Arrange
    commit = respx.get(f"{API}/projects/{ENCODED_PROJECT}/repository/commits/main").mock(
        return_value=httpx.Response(200, json={"id": SHA})
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/pipeline").mock(
        return_value=httpx.Response(201, json={"id": 902, "sha": SHA})
    )
    variable_value = "not-in-the-result"

    # Act
    result = await call_tool(
        "create_pipeline",
        {"ref": "main", "expected_sha": SHA, "variables": {"SECRET": variable_value}, "validate_only": True},
    )

    # Assert
    assert result == {"status": "validated", "kind": "pipeline", "sha": SHA, "ref": "main"}
    assert variable_value not in str(result)
    assert commit.call_count == 1
    assert write.call_count == 0


@respx.mock
async def test_create_rejects_stale_ref_sha_before_post(gitlab_env):
    """A ref that no longer points to expected_sha is not used to create a pipeline."""
    # Arrange
    respx.get(f"{API}/projects/{ENCODED_PROJECT}/repository/commits/main").mock(
        return_value=httpx.Response(200, json={"id": NEW_SHA})
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/pipeline").mock(
        return_value=httpx.Response(201, json={"id": 902, "sha": NEW_SHA})
    )

    # Act / Assert
    with pytest.raises(ToolError, match="does not match expected_sha"):
        await call_tool("create_pipeline", {"ref": "main", "expected_sha": SHA})
    assert write.call_count == 0


@respx.mock
async def test_variable_values_are_not_exposed_in_rejected_write_error(gitlab_env):
    """GitLab error bodies are hidden because they may echo pipeline variable values."""
    # Arrange
    variable_value = "sensitive-value"
    respx.get(f"{API}/projects/{ENCODED_PROJECT}/repository/commits/main").mock(
        return_value=httpx.Response(200, json={"id": SHA})
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/pipeline").mock(
        return_value=httpx.Response(422, json={"message": f"Rejected {variable_value}"})
    )

    # Act / Assert
    with pytest.raises(ToolError, match="HTTP 422") as error:
        await call_tool(
            "create_pipeline",
            {"ref": "main", "expected_sha": SHA, "variables": {"TOKEN": variable_value}},
        )
    assert variable_value not in str(error.value)
    assert write.call_count == 1


@pytest.mark.parametrize(
    "variables",
    [
        {"BAD-NAME": "invalid-key-secret"},
        {"TOKEN": 17},
        {f"V{index}": "x" for index in range(21)},
        {"TOKEN": "oversized-value-secret-" + "x" * 4097},
    ],
    ids=["invalid-key", "non-string-value", "too-many", "oversized-value"],
)
@respx.mock
async def test_invalid_variables_fail_before_any_http_request(gitlab_env, variables):
    """Invalid variable names, values, or bounds are rejected before GitLab is contacted."""
    # Arrange
    commit = respx.get(f"{API}/projects/{ENCODED_PROJECT}/repository/commits/main").mock(
        return_value=httpx.Response(200, json={"id": SHA})
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/pipeline").mock(
        return_value=httpx.Response(201, json={"id": 902, "sha": SHA})
    )

    # Act / Assert
    with pytest.raises(ToolError) as error:
        await call_tool("create_pipeline", {"ref": "main", "expected_sha": SHA, "variables": variables})
    for value in variables.values():
        if isinstance(value, str):
            assert value[:20] not in str(error.value)
    assert commit.call_count == write.call_count == 0


@respx.mock
async def test_missing_required_sha_does_not_echo_pipeline_variable(gitlab_env):
    """Missing required arguments keep their schema guard without leaking variable values in protocol errors."""
    # Arrange
    marker = "TOP_SECRET_SENTINEL"
    route = respx.route().mock(return_value=httpx.Response(200, json={"id": SHA}))
    async with Client(mcp) as client:
        tools = await client.list_tools()
        create = next(tool for tool in tools if tool.name == "create_pipeline")

        # Act / Assert
        assert "expected_sha" in create.input_schema.get("required", [])
        with pytest.raises(ToolError) as error:
            await client.call_tool(
                "create_pipeline",
                {"project": PROJECT, "ref": "main", "variables": {"TOKEN": marker}},
            )

    assert str(error.value) == SAFE_VALIDATION_HINT
    assert marker not in str(error.value)
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("tool", "arguments", "expected_url"),
    [
        (
            "create_pipeline",
            {"ref": "release/one", "expected_sha": SHA, "validate_only": True},
            f"{API}/projects/{ENCODED_PROJECT}/repository/commits/release%2Fone",
        ),
        (
            "retry_ci",
            {"kind": "pipeline", "id": 901, "expected_sha": SHA, "validate_only": True},
            f"{API}/projects/{ENCODED_PROJECT}/pipelines/901",
        ),
    ],
    ids=["slash-ref-is-one-path-component", "pipeline-preflight-path"],
)
@respx.mock
async def test_path_inputs_use_only_fixed_encoded_components(gitlab_env, tool, arguments, expected_url):
    """Refs and project paths cannot escape the fixed GitLab API routes."""
    # Arrange
    if tool == "create_pipeline":
        route = respx.get(expected_url).mock(return_value=httpx.Response(200, json={"id": SHA}))
    else:
        route = respx.get(expected_url).mock(return_value=httpx.Response(200, json=ci_resource("pipeline")))

    # Act
    await call_tool(tool, arguments)

    # Assert
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("create_pipeline", {"ref": "feature/../main", "expected_sha": SHA}),
        ("create_pipeline", {"ref": "main\n", "expected_sha": SHA}),
        ("retry_ci", {"kind": "pipeline", "id": 901, "expected_sha": SHA, "project": "group/../other"}),
        ("retry_ci", {"kind": "pipeline", "id": 901, "expected_sha": SHA, "project": "group%2Fother"}),
    ],
    ids=["dot-ref-segment", "control-ref", "dot-project-segment", "encoded-project-path"],
)
@respx.mock
async def test_unsafe_project_or_ref_is_rejected_before_network(gitlab_env, tool, arguments):
    """Unsafe path inputs are rejected before any REST request."""
    # Arrange
    route = respx.route().mock(return_value=httpx.Response(200, json={"id": SHA, "status": "failed"}))

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(tool, arguments)
    assert route.call_count == 0


@pytest.mark.parametrize(
    "response",
    [
        {"id": True, "sha": SHA},
        {"id": 902, "sha": "not-a-sha"},
        {"id": 0, "sha": SHA},
        {},
    ],
    ids=["boolean-id", "bad-sha", "zero-id", "empty-response"],
)
@respx.mock
async def test_malformed_success_response_is_unverified(gitlab_env, response):
    """A 2xx response without a valid ID and SHA is not reported as a confirmed write."""
    # Arrange
    respx.get(f"{API}/projects/{ENCODED_PROJECT}/pipelines/901").mock(
        return_value=httpx.Response(200, json=ci_resource("pipeline"))
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/pipelines/901/retry").mock(
        return_value=httpx.Response(200, json=response)
    )

    # Act / Assert
    with pytest.raises(ToolError, match="outcome is unverified"):
        await call_tool("retry_ci", retry_args("pipeline"))
    assert write.call_count == 1


@respx.mock
async def test_connection_error_during_create_is_unknown_and_not_retried(gitlab_env):
    """A lost connection after pipeline POST reports unknown outcome without repeating the write."""
    # Arrange
    respx.get(f"{API}/projects/{ENCODED_PROJECT}/repository/commits/main").mock(
        return_value=httpx.Response(200, json={"id": SHA})
    )
    write = respx.post(f"{API}/projects/{ENCODED_PROJECT}/pipeline").mock(
        side_effect=httpx.ConnectError("connection reset")
    )

    # Act / Assert
    with pytest.raises(ToolError, match="outcome is unknown") as error:
        await call_tool("create_pipeline", {"ref": "main", "expected_sha": SHA})
    assert "connection reset" not in str(error.value)
    assert write.call_count == 1


MR_PATH = f"{API}/projects/{ENCODED_PROJECT}/merge_requests/12"
MR_PIPELINE_ID = 903
OPEN_MR = {"sha": SHA, "state": "opened", "head_pipeline": {"id": 902, "status": "success"}}


@pytest.mark.parametrize(
    ("pipeline", "warning"),
    [
        # Merged-results pipelines test a merge commit, so a new SHA is normal there.
        ({"ref": "refs/merge-requests/12/merge", "sha": NEW_SHA}, ""),
        ({"ref": "refs/merge-requests/12/head", "sha": SHA}, ""),
        ({"ref": "refs/merge-requests/12/head", "sha": NEW_SHA}, "The MR head moved after preflight"),
    ],
    ids=["merged-result", "detached", "detached-head-moved"],
)
@respx.mock
async def test_create_merge_request_pipeline_posts_after_head_check(gitlab_env, pipeline, warning):
    """A matching MR head starts one MR pipeline and returns its ID, ref, status, and SHA."""
    # Arrange
    respx.get(MR_PATH).mock(return_value=httpx.Response(200, json=OPEN_MR))
    write = respx.post(f"{MR_PATH}/pipelines").mock(
        return_value=httpx.Response(200, json={"id": MR_PIPELINE_ID, "status": "created", **pipeline})
    )

    # Act
    result = await call_tool("create_merge_request_pipeline", {"iid": 12, "expected_sha": SHA})

    # Assert
    assert write.call_count == 1
    assert write.calls.last.request.content == b""
    assert result["id"] == MR_PIPELINE_ID
    assert result["ref"] == pipeline["ref"]
    assert result["pipeline_status"] == "created"
    assert result["sha"] == pipeline["sha"]
    assert result.get("warning", "").startswith(warning)


@pytest.mark.parametrize(
    ("mr_fields", "validate_only", "error"),
    [
        ({}, True, None),
        ({"head_pipeline": None}, True, None),
        ({"sha": NEW_SHA}, False, "does not match expected_sha"),
        ({"sha": None}, False, "full head SHA"),
        ({"state": "merged"}, False, "Only an open MR"),
        ({"head_pipeline": {"id": 902, "status": "running"}}, False, "pipeline 902 is still running"),
    ],
    ids=["validate-only", "validate-no-pipeline", "stale-head", "no-head", "not-open", "active-pipeline"],
)
@respx.mock
async def test_create_merge_request_pipeline_never_posts_without_a_fresh_idle_head(
    gitlab_env, mr_fields, validate_only, error
):
    """Validate-only, a stale or missing head, a closed MR, and an active pipeline all stop before the POST."""
    # Arrange
    respx.get(MR_PATH).mock(return_value=httpx.Response(200, json=OPEN_MR | mr_fields))
    write = respx.post(f"{MR_PATH}/pipelines").mock(return_value=httpx.Response(201, json={}))
    arguments = {"iid": 12, "expected_sha": SHA, "validate_only": validate_only}

    # Act / Assert
    if error:
        with pytest.raises(ToolError, match=error):
            await call_tool("create_merge_request_pipeline", arguments)
    else:
        assert await call_tool("create_merge_request_pipeline", arguments) == {
            "status": "validated",
            "kind": "pipeline",
            "sha": SHA,
        }
    assert write.call_count == 0


@respx.mock
async def test_rejected_merge_request_pipeline_shows_gitlab_reason_and_rules_hint(gitlab_env):
    """This POST sends no secrets, so GitLab's reason and a CI rules hint are shown."""
    # Arrange
    respx.get(MR_PATH).mock(return_value=httpx.Response(200, json=OPEN_MR))
    respx.post(f"{MR_PATH}/pipelines").mock(
        return_value=httpx.Response(400, json={"message": {"base": ["No stages / jobs for this pipeline."]}})
    )

    # Act / Assert
    with pytest.raises(ToolError, match=r"HTTP 400\. .*No stages / jobs.*merge_request_event"):
        await call_tool("create_merge_request_pipeline", {"iid": 12, "expected_sha": SHA})
