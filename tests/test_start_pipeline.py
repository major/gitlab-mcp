"""Compact pipeline creation contracts."""

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

import gitlab_mcp
from mcp_helpers import call_tool

SHA = "a" * 40
BASE = "https://gitlab.example.com/api/v4/projects/group%2Fproject"


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"target": "ref"},
        {"target": "ref", "ref": "main", "iid": 1},
        {"target": "merge_request"},
        {"target": "merge_request", "iid": 1, "ref": "main"},
        {"target": "merge_request", "iid": 1, "variables": {}},
        *({"target": "merge_request", "iid": iid} for iid in (True, 1.0, "1", 0, -1)),
    ],
)
async def test_invalid_modes(gitlab_env, extra):
    """Invalid modes and strict IDs never reach GitLab."""
    # Arrange / Act / Assert
    with respx.mock(assert_all_called=False) as router, pytest.raises(ToolError):
        await call_tool(
            "start_pipeline",
            {"project": "group/project", "expected_sha": SHA, **extra},
            app=gitlab_mcp.create_app("compact"),
        )
    assert not router.calls


async def test_ref_parity(gitlab_env):
    """The compact ref route has the same request and result as full."""
    # Arrange
    args = {"project": "group/project", "ref": "main", "expected_sha": SHA, "variables": {"TOKEN": "secret"}}
    with respx.mock(assert_all_called=True) as router:
        router.get(BASE + "/repository/commits/main").mock(return_value=httpx.Response(200, json={"id": SHA}))
        post = router.post(BASE + "/pipeline").mock(return_value=httpx.Response(201, json={"id": 7, "sha": SHA}))
        # Act
        compact = await call_tool("start_pipeline", {"target": "ref", **args}, app=gitlab_mcp.create_app("compact"))
        full = await call_tool("create_pipeline", args)
        # Assert
        assert compact == full
        assert post.call_count == 2  # noqa: PLR2004
        assert post.calls[0].request.content == post.calls[1].request.content


@pytest.mark.parametrize("ref", ["refs/merge-requests/1/head", "refs/merge-requests/1/merge"])
async def test_mr_parity(gitlab_env, ref):
    """Detached and merged-result pipelines preserve full behavior."""
    # Arrange
    args = {"project": "group/project", "iid": 1, "expected_sha": SHA}
    with respx.mock(assert_all_called=True) as router:
        router.get(BASE + "/merge_requests/1").mock(
            return_value=httpx.Response(200, json={"sha": SHA, "state": "opened"})
        )
        post = router.post(BASE + "/merge_requests/1/pipelines").mock(
            return_value=httpx.Response(201, json={"id": 7, "sha": "b" * 40, "ref": ref, "status": "created"})
        )
        # Act
        compact = await call_tool(
            "start_pipeline", {"target": "merge_request", **args}, app=gitlab_mcp.create_app("compact")
        )
        full = await call_tool("create_merge_request_pipeline", args)
        # Assert
        assert compact == full
        assert post.call_count == 2  # noqa: PLR2004


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("create_pipeline", {"project": "group/project", "ref": "main", "expected_sha": SHA}),
        ("create_merge_request_pipeline", {"project": "group/project", "iid": 1, "expected_sha": SHA}),
    ],
)
async def test_originals_hidden(gitlab_env, name, args):
    """Original tools are not callable in compact."""
    # Arrange / Act / Assert
    with respx.mock(assert_all_called=False) as router, pytest.raises(ToolError, match="Unknown tool"):
        await call_tool(name, args, app=gitlab_mcp.create_app("compact"))
    assert not router.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"project": None},
        {"target": None},
        {"expected_sha": None},
        {"ref": 3},
        {"target": "merge_request", "iid": "secret-value"},
        {"variables": {"X": {"secret": "secret-value"}}},
    ],
)
async def test_validation_redacts(gitlab_env, changes):
    """Validation and runtime errors do not echo secrets in other arguments."""
    # Arrange
    args = {
        "project": "group/project",
        "target": "ref",
        "ref": "main",
        "expected_sha": SHA,
        "variables": {"TOKEN": "secret-value"},
        **changes,
    }
    # Act / Assert
    with respx.mock(assert_all_called=False) as router, pytest.raises(ToolError) as error:
        await call_tool("start_pipeline", args, app=gitlab_mcp.create_app("compact"))
    assert "secret-value" not in str(error.value)
    assert not router.calls


@pytest.mark.parametrize("missing", ["project", "target", "expected_sha"])
async def test_missing_required_fields_do_not_expose_variables(gitlab_env, missing):
    """Missing-field errors never include the remaining payload's secret variables."""
    # Arrange
    args = {
        "project": "group/project",
        "target": "ref",
        "ref": "main",
        "expected_sha": SHA,
        "variables": {"TOKEN": "secret-value"},
    }
    del args[missing]

    # Act / Assert
    with respx.mock(assert_all_called=False) as router, pytest.raises(ToolError) as error:
        await call_tool("start_pipeline", args, app=gitlab_mcp.create_app("compact"))
    assert "Invalid start_pipeline arguments" in str(error.value)
    assert "secret-value" not in str(error.value)
    assert not router.calls


@pytest.mark.parametrize("active", [False, True])
async def test_mr_validate_and_active(gitlab_env, active):
    """Validation skips POST and an observed active pipeline is refused."""
    # Arrange
    mr = {"sha": SHA, "state": "opened", "head_pipeline": {"id": 7, "status": "running"} if active else None}
    with respx.mock(assert_all_called=True) as router:
        router.get(BASE + "/merge_requests/1").mock(return_value=httpx.Response(200, json=mr))
        args = {
            "project": "group/project",
            "target": "merge_request",
            "iid": 1,
            "expected_sha": SHA,
            "validate_only": True,
        }
        # Act / Assert
        if active:
            with pytest.raises(ToolError, match="still running"):
                await call_tool("start_pipeline", args, app=gitlab_mcp.create_app("compact"))
        else:
            assert (await call_tool("start_pipeline", args, app=gitlab_mcp.create_app("compact")))[
                "status"
            ] == "validated"
        assert len(router.calls) == 1


async def test_unknown_write_not_retried(gitlab_env):
    """An uncertain POST is sent once and asks the caller to check GitLab."""
    # Arrange
    with respx.mock(assert_all_called=True) as router:
        router.get(BASE + "/repository/commits/main").mock(return_value=httpx.Response(200, json={"id": SHA}))
        post = router.post(BASE + "/pipeline").mock(return_value=httpx.Response(503))
        # Act / Assert
        with pytest.raises(ToolError, match=r"unknown.*Check GitLab"):
            await call_tool(
                "start_pipeline",
                {"project": "group/project", "target": "ref", "ref": "main", "expected_sha": SHA},
                app=gitlab_mcp.create_app("compact"),
            )
        assert post.call_count == 1
