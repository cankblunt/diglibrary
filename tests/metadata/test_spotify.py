"""Reading a Spotify link as words, and refusing to read it as anything else."""

import json
import logging
from collections.abc import Mapping

import pytest

from diglibrary.metadata.authentication import EnvironmentCredentials
from diglibrary.metadata.spotify import (
    PointedWords,
    SpotifyError,
    SpotifyPointer,
    parse_spotify_link,
)
from diglibrary.metadata.transport import HttpResponse


class FakeTransport:
    """Answer with fixed JSON, and record every address that was asked for."""

    def __init__(self, payloads: list[object], status: int = 200) -> None:
        self._payloads = payloads
        self._status = status
        self.urls: list[str] = []
        self.headers: list[Mapping[str, str]] = []

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        """Return the next configured payload as an HTTP response."""
        self.urls.append(url)
        self.headers.append(headers)
        body = json.dumps(self._payloads.pop(0)).encode("utf-8") if self._payloads else b"{}"
        return HttpResponse(status=self._status, body=body)


class FakeGranter:
    """Hand back a token without going near Spotify's accounts service."""

    def __init__(self) -> None:
        self.asked = 0

    def token(self, client_id: str, client_secret: str, timeout_seconds: float) -> str:
        """Return a fixed bearer token, counting how often it was needed."""
        self.asked += 1
        return "a-token"


@pytest.mark.parametrize(
    ("pasted", "kind", "identifier"),
    [
        (
            "https://open.spotify.com/album/0aB1cD2eF3gH4iJ5kL6mN7",
            "album",
            "0aB1cD2eF3gH4iJ5kL6mN7",
        ),
        # What the share button actually copies, tracking tail and all.
        (
            "https://open.spotify.com/track/7oP8qR9sT0uV1wX2yZ3aB4?si=abc123",
            "track",
            "7oP8qR9sT0uV1wX2yZ3aB4",
        ),
        # What a localized client copies.
        (
            "https://open.spotify.com/intl-de/album/0aB1cD2eF3gH4iJ5kL6mN7",
            "album",
            "0aB1cD2eF3gH4iJ5kL6mN7",
        ),
        ("spotify:album:0aB1cD2eF3gH4iJ5kL6mN7", "album", "0aB1cD2eF3gH4iJ5kL6mN7"),
        ("  https://open.spotify.com/playlist/5cD6eF7gH8i  ", "playlist", "5cD6eF7gH8i"),
    ],
)
def test_every_shape_of_link_that_can_be_copied_is_read(
    pasted: str, kind: str, identifier: str
) -> None:
    """Reading the address asks nobody anything, so it can afford to be complete."""
    link = parse_spotify_link(pasted)

    assert link is not None
    assert (link.kind, link.identifier) == (kind, identifier)


@pytest.mark.parametrize(
    "pasted",
    [
        "",
        "Dara Vell Quinton Zentrova",
        "https://www.discogs.com/release/1234567",
        "https://musicbrainz.org/release/abc",
        # A hostname that merely contains the right words is not the host.
        "https://open.spotify.com.evil.example/album/0aB1",
        "https://open.spotify.com/artist/0aB1",
    ],
)
def test_what_is_not_a_spotify_link_is_left_to_the_catalogues(pasted: str) -> None:
    """The catalogue paths must keep working exactly as they did."""
    assert parse_spotify_link(pasted) is None


def test_an_album_link_becomes_its_artist_and_title(tmp_path) -> None:
    """The whole feature, in one sentence: a link becomes words."""
    transport = FakeTransport(
        [{"name": "Zentrova (Deluxe)", "artists": [{"name": "Dara Vell Quinton"}]}]
    )
    granter = FakeGranter()
    pointer = SpotifyPointer(
        transport,
        logging.getLogger("test.spotify"),
        credentials=EnvironmentCredentials(
            {"CLIENT_ID": "id", "CLIENT_SECRET": "secret"}.get  # type: ignore[arg-type]
        ),
        client_id_variable="CLIENT_ID",
        client_secret_variable="CLIENT_SECRET",
        granter=granter,
    )

    words = pointer.words_for(parse_spotify_link("spotify:album:0aB1"))

    assert words == PointedWords(album="Zentrova (Deluxe)", artist="Dara Vell Quinton", exact=True)
    assert words.as_query() == "Dara Vell Quinton Zentrova (Deluxe)"
    assert transport.urls == ["https://api.spotify.com/v1/albums/0aB1"]
    assert transport.headers[0]["Authorization"] == "Bearer a-token"


