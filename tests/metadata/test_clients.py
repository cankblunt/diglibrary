"""Unit tests for provider payload mapping into canonical metadata."""

import json
import logging
from collections.abc import Mapping

from diglibrary.application.contracts import ArtistMetadata, written_credit
from diglibrary.metadata.authentication import EnvironmentCredentials
from diglibrary.metadata.cache import JsonMetadataCache
from diglibrary.metadata.discogs import DiscogsClient
from diglibrary.metadata.models import MetadataQuery, MetadataSources
from diglibrary.metadata.musicbrainz import MusicBrainzClient
from diglibrary.metadata.rate_limit import RateLimiter
from diglibrary.metadata.retry import RetryPolicy
from diglibrary.metadata.transport import HttpResponse, JsonHttpClient


class FakeTransport:
    """Return fixed JSON payloads without performing network requests."""

    def __init__(self, payloads: list[bytes]) -> None:
        self._payloads = payloads
        self.urls: list[str] = []
        self.headers: list[Mapping[str, str]] = []

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        """Return the next configured payload as a successful HTTP response."""
        self.urls.append(url)
        self.headers.append(headers)
        return HttpResponse(status=200, body=self._payloads.pop(0))


def test_discogs_client_maps_full_release_to_canonical_model(tmp_path) -> None:
    """Discogs fields become normalized canonical artists, release, and tracks."""
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "id": 42,
                    "title": "  Album  ",
                    "artists": [{"id": 7, "name": "Artist (2)"}],
                    "released": "2010-03",
                    "genres": ["Electronic"],
                    "styles": ["House"],
                    "formats": [{"name": "Vinyl"}],
                    "labels": [{"name": "Label", "catno": "CAT-1"}],
                    "tracklist": [{"title": "Track", "duration": "03:30"}],
                }
            )
        ]
    )
    client = DiscogsClient(
        _json_client(tmp_path, "discogs", transport),
        EnvironmentCredentials({"TOKEN": "token"}.get),
        "TOKEN",
    )

    release = client.get_release("42")

    assert release.source is MetadataSources.DISCOGS
    assert release.title == "Album"
    assert release.artists[0].name == "Artist"
    assert release.tracks[0].duration_ms == 210_000
    assert release.labels == ("Label",)
    assert transport.headers[0]["Authorization"] == "Discogs token=token"


def test_a_discogs_search_answer_carries_the_label_and_catalogue_number(tmp_path) -> None:
    """What tells two pressings of one album apart, on the candidates a user picks from.

    In the service's answer `label` is a list of names and `catno` one string —
    so a mapper written for lists alone reads no number.
    """
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "results": [
                        {
                            "type": "release",
                            "id": 7,
                            "title": "Some Artist - Some Album",
                            "label": ["Sample Records", "Other Label"],
                            "catno": " SR 038 ",
                            "barcode": [],
                            "format": ["Vinyl", '12"', "EP"],
                            "year": "2007",
                            "country": "Sweden",
                        },
                        {"type": "release", "id": 8, "title": "No Label - No Number"},
                    ]
                }
            )
        ]
    )
    client = DiscogsClient(
        _json_client(tmp_path, "discogs", transport),
        EnvironmentCredentials({"TOKEN": "token"}.get),
        "TOKEN",
    )

    first, second = client.search_releases(MetadataQuery(artist="Some Artist"))

    assert first.labels == ("Sample Records", "Other Label")
    assert first.catalog_numbers == ("SR 038",)
    assert (second.labels, second.catalog_numbers) == ((), ())


def test_discogs_keeps_how_the_sleeve_credited_and_joined_its_artists(tmp_path) -> None:
    """`anv` and `join` are read, not dropped.

    A Discogs artist entry carries both how this sleeve credited the artist
    (`anv`) and the words it prints before the next one (`join`). A mapper that
    reads neither writes `&` for every collaboration whatever the record says,
    and `&` is only one of the joins a catalogue holds: `,`, `Feat.`, `+`,
    `Presents`, `With` and `Vs.` all occur.
    """
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "id": 42,
                    "title": "The Patience Of Being Marvolento",
                    "artists": [
                        {"id": 7, "name": "Tadeu Brançal", "anv": "", "join": "Presents"},
                        {"id": 8, "name": "A Marvolenta Fanfarra", "anv": "Fanfarra", "join": ""},
                    ],
                    "tracklist": [{"title": "Track", "duration": "03:30"}],
                }
            )
        ]
    )
    client = DiscogsClient(
        _json_client(tmp_path, "discogs", transport),
        EnvironmentCredentials({"TOKEN": "token"}.get),
        "TOKEN",
    )

    release = client.get_release("42")

    assert release.artists[0].joined_by == "Presents", "the sleeve's own word between the two"
    assert release.artists[1].joined_by is None, "and nothing after the last one"
    assert release.artists[0].credited_as is None, "an empty `anv` is not a variation"
    assert release.artists[1].credited_as == "Fanfarra", "how this sleeve credited them"
    assert written_credit(release.artists) == (
        "Tadeu Brançal Presents A Marvolenta Fanfarra"
    ), "the sleeve's join is written, not `&`"


