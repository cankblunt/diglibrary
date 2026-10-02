"""The bench: folders loaded to ask what their audio really is.

A bench keeps its folders and remembers the albums under them, so opening it
costs a database read rather than a walk of the disk. The measurements already
on record are the answer it shows; walking a large library takes orders of
magnitude longer than reading its albums back. Audio is decoded only for the
albums that are selected, so the bench is a reading surface first and a
measuring one second.

What is never kept here is a verdict. It is re-judged from the stored numbers
on every read, because a stored verdict cannot be trusted across a rule change,
and so is the confidence beside it.
"""

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path

from diglibrary.application.quality import QualitySurvey, verdict_from_stored
from diglibrary.database.library_store import (
    BenchAlbum,
    BenchRoot,
    LibraryStore,
    StoredQuality,
    word_for,
)
from diglibrary.library.audio_key import audio_key
from diglibrary.library.scanner import LibraryScanner
from diglibrary.quality.analysis import FfmpegQualityAnalyzer
from diglibrary.quality.confidence import Confidence, judge_confidence
from diglibrary.quality.models import Encoding, PassBand, TrackAnalysis
from diglibrary.quality.verdict import AlbumVerdict, TrackVerdict, judge

DEEP_WORKERS = 8
"""How many files a deep measurement decodes at once.

ffmpeg is a subprocess, so threads wait on it rather than compete for the
interpreter.
"""


@dataclass(frozen=True, slots=True)
class BenchFinding:
    """Purpose: pair one album on the bench with what its stored numbers say now.

    Responsibilities: carry the album, the verdict re-judged from what is on
    record, and how much that verdict is worth. Boundaries: it proposes no
    rename, writes nothing, and decodes nothing — every number here was already
    measured. Dependencies: ``BenchAlbum``, ``AlbumVerdict``, ``Confidence``.
    Collaborators: ``QualityBench`` and the window. Constraints: ``analyzed``
    counts the files a measurement was found for, which may be fewer than the
    album holds, and the difference is shown rather than hidden.
    """

    album: BenchAlbum
    verdict: AlbumVerdict
    confidence: Confidence
    analyzed: int
    shared: int
    spoken: int = 0
    """How many user verdicts stand on this album, the measurement notwithstanding.

    Shown rather than merely obeyed: the measurement stays on screen beside the
    user's verdict so a disagreement remains auditable, and a row that silently
    reads the user's answer looks identical to one that measured it.
    """

    @property
    def is_suspect(self) -> bool:
        """Report whether this album is not what its container claims."""
        return self.verdict.encoding.value in {"transcoded", "overstated"}


@dataclass(frozen=True, slots=True)
class DeepTrack:
    """One file measured in depth, beside whatever stood for it before."""

    file_name: str
    content_signature: str
    audio_key: str | None
    measured: StoredQuality
    bands: tuple[PassBand, ...]
    was: StoredQuality | None

    @property
    def differs(self) -> bool:
        """Report whether this measurement disagrees with what is on record.

        Only the conclusions are compared, never the decibels: two honest
        measurements of the same file differ in the last digit, and the whole
        file reads slightly differently from one minute of it while reaching
        the same verdict. Asking about that would be asking about noise.
        """
        if self.was is None:
            return True
        return (
            self.measured.encoding != self.was.encoding
            or self.measured.cutoff_hertz != self.was.cutoff_hertz
            or self.measured.effective_bitrate_kbps != self.was.effective_bitrate_kbps
        )


@dataclass(frozen=True, slots=True)
class DeepAlbum:
    """One album measured in depth, and what it would become if adopted."""

    album_id: int
    folder: str
    tracks: tuple[DeepTrack, ...]
    before: AlbumVerdict
    after: AlbumVerdict

    @property
    def changed(self) -> tuple[DeepTrack, ...]:
        """Return the files whose conclusion the fresh measurement moves."""
        return tuple(track for track in self.tracks if track.differs)

    @property
    def verdict_moved(self) -> bool:
        """Report whether the album itself would change what it is called."""
        return self.before.encoding is not self.after.encoding


