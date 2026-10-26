"""Settings for connecting to a GitLab instance 🔧.

Everything comes from environment variables so the server works with
gitlab.com and self-managed GitLab without needing the `glab` CLI.
"""

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
lazy from collections.abc import Mapping

import httpx

DEFAULT_URL = "https://gitlab.com"
_DEFAULT_HTTPS_PORT = 443
_MAX_HOSTNAME_LENGTH = 253
_MAX_LABEL_LENGTH = 63


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


class InvalidArtifactOriginsError(ConfigError):
    """GITLAB_ARTIFACT_ORIGINS is not a list of HTTPS origins."""

    def __init__(self) -> None:
        """Explain the required format without exposing configured values."""
        super().__init__("GITLAB_ARTIFACT_ORIGINS must be a comma-separated list of HTTPS origins")


def _is_valid_hostname(hostname: str) -> bool:
    """Return whether a hostname is a valid DNS name or IPv6 address."""
    if "%" in hostname:
        return False

    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            return False
        return True

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False

    ascii_hostname = ascii_hostname.removesuffix(".")
    if len(ascii_hostname) > _MAX_HOSTNAME_LENGTH:
        return False

    return all(
        1 <= len(label) <= _MAX_LABEL_LENGTH and re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?", label)
        for label in ascii_hostname.split(".")
    )


def _parse_artifact_origins(raw: str) -> tuple[str, ...]:
    """Parse comma-separated HTTPS origins and normalize their host and port."""
    if not raw.strip():
        return ()

    origins: list[str] = []
    for entry in raw.split(","):
        value = entry.strip()
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise InvalidArtifactOriginsError from None

        is_ipv6 = hostname is not None and ":" in hostname
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or any(character.isspace() for character in value)
            or parsed.netloc.endswith(":")
            or (parsed.netloc.startswith("[") and not is_ipv6)
            or not _is_valid_hostname(hostname)
            or port == 0
        ):
            raise InvalidArtifactOriginsError

        host_for_url = f"[{hostname}]" if is_ipv6 else hostname
        try:
            normalized_host = httpx.URL(f"https://{host_for_url}").raw_host.decode("ascii")
        except httpx.InvalidURL, UnicodeError:
            raise InvalidArtifactOriginsError from None
        authority = f"[{normalized_host}]" if is_ipv6 else normalized_host
        normalized_origin = f"https://{authority}"
        if port is not None and port != _DEFAULT_HTTPS_PORT:
            normalized_origin += f":{port}"
        origins.append(normalized_origin)

    return tuple(origins)


@dataclass(frozen=True)
class Settings:
    """Connection settings for one GitLab instance and trusted artifact destinations.

    Administrators should list only HTTPS destinations they trust for job artifacts.
    """

    url: str
    token: str
    timeout: float = 30.0
    artifact_origins: tuple[str, ...] = ()

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
        - GITLAB_ARTIFACT_ORIGINS: comma-separated HTTPS origins trusted by the
          administrator for job artifact downloads (default: empty)

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

        artifact_origins = _parse_artifact_origins(source.get("GITLAB_ARTIFACT_ORIGINS", ""))

        return cls(url=url, token=token, timeout=timeout, artifact_origins=artifact_origins)