def test_a_comma_between_artists_hugs_the_name_before_it() -> None:
    """One rule for spacing, not a table of separators.

    A comma is the only join written tight against the name it follows;
    everything else — `&`, `Feat.`, `Presents` — stands between spaces.
    """
    artists = (
        ArtistMetadata(name="Olavo Pretti", joined_by=","),
        ArtistMetadata(name="Síncope", joined_by="&"),
        ArtistMetadata(name="Renato Gavião"),
    )

    assert written_credit(artists) == "Olavo Pretti, Síncope & Renato Gavião", (
        "each join is written as the source gave it, the comma tight and the "
        "ampersand between spaces"
    )


def test_a_source_that_publishes_no_separator_still_joins_with_an_ampersand() -> None:
    """MusicBrainz and the tag reader say who, not how; they keep what they had."""
    artists = (ArtistMetadata(name="Jonas Alvim"), ArtistMetadata(name="Nora De Cândara"))

    assert written_credit(artists) == "Jonas Alvim & Nora De Cândara"


def test_musicbrainz_client_maps_full_release_to_canonical_model(tmp_path) -> None:
    """MusicBrainz artist credits, media, and IDs map into canonical metadata."""
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "id": "release-id",
                    "title": "Album",
                    "date": "2014-01-31",
                    "artist-credit": [
                        {
                            "artist": {
                                "id": "artist-id",
                                "name": "Artist",
                                "sort-name": "Artist, The",
                            }
                        }
                    ],
                    "media": [
                        {
                            "format": "CD",
                            "tracks": [
                                {
                                    "id": "track-id",
                                    "title": "Track",
                                    "length": 123000,
                                    "recording": {"isrcs": ["USABC123"]},
                                }
                            ],
                        }
                    ],
                    "label-info": [{"label": {"name": "Label"}, "catalog-number": "CAT-2"}],
                    "genres": [{"name": "Electronic"}],
                    "release-group": {"id": "group-id"},
                }
            )
        ]
    )
    client = MusicBrainzClient(
        _json_client(tmp_path, "musicbrainz", transport),
        EnvironmentCredentials({"CONTACT": "ops@example.com", "TOKEN": "bearer"}.get),
        "CONTACT",
        "TOKEN",
    )

    release = client.get_release("release-id")

    assert release.source is MetadataSources.MUSICBRAINZ
    assert release.artists[0].sort_name == "Artist, The"
    assert release.tracks[0].isrcs == ("USABC123",)
    assert release.release_group_id == "group-id"
    assert transport.headers[0]["Authorization"] == "Bearer bearer"


def test_search_results_are_cached_and_reused(tmp_path) -> None:
    """An identical metadata search performs one transport request then uses local cache."""
    transport = FakeTransport([b'{"results": []}'])
    client = DiscogsClient(
        _json_client(tmp_path, "discogs", transport),
        EnvironmentCredentials({"TOKEN": "token"}.get),
        "TOKEN",
    )
    query = MetadataQuery(artist="Artist", album="Album")

    assert client.search_releases(query) == ()
    assert client.search_releases(query) == ()
    assert len(transport.urls) == 1


