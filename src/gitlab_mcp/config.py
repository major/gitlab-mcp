"""Settings for connecting to a GitLab instance 🔧.

Everything comes from environment variables so the server works with
gitlab.com and self-managed GitLab without needing the `glab` CLI.
"""

import os
from dataclasses import dataclass
lazy from collections.abc import Mapping

DEFAULT_URL = "https://gitlab.com"


class ConfigError(RuntimeError):
    """Base error for missing or invalid settings."""


class MissingTokenError(ConfigError):
    """GITLAB_TOKEN is not set."""

    def __init__(self) -> None:
        """Explain how to create and export a token."""
        super().__init__(
            "GITLAB_TOKEN is not set. Create a personal access token with "
            "the 'read_api' or 'api' scope and export it as GITLAB_TOKEN."
        )


class InvalidUrlError(ConfigError):
    """GITLAB_URL is not an http(s) URL."""

    def __init__(self, url: str) -> None:
        """Show the bad URL so the user can spot the typo."""
        super().__init__(f"GITLAB_URL must start with https:// or http://, got {url!r}")


class InvalidTimeoutError(ConfigError):
    """GITLAB_TIMEOUT is not a number."""

    def __init__(self) -> None:
        """Explain the expected format."""
        super().__init__("GITLAB_TIMEOUT must be a number of seconds")


@dataclass(frozen=True)
class Settings:
    """Connection settings for one GitLab instance."""

    url: str
    token: str
    timeout: float = 30.0

    @property
    def api_url(self) -> str:
        """Return the REST API v4 base URL for this instance."""
        return f"{self.url}/api/v4"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from environment variables.

        Supported variables:
        - GITLAB_URL: instance URL (default: https://gitlab.com)
        - GITLAB_TOKEN: personal, project, or group access token (required)
        - GITLAB_TIMEOUT: request timeout in seconds (default: 30)

        Raises:
            ConfigError: When a variable is missing or invalid.
        """
        source = os.environ if env is None else env
        token = source.get("GITLAB_TOKEN", "").strip()
        if not token:
            raise MissingTokenError

        # Users often paste URLs with a trailing slash or the /api/v4 suffix.
        url = source.get("GITLAB_URL", DEFAULT_URL).strip().rstrip("/").removesuffix("/api/v4")
        if not url.startswith(("https://", "http://")):
            raise InvalidUrlError(url)

        try:
            timeout = float(source.get("GITLAB_TIMEOUT", "30"))
        except ValueError as exc:
            raise InvalidTimeoutError from exc

        return cls(url=url, token=token, timeout=timeout)
