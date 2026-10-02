"""Tests for the bench: folders that stay, verdicts that are re-judged."""

import logging
import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from diglibrary.application.bench import QualityBench
from diglibrary.database.connection import Database
from diglibrary.database.library_store import LibraryStore, StoredQuality
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.scanner import LibraryScanner
from diglibrary.quality.analysis import DEEP_PROBE_FREQUENCIES, FfmpegQualityAnalyzer
from diglibrary.quality.confidence import Sureness

FIXTURES = Path(__file__).parents[1] / "fixtures" / "audio"


def test_adding_a_folder_reads_what_is_under_it_and_decodes_nothing(tmp_path: Path) -> None:
    """The walk reads headers. Decoding audio is a separate, explicit request.

    The bench is a reading surface first: adding a folder never starts a
    measurement.
    """
    bench, _store, library = _bench(tmp_path)
    _album(library, "Some Album")

    root = bench.add(library)

    assert root.albums == 1
    assert root.walked_at is not None
    finding = bench.findings()[0]
    assert finding.album.folder_path.name == "Some Album"
    # Nothing was measured, and the bench says so rather than reporting it fine.
    assert finding.analyzed == 0
    assert finding.confidence.sureness is Sureness.WEAK


def test_the_bench_reads_its_verdict_back_out_of_what_is_on_record(tmp_path: Path) -> None:
    """Opening the bench costs a database read, not a measurement.

    The verdict is judged from the stored numbers on every read, because a
    stored verdict cannot be trusted across a rule change.
    """
    bench, store, library = _bench(tmp_path)
    album = _album(library, "Measured")
    bench.add(library)
    store.record_quality(
        {file.content_signature: _measurement("lossless") for file in album.audio_files}
    )

    finding = bench.findings()[0]

    assert str(finding.verdict.encoding) == "lossless"
    assert finding.analyzed == len({file.content_signature for file in album.audio_files})
    assert finding.confidence.sureness is Sureness.STRONG


def test_a_measurement_more_than_one_file_reads_lowers_the_confidence(tmp_path: Path) -> None:
    """A measurement that more than one file reads lowers the confidence.

    Nothing is repaired and nothing is re-measured by the bench itself. The
    doubt is stated next to the verdict, and the way out is named.
    """
    bench, store, library = _bench(tmp_path)
    folder = library / "Two Songs One Shape"
    folder.mkdir(parents=True)
    _colliding_pair(folder)
    bench.add(library)
    signature = bench.findings()[0].album.files[0].content_signature
    store.record_quality({signature: _measurement("lossless")})

    finding = bench.findings()[0]

    assert finding.shared == 2
    assert finding.confidence.sureness is Sureness.WEAK
    assert "in depth" in finding.confidence.reasons[0]


def test_a_measurement_taken_from_this_very_audio_is_not_doubted(tmp_path: Path) -> None:
    """The doubt is about a row nobody can attribute, not about sharing a signature."""
    bench, store, library = _bench(tmp_path)
    folder = library / "Two Songs One Shape"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "tone.flac", folder / "01. First.flac")
    shutil.copy(FIXTURES / "tone.flac", folder / "02. Second.flac")
    bench.add(library)
    album = bench.findings()[0].album
    signature = album.files[0].content_signature

    # The walk already named the audio of both files, so this measurement
    # can say which of the two it was taken from.
    store.record_quality({signature: _measurement("lossless", audio_key=album.files[0].audio_key)})

    finding = bench.findings()[0]
    assert finding.shared == 0
    assert finding.confidence.sureness is Sureness.STRONG


def test_an_album_that_left_the_folder_leaves_the_bench(tmp_path: Path) -> None:
    """A walk answers what is under the folder now, which is why it replaces."""
    bench, _store, library = _bench(tmp_path)
    _album(library, "Stays")
    _album(library, "Leaves")
    root = bench.add(library)

    shutil.rmtree(library / "Leaves")
    bench.walk(root.root_id)

    assert [finding.album.folder_path.name for finding in bench.findings()] == ["Stays"]


