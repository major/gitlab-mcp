"""Shared pytest fixtures for the GitLab MCP tool tests 🧪."""

import importlib
import warnings

import pytest


@pytest.fixture(scope="session", autouse=True)
def initialize_urllib3_connection():
    """Initialize urllib3's optional IPv6 probe under the blocked socket guard."""
    # pytest-socket's default per-test setup blocks sockets before this fixture runs.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"^A test tried to use socket\.socket\.\Z",
            category=UserWarning,
            module=r"pytest_socket\Z",
        )
        importlib.import_module("urllib3.util.connection")


@pytest.fixture
def gitlab_env(monkeypatch):
    """Point the server at a fake self-managed GitLab instance."""
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com/api/v4/")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
