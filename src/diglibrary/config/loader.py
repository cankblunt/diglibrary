"""TOML configuration loading and validation."""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from diglibrary.config.models import (
    ApplicationConfig,
    ArtworkConfig,
    DatabaseConfig,
    DiscogsConfig,
    LoggingConfig,
    MetadataConfig,
    MusicBrainzConfig,
    ProvidersConfig,
)
from diglibrary.connectors.slskd.configuration import (
    SlskdAuthenticationMode,
    SlskdConfiguration,
)
from diglibrary.library.naming import DEFAULT_NAMING_STYLE, NamingError, NamingPolicy
from diglibrary.providers.models import ProviderConfiguration
from diglibrary.providers.types import ProviderId


class ConfigurationError(ValueError):
    """Raised when the application's TOML configuration is invalid."""


def load_configuration(path: Path) -> ApplicationConfig:
    """Load and validate a DigLibrary TOML configuration file.

    Relative filesystem paths are resolved relative to the configuration file.
    """
    if not path.is_file():
        raise ConfigurationError(f"Configuration file does not exist: {path}")

    try:
        with path.open("rb") as config_file:
            raw_config = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in configuration file: {path}") from error

    root = path.parent.resolve()
    database = _section(raw_config, "database")
    logging = _section(raw_config, "logging")
    providers = _optional_section(raw_config, "providers")
    metadata = _optional_section(raw_config, "metadata")
    connectors = _optional_section(raw_config, "connectors")
    artwork = _optional_section(raw_config, "artwork")
    provider_config = ProvidersConfig(
        enabled=_provider_ids(providers, "enabled"),
        priority=_provider_ids(providers, "priority"),
        settings=_provider_settings(providers),
    )
    if not set(provider_config.priority).issubset(provider_config.enabled):
        raise ConfigurationError("Every prioritized provider must be enabled.")

    return ApplicationConfig(
        database=DatabaseConfig(
            path=_resolve_path(root, _string(database, "path")),
            # Defaulted rather than required: a configuration file without a
            # `copies_directory` line must go on opening.
            copies_directory=_resolve_path(
                root, _string_default(database, "copies_directory", "database-copies")
            ),
        ),
        logging=LoggingConfig(
            level=_log_level(_string(logging, "level")),
            directory=_resolve_path(root, _string(logging, "directory")),
            filename=_filename(_string(logging, "filename")),
        ),
        providers=provider_config,
        metadata=_metadata_config(root, metadata),
        slskd=_slskd_configuration(_optional_section(connectors, "slskd")),
        artwork=_artwork_config(root, artwork),
        naming=_naming_policy(_optional_section(raw_config, "naming")),
    )


def _naming_policy(section: Mapping[str, object]) -> NamingPolicy:
    """Build the naming policy from a shipped style plus any override.

    Templates are validated here, while they are still configuration: a typo in
    a placeholder must surface when the application opens, never when a file is
    about to be renamed.
    """
    try:
        return NamingPolicy.from_style(
            _string_default(section, "style", DEFAULT_NAMING_STYLE),
            folder_template=_optional_string(section, "folder_template"),
            track_template=_optional_string(section, "track_template"),
            disc_folder_template=_optional_string(section, "disc_folder_template"),
            various_artists_label=_optional_string(section, "various_artists_label"),
            front_image_name=_optional_string(section, "front_image_name"),
            back_image_name=_optional_string(section, "back_image_name"),
            include_edition=_bool_default(section, "include_edition", False),
            transcoded_label=_optional_string(section, "transcoded_label"),
            genre_spellings=_strings(section, "genre_spellings"),
        )
    except NamingError as error:
        raise ConfigurationError(str(error)) from error


def _optional_string(section: Mapping[str, object], key: str) -> str | None:
    """Return an override that was set, or ``None`` to keep the style's own value."""
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"Configuration value {key!r} must be a non-empty string.")
    return value


def _strings(section: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"Configuration value must be a list of strings: {key}")
    return tuple(item.strip() for item in value if item.strip())


def _artwork_config(root: Path, section: Mapping[str, object]) -> ArtworkConfig:
    """Load the cover-art settings, all of which have working defaults."""
    return ArtworkConfig(
        enabled=_bool_default(section, "enabled", True),
        include_back_cover=_bool_default(section, "include_back_cover", True),
        file_pixels=_positive_int(section, "file_pixels", 1200),
        embed_pixels=_positive_int(section, "embed_pixels", 500),
        parallel_downloads=_positive_int(section, "parallel_downloads", 4),
        release_group_fallback=_bool_default(section, "release_group_fallback", True),
        staging_directory=_resolve_path(
            root, _string_default(section, "staging_directory", "cache/artwork")
        ),
        backup_directory=_resolve_path(
            root, _string_default(section, "backup_directory", "backup")
        ),
    )


def _section(config: Mapping[str, object], name: str) -> Mapping[str, object]:
    section = config.get(name)
    if not isinstance(section, dict):
        raise ConfigurationError(f"Configuration section [{name}] is required.")
    return cast(Mapping[str, object], section)


def _optional_section(config: Mapping[str, object], name: str) -> Mapping[str, object]:
    section = config.get(name, {})
    if not isinstance(section, dict):
        raise ConfigurationError(f"Configuration section [{name}] must be a table.")
    return cast(Mapping[str, object], section)


def _string(section: Mapping[str, object], key: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"Configuration value {key!r} must be a non-empty string.")
    return value


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _filename(value: str) -> str:
    if Path(value).name != value:
        raise ConfigurationError("Logging filename must not include a directory.")
    return value


def _log_level(value: str) -> str:
    level = value.upper()
    valid_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
    if level not in valid_levels:
        raise ConfigurationError(f"Unsupported logging level: {value}")
    return level


