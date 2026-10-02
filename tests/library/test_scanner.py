"""Unit tests for library scanning, grouping, and content signatures."""

import logging
import struct
import wave
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from diglibrary.library.audio import AudioProperties, MutagenAudioProbe
from diglibrary.library.models import AudioFileFacts
from diglibrary.library.scanner import LibraryScanner, the_same_album_more_than_once
from diglibrary.library.signature import content_signature, unit_signature


class FakeProbe:
    """Return stream properties derived from file content, without real media.

    Deriving from content rather than from the path is what makes this double
    faithful: a real probe reads the stream, so renaming a file cannot change
    what it reports.
    """

    def __init__(self, unreadable: frozenset[str] = frozenset()) -> None:
        self._unreadable = unreadable

    def read(self, path: Path) -> AudioProperties | None:
        """Return deterministic properties keyed by the file's bytes."""
        if path.name in self._unreadable:
            return None
        payload = path.read_bytes()
        offset = len(payload) * 1_000 + sum(payload) % 997
        return AudioProperties(
            codec="flac",
            duration_ms=180_000 + offset,
            sample_rate=44_100,
            channels=2,
            total_samples=7_938_000 + offset,
            bit_depth=16,
        )


def test_each_folder_with_audio_is_one_album_unit(tmp_path: Path) -> None:
    """A folder is the unit of organization, and its companion files travel with it."""
    album = tmp_path / "Some Album"
    album.mkdir()
    for name in ("01.flac", "02.flac", "cover.jpg", "album.cue"):
        (album / name).write_bytes(b"x")

    units = _scanner().scan(tmp_path)

    assert len(units) == 1
    assert units[0].folder_path == album.resolve()
    assert units[0].track_count == 2
    assert [path.name for path in units[0].companion_files] == ["album.cue", "cover.jpg"]
    assert units[0].is_loose_track is False


def test_loose_audio_in_the_scan_root_becomes_one_unit_per_file(tmp_path: Path) -> None:
    """A downloads folder is not an album, so its loose tracks are identified alone."""
    (tmp_path / "Some Track.mp3").write_bytes(b"x")
    (tmp_path / "Another Track.flac").write_bytes(b"x")
    album = tmp_path / "Some Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"x")

    units = _scanner().scan(tmp_path)

    loose = [unit for unit in units if unit.is_loose_track]
    assert len(loose) == 2
    assert all(unit.track_count == 1 for unit in loose)
    assert len([unit for unit in units if not unit.is_loose_track]) == 1


def test_a_folder_that_mostly_holds_other_albums_is_not_an_album(tmp_path: Path) -> None:
    """One loose file must not name the folder above a shelf of albums.

    A discography folder: one file sitting beside a shelf of albums. A single
    file identifies as confidently as an album does, so without this the shelf
    would be given that file's name.
    """
    shelf = tmp_path / "Some Artist - Discography"
    shelf.mkdir()
    (shelf / "loose.flac").write_bytes(b"the one stray file")
    for index in range(3):
        album = shelf / f"(197{index}) Some Album"
        album.mkdir()
        for track in range(5):
            (album / f"0{track}.flac").write_bytes(f"album {index} track {track}".encode())

    units = _scanner().scan(tmp_path)

    named_folders = {unit.folder_path for unit in units if not unit.is_loose_track}
    assert shelf.resolve() not in named_folders
    assert len(named_folders) == 3
    loose = [unit for unit in units if unit.is_loose_track]
    assert [unit.folder_path.name for unit in loose] == ["loose.flac"]