def test_taking_the_folder_off_the_bench_leaves_the_measurements(tmp_path: Path) -> None:
    """Removing a root is a screen gesture. Putting it back costs a walk, not decoding."""
    bench, store, library = _bench(tmp_path)
    album = _album(library, "Album")
    root = bench.add(library)
    store.record_quality(
        {file.content_signature: _measurement("lossless") for file in album.audio_files}
    )

    bench.remove(root.root_id)

    assert bench.findings() == ()
    assert bench.add(library).albums == 1
    assert bench.findings()[0].analyzed > 0, "nothing had to be decoded again"


def test_a_stated_verdict_outranks_the_measurement_on_the_bench(tmp_path: Path) -> None:
    """A verdict stated by the user outranks the measurement on the bench.

    A bench that draws the measurement alone ignores the override and the
    control looks broken. The numbers stay beside the stated verdict, because
    a disagreement that erases its evidence cannot be audited or withdrawn.
    """
    bench, store, library = _bench(tmp_path)
    album = _album(library, "Album")
    bench.add(library)
    store.record_quality(
        {file.content_signature: _measurement("transcoded") for file in album.audio_files}
    )
    assert str(bench.findings()[0].verdict.encoding) == "transcoded"

    store.record_quality_override(album.unit_signature, "honest")

    finding = bench.findings()[0]
    assert str(finding.verdict.encoding) == "lossless"
    assert finding.spoken == 1, "a row reading a stated verdict must not look like a measured one"
    # And what it measured is still there to disagree with.
    assert "22.1" in finding.verdict.reason or "Replayed" in finding.verdict.reason


def test_withdrawing_a_stated_verdict_gives_the_measurement_back(tmp_path: Path) -> None:
    """An override is withdrawn, never stacked. The album returns to what it measured."""
    bench, store, library = _bench(tmp_path)
    album = _album(library, "Album")
    bench.add(library)
    store.record_quality(
        {file.content_signature: _measurement("transcoded") for file in album.audio_files}
    )
    store.record_quality_override(album.unit_signature, "honest")

    store.clear_quality_override(album.unit_signature)

    finding = bench.findings()[0]
    assert str(finding.verdict.encoding) == "transcoded"
    assert finding.spoken == 0


def test_a_deep_measurement_writes_nothing_until_it_is_adopted(tmp_path: Path) -> None:
    """The analysis is a proposal, like a plan waiting for `Approve & apply`.

    It reports what would change; what is stored is untouched until `adopt`,
    and dropping the proposal costs only the time already spent.
    """
    bench, store, library = _bench(tmp_path, deep=_deep_analyzer(cutoff=16_000))
    album = _album(library, "Album")
    bench.add(library)
    store.record_quality(
        {file.content_signature: _measurement("lossless") for file in album.audio_files}
    )
    album_id = bench.findings()[0].album.album_id

    analysis = bench.analyze_in_depth([album_id])

    # Measured, and the verdict it would move to is on the proposal…
    assert analysis.files == len(album.audio_files)
    assert analysis.albums[0].verdict_moved
    assert str(analysis.albums[0].after.encoding) == "transcoded"
    # …while the bench still reads what was on record.
    assert str(bench.findings()[0].verdict.encoding) == "lossless"

    written = bench.adopt(analysis)

    assert written > 0
    assert str(bench.findings()[0].verdict.encoding) == "transcoded"


