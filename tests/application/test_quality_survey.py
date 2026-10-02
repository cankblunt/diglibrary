"""Tests for surveying a library's audio quality, album by album."""

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from diglibrary.application.quality import (
    AlbumQuality,
    QualitySurvey,
    stored_track_is_transcoded,
    verdict_from_stored,
)
from diglibrary.database.connection import Database
from diglibrary.database.library_store import LibraryStore, StoredQuality
from diglibrary.library.audio import AudioProperties
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.library.scanner import LibraryScanner
from diglibrary.quality.framing import FrameGrid
from diglibrary.quality.models import (
    BandEnergy,
    Encoding,
    SpectralProfile,
    TimeDomainStats,
    TrackAnalysis,
)


class FakeProbe:
    """Report stream properties derived from file content, without real media.

    Deriving from the bytes rather than the path is what makes this double
    faithful: two files with the same audio must produce the same content
    signature, and two with different audio must not.
    """

    def read(self, path: Path) -> AudioProperties:
        """Return deterministic properties keyed by the file's bytes."""
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


class ScriptedAnalyzer:
    """Return a chosen wall drop per file name, and count what it measured.

    It produces a frame grid reading, because the real analyzer does. The
    survey remembers a file only when its row carries everything the rules
    read, so a fake without a grid reading writes rows no survey calls
    complete, and a test asserting that a second survey decodes nothing would
    fail. A clean, quiet reading is the default here; a test that wants a
    conviction says so.
    """

    def __init__(self, drops: dict[str, float], grid: FrameGrid | None = None) -> None:
        self._drops = drops
        self._grid = (
            grid if grid is not None else FrameGrid(peak_z=3.5, offset=17, agreeing_readings=1)
        )
        self.measured: list[Path] = []

    def analyze(self, path: Path) -> TrackAnalysis | None:
        """Return an analysis whose spectrum falls by this file's chosen amount."""
        self.measured.append(path)
        drop = self._drops.get(path.name)
        if drop is None:
            return None
        return TrackAnalysis(
            frame_grid=self._grid,
            path=path,
            declared_codec="flac",
            sample_rate=44_100,
            spectral=SpectralProfile(
                bands=(
                    BandEnergy(16_000, -50.0),
                    BandEnergy(19_000, -50.0 - drop),
                    # Deep falls come with empty tops, as real transcodes
                    # have; gentle falls keep a live ceiling.
                    BandEnergy(21_500, -55.0 - 2.0 * drop),
                )
            ),
            time_domain=TimeDomainStats(
                peak_db=-1.0,
                rms_db=-14.0,
                dc_offset=0.0,
                flat_factor=0.0,
                clipped_samples=0,
                noise_floor_db=-60.0,
            ),
        )


def _survey(analyzer: ScriptedAnalyzer) -> QualitySurvey:
    logger = logging.getLogger("test.quality.survey")
    return QualitySurvey(LibraryScanner(FakeProbe(), logger), analyzer, logger, workers=1)


def _album(root: Path, name: str, tracks: dict[str, bytes]) -> Path:
    folder = root / name
    folder.mkdir()
    for filename, payload in tracks.items():
        (folder / filename).write_bytes(payload)
    return folder


def test_a_survey_judges_each_album_and_reports_it_as_it_finishes(tmp_path: Path) -> None:
    """A library-sized wait must fill the screen in, not end in one silence."""
    _album(tmp_path, "Honest Album", {"01.flac": b"a", "02.flac": b"bb"})
    _album(tmp_path, "Fake Album", {"03.flac": b"ccc", "04.flac": b"dddd"})
    analyzer = ScriptedAnalyzer({"01.flac": 4.0, "02.flac": 5.0, "03.flac": 20.0, "04.flac": 25.0})
    survey = _survey(analyzer)
    seen: list[tuple[int, int, AlbumQuality]] = []

    findings = survey.measure(survey.albums(tmp_path), on_album=lambda *args: seen.append(args))

    by_name = {finding.folder_path.name: finding for finding in findings}
    assert by_name["Honest Album"].verdict.encoding is Encoding.LOSSLESS
    assert by_name["Fake Album"].verdict.encoding is Encoding.TRANSCODED
    assert by_name["Fake Album"].is_suspect
    assert [position for position, _, _ in seen] == [1, 2]


def test_a_container_folder_is_never_surveyed_as_an_album(tmp_path: Path) -> None:
    """The survey reuses the organizer's scan, so a container folder is not an album here."""
    shelf = tmp_path / "Discography"
    shelf.mkdir()
    (shelf / "stray.flac").write_bytes(b"stray")
    _album(shelf, "Real Album", {"01.flac": b"a", "02.flac": b"bb"})
    analyzer = ScriptedAnalyzer({"01.flac": 4.0, "02.flac": 5.0, "stray.flac": 4.0})
    survey = _survey(analyzer)

    names = {unit.folder_path.name for unit in survey.albums(tmp_path)}

    assert names == {"Real Album"}


def test_identical_audio_is_measured_once_however_many_folders_hold_it(tmp_path: Path) -> None:
    """Duplicates are common in a music library; decoding them twice is wasted time."""
    _album(tmp_path, "First Copy", {"01.flac": b"same", "02.flac": b"also same"})
    _album(tmp_path, "Second Copy", {"01.flac": b"same", "02.flac": b"also same"})
    analyzer = ScriptedAnalyzer({"01.flac": 4.0, "02.flac": 5.0})
    survey = _survey(analyzer)

    findings = survey.measure(survey.albums(tmp_path))

    assert len(findings) == 2
    assert len(analyzer.measured) == 2
    assert all(finding.verdict.encoding is Encoding.LOSSLESS for finding in findings)


def test_an_album_reports_how_much_of_it_could_be_measured(tmp_path: Path) -> None:
    """A verdict from two of five tracks is weaker evidence, and it must show."""
    _album(
        tmp_path,
        "Damaged Album",
        {"01.flac": b"a", "02.flac": b"bb", "03.flac": b"ccc"},
    )
    analyzer = ScriptedAnalyzer({"01.flac": 4.0, "02.flac": 5.0})
    survey = _survey(analyzer)

    finding = survey.measure(survey.albums(tmp_path))[0]

    assert finding.tracks == 3
    assert finding.analyzed == 2


def test_an_album_with_nothing_measurable_is_undecided_not_lossless(tmp_path: Path) -> None:
    """Silence about an unmeasurable album must never read as a clean bill of health."""
    _album(tmp_path, "Unreadable", {"01.flac": b"a"})
    survey = _survey(ScriptedAnalyzer({}))

    finding = survey.measure(survey.albums(tmp_path))[0]

    assert finding.verdict.encoding is Encoding.UNDECIDED
    assert not finding.is_suspect
    assert finding.analyzed == 0


def test_what_is_measured_is_kept_before_the_survey_reaches_its_end(tmp_path: Path) -> None:
    """Hours of decoding may not depend on the survey finishing.

    If measurements are held in memory until the last album, a crash, a quit,
    or one file ffmpeg does not release costs every measurement taken before
    it. Each album is stored as it is judged.
    """
    library = tmp_path / "library"
    library.mkdir()
    _album(library, "First Album", {"01.flac": b"a", "02.flac": b"bb"})
    _album(library, "Second Album", {"03.flac": b"ccc"})
    store = _store(tmp_path)
    analyzer = ScriptedAnalyzer({"01.flac": 4.0, "02.flac": 5.0, "03.flac": 20.0})
    logger = logging.getLogger("test.quality.survey")
    survey = QualitySurvey(
        LibraryScanner(FakeProbe(), logger), analyzer, logger, workers=1, store=store
    )
    units = survey.albums(library)
    kept: list[int] = []

    signatures = [file.content_signature for unit in units for file in unit.audio_files]
    survey.measure(units, on_album=lambda *_: kept.append(len(store.quality_for(signatures))))

    assert kept[0] == 2, "the first album is in the database before the second is measured"
    assert kept[-1] == 3


def _store(tmp_path: Path) -> LibraryStore:
    logger = logging.getLogger("test.quality.store")
    database = Database(tmp_path / "library.sqlite3", logger)
    database.initialize()
    return LibraryStore(database, logger)


def test_a_measured_library_is_never_decoded_twice(tmp_path: Path) -> None:
    """The whole reason the organizer can afford to measure everything.

    Decoding a whole library is slow; doing it on every scan would make the
    organizer unusable. The signature survives a retag and a rename, so the
    stored measurement outlives the rename it informs.
    """
    library = tmp_path / "library"
    library.mkdir()
    _album(library, "Some Album", {"01.flac": b"a", "02.flac": b"bb"})
    store = _store(tmp_path)
    analyzer = ScriptedAnalyzer({"01.flac": 4.0, "02.flac": 5.0})
    logger = logging.getLogger("test.quality.survey")
    survey = QualitySurvey(
        LibraryScanner(FakeProbe(), logger), analyzer, logger, workers=1, store=store
    )
    units = survey.albums(library)
    first = survey.measure(units)

    analyzer.measured.clear()
    second = QualitySurvey(
        LibraryScanner(FakeProbe(), logger),
        analyzer,
        logger,
        workers=1,
        store=store,
    ).measure(units)

    assert analyzer.measured == [], "a second survey must decode nothing"
    assert second[0].analyzed == first[0].analyzed
    assert second[0].verdict.encoding is first[0].verdict.encoding


def test_a_remembered_transcode_is_still_a_transcode(tmp_path: Path) -> None:
    """A replayed verdict and a freshly measured one must agree."""
    library = tmp_path / "library"
    library.mkdir()
    _album(library, "Fake Album", {"01.flac": b"a", "02.flac": b"bb"})
    store = _store(tmp_path)
    logger = logging.getLogger("test.quality.survey")
    analyzer = ScriptedAnalyzer({"01.flac": 20.0, "02.flac": 25.0})
    survey = QualitySurvey(
        LibraryScanner(FakeProbe(), logger), analyzer, logger, workers=1, store=store
    )
    units = survey.albums(library)
    assert survey.measure(units)[0].verdict.encoding is Encoding.TRANSCODED

    analyzer.measured.clear()
    replayed = survey.measure(units)[0]

    assert analyzer.measured == []
    assert replayed.verdict.encoding is Encoding.TRANSCODED
    assert replayed.analyzed == 2


def test_a_replayed_album_reaches_the_same_verdict_as_a_measured_one(tmp_path: Path) -> None:
    """The stored verdict must not contradict the measurement.

    If the replay judges by counting transcoded tracks while a fresh
    measurement judges by the median, one album reads lossless from the
    database and transcoded from the audio. Screens are drawn from replays,
    so both paths must apply the same rule.
    """
    library = tmp_path / "library"
    library.mkdir()
    # Falls whose median clears the album threshold while a minority of tracks
    # clear the cautious per-track one, which is where two rules would diverge.
    _album(library, "Borderline", {f"{i:02d}.flac": bytes([i] * (i + 1)) for i in range(1, 6)})
    drops = {"01.flac": 16.0, "02.flac": 17.0, "03.flac": 19.2, "04.flac": 21.0, "05.flac": 34.7}
    store = _store(tmp_path)
    logger = logging.getLogger("test.quality.survey")

    def survey(analyzer: ScriptedAnalyzer) -> QualitySurvey:
        return QualitySurvey(
            LibraryScanner(FakeProbe(), logger), analyzer, logger, workers=1, store=store
        )

    analyzer = ScriptedAnalyzer(drops)
    first = survey(analyzer)
    units = first.albums(library)
    measured = first.measure(units)[0]

    analyzer.measured.clear()
    replayed = survey(analyzer).measure(units)[0]

    assert analyzer.measured == [], "the second pass must come from the database"
    assert replayed.verdict.encoding is measured.verdict.encoding
    assert replayed.verdict.encoding is Encoding.TRANSCODED


def test_a_lossless_album_reports_no_bitrate(tmp_path: Path) -> None:
    """ "Lossless, looks like 320 kbps" is a contradiction on screen.

    A bitrate is what a lossy stream is, so an honest lossless album has none
    to report.
    """
    library = tmp_path / "library"
    library.mkdir()
    _album(library, "Honest", {"01.flac": b"a", "02.flac": b"bb", "03.flac": b"ccc"})
    store = _store(tmp_path)
    logger = logging.getLogger("test.quality.survey")
    analyzer = ScriptedAnalyzer({"01.flac": 4.0, "02.flac": 5.0, "03.flac": 6.0})
    survey = QualitySurvey(
        LibraryScanner(FakeProbe(), logger), analyzer, logger, workers=1, store=store
    )
    units = survey.albums(library)
    survey.measure(units)

    replayed = survey.measure(units)[0]

    assert replayed.verdict.encoding is Encoding.LOSSLESS
    assert replayed.verdict.effective_bitrate_kbps is None


def test_a_row_written_under_the_old_rule_does_not_poison_the_naming() -> None:
    """The verdict written with a superseded rule is never trusted; the numbers are.

    A row written under a slope-only rule can say `transcoded` about a
    genuine FLAC, and a name would follow from it. The measurements are
    still right, so the conclusion is drawn again from the stored decay and
    ceiling every time they are read.
    """
    poisoned = StoredQuality(
        encoding="transcoded",
        effective_bitrate_kbps=None,
        cutoff_hertz=None,
        steepest_drop_db=17.0,
        findings=("transcoded",),
        reason="written by the slope-only rule",
        decay_db=8.2,
        ceiling_db=-85.1,
    )
    genuine_fake = StoredQuality(
        encoding="lossless",
        effective_bitrate_kbps=None,
        cutoff_hertz=16_000,
        steepest_drop_db=5.5,
        findings=(),
        reason="written before the cutoff convicted",
        decay_db=5.5,
        ceiling_db=-111.5,
    )

    assert not stored_track_is_transcoded(poisoned)
    assert stored_track_is_transcoded(genuine_fake)


def test_rows_from_before_the_two_sign_rule_are_measured_again(tmp_path: Path) -> None:
    """A row without decay and ceiling cannot answer the current question."""
    library = tmp_path / "library"
    library.mkdir()
    _album(library, "Old Album", {"01.flac": b"a", "02.flac": b"bb"})
    store = _store(tmp_path)
    logger = logging.getLogger("test.quality.survey")
    analyzer = ScriptedAnalyzer({"01.flac": 4.0, "02.flac": 5.0})
    survey = QualitySurvey(
        LibraryScanner(FakeProbe(), logger), analyzer, logger, workers=1, store=store
    )
    units = survey.albums(library)
    legacy = StoredQuality(
        encoding="transcoded",
        effective_bitrate_kbps=None,
        cutoff_hertz=None,
        steepest_drop_db=20.0,
        findings=(),
        reason="pre-migration row",
        decay_db=None,
        ceiling_db=None,
    )
    store.record_quality({file.content_signature: legacy for file in units[0].audio_files})

    finding = survey.measure(units)[0]

    assert len(analyzer.measured) == 2, "legacy rows must be decoded again"
    assert finding.verdict.encoding is Encoding.LOSSLESS


class ExplodingAnalyzer:
    """Blow up on one named file, and measure every other one normally."""

    def __init__(self, doomed: str) -> None:
        self._doomed = doomed
        self._inner = ScriptedAnalyzer({})
        self.measured: list[Path] = []

    def analyze(self, path: Path) -> TrackAnalysis | None:
        """Raise for the doomed file, exactly as a failing decode would."""
        self.measured.append(path)
        if path.name == self._doomed:
            raise UnicodeDecodeError("utf-8", b"\xe7", 0, 1, "invalid continuation byte")
        return None


def test_one_album_may_not_cost_the_library(tmp_path: Path) -> None:
    """A failure on one file must not end the survey.

    ffmpeg echoes a file's own tags into the report it writes, so a tag that
    is not valid UTF-8 raises when the report is decoded strictly. The error
    is confined to the album that holds the file.
    """
    _album(tmp_path, "Good Album", {"01.flac": b"a"})
    _album(tmp_path, "Cursed Album", {"02.flac": b"bb"})
    _album(tmp_path, "Later Album", {"03.flac": b"ccc"})
    analyzer = ExplodingAnalyzer("02.flac")
    survey = _survey(analyzer)

    findings = survey.measure(survey.albums(tmp_path))

    assert len(findings) == 3, "the survey must reach the albums after the failure"
    cursed = next(f for f in findings if f.folder_path.name == "Cursed Album")
    assert cursed.verdict.encoding is Encoding.UNDECIDED, "never a clean bill of health"
    assert cursed.analyzed == 0
    assert {path.name for path in analyzer.measured} == {"01.flac", "02.flac", "03.flac"}


class Mp3Analyzer:
    """Measure every file as a real MP3 160 does: dead above 16 kHz."""

    def analyze(self, path: Path) -> TrackAnalysis | None:
        """Return a stream with a wall, which is what a lossy encoder leaves."""
        return TrackAnalysis(
            path=path,
            declared_codec="mp3",
            sample_rate=44_100,
            spectral=SpectralProfile(
                bands=(
                    BandEnergy(16_000, -100.0),
                    BandEnergy(19_000, -100.0),
                    BandEnergy(21_500, -100.0),
                )
            ),
            time_domain=TimeDomainStats(
                peak_db=-1.0,
                rms_db=-14.0,
                dc_offset=0.0,
                flat_factor=0.0,
                clipped_samples=0,
                noise_floor_db=-60.0,
            ),
        )


class RefusingAnalyzer:
    """Fail loudly if anything is decoded, so a replay is proved to be one."""

    def analyze(self, path: Path) -> TrackAnalysis | None:
        """Never called when every file is already measured."""
        raise AssertionError(f"{path} was measured twice")


def test_an_honest_mp3_album_stays_honest_when_replayed(tmp_path: Path) -> None:
    """An album of honest MP3s is not convicted by the wall-majority test.

    That test belongs to a lossless container, which is the only thing that
    can be hiding a lossy origin. A wall below 19 kHz is not evidence against
    an MP3; it is what an MP3 is. The verdict that names the folder comes
    from this path on every scan after the first.
    """
    library = tmp_path / "library"
    library.mkdir()
    _album(library, "Honest MP3", {"01.mp3": b"a", "02.mp3": b"bb", "03.mp3": b"ccc"})
    store = _store(tmp_path)
    logger = logging.getLogger("test.quality.replay")
    scanner = LibraryScanner(FakeProbe(), logger)
    units = [unit for unit in scanner.scan(library) if not unit.is_loose_track]

    measured = QualitySurvey(scanner, Mp3Analyzer(), logger, workers=1, store=store).measure(units)
    replayed = QualitySurvey(scanner, RefusingAnalyzer(), logger, workers=1, store=store).measure(
        units
    )

    assert measured[0].verdict.encoding is Encoding.LOSSY
    assert replayed[0].verdict.encoding is measured[0].verdict.encoding
    assert not replayed[0].is_suspect, "an honest album may never be named a fake"


def test_an_album_is_judged_by_all_its_tracks_and_not_by_the_ones_just_decoded(
    tmp_path: Path,
) -> None:
    """Half remembered and half fresh must still be one verdict about one album.

    With ten lossless tracks on record and one file measured again, a verdict
    drawn from the decoded file alone calls the album a transcode, and an
    album judged a transcode names every one of its files a transcode.

    The album is judged by the median of its tracks. A median of whichever
    tracks happened to need decoding is a statement about a subset, printed as
    a statement about the album.
    """
    library = tmp_path / "library"
    library.mkdir()
    tracks = {f"{index:02d}.flac": bytes([index]) * index for index in range(1, 12)}
    _album(library, "Mostly Honest", tracks)
    store = _store(tmp_path)
    logger = logging.getLogger("test.quality.survey")

    # Ten gentle falls and one cliff: an honest album with a single dissenter.
    drops = {name: 4.0 for name in tracks}
    drops["11.flac"] = 25.0
    survey = QualitySurvey(
        LibraryScanner(FakeProbe(), logger),
        ScriptedAnalyzer(drops),
        logger,
        workers=1,
        store=store,
    )
    units = survey.albums(library)
    assert survey.measure(units)[0].verdict.encoding is Encoding.LOSSLESS

    # Now forget only the dissenter and measure again: the one file is
    # decoded, the ten are replayed.
    signatures = [file.content_signature for file in units[0].audio_files]
    remaining = {
        signature: quality
        for signature, quality in store.quality_for(signatures).items()
        if not stored_track_is_transcoded(quality)
    }
    with Database(tmp_path / "library.sqlite3", logger).connect() as connection:
        connection.execute("DELETE FROM track_quality")
    store.record_quality(remaining)
    analyzer = ScriptedAnalyzer(drops)
    again = QualitySurvey(
        LibraryScanner(FakeProbe(), logger), analyzer, logger, workers=1, store=store
    ).measure(units)

    assert len(analyzer.measured) == 1, "only the forgotten file is decoded again"
    assert (
        again[0].verdict.encoding is Encoding.LOSSLESS
    ), "one decoded track must not sentence the ten that were remembered"


def test_a_replayed_verdict_is_not_decided_by_whichever_track_is_first(
    tmp_path: Path,
) -> None:
    """The agreement between a replayed and a fresh verdict is this function's contract.

    An album with a minority of dissenting tracks and a gentle median fall is
    not a transcode. A replayed path that ends on `tracks[0].encoding` answers
    TRANSCODED whenever the first track is one of the dissenters, while a
    fresh measurement of the same album says LOSSLESS.

    An album called a transcode names every one of its files a transcode, so
    one file's position in a folder listing would rename all of them.
    """
    library = tmp_path / "library"
    library.mkdir()
    tracks = {f"{index:02d}.flac": bytes([index]) * index for index in range(1, 13)}
    _album(library, "First Track Lies", tracks)
    store = _store(tmp_path)
    logger = logging.getLogger("test.quality.survey")
    # The first track is the dissenter; the other eleven fade gently.
    drops = {name: 4.0 for name in tracks}
    drops["01.flac"] = 30.0
    analyzer = ScriptedAnalyzer(drops)
    survey = QualitySurvey(
        LibraryScanner(FakeProbe(), logger), analyzer, logger, workers=1, store=store
    )
    units = survey.albums(library)
    fresh = survey.measure(units)[0].verdict.encoding

    analyzer.measured.clear()
    replayed = (
        QualitySurvey(
            LibraryScanner(FakeProbe(), logger),
            ScriptedAnalyzer(drops),
            logger,
            workers=1,
            store=store,
        )
        .measure(units)[0]
        .verdict.encoding
    )

    assert analyzer.measured == [], "the second reading decodes nothing"
    assert fresh is Encoding.LOSSLESS
    assert replayed is fresh, "a replayed verdict and a fresh one must agree"


def test_every_measurement_is_filed_under_the_signature_that_asked_for_it(
    tmp_path: Path,
) -> None:
    """A measurement written under a key nothing can look up is a measurement lost.

    Two albums can hold the same recording: a track, and the compilation it
    was also collected on. The run decodes that audio once and hands the
    second album the first album's analysis, whose path is in the first
    album's folder. Looking the signature up by that path finds nothing, and
    a fallback to the file's bare name writes a row keyed by a filename,
    which matches no signature the application knows.
    """
    library = tmp_path / "library"
    library.mkdir()
    _album(library, "An Album", {"shared.flac": b"a", "another.flac": b"bb"})
    _album(library, "A Compilation", {"shared.flac": b"a", "different.flac": b"ccc"})
    store = _store(tmp_path)
    analyzer = ScriptedAnalyzer({"shared.flac": 4.0, "another.flac": 5.0, "different.flac": 6.0})
    logger = logging.getLogger("test.quality.survey")
    survey = QualitySurvey(
        LibraryScanner(FakeProbe(), logger), analyzer, logger, workers=1, store=store
    )
    units = survey.albums(library)

    survey.measure(units)

    known = {file.content_signature for unit in units for file in unit.audio_files}
    with store._database.connect() as connection:
        written = {
            row[0] for row in connection.execute("SELECT content_signature FROM track_quality")
        }
    assert written, "the survey has to have written something"
    assert written <= known, (
        "every row has to be filed under a signature this app can look up; "
        f"these cannot be: {sorted(written - known)}"
    )


def test_a_measurement_names_the_audio_it_was_taken_from(tmp_path: Path) -> None:
    """Without a key the row answers for every file of its content signature.

    Different songs of the same length and format share a signature, and the
    quality engine keeps one measurement per signature, so the first track
    measured writes the row and the second reads it. A number read from a row
    without a key never reaches a filename, so a row written without one
    withholds the number from its own file too.

    The key is written here because this is where the audio was just
    decoded, and reading it is cheap: a FLAC answers from its own header
    without reading any audio.
    """
    library = tmp_path / "library"
    library.mkdir()
    # Real audio, because a key is read from the stream: a blob of bytes has no
    # audio to name, and answering `None` for one is correct rather than a
    # failure. The other tests here do not care what the files contain.
    fixtures = Path(__file__).parent.parent / "fixtures" / "audio"
    _album(
        library,
        "An Album",
        {
            "01.flac": (fixtures / "tone.flac").read_bytes(),
            "02.flac": (fixtures / "tone-long.flac").read_bytes(),
        },
    )
    store = _store(tmp_path)
    logger = logging.getLogger("test.quality.survey")
    survey = QualitySurvey(
        LibraryScanner(FakeProbe(), logger),
        ScriptedAnalyzer({"01.flac": 4.0, "02.flac": 5.0}),
        logger,
        workers=1,
        store=store,
    )
    units = survey.albums(library)

    survey.measure(units)

    signatures = [file.content_signature for unit in units for file in unit.audio_files]
    with store._database.connect() as connection:
        keyed = connection.execute(
            "SELECT COUNT(*) FROM track_quality WHERE audio_key IS NOT NULL"
        ).fetchone()[0]
    assert keyed == len(signatures), (
        "every row a scan writes has to name the audio it was taken from, "
        f"and {keyed} of {len(signatures)} do"
    )


def test_a_row_measured_under_the_old_ruler_keeps_the_answer_it_was_given() -> None:
    """A change of rule applies to new measurements, not to stored rows.

    A wall is a 20 dB step between two rungs; an earlier rule used the first
    band under an absolute -95.0 dBFS. The verdict is re-derived from stored
    numbers on every read, so without this exception the newer rule would
    re-judge every row written under the earlier one, none of which was
    measured for the question the newer rule asks.

    Such a row goes on reading `transcoded` until its album is measured
    again. That is an accepted cost.
    """
    old_ruler = StoredQuality(
        encoding="transcoded",
        effective_bitrate_kbps=None,
        # Its 19 kHz band read -95.3 against a floor of -95.0.
        cutoff_hertz=19_000,
        steepest_drop_db=7.9,
        findings=("transcoded",),
        reason="written when a wall was a band under a fixed level",
        decay_db=3.6,
        ceiling_db=-105.3,
        wall_low_hertz=None,
    )

    assert stored_track_is_transcoded(old_ruler), "the past is not re-judged"
    # And the screen can tell: a cutoff with no rung below it is a reading from
    # the old ruler, which is the one case the column answers unambiguously.
    assert old_ruler.cutoff_hertz is not None and old_ruler.wall_low_hertz is None


def test_the_same_file_measured_again_comes_back_clean() -> None:
    """Measured again, the same file reads lossless.

    The same numbers as above. This row was written by a measurement that
    looks for a step and found no straight cut anywhere (a fall of 3.6 dB is
    not one), so it records no wall at all. The verdict follows from the
    absence, not from a re-reading of the old row.
    """
    measured_again = StoredQuality(
        encoding="lossless",
        effective_bitrate_kbps=None,
        cutoff_hertz=None,
        steepest_drop_db=7.9,
        findings=(),
        reason="no step anywhere: it descends and accelerates at the top",
        decay_db=3.6,
        ceiling_db=-105.3,
        wall_low_hertz=None,
    )

    assert not stored_track_is_transcoded(measured_again)


def test_a_row_measured_before_the_floor_was_recorded_keeps_its_answer() -> None:
    """A reprieve is not granted on a number that was never measured.

    The fall-and-emptiness rule stops in the converter band, and it needs the
    floor to know it is there. Rows written before the floor was recorded
    have none, and absolving them would re-judge stored rows on evidence that
    was never taken. A `None` floor keeps the answer the row was given, and
    the correction reaches an album when that album is measured again.
    """
    before_the_column = StoredQuality(
        encoding="transcoded",
        effective_bitrate_kbps=None,
        cutoff_hertz=None,
        steepest_drop_db=19.3,
        findings=("transcoded",),
        reason="a fall into an empty top, measured before the floor was kept",
        decay_db=19.2,
        ceiling_db=-89.5,
        floor_hertz=None,
    )

    assert stored_track_is_transcoded(before_the_column), "no floor, no reprieve"


def test_a_stored_floor_in_the_converter_band_is_absolved() -> None:
    """And with the number present, the same row is spared.

    Identical in every other figure to the row above: the difference is that
    this measurement recorded where the audio stopped, and it stopped at
    20 kHz — where an honest converter's filter lands. A replayed verdict and a
    freshly measured one have to agree, so this is the same rule
    `test_a_floor_in_the_converter_band_is_not_convicted` asserts from audio.
    """
    with_the_floor = StoredQuality(
        encoding="transcoded",
        effective_bitrate_kbps=None,
        cutoff_hertz=None,
        steepest_drop_db=19.3,
        findings=("transcoded",),
        reason="a fall into an empty top, with the floor beginning at 20 kHz",
        decay_db=19.2,
        ceiling_db=-89.5,
        floor_hertz=20_000,
    )

    assert not stored_track_is_transcoded(with_the_floor)


def test_an_album_nobody_measured_is_not_accused_of_anything() -> None:
    """An album with no measured track is `undecided`, not `lossy`.

    With no rows at all there are no lossless containers among them, so
    without this case the album falls through to the branch that exists for
    a folder of honest MP3s and is announced as one, from no evidence.

    `undecided` is what this enum keeps for no answer, and the reason says
    that nothing was measured rather than naming a cause.
    """
    verdict = verdict_from_stored([])

    assert verdict.encoding is Encoding.UNDECIDED
    assert verdict.tracks == ()
    assert "measured" in verdict.reason


def test_a_folder_of_honest_mp3s_is_still_read_as_lossy() -> None:
    """The `lossy` branch still answers for its own case.

    An album of measured MP3s has no lossless container to hide a lossy origin
    in, and `lossy` is what it is. The empty case above must not take that
    answer away.
    """
    rows = [
        StoredQuality(
            encoding="lossy",
            effective_bitrate_kbps=320,
            cutoff_hertz=20_000,
            steepest_drop_db=4.0,
            findings=(),
            reason="An MP3 that is what it says it is.",
        )
    ]

    assert verdict_from_stored(rows).encoding is Encoding.LOSSY


def test_a_frame_grid_conviction_survives_the_re_judgement_of_its_row() -> None:
    """A conviction by the frame grid survives the re-judgement of its row.

    Every stored row is re-judged from its own numbers when read, because a
    verdict written under an old rule cannot be trusted. A file convicted by
    its frame grid has a spectrum that reads lossless, which is the point of
    that measurement, so a re-judgement that asked only the spectrum would
    absolve on the next launch what was just caught. The row carries the
    grid, and the re-judgement asks it first.
    """
    grid_caught = StoredQuality(
        encoding="transcoded",
        effective_bitrate_kbps=None,
        cutoff_hertz=None,
        steepest_drop_db=4.1,
        findings=("transcoded",),
        reason="This file was an MP3 before it was a FLAC.",
        # A spectrum that fades gently and lives to the top: on these numbers
        # alone the current rule says lossless, and says it correctly.
        decay_db=5.2,
        ceiling_db=-70.8,
        frame_grid_z=28.6,
        frame_grid_agrees=True,
    )
    faint = replace(grid_caught, frame_grid_z=7.9)
    alone = replace(grid_caught, frame_grid_agrees=False)
    older_than_the_question = replace(grid_caught, frame_grid_z=None, frame_grid_agrees=None)

    assert stored_track_is_transcoded(grid_caught), "the grid conviction stands on re-reading"
    assert not stored_track_is_transcoded(faint), "half the rule is not the rule"
    assert not stored_track_is_transcoded(alone), "nor is the other half"
    assert not stored_track_is_transcoded(
        older_than_the_question
    ), "a row measured before this question keeps the answer its own numbers give"


def test_an_album_replayed_from_grid_caught_rows_is_still_a_transcoded_album() -> None:
    """A replayed verdict agreeing with a fresh one is this function's contract.

    The fresh path asks the tracks it judged whether the grid caught them. A
    replayed path that works from the spectral medians alone reads the same
    album `transcoded` after a scan and `lossless` on the next launch, with
    the row on disk right and the screen wrong.
    """
    caught = [
        StoredQuality(
            encoding="transcoded",
            effective_bitrate_kbps=None,
            cutoff_hertz=None,
            steepest_drop_db=4.1,
            findings=("transcoded",),
            reason="This file was an MP3 before it was a FLAC.",
            decay_db=5.2,
            ceiling_db=-70.8,
            frame_grid_z=30.0,
            frame_grid_agrees=True,
        )
        for _ in range(10)
    ]

    album = verdict_from_stored(caught)

    assert album.encoding is Encoding.TRANSCODED
    assert all(track.encoding is Encoding.TRANSCODED for track in album.tracks)

    # And a minority stays a minority, as it does in the fresh path.
    few = caught[:3] + [
        replace(row, encoding="lossless", findings=(), frame_grid_z=3.4, frame_grid_agrees=False)
        for row in caught[3:]
    ]
    assert verdict_from_stored(few).encoding is Encoding.LOSSLESS


def _file(path: Path, signature: str, key: str | None) -> AudioFileFacts:
    """One scanned file with a chosen signature and audio key."""
    return AudioFileFacts(
        path=path,
        content_signature=signature,
        file_size_bytes=1_000,
        modified_at=datetime(2001, 2, 3, 4, 5, tzinfo=UTC),
        audio_key=key,
    )


def _unit(folder: Path, files: tuple[AudioFileFacts, ...]) -> AlbumUnit:
    return AlbumUnit(folder_path=folder, unit_signature=folder.name, audio_files=files)


def test_a_file_never_wears_the_verdict_measured_from_another_recording(
    tmp_path: Path,
) -> None:
    """A file does not read a row measured from another recording.

    Two folders hold the same album, one transcoded from MP3 and one honest.
    A content signature is the declared shape (codec, sample count, rate,
    channels, depth), and the two versions of one track share all of it. A
    lookup by signature alone finds the transcode's row under the honest
    file's signature and replays it, so the honest album is accused.
    `quality_for_files` tells the recordings apart by their audio key, and
    the survey must ask it.

    A file with no key of its own keeps reading the signature's row, which
    is the ordinary case.
    """
    store = _store(tmp_path)
    shared = "same-shape"
    store.record_quality(
        {
            shared: StoredQuality(
                encoding="transcoded",
                effective_bitrate_kbps=None,
                cutoff_hertz=None,
                steepest_drop_db=4.1,
                findings=("transcoded",),
                reason="This file was an MP3 before it was a FLAC.",
                decay_db=5.2,
                ceiling_db=-70.8,
                audio_key="flac:the-transcode",
                frame_grid_z=49.4,
                frame_grid_agrees=True,
            )
        }
    )
    folder = tmp_path / "honest"
    folder.mkdir()
    honest = _unit(folder, (_file(folder / "01.flac", shared, "flac:the-honest-one"),))

    survey = QualitySurvey(
        LibraryScanner(FakeProbe(), logging.getLogger("test.q")),
        ScriptedAnalyzer({}),
        logging.getLogger("test.q"),
        store=store,
        workers=1,
    )
    known = survey.known([honest])

    assert not known, "the honest file must not read a row taken from other audio"

    # And the file that *was* measured still reads its own row.
    transcoded_folder = tmp_path / "transcoded"
    transcoded_folder.mkdir()
    theirs = _unit(
        transcoded_folder,
        (_file(transcoded_folder / "01.flac", shared, "flac:the-transcode"),),
    )
    assert survey.known([theirs]), "the row is still found by the file it was taken from"


def test_a_row_from_before_the_frame_grid_is_measured_again() -> None:
    """A lossless row with no frame grid reading is not a complete row.

    A survey skips a file whose row already carries what the rules need. The
    frame grid is part of the ordinary pass, so the list of what they need
    includes it. Otherwise a row that has only `decay_db` and `ceiling_db` is
    remembered for ever, and the one measurement that reaches a high-bitrate
    transcode stored as FLAC never runs on that file, however many times its
    album is scanned.
    """
    before_the_grid = StoredQuality(
        encoding="lossless",
        effective_bitrate_kbps=None,
        cutoff_hertz=None,
        steepest_drop_db=16.6,
        findings=(),
        reason="measured when no alignment was swept",
        decay_db=13.2,
        ceiling_db=-94.2,
        frame_grid_z=None,
    )

    assert not before_the_grid.answers_the_rules_in_force


def test_a_lossy_container_is_never_waiting_for_a_grid() -> None:
    """A `lossy` row with no grid reading is complete.

    The sweep runs on lossless containers only: a file that never claimed to
    be lossless has no lossy origin to hide. Demanding the column of every
    row would decode every lossy file again on every pass and learn nothing.
    """
    mp3 = StoredQuality(
        encoding="lossy",
        effective_bitrate_kbps=192,
        cutoff_hertz=18_000,
        steepest_drop_db=30.0,
        findings=(),
        reason="an MP3, measured as one",
        decay_db=9.0,
        ceiling_db=-96.0,
        frame_grid_z=None,
    )

    assert mp3.answers_the_rules_in_force


def test_a_row_the_probe_could_not_read_is_not_mistaken_for_a_complete_one() -> None:
    """The cost of the rule: a file the sweep cannot read is measured on every scan.

    A lossless file too short for the sweep gets no grid and reads here like
    a row written without the sweep, so it is measured again on every scan of
    its album. Such files are few. A column recording which rule wrote the
    row would remove the cost.
    """
    unreadable = StoredQuality(
        encoding="lossless",
        effective_bitrate_kbps=None,
        cutoff_hertz=None,
        steepest_drop_db=4.0,
        findings=(),
        reason="a two-second interlude the sweep cannot hold",
        decay_db=3.0,
        ceiling_db=-99.0,
        frame_grid_z=None,
    )

    assert not unreadable.answers_the_rules_in_force


def test_a_complete_row_is_still_remembered() -> None:
    """Or a library already measured would be decoded again from the start."""
    complete = StoredQuality(
        encoding="transcoded",
        effective_bitrate_kbps=None,
        cutoff_hertz=16_000,
        steepest_drop_db=38.3,
        findings=("transcoded",),
        reason="the grid caught it",
        decay_db=9.4,
        ceiling_db=-102.4,
        frame_grid_z=101.5,
        frame_grid_agreeing=6,
    )

    assert complete.answers_the_rules_in_force


def test_a_row_carrying_the_flag_and_not_the_count_is_measured_again() -> None:
    """The rule reads how many readings agreed, not a flag saying all did.

    A row that stores only the flag cannot answer the count: four of six is
    a conviction, and a flag cannot say four. Such a row is incomplete, or it
    would be remembered for ever without the number the rule reads.
    """
    by_the_old_question = StoredQuality(
        encoding="lossless",
        effective_bitrate_kbps=None,
        cutoff_hertz=None,
        steepest_drop_db=8.1,
        findings=(),
        reason="energy fades gradually",
        decay_db=9.4,
        ceiling_db=-92.0,
        frame_grid_z=32.9,
        frame_grid_agreeing=None,
    )

    assert not by_the_old_question.answers_the_rules_in_force


def test_a_survey_decodes_again_a_file_whose_row_predates_the_frame_grid(
    tmp_path: Path,
) -> None:
    """The survey itself decodes a file whose row has no grid reading.

    The tests above assert a property of the row. If the survey stops
    consulting that property, they still pass. This one runs the survey over
    a row with no grid and asks the analyzer what it decoded.
    """
    library = tmp_path / "library"
    library.mkdir()
    _album(library, "Half Without A Grid", {"01.flac": b"a", "02.flac": b"bb"})
    store = _store(tmp_path)
    logger = logging.getLogger("test.quality.survey")
    analyzer = ScriptedAnalyzer({"01.flac": 4.0, "02.flac": 4.0})
    survey = QualitySurvey(
        LibraryScanner(FakeProbe(), logger), analyzer, logger, workers=1, store=store
    )
    units = survey.albums(library)
    survey.measure(units)
    assert len(analyzer.measured) == 2, "both were decoded the first time"

    # A second pass decodes nothing, which is the whole point of remembering.
    analyzer.measured.clear()
    survey.measure(units)
    assert analyzer.measured == []

    # Now take the grid off one row, as a row written without the sweep
    # stands, and leave the other whole.
    first = units[0].audio_files[0].content_signature
    with Database(tmp_path / "library.sqlite3", logger).connect() as connection:
        connection.execute(
            "UPDATE track_quality SET frame_grid_z = NULL, frame_grid_agrees = NULL "
            "WHERE content_signature = ?",
            (first,),
        )

    analyzer.measured.clear()
    survey.measure(units)

    assert [path.name for path in analyzer.measured] == [
        "01.flac"
    ], "the row from before the sweep was measured again, and only it"