@dataclass(frozen=True, slots=True)
class DeepAnalysis:
    """Purpose: hold a finished deep measurement that has not been written down.

    Responsibilities: carry every album measured, what each file now reads, and
    what it read before. Boundaries: nothing here has touched the database — a
    stored measurement is replaced only after the user is asked, and told what
    the work cost. Dependencies: ``DeepAlbum``. Collaborators: ``QualityBench``
    and the window. Constraints: it is a proposal and it expires with the
    session, exactly like a plan waiting for `Approve & apply`.
    """

    albums: tuple[DeepAlbum, ...]
    files: int
    seconds: float

    @property
    def differing(self) -> tuple[DeepAlbum, ...]:
        """Return only the albums this would change something about."""
        return tuple(album for album in self.albums if album.changed)


def _album_word(verdict: AlbumVerdict, tracks: tuple[TrackVerdict, ...]) -> Encoding:
    """Re-derive the album from its tracks, after some of them were corrected.

    **The same majority the measurement itself uses.** ``verdict_from_stored``
    calls an album a transcode when ``walls * 2 > len(inside_lossless)``, so
    asking the same question of the same tracks after some of their answers
    were changed invents no rule — it re-runs the one that is already there.
    Without this, an album whose every track was marked lossy would go on
    announcing `LOSSLESS` over them.

    Only a lossless container can be re-judged: an honest MP3 album is `lossy`
    because that is what it *is*, and no verdict about a track makes it
    something else. The measured conclusion stands where nothing was said.
    """
    # Named rather than written as a complement: `undecided` is returned before
    # any container has been looked at, so it answers nothing here and belongs
    # to neither side. The sibling statements of this pair use strings or a set
    # of the enum; this one uses the enum in a tuple.
    if verdict.encoding not in (Encoding.LOSSLESS, Encoding.TRANSCODED):
        return verdict.encoding
    if not tracks:
        return verdict.encoding
    condemned = sum(1 for track in tracks if track.encoding is Encoding.TRANSCODED)
    return Encoding.TRANSCODED if condemned * 2 > len(tracks) else Encoding.LOSSLESS


def _with_user_word(
    verdict: AlbumVerdict, words: Mapping[tuple[str, str | None], str], album: BenchAlbum
) -> AlbumVerdict:
    """Let the user's verdict stand where it was given, over what was measured.

    The verdict replaces the *conclusion* and never the numbers: the fall, the
    ceiling and the reason stay exactly as measured, on screen beside the
    answer, because a disagreement that erases its own evidence cannot be
    audited or withdrawn.

    An album-level verdict decides the album outright. A track-level verdict
    decides that track, and the album is then re-derived from its tracks by
    ``_album_word``, the same majority the measurement uses.
    """
    if not words:
        return verdict
    tracks = verdict.tracks
    # Asked per file rather than per signature, because two files of one album
    # can share a signature. When they are the same recording twice, one
    # verdict rightly answers for both; when they are two different recordings
    # under one declared shape, a verdict about one must not reach the other.
    said_about = [word_for(words, file.content_signature, file.audio_key) for file in album.files]
    if any(said_about):
        tracks = tuple(
            (
                replace(
                    track,
                    encoding=(
                        Encoding.TRANSCODED
                        if said_about[index] == "transcoded"
                        else Encoding.LOSSLESS
                    ),
                )
                if index < len(said_about) and said_about[index]
                else track
            )
            for index, track in enumerate(tracks)
        )
    whole = words.get(("", None))
    if whole is None:
        return replace(verdict, tracks=tracks, encoding=_album_word(verdict, tracks))
    return replace(
        verdict,
        encoding=Encoding.TRANSCODED if whole == "transcoded" else Encoding.LOSSLESS,
        reason=f"You said this album is {'lossy' if whole == 'transcoded' else 'not lossy'}. "
        f"What it measured is unchanged: {verdict.reason}",
        tracks=tracks,
    )


@dataclass(frozen=True, slots=True)
class AcousticSweep:
    """What one round of asking the audio cost, and what it got back.

    Reported rather than hidden because a gesture's cost belongs to whoever
    made it: every file here is one request against a service that allows
    about three a second, and `named` is the measure of how much of the
    catalogue in hand that service knows.
    """

    albums: int
    files: int
    named: int
    seconds: float