def _json_client(tmp_path, source: str, transport: FakeTransport) -> JsonHttpClient:
    return JsonHttpClient(
        source,
        transport,
        JsonMetadataCache(tmp_path / source, ttl_seconds=60),
        RateLimiter(1_000_000, sleep=lambda _: None),
        RetryPolicy(1, 0.0, sleep=lambda _: None),
        1.0,
        logging.getLogger("test.metadata"),
    )


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_an_enhanced_discs_data_portion_is_not_a_track(tmp_path) -> None:
    """The data portion of an enhanced disc is listed beside the music.

    Discogs lists the enhanced portion as a track whose position is the word
    `Enhanced` and whose duration is empty. Counted, it leaves a track no rip
    could ever hold and pushes every real track's number up by one, so track one
    on the sleeve is planned as `02.`.
    """
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "id": 12345678,
                    "title": "Rótulo",
                    "artists": [{"id": 1, "name": "Balão Amarelo"}],
                    "tracklist": [
                        {
                            "type_": "track",
                            "position": "Enhanced",
                            "duration": "",
                            "title": "Trilha Multimídia",
                        },
                        {
                            "type_": "track",
                            "position": "1",
                            "duration": "2:58",
                            "title": "Só Pra Constar",
                        },
                        {
                            "type_": "track",
                            "position": "2",
                            "duration": "3:39",
                            "title": "Vadiagem Dá Um Jeito",
                        },
                    ],
                }
            )
        ]
    )
    client = DiscogsClient(
        _json_client(tmp_path, "discogs", transport),
        EnvironmentCredentials({"TOKEN": "token"}.get),
        "TOKEN",
    )

    release = client.get_release("12345678")

    assert [track.title for track in release.tracks] == [
        "Só Pra Constar",
        "Vadiagem Dá Um Jeito",
    ]
    assert [track.position_on_medium for track in release.tracks] == [1, 2], "as on the sleeve"


def test_a_vinyl_side_with_a_length_is_still_a_track(tmp_path) -> None:
    """`A` carries no digit, so both signs are required before dropping anything."""
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "id": 1,
                    "title": "Album",
                    "tracklist": [
                        {"type_": "track", "position": "A", "duration": "18:20", "title": "Side A"},
                        {"type_": "track", "position": "B", "duration": "17:05", "title": "Side B"},
                    ],
                }
            )
        ]
    )
    client = DiscogsClient(
        _json_client(tmp_path, "discogs", transport),
        EnvironmentCredentials({"TOKEN": "token"}.get),
        "TOKEN",
    )

    release = client.get_release("1")

    assert [track.title for track in release.tracks] == ["Side A", "Side B"]


def test_musicbrainz_data_track_is_dropped_and_the_rest_renumbered(tmp_path) -> None:
    """MusicBrainz writes the same thing its own way: a track called `[data track]`."""
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "id": "mbid",
                    "title": "Rótulo",
                    "media": [
                        {
                            "position": 1,
                            "tracks": [
                                {"id": "t0", "position": 1, "title": "[data track]"},
                                {
                                    "id": "t1",
                                    "position": 2,
                                    "title": "Só Pra Constar",
                                    "length": 178000,
                                },
                                {
                                    "id": "t2",
                                    "position": 3,
                                    "title": "Vadiagem Dá Um Jeito",
                                    "length": 219000,
                                },
                            ],
                        }
                    ],
                }
            )
        ]
    )
    client = MusicBrainzClient(
        _json_client(tmp_path, "musicbrainz", transport),
        EnvironmentCredentials({"CONTACT": "someone@example.com"}.get),
        "CONTACT",
        "TOKEN",
    )

    release = client.get_release("mbid")

    assert [track.title for track in release.tracks] == [
        "Só Pra Constar",
        "Vadiagem Dá Um Jeito",
    ]
    assert [track.position_on_medium for track in release.tracks] == [1, 2]
    assert [track.position for track in release.tracks] == [1, 2]


def test_musicbrainz_browses_every_pressing_of_a_release_group(tmp_path) -> None:
    """An acoustic answer names a recording; only a pressing has a tracklist.

    The shape is the service's own: a group answers with every pressing, and
    not every pressing carries its media.
    """
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "release-count": 2,
                    "releases": [
                        {
                            "id": "0a1b2c3d",
                            "title": "QX7",
                            "date": "2016",
                            "country": "DE",
                            "media": [
                                {
                                    "tracks": [
                                        {
                                            "position": 1,
                                            "title": "Três Vizinhos",
                                            "length": 217000,
                                            "recording": {"id": "4e5f6a7b"},
                                        }
                                    ]
                                }
                            ],
                        },
                        {"id": "8c9d0e1f", "title": "QX7", "date": "2016", "country": "FR"},
                    ],
                }
            )
        ]
    )
    client = MusicBrainzClient(
        _json_client(tmp_path, "musicbrainz", transport),
        EnvironmentCredentials({"CONTACT": "ops@example.com"}.get),
        "CONTACT",
        "TOKEN",
    )

    releases = client.releases_in_group("2b3c4d5e")

    # Every pressing comes back and none is chosen here: which one the files
    # are is a question of durations, and that belongs to the Decision Engine.
    assert [release.source_release_id for release in releases] == ["0a1b2c3d", "8c9d0e1f"]
    assert releases[0].tracks[0].title == "Três Vizinhos"
    assert "release-group=2b3c4d5e" in transport.urls[0]
    assert "recordings" in transport.urls[0]