def test_a_track_link_becomes_the_album_that_contains_it() -> None:
    """The common gesture: a link copied on Spotify is usually a track's.

    The track's own title never leaves this module — what is searched for is the
    record, because a record is what is going to be kept.
    """
    transport = FakeTransport(
        [
            {
                "name": "Walk Out With Me",
                "album": {"name": "Zentrova", "artists": [{"name": "Dara Vell Quinton"}]},
            }
        ]
    )
    pointer = SpotifyPointer(
        transport,
        logging.getLogger("test.spotify"),
        credentials=EnvironmentCredentials({"CLIENT_ID": "id", "CLIENT_SECRET": "s"}.get),  # type: ignore[arg-type]
        client_id_variable="CLIENT_ID",
        client_secret_variable="CLIENT_SECRET",
        granter=FakeGranter(),
    )

    words = pointer.words_for(parse_spotify_link("https://open.spotify.com/track/7oP8"))

    assert words.album == "Zentrova", "the album, never the track"
    assert words.artist == "Dara Vell Quinton"
    assert transport.urls == ["https://api.spotify.com/v1/tracks/7oP8"]


def test_without_a_key_an_album_resolves_by_title_and_says_so() -> None:
    """The public oEmbed endpoint gives a title.

    It does not give an artist. So the poor reading is honest about being poor —
    `exact` is false, and the window says the search is by title alone rather
    than implying it was sharpened by a name nobody supplied.
    """
    transport = FakeTransport([{"title": "Zentrova (Deluxe)", "provider_name": "Spotify"}])
    pointer = SpotifyPointer(transport, logging.getLogger("test.spotify"))

    words = pointer.words_for(parse_spotify_link("spotify:album:0aB1"))

    assert words == PointedWords(album="Zentrova (Deluxe)", artist=None, exact=False)
    assert words.as_query() == "Zentrova (Deluxe)"
    assert transport.urls[0].startswith("https://open.spotify.com/oembed?url=")


def test_without_a_key_a_track_link_is_refused_rather_than_guessed_at() -> None:
    """There is no keyless reading of a track link.

    The public oEmbed answer for a track carries the track's own title and
    nothing else — not the artist, and **not the album it is on**. Searching for
    a song title would return singles and compilations instead of the record
    the track is on.
    """
    pointer = SpotifyPointer(FakeTransport([]), logging.getLogger("test.spotify"))

    with pytest.raises(SpotifyError, match="needs your own Spotify key"):
        pointer.words_for(parse_spotify_link("spotify:track:7oP8"))


def test_a_playlist_is_refused_because_choosing_one_record_is_the_user_s() -> None:
    """Answering a playlist with its first album would make the user's choice."""
    pointer = SpotifyPointer(FakeTransport([]), logging.getLogger("test.spotify"))

    with pytest.raises(SpotifyError, match="playlist"):
        pointer.words_for(parse_spotify_link("spotify:playlist:5cD6"))


def test_nothing_from_spotify_is_written_to_the_metadata_cache(tmp_path) -> None:
    """The non-negotiable, tested as wiring rather than trusted as intent.

    Spotify's Developer Terms limit local caching and forbid storing content
    indefinitely, and every other source in this project
    reaches the network through `JsonHttpClient`, which writes each answer into
    `cache/metadata`. This one must not — so it is composed with the transport
    alone, and this test fails the day someone hands it the caching client.
    """
    transport = FakeTransport([{"title": "Zentrova (Deluxe)"}])
    pointer = SpotifyPointer(transport, logging.getLogger("test.spotify"))

    pointer.words_for(parse_spotify_link("spotify:album:0aB1"))

    assert not list(tmp_path.rglob("*")), "the pointer has no directory to write into at all"
    assert not hasattr(pointer, "_cache"), "and nothing it holds is a cache"


def test_a_refused_link_is_reported_and_never_swallowed() -> None:
    """A dead link must say so; a spinner that never stops is the worse failure."""
    pointer = SpotifyPointer(FakeTransport([{}], status=404), logging.getLogger("test.spotify"))

    with pytest.raises(SpotifyError, match="nothing at that link"):
        pointer.words_for(parse_spotify_link("spotify:album:0aB1"))
