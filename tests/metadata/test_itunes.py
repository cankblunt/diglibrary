"""Unit tests for the iTunes verification witness."""

import logging
import urllib.error
from datetime import date

import pytest

from diglibrary.application.contracts import ArtistMetadata, MetadataSources, ReleaseMetadata
from diglibrary.metadata.itunes import ITunesVerifier, WitnessThrottledError, store_country
from diglibrary.metadata.rate_limit import RateLimiter


def _verifier(payloads: dict[str, dict]) -> tuple[ITunesVerifier, list[str]]:
    requested: list[str] = []

    def transport(url: str) -> dict:
        requested.append(url)
        for fragment, payload in payloads.items():
            if fragment in url:
                return payload
        return {"results": []}

    limiter = RateLimiter(10_000)  # tests never sleep
    return ITunesVerifier(logging.getLogger("test"), transport, limiter), requested


def _release(title: str = "Rumor Turvo") -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="x",
        title=title,
        artists=(ArtistMetadata(name="Bellino"),),
        released_on=date(2000, 1, 1),
        original_released_on=date(1990, 1, 1),
    )


def test_an_agreeing_witness_corroborates_count_year_and_durations() -> None:
    """The verdicts are computed here, from the audio's own durations."""
    verifier, _ = _verifier(
        {
            "search": {
                "results": [
                    {
                        "collectionName": "Rumor Turvo",
                        "collectionId": 1,
                        "trackCount": 2,
                        "releaseDate": "1990-05-01T07:00:00Z",
                    }
                ]
            },
            "lookup": {
                "results": [
                    {"wrapperType": "collection"},
                    {"wrapperType": "track", "trackTimeMillis": 200_000},
                    {"wrapperType": "track", "trackTimeMillis": 300_500},
                ]
            },
        }
    )

    verdicts = verifier.verify(_release(), [200_300, 300_000])

    assert verdicts == {
        "witness": "itunes",
        "found": True,
        "track_count_agrees": True,
        "track_count": 2,
        "year_agrees": True,
        "durations_compared": 2,
        "durations_agreeing": 2,
        "durations_agree": True,
    }


def test_a_witness_that_cannot_find_the_album_is_silence() -> None:
    """No testimony is an honest answer, never a fabricated disagreement."""
    verifier, _ = _verifier({"search": {"results": [{"collectionName": "Unrelated"}]}})

    assert verifier.verify(_release(), [200_000]) is None


def test_a_disagreeing_witness_says_so() -> None:
    """Disagreement is displayed, never silenced."""
    verifier, _ = _verifier(
        {
            "search": {
                "results": [
                    {
                        "collectionName": "Rumor Turvo (Deluxe)",
                        "collectionId": 1,
                        "trackCount": 5,
                        "releaseDate": "2011-01-01",
                    }
                ]
            },
            "lookup": {"results": []},
        }
    )

    verdicts = verifier.verify(_release(), [200_000, 300_000])

    assert verdicts["track_count_agrees"] is False
    assert verdicts["year_agrees"] is False


def test_a_network_failure_is_silence_not_an_error() -> None:
    """A witness that cannot be reached must not block a library."""

    def broken(url: str) -> dict:
        raise OSError("offline")

    verifier = ITunesVerifier(logging.getLogger("test"), broken, RateLimiter(10_000))

    assert verifier.verify(_release(), [200_000]) is None


def test_the_witness_counts_names_and_returns_none_of_them() -> None:
    """Titles are compared as well as lengths, and only the count leaves.

    A catalogue that publishes every title of an album is saying something about
    the record; agreeing lengths alone can be agreeing stopwatches. So the
    witness compares titles too — and what leaves this class is the count, never
    a title, because a verification source contributes no written value.

    One request for both, because the lookup already answers with the whole
    tracklist and the witness is limited to twenty calls a minute.
    """
    verifier, requested = _verifier(
        {
            "search": {
                "results": [
                    {
                        "collectionName": "Rumor Turvo",
                        "collectionId": 1,
                        "trackCount": 2,
                        "releaseDate": "1990-05-01T07:00:00Z",
                    }
                ]
            },
            "lookup": {
                "results": [
                    {"wrapperType": "collection"},
                    {
                        "wrapperType": "track",
                        "trackTimeMillis": 200_000,
                        "trackName": "Zurvo Da Quelma",
                    },
                    {
                        "wrapperType": "track",
                        "trackTimeMillis": 300_500,
                        "trackName": "Trinta E Tantos Dias",
                    },
                ]
            },
        }
    )

    verdicts = verifier.verify(
        _release(),
        [200_300, 300_000],
        ["Zurvo da Quelma", "Só De Brumelo"],
    )

    assert verdicts["titles_compared"] == 2
    assert verdicts["titles_agreeing"] == 1
    assert not any(
        isinstance(value, str) and "Trinta" in value for value in verdicts.values()
    ), "a title it read may not leave this class"
    assert sum(1 for url in requested if "lookup" in url) == 1


