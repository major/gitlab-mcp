"""Tests for the gitlab_mcp package entry point."""

from typing import TYPE_CHECKING

from gitlab_mcp import main

if TYPE_CHECKING:
    import pytest


def test_main_prints_greeting(capsys: pytest.CaptureFixture[str]) -> None:
    """The entry point writes a greeting to stdout."""
    # Arrange: nothing to set up.

    # Act
    main()

    # Assert
    assert capsys.readouterr().out == "Hello from gitlab-mcp!\n"
