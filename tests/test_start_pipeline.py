"""Compact pipeline creation contracts."""

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

import gitlab_mcp
from mcp_helpers import call_tool

SHA = "a" * 40
BASE = "https://gitlab.example.com/api/v4/projects/group%2Fproject"


@pytest.mark.parametrize("extra", [{}, {"target": "ref"}, {"target": "ref", "ref": "main", "iid": 1},
                                   {"target": "merge_request"}, {"target": "merge_request", "iid": 1, "ref": "main"},
                                   {"target": "merge_request", "iid": 1, "variables": {}},
                                   *({"target": "merge_request", "iid": iid} for iid in (True, 1.0, "1", 0, -1))])
async def test_invalid_modes(gitlab_env, extra):
    """Invalid modes and strict IDs never reach GitLab."""
    # Arrange / Act / Assert
    with respx.mock(assert_all_called=False) as router, pytest.raises(ToolError):
        await call_tool("start_pipeline", {"project": "group/project", "expected_sha": SHA, **extra},
                        app=gitlab_mcp.create_app("compact"))
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
