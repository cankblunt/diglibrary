"""Tests for TOML configuration loading."""

from pathlib import Path

import pytest

from diglibrary.config.loader import ConfigurationError, load_configuration
from diglibrary.library.naming import NAMING_STYLES
from diglibrary.providers.types import ProviderId


def test_load_configuration_resolves_relative_paths(tmp_path: Path) -> None:
    """Configuration paths resolve from the TOML file's directory."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[database]
path = "database.sqlite3"

[logging]
level = "debug"
directory = "logs"
filename = "app.jsonl"

[providers]
enabled = ["youtube"]
priority = ["youtube"]
""",
        encoding="utf-8",
    )

    config = load_configuration(config_path)

    assert config.database.path == tmp_path / "database.sqlite3"
    assert config.logging.level == "DEBUG"
    assert config.logging.path == tmp_path / "logs" / "app.jsonl"
    assert config.providers.priority == (ProviderId("youtube"),)


def test_load_configuration_reads_metadata_client_settings(tmp_path: Path) -> None:
    """Metadata cache, retry, pacing, and authentication settings are typed from TOML."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[database]
path = "database.sqlite3"

[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"

[providers]
enabled = ["youtube"]
priority = ["youtube"]

[metadata]
cache_directory = "metadata-cache"
cache_ttl_seconds = 120
request_timeout_seconds = 4.0
max_attempts = 2
backoff_seconds = 0.25

[metadata.discogs]
token_environment_variable = "DISCOGS_TOKEN"
requests_per_minute = 30

[metadata.musicbrainz]
contact_environment_variable = "MUSICBRAINZ_CONTACT"
access_token_environment_variable = "MUSICBRAINZ_TOKEN"
requests_per_second = 0.5
""",
        encoding="utf-8",
    )

    config = load_configuration(config_path)

    assert config.metadata.cache_directory == tmp_path / "metadata-cache"
    assert config.metadata.discogs.requests_per_minute == 30
    assert config.metadata.musicbrainz.requests_per_second == 0.5


def test_load_configuration_reads_non_secret_slskd_connector_settings(tmp_path: Path) -> None:
    """slskd settings are typed while an API-key value stays outside TOML."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[database]
path = "database.sqlite3"
[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"
[providers]
enabled = ["youtube"]
priority = ["youtube"]
[connectors.slskd]
enabled = true
base_url = "https://slskd.example"
timeout = 12.5
retry_limit = 2
authentication_mode = "api_key"
api_key_environment_variable = "TEST_SLSKD_KEY"
response_limit = 50
""",
        encoding="utf-8",
    )

    config = load_configuration(config_path)

    assert config.slskd.enabled is True
    assert config.slskd.base_url == "https://slskd.example"
    assert config.slskd.timeout_seconds == 12.5
    assert config.slskd.api_key_environment_variable == "TEST_SLSKD_KEY"


def test_load_configuration_reads_extensible_provider_settings(tmp_path: Path) -> None:
    """Provider framework settings accept future identifiers and opaque settings safely."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[database]
path = "database.sqlite3"

[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"

[providers]
enabled = ["youtube"]
priority = ["youtube"]

[providers.settings]

[providers.settings.future_provider]
enabled = false
priority = 7
timeout = 15.5
retry_limit = 1
region = "global"
""",
        encoding="utf-8",
    )

    config = load_configuration(config_path)
    provider = config.providers.settings[ProviderId("future_provider")]

    assert provider.enabled is False
    assert provider.priority == 7
    assert provider.timeout_seconds == 15.5
    assert provider.settings == {"region": "global"}


def test_load_configuration_accepts_a_provider_this_project_does_not_ship(tmp_path: Path) -> None:
    """A third-party provider configures without any change to the core."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[database]
path = "database.sqlite3"

[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"

[providers]
enabled = ["some_third_party_provider"]
priority = ["some_third_party_provider"]
""",
        encoding="utf-8",
    )

    config = load_configuration(config_path)

    assert config.providers.enabled == (ProviderId("some_third_party_provider"),)


def test_load_configuration_accepts_a_configuration_without_providers(tmp_path: Path) -> None:
    """No acquisition provider configured is a valid state, not an error."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[database]
path = "database.sqlite3"

[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"
""",
        encoding="utf-8",
    )

    config = load_configuration(config_path)

    assert config.providers.enabled == ()
    assert config.providers.priority == ()