def test_one_album_below_is_enough_to_make_a_folder_a_container(tmp_path: Path) -> None:
    """The rule is not a proportion: shelving even one album keeps the name.

    Fourteen own tracks against a three-track subfolder is a folder better
    left alone than renamed on a guess.
    """
    folder = tmp_path / "Some Album (2011)"
    folder.mkdir()
    for index in range(14):
        (folder / f"{index:02d}.flac").write_bytes(f"own track {index}".encode())
    extras = folder / "Extras"
    extras.mkdir()
    for index in range(3):
        (extras / f"e{index}.flac").write_bytes(f"extra {index}".encode())

    units = _scanner().scan(tmp_path)

    named = {unit.folder_path for unit in units if not unit.is_loose_track}
    assert folder.resolve() not in named
    assert extras.resolve() in named
    assert len([unit for unit in units if unit.is_loose_track]) == 14


def test_a_stray_file_beside_an_album_does_not_make_it_a_container(tmp_path: Path) -> None:
    """Only subfolders shelve. Loose audio in the album's own folder is its mess."""
    album = tmp_path / "Some Album"
    album.mkdir()
    for name in ("01.flac", "02.flac", "a stray recording.flac"):
        (album / name).write_bytes(name.encode())

    units = _scanner().scan(tmp_path)

    assert len(units) == 1
    assert units[0].folder_path == album.resolve()
    assert units[0].track_count == 3


def test_unit_signature_survives_renaming_and_reordering(tmp_path: Path) -> None:
    """Renaming a folder and its tracks must not make the album look new."""
    album = tmp_path / "Wrong Name"
    album.mkdir()
    (album / "track_one.flac").write_bytes(b"first track audio")
    (album / "track_two.flac").write_bytes(b"second track audio, different")
    before = _scanner().scan(tmp_path)[0]

    renamed = tmp_path / "Correct Name (1970)"
    album.rename(renamed)
    (renamed / "track_one.flac").rename(renamed / "01. First.flac")
    (renamed / "track_two.flac").rename(renamed / "02. Second.flac")
    after = _scanner().scan(tmp_path)[0]

    assert after.folder_path != before.folder_path
    assert after.unit_signature == before.unit_signature


def test_hidden_and_symlinked_entries_are_ignored(tmp_path: Path) -> None:
    """Scanning must not follow links or descend into hidden directories."""
    album = tmp_path / "Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"x")
    (album / ".hidden.flac").write_bytes(b"x")
    (album / "link.flac").symlink_to(album / "01.flac")
    hidden_folder = tmp_path / ".Trash"
    hidden_folder.mkdir()
    (hidden_folder / "junk.flac").write_bytes(b"x")

    units = _scanner().scan(tmp_path)

    assert len(units) == 1
    assert [file.path.name for file in units[0].audio_files] == ["01.flac"]


def test_an_unreadable_file_is_recorded_rather_than_raised(tmp_path: Path) -> None:
    """A corrupt file must not abort the scan of everything around it."""
    album = tmp_path / "Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"x")
    (album / "02.flac").write_bytes(b"broken")
    scanner = LibraryScanner(FakeProbe(frozenset({"02.flac"})), _logger())

    unit = scanner.scan(tmp_path)[0]

    assert unit.track_count == 2
    assert unit.audio_files[1].properties is None
    assert unit.audio_files[1].content_signature
    assert unit.total_duration_ms is None


def test_a_folder_without_audio_is_not_a_unit(tmp_path: Path) -> None:
    """Artwork-only or document-only folders are not albums."""
    folder = tmp_path / "Scans"
    folder.mkdir()
    (folder / "booklet.pdf").write_bytes(b"x")

    assert _scanner().scan(tmp_path) == ()


def test_scanning_a_missing_root_is_an_error(tmp_path: Path) -> None:
    """A mistyped path fails immediately instead of silently reporting nothing."""
    with pytest.raises(NotADirectoryError):
        _scanner().scan(tmp_path / "does-not-exist")


def test_content_signature_ignores_tags_on_a_real_audio_file(tmp_path: Path) -> None:
    """The signature is computed from the stream, so writing a tag must not change it."""
    path = tmp_path / "tone.wav"
    _write_wave(path)
    probe = MutagenAudioProbe()

    properties = probe.read(path)
    before = content_signature(properties, path.stat().st_size)
    _append_bytes(path, b"ID3 junk appended after the audio data")
    after = content_signature(probe.read(path), path.stat().st_size)

    assert properties is not None
    assert properties.sample_rate == 44_100
    assert properties.channels == 1
    assert after == before


