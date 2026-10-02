"""Tests proving provider identity is open to third-party providers."""

import pytest

from diglibrary.config.models import ProvidersConfig
from diglibrary.providers.types import ProviderId


def test_any_well_formed_identifier_is_a_valid_provider() -> None:
    """A provider this project never heard of configures without a core change."""
    third_party = ProviderId("some_third_party_provider")

    configured = ProvidersConfig(enabled=(third_party,), priority=(third_party,))

    assert configured.priority == (third_party,)
    assert str(third_party) == "some_third_party_provider"


def test_no_providers_configured_is_a_valid_state() -> None:
    """The project ships no acquisition provider yet, so an empty set must be loadable."""
    configured = ProvidersConfig()

    assert configured.enabled == ()
    assert configured.priority == ()


@pytest.mark.parametrize("value", ["", "Soulseek", "sou lseek", "1soulseek", "soulseek-flac"])
def test_non_portable_identifiers_are_rejected(value: str) -> None:
    """Openness stops at the identifier format, which must survive files and storage."""
    with pytest.raises(ValueError, match="Invalid provider identifier"):
        ProviderId(value)