def _metadata_config(root: Path, section: Mapping[str, object]) -> MetadataConfig:
    discogs = _optional_section(section, "discogs")
    musicbrainz = _optional_section(section, "musicbrainz")
    return MetadataConfig(
        cache_directory=_resolve_path(
            root, _string_default(section, "cache_directory", "cache/metadata")
        ),
        cache_ttl_seconds=_positive_int(section, "cache_ttl_seconds", 86_400),
        request_timeout_seconds=_positive_float(section, "request_timeout_seconds", 10.0),
        max_attempts=_positive_int(section, "max_attempts", 3),
        backoff_seconds=_non_negative_float(section, "backoff_seconds", 0.5),
        discogs=DiscogsConfig(
            token_environment_variable=_string_default(
                discogs, "token_environment_variable", "DIGLIBRARY_DISCOGS_TOKEN"
            ),
            requests_per_minute=_positive_int(discogs, "requests_per_minute", 60),
        ),
        musicbrainz=MusicBrainzConfig(
            contact_environment_variable=_string_default(
                musicbrainz, "contact_environment_variable", "DIGLIBRARY_MUSICBRAINZ_CONTACT"
            ),
            access_token_environment_variable=_string_default(
                musicbrainz,
                "access_token_environment_variable",
                "DIGLIBRARY_MUSICBRAINZ_ACCESS_TOKEN",
            ),
            requests_per_second=_positive_float(musicbrainz, "requests_per_second", 1.0),
        ),
    )


def _slskd_configuration(section: Mapping[str, object]) -> SlskdConfiguration:
    """Load non-secret slskd connector settings from the optional connector table."""
    try:
        authentication_mode = SlskdAuthenticationMode(
            _string_default(section, "authentication_mode", SlskdAuthenticationMode.API_KEY.value)
        )
    except ValueError as error:
        raise ConfigurationError("Unsupported slskd authentication_mode.") from error
    base_url = _string_default(section, "base_url", "http://127.0.0.1:5030")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigurationError("slskd base_url must use HTTP or HTTPS.")
    api_base_path = _string_default(section, "api_base_path", "/api/v0")
    if not api_base_path.startswith("/") or api_base_path.endswith("/"):
        raise ConfigurationError("slskd api_base_path must start with / and not end with /.")
    return SlskdConfiguration(
        enabled=_bool_default(section, "enabled", False),
        base_url=base_url.rstrip("/"),
        timeout_seconds=_positive_float(section, "timeout", 30.0),
        retry_limit=_non_negative_int(section, "retry_limit", 3),
        authentication_mode=authentication_mode,
        api_key_environment_variable=_string_default(
            section, "api_key_environment_variable", "DIGLIBRARY_SLSKD_API_KEY"
        ),
        api_base_path=api_base_path,
        response_limit=_positive_int(section, "response_limit", 100),
        search_settle_seconds=_positive_float(section, "search_settle", 90.0),
        search_quiet_seconds=_positive_float(section, "search_quiet", 3.0),
        search_poll_seconds=_positive_float(section, "search_poll", 1.0),
    )


def _string_default(section: Mapping[str, object], key: str, default: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"Configuration value {key!r} must be a non-empty string.")
    return value


def _positive_int(section: Mapping[str, object], key: str, default: int) -> int:
    value = section.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"Configuration value {key!r} must be a positive integer.")
    return value


def _positive_float(section: Mapping[str, object], key: str, default: float) -> float:
    value = section.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"Configuration value {key!r} must be a positive number.")
    return float(value)


def _non_negative_float(section: Mapping[str, object], key: str, default: float) -> float:
    value = section.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ConfigurationError(f"Configuration value {key!r} must be a non-negative number.")
    return float(value)


def _non_negative_int(section: Mapping[str, object], key: str, default: int) -> int:
    value = section.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigurationError(f"Configuration value {key!r} must be a non-negative integer.")
    return value


def _bool_default(section: Mapping[str, object], key: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"Configuration value {key!r} must be a boolean.")
    return value


def _provider_ids(section: Mapping[str, object], key: str) -> tuple[ProviderId, ...]:
    """Read an ordered list of provider identifiers without a closed provider list.

    An unknown identifier is a valid third-party provider, so only the
    identifier format is validated here. Whether a provider is actually
    registered is a composition-root concern, not a configuration one.
    """
    raw_values = section.get(key, [])
    if not isinstance(raw_values, list):
        raise ConfigurationError(f"Provider configuration {key!r} must be an array.")
    try:
        providers = tuple(ProviderId(value) for value in raw_values if isinstance(value, str))
    except ValueError as error:
        raise ConfigurationError(str(error)) from error
    if len(providers) != len(raw_values) or len(set(providers)) != len(providers):
        message = f"Provider configuration {key!r} must contain unique provider names."
        raise ConfigurationError(message)
    return providers


def _provider_settings(section: Mapping[str, object]) -> dict[ProviderId, ProviderConfiguration]:
    settings = _optional_section(section, "settings")
    configurations: dict[ProviderId, ProviderConfiguration] = {}
    for identifier, value in settings.items():
        if not isinstance(value, dict):
            raise ConfigurationError(f"Provider settings for {identifier!r} must be a table.")
        try:
            provider_id = ProviderId(identifier)
            configurations[provider_id] = ProviderConfiguration(
                identifier=provider_id,
                enabled=_bool_default(value, "enabled", True),
                priority=_non_negative_int(value, "priority", 10),
                timeout_seconds=_positive_float(value, "timeout", 30.0),
                retry_limit=_non_negative_int(value, "retry_limit", 3),
                settings={
                    key: setting
                    for key, setting in value.items()
                    if key not in {"enabled", "priority", "timeout", "retry_limit"}
                },
            )
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
    return configurations
