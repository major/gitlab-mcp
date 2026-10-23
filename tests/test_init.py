"""Tests for the gitlab_mcp package entry point."""

from typing import TYPE_CHECKING

from gitlab_mcp import main, mcp

if TYPE_CHECKING:
    import pytest


def test_main_runs_the_mcp_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console script starts the FastMCP server."""
    # Arrange
    calls: list[bool] = []
    monkeypatch.setattr(type(mcp), "run", lambda _self: calls.append(True))

    # Act
    main()

    # Assert
    assert calls == [True]