def test_content_signature_distinguishes_different_audio() -> None:
    """Two streams that differ in length or format must not share a signature."""
    base = AudioProperties(
        codec="flac", duration_ms=180_000, sample_rate=44_100, channels=2, total_samples=7_938_000
    )
    longer = AudioProperties(
        codec="flac", duration_ms=181_000, sample_rate=44_100, channels=2, total_samples=7_982_100
    )

    assert content_signature(base, 1) != content_signature(longer, 1)
    assert content_signature(base, 1) == content_signature(base, 999)


def test_unit_signature_is_order_independent() -> None:
    """Member order must not affect the unit, since track names are not authoritative."""
    assert unit_signature(["a", "b"]) == unit_signature(["b", "a"])
    assert unit_signature(["a", "b"]) != unit_signature(["a", "c"])


def _write_wave(path: Path, frames: int = 4_410) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44_100)
        handle.writeframes(b"".join(struct.pack("<h", index % 1000) for index in range(frames)))


def _append_bytes(path: Path, payload: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(payload)


def test_the_folder_of_one_album_is_an_album_when_it_is_the_root(tmp_path: Path) -> None:
    """Scanning one album's own folder gives one album, not one per track.

    If the root were exempt from being an album at all, its audio could only be
    loose tracks, and a folder already named exactly right would be reported as
    thirteen albums.
    """
    album = tmp_path / "Os Zorcas Do Vurco - Plimbino (Remastered - 1994) [FLAC]"
    album.mkdir()
    for index in range(13):
        (album / f"{index:02d}.flac").write_bytes(f"track {index}".encode())

    units = _scanner().scan(album)

    assert len(units) == 1
    assert units[0].track_count == 13
    assert units[0].is_loose_track is False
    assert units[0].folder_path == album.resolve()


def test_a_root_that_shelves_albums_is_still_not_one(tmp_path: Path) -> None:
    """The rule that protects a downloads folder is the container rule, not the exemption.

    A folder with many albums under it and one stray file is not an album of
    one track, whether or not it is the scan root.
    """
    (tmp_path / "loose.flac").write_bytes(b"stray")
    for index in range(2):
        album = tmp_path / f"Some Album {index}"
        album.mkdir()
        for track in range(5):
            (album / f"{track}.flac").write_bytes(f"{index}-{track}".encode())

    units = _scanner().scan(tmp_path)

    assert [unit.folder_path.name for unit in units if unit.is_loose_track] == ["loose.flac"]
    assert tmp_path.resolve() not in {unit.folder_path for unit in units}


def test_one_unreadable_file_does_not_end_the_scan(tmp_path: Path, monkeypatch) -> None:
    """A scan of four thousand folders may not be lost to one file.

    The file is skipped and named in the log; the album it sat in is still
    reported, with what could be read.
    """
    album = tmp_path / "album"
    album.mkdir()
    (album / "aaa.flac").write_bytes(b"first")
    (album / "bbb.flac").write_bytes(b"second")
    real_stat = Path.stat

    def refuse_one(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "bbb.flac":
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", refuse_one)

    units = _scanner().scan(tmp_path)

    assert len(units) == 1
    assert [file.path.name for file in units[0].audio_files] == ["aaa.flac"]


def test_a_folder_that_cannot_be_listed_is_reported_not_swallowed(tmp_path: Path, caplog) -> None:
    """os.walk hides these by default, and a count nobody can verify is worse than none."""
    readable = tmp_path / "album"
    readable.mkdir()
    (readable / "aaa.flac").write_bytes(b"first")
    closed = tmp_path / "closed"
    closed.mkdir()
    (closed / "bbb.flac").write_bytes(b"second")
    closed.chmod(0o000)
    try:
        if [entry for entry in closed.iterdir()]:  # pragma: no cover - running as root
            pytest.skip("this user can read a folder with no permissions")
    except PermissionError:
        pass

    try:
        with caplog.at_level(logging.WARNING, logger="test.library"):
            units = _scanner().scan(tmp_path)
    finally:
        closed.chmod(0o700)

    assert [unit.folder_path.name for unit in units] == ["album"]
    assert any("could not be read" in record.message for record in caplog.records)


def test_a_folder_holding_the_album_twice_is_sent_to_review(tmp_path: Path) -> None:
    """Every song present twice, under two naming conventions.

    Read as an album of twice the length it matches no release of that record,
    falls through to a larger unrelated one, and files are paired with another
    song because the aligner must give each file a distinct track.
    """
    album = tmp_path / "Coastal Beats Vol 2"
    album.mkdir()
    for track in range(1, 4):
        audio = f"track {track}".encode()
        (album / f"0{track} Song.flac").write_bytes(audio)
        (album / f"0{track}. Artist - Song.flac").write_bytes(audio)

    units = _scanner().scan(album)

    assert len(units) == 1
    assert "6 files that are only 3 tracks, each of them present 2 times" in (
        units[0].ambiguous_layout
    )


def test_one_repeated_track_is_a_collision_and_not_a_second_copy(tmp_path: Path) -> None:
    """Two different recordings of one length wear one signature.

    Many groups of files that share a signature are provably different music,
    so a rule that fired on any repetition would call those albums duplicates.
    """
    album = tmp_path / "Some Artist - Album (1999)"
    album.mkdir()
    (album / "01.flac").write_bytes(b"one")
    (album / "02.flac").write_bytes(b"two")
    (album / "03.flac").write_bytes(b"two")

    units = _scanner().scan(album)

    assert not units[0].ambiguous_layout


def test_the_audio_key_decides_and_not_the_declared_shape() -> None:
    """Same signature, different audio: a length collision twice over, not a copy."""
    doubled = tuple(
        AudioFileFacts(
            path=Path(f"/library/{name}.flac"),
            content_signature="one-shape",
            file_size_bytes=1,
            modified_at=datetime(2001, 2, 3, tzinfo=UTC),
            audio_key=key,
        )
        for name, key in (("a", "flac:1"), ("b", "flac:1"), ("c", "flac:2"), ("d", "flac:2"))
    )
    collided = tuple(
        replace(file, audio_key=key)
        for file, key in zip(doubled, ("flac:1", "flac:2", "flac:3", "flac:4"), strict=True)
    )

    assert the_same_album_more_than_once(doubled)
    assert not the_same_album_more_than_once(collided)


def test_one_track_repeated_alone_is_left_out_because_there_is_no_album_shape() -> None:
    """With nothing else to agree with it, a single repeated key says nothing."""
    twice = tuple(
        AudioFileFacts(
            path=Path(f"/library/{name}.flac"),
            content_signature="one-shape",
            file_size_bytes=1,
            modified_at=datetime(2001, 2, 3, tzinfo=UTC),
            audio_key="flac:1",
        )
        for name in ("a", "b")
    )

    assert not the_same_album_more_than_once(twice)


def _scanner() -> LibraryScanner:
    return LibraryScanner(FakeProbe(), _logger())


def _logger() -> logging.Logger:
    return logging.getLogger("test.library")


def test_disc_subfolders_become_one_multi_disc_album(tmp_path: Path) -> None:
    """A rip split into CD1 and CD2 is one album, not two."""
    album = tmp_path / "Some Artist - Anthology (1998)"
    (album / "CD1").mkdir(parents=True)
    (album / "CD 2").mkdir()
    (album / "CD1" / "01.flac").write_bytes(b"disc one track one")
    (album / "CD1" / "02.flac").write_bytes(b"disc one track two")
    (album / "CD 2" / "01.flac").write_bytes(b"disc two track one")
    (album / "cover.jpg").write_bytes(b"art")

    units = _scanner().scan(tmp_path)

    assert len(units) == 1
    assert units[0].folder_path == album.resolve()
    assert units[0].track_count == 3
    assert units[0].disc_count == 2
    assert sorted({file.disc_hint for file in units[0].audio_files}) == [1, 2]
    assert [path.name for path in units[0].companion_files] == ["cover.jpg"]


def test_a_volume_folder_is_a_separate_album(tmp_path: Path) -> None:
    """Vol. 2 is another release, not another disc of the same one."""
    parent = tmp_path / "Orquestra Zorca-Plimbeira"
    (parent / "Vol 1").mkdir(parents=True)
    (parent / "Vol 2").mkdir()
    (parent / "Vol 1" / "01.flac").write_bytes(b"first volume")
    (parent / "Vol 2" / "01.flac").write_bytes(b"second volume")

    units = _scanner().scan(tmp_path)

    assert len(units) == 2
    assert all(unit.disc_count == 1 for unit in units)


def test_a_disc_folder_beside_audio_in_its_parent_goes_to_review(tmp_path: Path) -> None:
    """An ambiguous layout is never merged silently — and never split silently either.

    Both halves are marked and neither is planned. Splitting is not a neutral
    option, because the two halves then both want the whole album's name and
    collide.
    """
    album = tmp_path / "Album"
    (album / "CD1").mkdir(parents=True)
    (album / "01.flac").write_bytes(b"parent audio")
    (album / "CD1" / "01.flac").write_bytes(b"disc audio")

    units = _scanner().scan(tmp_path)

    assert len(units) == 2
    assert all(unit.disc_count == 1 for unit in units)
    assert all(unit.ambiguous_layout for unit in units), "both halves ask rather than guess"


def test_a_disc_folder_shelved_among_albums_has_no_album_to_belong_to(tmp_path: Path) -> None:
    """A disc folder beside other albums is not an album.

    The folder around it holds other albums, so it cannot be the album this
    disc belongs to, and nothing in the scan can name it.
    """
    (tmp_path / "Some Artist - Album (1999)").mkdir()
    (tmp_path / "Some Artist - Album (1999)" / "01.flac").write_bytes(b"an album")
    (tmp_path / "CD2").mkdir()
    (tmp_path / "CD2" / "01.flac").write_bytes(b"an orphan disc")

    units = {unit.folder_path.name: unit for unit in _scanner().scan(tmp_path)}

    assert units["CD2"].ambiguous_layout
    assert not units["Some Artist - Album (1999)"].ambiguous_layout


def test_a_disc_folder_scanned_on_its_own_never_reaches_for_its_parent(tmp_path: Path) -> None:
    """Pointing the app at `CD2` itself must not plan a rename above the scan root.

    The album that would name this disc is the folder above, which the scan was
    not pointed at and may not touch.
    """
    disc = tmp_path / "CD2"
    disc.mkdir()
    (disc / "01.flac").write_bytes(b"disc two track one")

    units = _scanner().scan(disc)

    assert len(units) == 1
    assert units[0].folder_path == disc.resolve(), "the unit is the disc, never its parent"
    assert units[0].ambiguous_layout


def test_an_album_scanned_by_its_own_folder_still_joins_its_discs(tmp_path: Path) -> None:
    """Scanning the album folder itself gives one album, not one per disc.

    The root is judged by the same rules as any other folder, so the discs
    below it belong to it exactly as they would one level down.
    """
    album = tmp_path / "Some Artist - Anthology (1998)"
    (album / "CD1").mkdir(parents=True)
    (album / "CD2").mkdir()
    (album / "CD1" / "01.flac").write_bytes(b"disc one")
    (album / "CD2" / "01.flac").write_bytes(b"disc two")

    units = _scanner().scan(album)

    assert len(units) == 1
    assert units[0].folder_path == album.resolve()
    assert units[0].disc_count == 2
    assert not units[0].ambiguous_layout