def test_load_configuration_rejects_a_malformed_provider_identifier(tmp_path: Path) -> None:
    """Openness stops at the identifier format, which must survive files and storage."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[database]
path = "database.sqlite3"

[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"

[providers]
enabled = ["Not A Provider"]
priority = []
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="Invalid provider identifier"):
        load_configuration(config_path)


def test_load_configuration_rejects_missing_file(tmp_path: Path) -> None:
    """A missing configuration file is a configuration error."""
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_configuration(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[database", "Invalid TOML"),
        ('[database]\npath = "database.sqlite3"', r"section \[logging\]"),
        (
            """[database]
path = ""

[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"

[providers]
enabled = ["youtube"]
priority = ["youtube"]
""",
            "must be a non-empty string",
        ),
        (
            """[database]
path = "database.sqlite3"

[logging]
level = "TRACE"
directory = "logs"
filename = "app.jsonl"

[providers]
enabled = ["youtube"]
priority = ["youtube"]
""",
            "Unsupported logging level",
        ),
        (
            """[database]
path = "database.sqlite3"

[logging]
level = "INFO"
directory = "logs"
filename = "nested/app.jsonl"

[providers]
enabled = ["youtube"]
priority = ["youtube"]
""",
            "must not include a directory",
        ),
        (
            """[database]
path = "database.sqlite3"

[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"

[providers]
enabled = ["youtube", 1]
priority = ["youtube"]
""",
            "must contain unique provider names",
        ),
        (
            """[database]
path = "database.sqlite3"

[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"

[providers]
enabled = ["youtube"]
priority = ["soulseek_flac"]
""",
            "must be enabled",
        ),
    ],
)
def test_load_configuration_rejects_invalid_values(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    """Validation rejects malformed and inconsistent configuration values."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(config_path)


def test_load_configuration_defaults_to_the_style_the_library_already_uses(
    tmp_path: Path,
) -> None:
    """A configuration with no [naming] table names albums the way it always did."""
    config = load_configuration(_minimal(tmp_path))

    assert config.naming.folder_template == NAMING_STYLES["spotiflac"].folder_template
    # Absent, the pressing year is not written. The default is stated in the
    # dataclass, the loader and the shipped `default.toml`, and the three must
    # agree: the shipped answer is the one an installation gets.
    assert config.naming.include_edition is False


def test_load_configuration_reads_a_naming_style_and_its_overrides(tmp_path: Path) -> None:
    """A style is a starting point; any single field may still be overridden."""
    config = load_configuration(
        _minimal(
            tmp_path,
            """
[naming]
style = "minimal"
include_edition = false
disc_folder_template = "Disc %disc%"
front_image_name = "folder"
""",
        )
    )

    assert config.naming.folder_template == NAMING_STYLES["minimal"].folder_template
    assert config.naming.disc_folder_template == "Disc %disc%"
    assert config.naming.front_image_name == "folder"
    assert config.naming.include_edition is False


@pytest.mark.parametrize(
    ("naming", "message"),
    [
        ('style = "spotifiac"', "Unknown naming style"),
        ('folder_template = "%albumartist% - %albom%"', "unknown placeholders"),
        ('folder_template = "no placeholder here"', "contains no placeholder"),
        ('track_template = "%track%. %title%{"', "unbalanced"),
        ('front_image_name = "art/cover"', "plain file name"),
    ],
)
def test_load_configuration_rejects_a_broken_naming_setting(
    tmp_path: Path, naming: str, message: str
) -> None:
    """A typo in a template surfaces when the app opens, never during a rename."""
    with pytest.raises(ConfigurationError, match=message):
        load_configuration(_minimal(tmp_path, f"\n[naming]\n{naming}\n"))


def _minimal(tmp_path: Path, extra: str = "") -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[database]
path = "database.sqlite3"

[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"
""" + extra,
        encoding="utf-8",
    )
    return config_path


def test_load_configuration_adds_the_users_genre_spellings_to_the_shipped_ones(
    tmp_path: Path,
) -> None:
    """The list ships with the application, and the user's configuration extends it."""
    config = load_configuration(
        _minimal(tmp_path, '\n[naming]\ngenre_spellings = ["Post-Bop", " "]\n')
    )

    assert config.naming.genre_spellings == ("Post-Bop",)


def test_load_configuration_refuses_genre_spellings_that_are_not_a_list(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="genre_spellings"):
        load_configuration(_minimal(tmp_path, '\n[naming]\ngenre_spellings = "Post-Bop"\n'))
