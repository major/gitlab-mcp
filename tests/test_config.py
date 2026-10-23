"""Tests for reading settings from the environment 🧪."""

import pytest

from gitlab_mcp.config import ConfigError, Settings


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        (None, "https://gitlab.com"),
        ("https://gitlab.example.com/", "https://gitlab.example.com"),
        ("https://gitlab.example.com/api/v4", "https://gitlab.example.com"),
        ("https://example.com/gitlab", "https://example.com/gitlab"),
    ],
)
def test_url_is_normalized(raw_url, expected):
    """GITLAB_URL is trimmed to the instance root."""
    # Arrange
    env = {"GITLAB_TOKEN": "t"} | ({"GITLAB_URL": raw_url} if raw_url else {})

    # Act
    settings = Settings.from_env(env)

    # Assert
    assert settings.url == expected
    assert settings.api_url == f"{expected}/api/v4"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "GITLAB_TOKEN is not set"),
        ({"GITLAB_TOKEN": "t", "GITLAB_URL": "gitlab.com"}, "must start with"),
        ({"GITLAB_TOKEN": "t", "GITLAB_TIMEOUT": "soon"}, "GITLAB_TIMEOUT"),
    ],
)
def test_bad_settings_raise(env, message):
    """Missing or invalid settings raise a clear ConfigError."""
    # Act / Assert
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(env)