def test_what_a_deep_measurement_writes_names_the_audio_it_came_from(tmp_path: Path) -> None:
    """Every fresh row answers for its own file and never for whatever shares its shape.

    This is the point of measuring one album instead of sweeping a library:
    afterwards the doubt is gone for these files, and only for these.
    """
    bench, store, library = _bench(tmp_path, deep=_deep_analyzer(cutoff=None))
    folder = library / "Two Songs One Shape"
    folder.mkdir(parents=True)
    _colliding_pair(folder)
    bench.add(library)
    signature = bench.findings()[0].album.files[0].content_signature
    store.record_quality({signature: _measurement("transcoded")})
    assert bench.findings()[0].shared == 2

    written = bench.adopt(bench.analyze_in_depth([bench.findings()[0].album.album_id]))

    # One row per file, not one per shape. Collecting the measurements into a
    # dictionary keyed by signature keeps only the last file of each
    # signature, and the others go on reading whatever stood before.
    assert written == 2, "a measurement was dropped between the pass and the store"
    finding = bench.findings()[0]
    assert finding.shared == 0, "these files no longer read a borrowed measurement"
    assert str(finding.verdict.encoding) == "lossless"
    assert finding.confidence.sureness is Sureness.STRONG


def test_the_band_by_band_reading_comes_back_with_the_measurement(tmp_path: Path) -> None:
    """The band-by-band reading travels with the proposal, so no other tool is needed."""
    bench, _store, library = _bench(tmp_path, deep=_deep_analyzer(cutoff=None))
    _album(library, "Album")
    bench.add(library)

    analysis = bench.analyze_in_depth([bench.findings()[0].album.album_id])

    bands = analysis.albums[0].tracks[0].bands
    assert [band.low_hertz for band in bands] == list(range(15_000, 22_000, 1_000))
    assert all(band.high_hertz - band.low_hertz == 1_000 for band in bands)


def _deep_analyzer(cutoff: int | None) -> FfmpegQualityAnalyzer:
    """An analyzer over a runner that replays reports instead of decoding.

    ``cutoff`` is where the audio is made to stop: a band at or below it reads
    as silence, which is a wall, and everything else reads as ordinary music.
    """

    class Runner:
        def run(self, arguments: Sequence[str]) -> str:
            joined = " ".join(arguments)
            level = -50.0
            # ffmpeg reports the depth once per measure, four values in a row.
            depth = "/".join(["16"] * 4)
            match = re.search(r"lt\(f,(\d+)\)", joined) or re.search(r"between\(f,(\d+)", joined)
            if match is not None and cutoff is not None and int(match.group(1)) >= cutoff:
                level = -120.0
            return (
                "  Duration: 00:03:20.00, bitrate: 900 kb/s\n"
                "  Stream #0:0: Audio: flac, 44100 Hz, stereo, s16\n"
                "[Parsed_astats_0 @ 0x1] DC offset: 0.0\n"
                "[Parsed_astats_0 @ 0x1] Peak level dB: -1.2\n"
                f"[Parsed_astats_0 @ 0x1] RMS level dB: {level}\n"
                "[Parsed_astats_0 @ 0x1] Flat factor: 0.000000\n"
                "[Parsed_astats_0 @ 0x1] Abs Peak count: 4\n"
                "[Parsed_astats_0 @ 0x1] Noise floor dB: -60.0\n"
                f"[Parsed_astats_0 @ 0x1] Bit depth: {depth}\n"
            )

    return FfmpegQualityAnalyzer(
        Runner(), logging.getLogger("test.deep"), frequencies=DEEP_PROBE_FREQUENCIES, seconds=None
    )


def _bench(
    tmp_path: Path, deep: FfmpegQualityAnalyzer | None = None
) -> tuple[QualityBench, LibraryStore, Path]:
    logger = logging.getLogger("test.bench")
    database = Database(tmp_path / "library.sqlite3", logger)
    database.initialize()
    store = LibraryStore(database, logger)
    scanner = LibraryScanner(MutagenAudioProbe(), logger)
    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    return QualityBench(scanner, store, logger, deep=deep), store, library


def _album(library: Path, name: str, tracks: int = 2):
    folder = library / name
    folder.mkdir(parents=True, exist_ok=True)
    sources = ("tone.flac", "tone-long.flac")
    for index in range(tracks):
        shutil.copy(
            FIXTURES / sources[index % len(sources)], folder / f"{index + 1:02d}. Track.flac"
        )
    scanner = LibraryScanner(MutagenAudioProbe(), logging.getLogger("test.bench"))
    return next(unit for unit in scanner.scan(library) if unit.folder_path.name == name)