def test_a_seven_inch_side_with_no_published_length_is_still_a_track(tmp_path) -> None:
    """A single's two sides are tracks, with or without a length.

    A 7" has two sides, `A` and `B`, and Discogs often publishes no length for
    either. A rule that drops an entry whose position has no digit *and* whose
    duration is empty guards against each sign alone and lands squarely on the
    case where both are true: both tracks go, the release arrives with none,
    and two files are reported as matching no release track of a record whose
    titles are exactly theirs.

    A side is not a word. One or two letters is a place on a record; `Enhanced`
    is the thing the rule exists to skip.
    """
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "id": 23456789,
                    "title": "Olavo Pretti Encontra Síncope & Zurvão",
                    "tracklist": [
                        {
                            "type_": "track",
                            "position": "A",
                            "duration": "",
                            "title": "Trinco De Zurvelho ",
                        },
                        {
                            "type_": "track",
                            "position": "B",
                            "duration": "",
                            "title": "Nosso Quelmo ",
                        },
                    ],
                }
            )
        ]
    )
    client = DiscogsClient(
        _json_client(tmp_path, "discogs", transport),
        EnvironmentCredentials({"TOKEN": "token"}.get),
        "TOKEN",
    )

    release = client.get_release("23456789")

    assert [track.title for track in release.tracks] == ["Trinco De Zurvelho", "Nosso Quelmo"]
    assert [track.duration_ms for track in release.tracks] == [None, None]


def test_a_data_track_is_still_dropped_when_it_has_no_length(tmp_path) -> None:
    """The other side of the same rule: accepting a side does not accept a word.

    A word is a word however short: `DVD` and `Video` name a medium, not a place
    on a record, and no rip holds either.
    """
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "id": 2,
                    "title": "Album",
                    "tracklist": [
                        {"type_": "track", "position": "DVD", "duration": "", "title": "Bonus"},
                        {"type_": "track", "position": "Video", "duration": "", "title": "Clip"},
                        {"type_": "track", "position": "A", "duration": "", "title": "The music"},
                    ],
                }
            )
        ]
    )
    client = DiscogsClient(
        _json_client(tmp_path, "discogs", transport),
        EnvironmentCredentials({"TOKEN": "token"}.get),
        "TOKEN",
    )

    release = client.get_release("2")

    assert [track.title for track in release.tracks] == ["The music"]


def test_a_side_written_with_a_full_stop_is_still_a_side(tmp_path) -> None:
    """A tracklist may write its sides `A.` and `B.`.

    A rule that asks for `position.isalpha()` is failed by the dot, and the
    whole record goes back to matching no track.

    Whoever typed the tracklist decides the punctuation, not this rule. The
    letters are what is counted; everything else is ignored.
    """
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "id": 34567890,
                    "title": "Zimbra Prolva",
                    "tracklist": [
                        {
                            "type_": "track",
                            "position": "A.",
                            "duration": "",
                            "title": "Zimbra Prolva",
                        },
                        {"type_": "track", "position": "B.", "duration": "", "title": "Tuvelina"},
                    ],
                }
            )
        ]
    )
    client = DiscogsClient(
        _json_client(tmp_path, "discogs", transport),
        EnvironmentCredentials({"TOKEN": "token"}.get),
        "TOKEN",
    )

    release = client.get_release("34567890")

    assert [track.title for track in release.tracks] == ["Zimbra Prolva", "Tuvelina"]


def test_a_worded_position_is_skipped_however_it_is_punctuated(tmp_path) -> None:
    """Counting letters must not let a word through for wearing a space or a dot."""
    transport = FakeTransport(
        [
            _json_bytes(
                {
                    "id": 3,
                    "title": "Album",
                    "tracklist": [
                        {"type_": "track", "position": "Bonus Track", "duration": "", "title": "X"},
                        {"type_": "track", "position": "Enhanced.", "duration": "", "title": "Y"},
                        {"type_": "track", "position": "A.", "duration": "", "title": "The music"},
                    ],
                }
            )
        ]
    )
    client = DiscogsClient(
        _json_client(tmp_path, "discogs", transport),
        EnvironmentCredentials({"TOKEN": "token"}.get),
        "TOKEN",
    )

    release = client.get_release("3")

    assert [track.title for track in release.tracks] == ["The music"]