class QualityBench:
    """Purpose: keep the folders being asked about, and answer from record.

    Responsibilities: walk a root once and remember what is under it, read every
    album's verdict back out of the measurements already stored, and measure the
    albums that are selected. Boundaries: it renames nothing, tags nothing, and
    never decodes audio that was not asked about — nothing sweeps a library on
    its own. Dependencies: an injected ``LibraryScanner``, ``LibraryStore``,
    and optionally a ``QualitySurvey``. Collaborators: ``LibraryApi``.
    Constraints: an audio key is read only for the files where one could change
    the answer, so the ordinary path costs a database lookup and no file access
    at all.
    """

    def __init__(
        self,
        scanner: LibraryScanner,
        store: LibraryStore,
        logger: logging.Logger,
        survey: QualitySurvey | None = None,
        deep: FfmpegQualityAnalyzer | None = None,
        clock: Callable[[], float] = time.perf_counter,
        acoustic: object | None = None,
    ) -> None:
        """Create a bench over one scanner, one store, and whatever can measure."""
        self._scanner = scanner
        self._store = store
        self._logger = logger
        self._survey = survey
        # The fingerprint service, present only when fpcalc and a user-supplied
        # key are. Absent, the bench works exactly the same: this screen does
        # not need it to draw anything.
        self._acoustic = acoustic
        # Injected rather than built here, and injected separately from the
        # survey: they are the same class with different rungs and a different
        # length of audio, and the test that drives this must be able to hand it
        # a runner that never starts a subprocess.
        self._deep = deep
        self._clock = clock

    def roots(self) -> tuple[BenchRoot, ...]:
        """Return every folder on the bench."""
        return self._store.bench_roots()

    def add(self, path: Path, on_progress: Callable[[int], None] | None = None) -> BenchRoot:
        """Put a folder on the bench and read what is under it, decoding nothing.

        The walk is the real cost, and it is paid once, with a progress report,
        because the folder was pointed at. It reads headers, not audio: what
        comes back is what is already known.
        """
        root_id = self._store.add_bench_root(path)
        self.walk(root_id, on_progress)
        return next(root for root in self.roots() if root.root_id == root_id)

    def walk(self, root_id: int, on_progress: Callable[[int], None] | None = None) -> int:
        """Re-read one root off the disk and record what is under it now."""
        root = next((entry for entry in self.roots() if entry.root_id == root_id), None)
        if root is None:
            return 0
        units = []
        for unit in self._scanner.scan(root.path):
            if unit.is_loose_track:
                continue
            units.append(unit)
            if on_progress is not None and len(units) % 100 == 0:
                on_progress(len(units))
        self._store.record_bench_albums(root_id, units)
        return len(units)

    def remove(self, root_id: int) -> None:
        """Take a folder off the bench, touching no file and no measurement."""
        self._store.remove_bench_root(root_id)

    def findings(self, root_id: int | None = None) -> tuple[BenchFinding, ...]:
        """Return what the bench knows, re-judged from the numbers on record.

        Everything is read in one pass rather than per album: a bench can hold
        thousands of albums, and one round trip to the database per album is
        too slow for a screen that has to open instantly.
        """
        albums = self._store.bench_albums(root_id)
        if not albums:
            return ()
        shared = self._store.shared_signatures()
        # A user verdict about the audio outranks the measurement, so it has to
        # be drawn here: a screen showing the measurement alone makes `Not
        # lossy` look like a control that does nothing. Read in one pass, since
        # every album asks about the same few rows.
        spoken = self._store.all_quality_overrides()
        measured = self._store.quality_for_files(
            (file.content_signature, file.audio_key) for album in albums for file in album.files
        )
        findings: list[BenchFinding] = []
        for album in albums:
            stored = [
                found
                for file in album.files
                if (found := measured.get((file.content_signature, file.audio_key))) is not None
            ]
            # A file is reading a borrowed measurement when the row that
            # answered was not taken from any named audio *and* more than one
            # name on this bench reads it. Either alone is ordinary.
            borrowed = sum(
                1
                for file in album.files
                if file.content_signature in shared
                and (found := measured.get((file.content_signature, file.audio_key))) is not None
                and not found.is_this_file
            )
            words = spoken.get(album.unit_signature, {})
            verdict = _with_user_word(verdict_from_stored(stored), words, album)
            findings.append(
                BenchFinding(
                    album=album,
                    verdict=verdict,
                    confidence=judge_confidence(
                        verdict, album.track_count, len(stored), shared=borrowed
                    ),
                    analyzed=len(stored),
                    shared=borrowed,
                    spoken=len(words),
                )
            )
        return tuple(findings)

    def analyze_in_depth(
        self,
        album_ids: Sequence[int],
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> DeepAnalysis:
        """Measure these albums properly, and write down nothing at all.

        Thirteen rungs from 14 to 21.5 kHz instead of five, the whole file
        instead of a minute, and the band-pass reading that shows whether what
        is left above the wall is a floor or falling music.

        The result is a proposal: nothing replaces what is stored until the
        user is asked and told what the work cost, which is why none of this
        happens by itself.
        """
        if self._deep is None:
            raise RuntimeError("ffmpeg is not available, so nothing can be measured.")
        wanted = {int(album_id) for album_id in album_ids}
        albums = [album for album in self._store.bench_albums() if album.album_id in wanted]
        started = self._clock()
        files = 0
        measured: list[DeepAlbum] = []
        # **Counted in files, because that is what the work is.** Counted in
        # albums, a run over two of them shows `Measuring 1 of 2` for the whole
        # of the first album and reads as a hang: a bar that cannot move during
        # the only phase that takes time says nothing about being alive.
        total = sum(len(album.files) for album in albums)
        done = 0

        def one_file_done(folder: str) -> None:
            nonlocal done
            done += 1
            if on_progress is not None:
                on_progress(done, total, folder)

        for album in albums:
            if on_progress is not None:
                on_progress(done, total, album.folder_path.name)
            tracks = self._measure_deeply(album, one_file_done)
            files += len(tracks)
            measured.append(
                DeepAlbum(
                    album_id=album.album_id,
                    folder=album.folder_path.name,
                    tracks=tracks,
                    before=verdict_from_stored(
                        [track.was for track in tracks if track.was is not None]
                    ),
                    after=verdict_from_stored([track.measured for track in tracks]),
                )
            )
        return DeepAnalysis(albums=tuple(measured), files=files, seconds=self._clock() - started)

    def _measure_deeply(
        self, album: BenchAlbum, on_file: Callable[[str], None] | None = None
    ) -> tuple[DeepTrack, ...]:
        """Measure one album's files, in parallel, and pair each with what stood.

        **One file is one unit of work, both passes together.** Run as two
        pooled passes with a barrier between them, every file waits for the
        slowest file of the first pass before any of the second can start, and
        nothing can be called finished until both have run — which leaves no
        moment to report progress from.

        The time per file depends on how the file was encoded as well as on its
        length: a FLAC written with very small blocks decodes many times slower
        than one with ordinary blocks, for the same samples and the same
        verdict. A test folder of fabricated files is therefore not a measure
        of what this costs on real ones.
        """
        assert self._deep is not None
        standing = self._store.quality_for_files(
            (file.content_signature, file.audio_key) for file in album.files
        )
        paths = [album.folder_path / file.name for file in album.files]
        deep = self._deep

        def measure(path: Path) -> tuple[TrackAnalysis | None, tuple[PassBand, ...]]:
            analysis = deep.analyze(path)
            if analysis is None:
                return None, ()
            return analysis, deep.pass_bands(path, analysis.sample_rate)

        results: list[tuple[TrackAnalysis | None, tuple[PassBand, ...]]] = [(None, ())] * len(paths)
        with ThreadPoolExecutor(max_workers=DEEP_WORKERS) as pool:
            pending = {pool.submit(measure, path): index for index, path in enumerate(paths)}
            for future in as_completed(pending):
                results[pending[future]] = future.result()
                if on_file is not None:
                    on_file(album.folder_path.name)
        analyses = [analysis for analysis, _ in results]
        banded = [bands for _, bands in results]
        tracks: list[DeepTrack] = []
        for file, path, analysis, bands in zip(album.files, paths, analyses, banded, strict=True):
            if analysis is None:
                continue
            key = file.audio_key or audio_key(path)
            verdict = judge(analysis)
            tracks.append(
                DeepTrack(
                    file_name=file.name,
                    content_signature=file.content_signature,
                    audio_key=key,
                    measured=StoredQuality(
                        encoding=str(verdict.encoding),
                        effective_bitrate_kbps=verdict.effective_bitrate_kbps,
                        cutoff_hertz=verdict.cutoff_hertz,
                        steepest_drop_db=analysis.spectral.steepest_drop_db,
                        findings=tuple(str(name) for name in verdict.findings),
                        reason=verdict.reason,
                        decay_db=analysis.spectral.decay_db,
                        ceiling_db=analysis.spectral.ceiling_db,
                        audio_key=key,
                        # Which ruler found the wall, and where the audio stops
                        # changing. The survey's row carries both, so this one
                        # must too: a row written without a floor is judged by
                        # fall-and-emptiness alone, and the deep pass would then
                        # judge a file lossless while the row it wrote reads
                        # back as lossy on the next launch.
                        wall_low_hertz=analysis.spectral.wall_low_hertz,
                        floor_hertz=analysis.spectral.floor_from_hertz,
                        # And the framing, for the same reason: this is the
                        # second place a measurement becomes a row, and a column
                        # carried by one writer and not the other makes the deep
                        # pass disagree with the survey about the same file.
                        frame_grid_z=(
                            None if analysis.frame_grid is None else analysis.frame_grid.peak_z
                        ),
                        frame_grid_agrees=(
                            None
                            if analysis.frame_grid is None
                            else analysis.frame_grid.readings_agree
                        ),
                        # The count as well as the flag, for the same reason the
                        # two lines above exist: the deep pass and the survey
                        # must write the same facts or they judge one file two
                        # ways.
                        frame_grid_agreeing=(
                            None
                            if analysis.frame_grid is None
                            else analysis.frame_grid.agreeing_readings
                        ),
                    ),
                    bands=bands,
                    was=standing.get((file.content_signature, file.audio_key)),
                )
            )
        return tuple(tracks)

    def identify_in_depth(
        self,
        album_ids: Sequence[int],
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> AcousticSweep:
        """Put every file of these albums to the fingerprint service, on request.

        The identification path asks the service about an album only when its
        names failed, so on its own it covers a small fraction of the files.
        This is the gesture that fills the rest, and it is deliberately per
        album and never over a library: the service allows about three requests
        a second, and a library of tens of thousands of files would take hours.

        Nothing is decided here and nothing is renamed. What it produces is a
        record of what the service knows about the catalogue in hand.
        """
        if self._acoustic is None:
            raise RuntimeError("The fingerprint service is not available.")
        wanted = {int(album_id) for album_id in album_ids}
        albums = [album for album in self._store.bench_albums() if album.album_id in wanted]
        started = self._clock()
        asked = 0
        named = 0
        for index, album in enumerate(albums, start=1):
            if on_progress is not None:
                on_progress(index, len(albums), album.folder_path.name)
            for file in album.files:
                recording = self._acoustic.recognize(
                    album.folder_path / file.name, file.content_signature
                )
                asked += 1
                named += recording is not None
        self._logger.info(
            "The bench put files to the fingerprint service.",
            extra={
                "operation": "quality.bench.identified",
                "albums": len(albums),
                "files": asked,
                "named": named,
            },
        )
        return AcousticSweep(
            albums=len(albums), files=asked, named=named, seconds=self._clock() - started
        )

    def adopt(self, analysis: DeepAnalysis) -> int:
        """Write a deep measurement down, once approved, and remember the keys.

        Every row carries the audio it was taken from, so it answers for that
        file alone and never for whatever else happens to share its shape. The
        row that was already there is left standing for the files that still
        read it.
        """
        # **A list of pairs, never a map keyed by signature.** Two files of one
        # declared shape are two measurements, and a dictionary keyed by that
        # shape keeps the last one — so an album holding a collision would
        # write one row for two files, and the first file would go on reading
        # whatever stood before.
        rows: list[tuple[str, StoredQuality]] = []
        keys: dict[tuple[int, str], str] = {}
        for album in analysis.albums:
            for track in album.tracks:
                rows.append((track.content_signature, track.measured))
                if track.audio_key is not None:
                    keys[(album.album_id, track.file_name)] = track.audio_key
        self._store.record_measurements(rows)
        self._store.remember_audio_keys(keys)
        self._logger.info(
            "A deep measurement was adopted.",
            extra={
                "operation": "quality.bench.adopted",
                "albums": len(analysis.albums),
                "files": len(rows),
            },
        )
        return len(rows)

    def audio_keys_for(self, albums: Sequence[BenchAlbum]) -> dict[tuple[int, str], str]:
        """Read the audio key of every file of these albums that lacks one.

        Only ever called about selected albums. Reading a key is a seek and a
        read, which is nothing for one album and one per file for a whole
        library — so the bench never does it to draw a screen.
        """
        found: dict[tuple[int, str], str] = {}
        for album in albums:
            for file in album.files:
                if file.audio_key is not None:
                    continue
                key = audio_key(album.folder_path / file.name)
                if key is not None:
                    found[(album.album_id, file.name)] = key
        self._store.remember_audio_keys(found)
        return found