def _measurement(encoding: str, audio_key: str | None = None) -> StoredQuality:
    """A stored row whose NUMBERS say what its word says.

    They have to agree, because a stored verdict is never trusted across a rule
    change: every read re-judges the row from its decibels. A fixture that
    writes `transcoded` over a gentle fall reads back lossless, correctly, and
    a test using it would be testing the fixture.
    """
    transcoded = encoding == "transcoded"
    return StoredQuality(
        encoding=encoding,
        effective_bitrate_kbps=256 if transcoded else None,
        cutoff_hertz=None,
        steepest_drop_db=22.1 if transcoded else 8.0,
        findings=("transcoded",) if transcoded else (),
        reason=f"measured {encoding}",
        decay_db=22.1 if transcoded else 6.1,
        ceiling_db=-104.3 if transcoded else -80.1,
        audio_key=audio_key,
    )


class _KnowsOne:
    """A fingerprint service that recognizes one file and nothing else."""

    def __init__(self, known: str) -> None:
        self._known = known
        self.asked: list[str] = []

    def recognize(self, path: object, content_signature: str) -> str | None:
        """Answer for this audio, and remember having been asked about it."""
        self.asked.append(Path(path).name)
        return "rec-1" if Path(path).name == self._known else None


def test_the_audio_of_marked_albums_can_be_put_to_the_service(tmp_path: Path) -> None:
    """Every file of the selected albums is put to the fingerprint service.

    The identification path samples an album and only when its names failed,
    so most files are never asked about. This request fills that in, per
    album and never over a library, because the service is rate-limited.

    It returns what the round cost and what it got back: `named` is how many
    files the service recognised.
    """
    library = tmp_path / "library"
    folder = library / "an album"
    folder.mkdir(parents=True)
    for name in ("01.flac", "02.flac"):
        shutil.copy(FIXTURES / "tone.flac", folder / name)
    (folder / "02.flac").write_bytes((FIXTURES / "tone-long.flac").read_bytes())
    logger = logging.getLogger("test.bench.acoustic")
    database = Database(tmp_path / "library.sqlite3", logger)
    database.initialize()
    store = LibraryStore(database, logger)
    service = _KnowsOne("01.flac")
    bench = QualityBench(
        LibraryScanner(MutagenAudioProbe(), logger), store, logger, acoustic=service
    )
    bench.add(library)
    album = store.bench_albums()[0]

    swept = bench.identify_in_depth([album.album_id])

    assert sorted(service.asked) == ["01.flac", "02.flac"], "every file of the album is asked"
    assert (swept.albums, swept.files, swept.named) == (
        1,
        2,
        1,
    ), "the answer says what it cost and how much of it the service knew"


def test_marking_every_track_lossy_makes_the_album_say_so(tmp_path: Path) -> None:
    """Every track marked lossy makes the album's header say lossy.

    A track override that decided only its track would leave the header
    announcing `LOSSLESS` over tracks all marked otherwise. The album is
    re-read by the same majority the measurement itself uses
    (`walls * 2 > len`), so overrides and the analyzer's findings are counted
    by one rule.
    """
    bench, store, library = _bench(tmp_path)
    album = _album(library, "Album", tracks=4)
    bench.add(library)
    store.record_quality(
        {file.content_signature: _measurement("lossless") for file in album.audio_files}
    )
    assert str(bench.findings()[0].verdict.encoding) == "lossless"

    for file in album.audio_files:
        store.record_quality_override(album.unit_signature, "transcoded", file.content_signature)

    finding = bench.findings()[0]
    assert str(finding.verdict.encoding) == "transcoded", "every track was marked"
    assert all(str(track.encoding) == "transcoded" for track in finding.verdict.tracks)


