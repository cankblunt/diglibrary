"""An artist MusicBrainz names in another script is written in Latin letters.

The catalogue files an artist under the name in its original script. Two
other spellings arrive in the same answer: the credit as this release prints
it, and the alias marked as the primary one for English. The printed credit
comes first, the alias second, and an artist with neither keeps the
catalogue's name.
"""

from __future__ import annotations

import logging

from diglibrary.metadata.authentication import EnvironmentCredentials
from diglibrary.metadata.cache import JsonMetadataCache
from diglibrary.metadata.musicbrainz import MusicBrainzClient, _musicbrainz_artists
from diglibrary.metadata.transport import HttpResponse, JsonHttpClient, RateLimiter, RetryPolicy


def _alias(name: str, locale: str | None = "en", primary: bool | None = True) -> dict[str, object]:
    return {"name": name, "locale": locale, "primary": primary, "type": "Artist name"}


def _credit(
    name: str, printed: str | None = None, *aliases: dict[str, object]
) -> dict[str, object]:
    credit: dict[str, object] = {
        "artist": {"id": "artist-id", "name": name, "sort-name": "Tarvo, Brumel"}
    }
    if printed is not None:
        credit["name"] = printed
    if aliases:
        credit["artist"]["aliases"] = list(aliases)  # type: ignore[index]
    return credit


def test_the_spelling_the_release_prints_names_the_artist() -> None:
    (artist,) = _musicbrainz_artists([_credit("北の人", "Brumel Tarvo")])

    assert artist.name == "Brumel Tarvo"


def test_the_printed_credit_comes_before_the_alias() -> None:
    (artist,) = _musicbrainz_artists(
        [_credit("北の人", "Brumel Tarvo & Group", _alias("Brumel Tarvo & The Group"))]
    )

    assert artist.name == "Brumel Tarvo & Group"


def test_the_primary_english_alias_names_an_artist_printed_in_another_script() -> None:
    (artist,) = _musicbrainz_artists(
        [
            _credit(
                "北の人",
                "北の人",
                _alias("TARVO Brumel", locale=None, primary=None),
                _alias("Brumel Tarvó", locale="fr"),
                _alias("Brumel Tarvo"),
                _alias("B. Tarvo", primary=False),
            )
        ]
    )

    assert artist.name == "Brumel Tarvo"


def test_the_name_is_in_reading_order_and_not_the_sort_name_s() -> None:
    (artist,) = _musicbrainz_artists([_credit("北の人", None, _alias("Brumel Tarvo"))])

    assert artist.name == "Brumel Tarvo"
    assert artist.sort_name == "Tarvo, Brumel"


def test_an_alias_that_is_not_the_primary_english_one_names_nobody() -> None:
    """A search hint, another locale's spelling or a past name is not this artist's name."""
    (artist,) = _musicbrainz_artists(
        [
            _credit(
                "北の人",
                "北の人",
                _alias("Tarvo", locale=None, primary=None),
                _alias("Brumel Tarvó", locale="fr"),
                _alias("B. Tarvo", primary=False),
                _alias("北の人たち"),
            )
        ]
    )

    assert artist.name == "北の人"
    assert artist.credited_as is None


def test_an_artist_with_no_latin_spelling_keeps_the_catalogue_s_name() -> None:
    (artist,) = _musicbrainz_artists([_credit("北の人")])

    assert artist.name == "北の人"


def test_a_name_already_in_latin_letters_is_the_catalogue_s() -> None:
    """Neither the printed credit nor an alias replaces a name that needs no reading."""
    (artist,) = _musicbrainz_artists(
        [_credit("Brumel Tarvo", "B. Tarvo", _alias("Brumel Tarvo Jr."))]
    )

    assert artist.name == "Brumel Tarvo"
    assert artist.credited_as is None


def test_the_catalogue_s_spelling_is_kept_beside_the_one_written() -> None:
    """Files tagged in the original script are still compared as this artist's."""
    (artist,) = _musicbrainz_artists([_credit("北の人", "Brumel Tarvo")])

    assert artist.credited_as == "北の人"


def test_several_qualifying_aliases_give_one_answer_whatever_their_order() -> None:
    """The catalogue returns aliases as a set, so position decides nothing."""
    one, other = _alias("Brumel Tarvo"), _alias("Brumel Tarvo II")

    (first,) = _musicbrainz_artists([_credit("北の人", None, one, other)])
    (second,) = _musicbrainz_artists([_credit("北の人", None, other, one)])

    assert first.name == second.name == "Brumel Tarvo"


class _Transport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, headers: object, timeout_seconds: float) -> HttpResponse:
        self.urls.append(url)
        return HttpResponse(status=200, body=b'{"id": "x", "title": "T", "releases": []}')


def test_every_lookup_that_names_an_artist_asks_for_the_aliases(tmp_path) -> None:
    """An alias never asked for is an artist left in the script the catalogue files them under."""
    transport = _Transport()
    client = MusicBrainzClient(
        JsonHttpClient(
            "musicbrainz",
            transport,
            JsonMetadataCache(tmp_path, ttl_seconds=60),
            RateLimiter(1_000_000, sleep=lambda _: None),
            RetryPolicy(1, 0.0, sleep=lambda _: None),
            1.0,
            logging.getLogger("test.metadata"),
        ),
        EnvironmentCredentials({}.get),
        "CONTACT",
        "TOKEN",
    )

    client.get_release("release-id")
    client.releases_in_group("group-id")
    client.recording("recording-id")

    assert len(transport.urls) == 3
    assert all("aliases" in url for url in transport.urls)
