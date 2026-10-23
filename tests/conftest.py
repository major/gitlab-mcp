"""Shared pytest fixtures for the GitLab MCP tool tests 🧪."""

import pytest


@pytest.fixture
def gitlab_env(monkeypatch):
    """Point the server at a fake self-managed GitLab instance."""
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com/api/v4/")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