def test_one_corrected_track_does_not_turn_the_album_over(tmp_path: Path) -> None:
    """A majority, not a vote of one.

    Correcting a single track says something about that track. The
    measurement never lets one track speak for an album, and an override is
    counted by the same rule.
    """
    bench, store, library = _bench(tmp_path)
    album = _album(library, "Album", tracks=4)
    bench.add(library)
    store.record_quality(
        {file.content_signature: _measurement("lossless") for file in album.audio_files}
    )

    store.record_quality_override(
        album.unit_signature, "transcoded", album.audio_files[0].content_signature
    )

    finding = bench.findings()[0]
    assert str(finding.verdict.encoding) == "lossless", "one of four is not the album"
    assert str(finding.verdict.tracks[0].encoding) == "transcoded", "the track keeps its override"


def test_the_album_word_nothing_can_write_still_answers_when_it_is_there(
    tmp_path: Path,
) -> None:
    """An album-wide override is still read, although the window cannot write one.

    The bench draws `verdictControls` always with a file, so the window writes
    overrides per track only. Album-wide rows may exist in a database, and a
    stored override must not start being ignored. The two rules must not
    disagree: an album-wide override decides outright, and the majority of the
    tracks decides only when there is none.
    """
    bench, store, library = _bench(tmp_path)
    album = _album(library, "Album", tracks=4)
    bench.add(library)
    store.record_quality(
        {file.content_signature: _measurement("transcoded") for file in album.audio_files}
    )

    store.record_quality_override(album.unit_signature, "honest")

    finding = bench.findings()[0]
    assert str(finding.verdict.encoding) == "lossless", (
        "an album-wide override outranks the tracks under it, and the "
        "majority rule must not take that away"
    )


def test_the_deep_measurement_reports_its_progress_in_files(tmp_path: Path) -> None:
    """Progress is counted in files, not in albums.

    Two albums are two steps, and the time is spent inside each of them, so a
    bar counting albums stays on one number for most of the run and a slow
    measurement cannot be told from a dead one.
    """
    bench, store, library = _bench(tmp_path, deep=_deep_analyzer(cutoff=16_000))
    first = _album(library, "One", tracks=3)
    second = _album(library, "Two", tracks=2)
    bench.add(library)
    store.record_quality(
        {
            file.content_signature: _measurement("lossless")
            for album in (first, second)
            for file in album.audio_files
        }
    )
    files = len(first.audio_files) + len(second.audio_files)
    seen: list[tuple[int, int]] = []

    bench.analyze_in_depth(
        [finding.album.album_id for finding in bench.findings()],
        on_progress=lambda done, total, folder: seen.append((done, total)),
    )

    assert {total for _, total in seen} == {
        files
    }, "progress is reported against a total that is not the number of files"
    # Every file moves it, and it opens at nought rather than at the first
    # album already claimed to be done.
    assert seen[0][0] == 0
    assert max(done for done, _ in seen) == files
    assert len({done for done, _ in seen}) == files + 1


def _colliding_pair(folder: Path) -> None:
    """Write two files of one declared shape whose audio is provably different.

    A ``content_signature`` is codec, sample count, rate, channels and depth,
    so two recordings of one length and format are one signature. That is the
    collision this bench warns about, and copying a fixture twice does not
    reproduce it: two copies of one file are one recording, the application
    can prove that, and it raises no doubt it can prove false.

    So the second copy is given a different STREAMINFO md5, which is what
    ``audio_key`` reads out of a FLAC and is FLAC's own statement about which
    audio it holds. The declared shape is untouched, so the signature is
    identical and the two files disagree on exactly the one fact that decides.
    """
    shutil.copy(FIXTURES / "tone.flac", folder / "01. First.flac")
    second = folder / "02. Second.flac"
    shutil.copy(FIXTURES / "tone.flac", second)
    raw = bytearray(second.read_bytes())
    # STREAMINFO is the first metadata block: 4 bytes of magic, 4 of block
    # header, then 34 of payload whose last 16 are the md5 of the samples.
    raw[26:42] = bytes(range(16))
    second.write_bytes(raw)
