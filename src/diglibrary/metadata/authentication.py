"""Environment-backed authentication and identification header construction."""

import os
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError, version

from diglibrary.metadata.transport import CredentialError


def application_version() -> str:
    """Return the installed package version, which lives in pyproject.toml alone."""
    try:
        return version("diglibrary")
    except PackageNotFoundError:
        return "0"


class EnvironmentCredentials:
    """Resolve credential values lazily from named environment variables."""

    def __init__(self, getenv: Callable[[str], str | None] = os.getenv) -> None:
        """Create a credential resolver without reading any secret yet."""
        self._getenv = getenv

    def required(self, variable_name: str) -> str:
        """Return a required environment value without exposing it in error text."""
        value = self._getenv(variable_name)
        if not value:
            raise CredentialError(f"Required environment variable is not set: {variable_name}")
        return value

    def optional(self, variable_name: str) -> str | None:
        """Return an optional environment value when it is available."""
        return self._getenv(variable_name) or None


def discogs_headers(token: str) -> Mapping[str, str]:
    """Build Discogs personal-token authentication headers."""
    return {
        "Accept": "application/json",
        "Authorization": f"Discogs token={token}",
        "User-Agent": f"DigLibrary/{application_version()}",
    }


def musicbrainz_headers(contact: str, access_token: str | None) -> Mapping[str, str]:
    """Build MusicBrainz identification and optional bearer-authentication headers."""
    headers = {
        "Accept": "application/json",
        "User-Agent": f"DigLibrary/{application_version()} ({contact})",
    }
    if access_token is not None:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers
