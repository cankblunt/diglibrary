"""Tag round-trip tests against real audio files in every supported format."""

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.audio import AudioProperties
from diglibrary.library.matching import TrackAssignment
from diglibrary.library.models import AudioFileFacts
from diglibrary.library.tags import MutagenTagStore, TagError, desired_tags

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"
FORMATS = ("flac", "mp3", "m4a", "wav", "aiff")


@pytest.mark.parametrize("extension", FORMATS)
def test_every_supported_format_round_trips_the_core_tags(tmp_path: Path, extension: str) -> None:
    """A value written must come back identical, in every container this project accepts."""
    path = _fixture(tmp_path, extension)
    store = MutagenTagStore()
    written = {
        "title": ("Vão de Zorvelas",),
        "artist": ("Marina do Acordeão",),
        "albumartist": ("Marina do Acordeão",),
        "album": ("Forró",),
        "date": ("1955-03-01",),
        "tracknumber": ("3",),
        "totaltracks": ("12",),
    }

    store.write(path, written)
    read_back = store.read(path)

    assert read_back["title"] == ("Vão de Zorvelas",)
    assert read_back["artist"] == ("Marina do Acordeão",)
    assert read_back["album"] == ("Forró",)
    assert read_back["date"][0].startswith("1955")
    assert read_back["tracknumber"][0].startswith("3")


@pytest.mark.parametrize("extension", FORMATS)
def test_writing_tags_never_changes_the_audio_stream(tmp_path: Path, extension: str) -> None:
    """Tagging must leave the audio itself untouched, which the content signature relies on."""
    from diglibrary.library.audio import MutagenAudioProbe
    from diglibrary.library.signature import content_signature

    path = _fixture(tmp_path, extension)
    probe = MutagenAudioProbe()
    before = content_signature(probe.read(path), path.stat().st_size)

    MutagenTagStore().write(path, {"title": ("Something Else",), "album": ("Another Album",)})

    assert content_signature(probe.read(path), path.stat().st_size) == before


@pytest.mark.parametrize("extension", ("flac", "mp3", "m4a"))
def test_catalogue_fields_round_trip_where_the_container_supports_them(
    tmp_path: Path, extension: str
) -> None:
    """Label, catalogue number, and source identifiers survive a write."""
    path = _fixture(tmp_path, extension)
    store = MutagenTagStore()

    store.write(
        path,
        {
            "title": ("Track",),
            "catalognumber": ("CAT-1",),
            "isrc": ("BRABC1234567",),
            "discogs_release_id": ("1000001",),
            "musicbrainz_albumid": ("abc-123",),
        },
    )
    read_back = store.read(path)

    assert read_back["catalognumber"] == ("CAT-1",)
    assert read_back["isrc"] == ("BRABC1234567",)
    assert read_back["discogs_release_id"] == ("1000001",)
    assert read_back["musicbrainz_albumid"] == ("abc-123",)


def test_a_multi_valued_artist_survives_a_round_trip(tmp_path: Path) -> None:
    """A collaboration credits every artist, and re-reading must not collapse them."""
    path = _fixture(tmp_path, "flac")
    store = MutagenTagStore()

    store.write(path, {"title": ("ZORCA",), "artist": ("ZBC", "VHORR")})

    assert store.read(path)["artist"] == ("ZBC", "VHORR")


def test_writing_removes_a_field_the_release_does_not_have(tmp_path: Path) -> None:
    """Stale values from a previous tool must not survive alongside correct ones."""
    path = _fixture(tmp_path, "flac")
    store = MutagenTagStore()
    store.write(path, {"title": ("Old",), "catalognumber": ("STALE-9",)})

    store.write(path, {"title": ("New",)})
    read_back = store.read(path)

    assert read_back["title"] == ("New",)
    assert "catalognumber" not in read_back


def test_an_unreadable_file_raises_a_tag_error(tmp_path: Path) -> None:
    """A file that is not audio fails as a tag error, not an arbitrary library error."""
    path = tmp_path / "not-audio.flac"
    path.write_bytes(b"definitely not a FLAC stream")

    with pytest.raises(TagError):
        MutagenTagStore().read(path)


def test_desired_tags_takes_every_value_from_the_release() -> None:
    """Nothing written is inferred from the file's current state."""
    release = _release()
    assignment = _assignment(release.tracks[0], track_number=1, disc_number=1)

    tags = desired_tags(release, assignment, total_tracks=2, total_discs=1)

    assert tags["title"] == ("Primeira",)
    assert tags["album"] == ("Forró",)
    assert tags["date"] == ("1955-03-01",)
    assert tags["organization"] == ("Selo",)
    assert tags["catalognumber"] == ("CAT-1",)
    assert tags["discogs_release_id"] == ("r1000001",)
    assert "discnumber" not in tags
    assert "compilation" not in tags


def test_desired_tags_marks_a_compilation_and_keeps_the_track_artist() -> None:
    """A compilation credits each track's own artist and tags the album as Various."""
    release = _release(album_artist="Various")
    assignment = _assignment(release.tracks[0], track_number=1, disc_number=1)

    tags = desired_tags(release, assignment, total_tracks=2, total_discs=1)

    assert tags["albumartist"] == ("Various Artists",)
    assert tags["artist"] == ("Some Guest",)
    assert tags["compilation"] == ("1",)


def test_desired_tags_omits_what_the_source_did_not_publish() -> None:
    """A missing field stays absent rather than being filled with a placeholder."""
    release = ReleaseMetadata(
        source=MetadataSources.MUSICBRAINZ,
        source_release_id="mb-1",
        title="Untitled",
        artists=(ArtistMetadata(name="Artist"),),
        tracks=(TrackMetadata(title="Track", position=1, duration_ms=1_000),),
    )
    assignment = _assignment(release.tracks[0], track_number=1, disc_number=1)

    tags = desired_tags(release, assignment, total_tracks=1, total_discs=1)

    assert "date" not in tags
    assert "genre" not in tags
    assert "catalognumber" not in tags
    assert tags["musicbrainz_albumid"] == ("mb-1",)


def test_the_genre_tag_leads_with_the_styles_and_keeps_the_genres() -> None:
    """Styles first, genres after or alone, each in one spelling whichever source said it."""
    track = TrackMetadata(title="Maré Alta", position=1, duration_ms=1_000)
    styled = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Zorvizonte",
        artists=(ArtistMetadata(name="Lívia Serrano"),),
        tracks=(track,),
        genres=("Jazz", "Latin"),
        styles=("MPB", "Latin", "Contemporary r&b"),
    )
    unstyled = ReleaseMetadata(
        source=MetadataSources.MUSICBRAINZ,
        source_release_id="mb-1",
        title="Zorvizonte",
        artists=(ArtistMetadata(name="Lívia Serrano"),),
        tracks=(track,),
        genres=("mpb", "jazz funk", "alternative hip hop"),
    )
    assignment = _assignment(track, track_number=1, disc_number=1)

    assert desired_tags(styled, assignment, total_tracks=1, total_discs=1)["genre"] == (
        "MPB",
        "Latin",
        "Contemporary R&B",
        "Jazz",
    )
    assert desired_tags(unstyled, assignment, total_tracks=1, total_discs=1)["genre"] == (
        "MPB",
        "Jazz-Funk",
        "Alternative Hip Hop",
    )


def _fixture(tmp_path: Path, extension: str) -> Path:
    destination = tmp_path / f"track.{extension}"
    shutil.copy(FIXTURES / f"tone.{extension}", destination)
    return destination


def _release(album_artist: str = "Marina do Acordeão") -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1000001",
        title="Forró",
        artists=(ArtistMetadata(name=album_artist),),
        tracks=(
            TrackMetadata(
                title="Primeira",
                position=1,
                artists=(ArtistMetadata(name="Some Guest"),),
                duration_ms=180_000,
            ),
            TrackMetadata(title="Segunda", position=2, duration_ms=240_000),
        ),
        released_on=date(1955, 3, 1),
        labels=("Selo",),
        catalog_numbers=("CAT-1",),
        country="BR",
    )


def _assignment(track: TrackMetadata, track_number: int, disc_number: int) -> TrackAssignment:
    return TrackAssignment(
        file=AudioFileFacts(
            path=Path("/music/Album/01.flac"),
            content_signature="signature",
            file_size_bytes=1,
            modified_at=datetime(2001, 2, 3, tzinfo=UTC),
            properties=AudioProperties(
                codec="flac", duration_ms=180_000, sample_rate=44_100, channels=2
            ),
        ),
        track=track,
        disc_number=disc_number,
        track_number=track_number,
        duration_delta_ms=0,
    )


def test_a_reissue_is_tagged_with_the_album_s_own_year() -> None:
    """A player must show 1974 for a 1974 album, whatever pressing was matched."""
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="A Plimba de Zorvalda",
        artists=(ArtistMetadata(name="Jonas Bem Zor"),),
        tracks=(TrackMetadata(title="Os Vurquimistas", position=1, duration_ms=1),),
        released_on=date(2006, 1, 1),
        original_released_on=date(1974, 1, 1),
    )
    assignment = _assignment(release.tracks[0], track_number=1, disc_number=1)

    tags = desired_tags(release, assignment, total_tracks=1, total_discs=1)

    assert tags["date"] == ("1974-01-01",)
    assert tags["originaldate"] == ("1974-01-01",)
    assert tags["releasedate"] == ("2006-01-01",), "the pressing's own date is not lost"


def test_a_compilation_credited_to_its_curator_writes_the_compilation_tags() -> None:
    """The same rule, on the layer beside the file name.

    `is_various` decides three things at once, and a rule that reached the name
    and not the tags would leave a player grouping unrelated songs under the
    curator. Written here as well as in `test_naming.py` because they are two
    layers, and a rule has to hold on both.
    """
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="c1",
        title="É Vorca Plimba Vol. 2",
        artists=(ArtistMetadata(name="Zeke Uno"),),
        tracks=(
            TrackMetadata(
                title="Zildren Of The Vorca",
                position=1,
                artists=(ArtistMetadata(name="The Plim Thing"),),
                duration_ms=278_000,
            ),
            TrackMetadata(
                title="Zaby, This Vorca I Have",
                position=2,
                artists=(ArtistMetadata(name="Zavis"),),
                duration_ms=279_000,
            ),
        ),
    )
    assignment = _assignment(release.tracks[0], track_number=1, disc_number=1)

    tags = desired_tags(release, assignment, total_tracks=2, total_discs=1)

    assert tags["compilation"] == ("1",)
    assert tags["albumartist"] == ("Various Artists",)
    assert tags["artist"] == ("The Plim Thing",)
