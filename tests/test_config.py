"""Tests for reading settings from the environment 🧪."""

import pytest

from gitlab_mcp.config import ConfigError, InvalidArtifactOriginsError, Settings


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


@pytest.mark.parametrize(
    ("raw_origins", "expected"),
    [
        (None, ()),
        ("  ", ()),
        (
            "https://STORE.example:443, https://cache.example:8443",
            ("https://store.example", "https://cache.example:8443"),
        ),
        ("https://BÜCHER.example", ("https://xn--bcher-kva.example",)),
        ("https://例え.テスト:8443", ("https://xn--r8jz45g.xn--zckzah:8443",)),
        ("https://[2001:db8::1]", ("https://[2001:db8::1]",)),
        ("https://cache.example:8443", ("https://cache.example:8443",)),
        ("https://[2001:DB8::1]:443", ("https://[2001:db8::1]",)),
    ],
)
def test_artifact_origins_are_parsed_and_normalized(raw_origins, expected):
    """Artifact origins are parsed as a normalized tuple, or default to empty."""
    # Arrange
    env = {"GITLAB_TOKEN": "t"}
    if raw_origins is not None:
        env["GITLAB_ARTIFACT_ORIGINS"] = raw_origins

    # Act
    settings = Settings.from_env(env)

    # Assert
    assert settings.artifact_origins == expected


@pytest.mark.parametrize(
    "raw_origins",
    [
        "http://store.example",
        "https:///missing-host",
        "https://secret-token@store.example",
        "https://store.example/path",
        "https://store.example?token=secret-token",
        "https://store.example#secret-token",
        "https://store.example:invalid",
        "https://store.example:0",
        "https://store.example:",
        "https://store.example,",
        pytest.param("https://[2001:db8::zz]", id="invalid-ipv6"),
        pytest.param("https://[fe80::1%25eth0]", id="scoped-ipv6"),
        pytest.param("https://☃.example", id="unsupported-unicode-hostname"),
        pytest.param("https://\ud800.example", id="surrogate-hostname"),
        pytest.param(f"https://{'a' * 64}.example", id="label-too-long"),
        pytest.param("https://" + ".".join(["a" * 63] * 4), id="hostname-too-long"),
    ],
)
def test_invalid_artifact_origins_raise_without_echoing_input(raw_origins):
    """Invalid origins raise a safe config error without exposing their value."""
    # Arrange
    env = {"GITLAB_TOKEN": "t", "GITLAB_ARTIFACT_ORIGINS": raw_origins}

    # Act / Assert
    with pytest.raises(ConfigError) as error:
        Settings.from_env(env)

    assert isinstance(error.value, InvalidArtifactOriginsError)
    assert str(error.value) == "GITLAB_ARTIFACT_ORIGINS must be a comma-separated list of HTTPS origins"
    assert "secret-token" not in str(error.value)


def test_invalid_artifact_port_does_not_expose_parse_error():
    """Invalid ports do not chain an exception that may contain configured text."""
    # Arrange
    env = {"GITLAB_TOKEN": "t", "GITLAB_ARTIFACT_ORIGINS": "https://store.example:secret-token"}

    # Act
    with pytest.raises(InvalidArtifactOriginsError) as error:
        Settings.from_env(env)

    # Assert
    assert "secret-token" not in str(error.value)
    assert error.value.__suppress_context__