def test_the_store_asked_is_the_one_of_the_configured_country() -> None:
    """A different store is a different catalogue, not a different language.

    The US store — the one this service answers from when nobody says which —
    can return nothing at all for a record that another country's store holds.
    A witness asked in the wrong shop says "I have never heard of it" about
    music it knows, which is a false negative and a defect.
    """
    asked: list[str] = []

    def transport(url: str) -> dict[str, object]:
        asked.append(url)
        return {"results": []}

    verifier = ITunesVerifier(
        logging.getLogger("test.itunes"),
        transport=transport,
        limiter=RateLimiter(10_000),
        country="se",
    )

    verifier.verify(_release("Brumelos de Tarvim"), (200_000,))

    assert asked, "the witness asked nothing at all"
    assert all("country=SE" in url for url in asked), asked


def test_the_country_comes_from_the_platform_not_the_environment() -> None:
    """`LANG` may be unset and `locale.getlocale()` may say `('C', 'UTF-8')`.

    An app launched from the Finder inherits none of the shell's variables, so
    the environment cannot answer this; the platform's own locale does.
    US is the last resort rather than the assumption.
    """
    assert store_country(lambda: "se") == "SE"
    assert store_country(lambda: None) == "US"


def test_a_throttled_witness_is_told_apart_from_an_unreachable_one() -> None:
    """Being refused for asking too much needs the opposite answer to a failure.

    Asked too often, the service answers
    `429`, and then `403` for a while afterwards even to a slow caller. Asking
    again is what extends the block, so this failure has its own type and the
    caller stops instead of retrying.
    """

    def throttled(url: str) -> dict[str, object]:
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)  # type: ignore[arg-type]

    verifier = ITunesVerifier(
        logging.getLogger("test.itunes"),
        transport=throttled,
        limiter=RateLimiter(10_000),
        country="SE",
    )

    with pytest.raises(WitnessThrottledError):
        verifier.verify(_release("Gaveta"), (200_000,))


def test_what_the_witness_recognised_is_handed_over_to_be_read() -> None:
    """The one method here that returns catalogue words, and it is for the screen.

    A record can be on iTunes complete while neither catalogue allowed to write
    has it at all, and a count alone does not show *what* was recognised.
    Reading is neither writing nor persisting — nothing of this reaches a tag, a
    name, or any store.
    """
    verifier, requested = _verifier(
        {
            "search": {
                "results": [
                    {
                        "collectionName": "Rumor Turvo",
                        "artistName": "Bellino",
                        "collectionId": 7,
                        "trackCount": 2,
                        "releaseDate": "1990-05-01T07:00:00Z",
                        "collectionViewUrl": "https://music.apple.com/se/album/rumor-turvo/7",
                    }
                ]
            },
            "lookup": {
                "results": [
                    {"wrapperType": "collection"},
                    {
                        "wrapperType": "track",
                        "trackName": "Tarvim Quelmo",
                        "trackTimeMillis": 121000,
                    },
                    {
                        "wrapperType": "track",
                        "trackName": "Ela Trouxe Brumelo",
                        "trackTimeMillis": 98000,
                    },
                ]
            },
        }
    )

    record = verifier.recognised(_release())

    assert record is not None
    assert record.title == "Rumor Turvo"
    assert record.artist == "Bellino"
    assert record.year == 1990
    assert record.track_count == 2
    assert record.url == "https://music.apple.com/se/album/rumor-turvo/7"
    assert [(t.position, t.name, t.duration_ms) for t in record.tracks] == [
        (1, "Tarvim Quelmo", 121000),
        (2, "Ela Trouxe Brumelo", 98000),
    ]
    assert not any(
        "artwork" in url or "preview" in url for url in requested
    ), "only textual catalogue fields may be requested"


def test_a_witness_that_does_not_know_the_record_recognises_nothing() -> None:
    """An answer, not a failure — and nothing for the screen to draw."""
    verifier, _ = _verifier({"search": {"results": []}})

    assert verifier.recognised(_release()) is None
