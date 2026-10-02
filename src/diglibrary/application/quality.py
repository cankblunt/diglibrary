"""Walking a library and measuring what every album actually is."""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from statistics import median, median_low

from diglibrary.database.library_store import LibraryStore, StoredQuality
from diglibrary.library.audio_key import audio_key
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.library.scanner import LibraryScanner
from diglibrary.quality.analysis import FfmpegQualityAnalyzer
from diglibrary.quality.framing import READINGS, FrameGrid
from diglibrary.quality.models import Encoding, Proof, TrackAnalysis
from diglibrary.quality.verdict import (
    CUTOFF_TRUST_HZ,
    AlbumVerdict,
    TrackVerdict,
    album_is_transcoded,
    album_proof,
    album_transcode_reason,
    convicted_by_frame_grid,
    judge_album,
    track_is_transcoded,
)

DEFAULT_WORKERS = 8
"""How many files to measure at once.

ffmpeg is a subprocess, so threads wait on it rather than compete for the
interpreter. Eight keeps a laptop responsive while turning hours of serial
measurement into minutes.
"""


@dataclass(frozen=True, slots=True)
class AlbumQuality:
    """Purpose: pair one album on disk with what its audio measured.

    Responsibilities: carry the folder, the verdict, and how much of the album
    could actually be measured. Boundaries: it proposes no rename and writes
    nothing — it is the finding the user is shown before any of that is
    offered. Dependencies: ``AlbumVerdict``. Collaborators: the quality survey
    and the window. Constraints: ``analyzed`` may be smaller than ``tracks``
    when files are damaged, and the difference is shown rather than hidden,
    because a verdict from two of fourteen tracks is weaker evidence and the
    user is entitled to know that.
    """

    folder_path: Path
    unit_signature: str
    verdict: AlbumVerdict
    tracks: int
    analyzed: int

    @property
    def is_suspect(self) -> bool:
        """Report whether this album is not what its container claims."""
        return self.verdict.encoding in {Encoding.TRANSCODED, Encoding.OVERSTATED}


class QualitySurvey:
    """Purpose: measure every album beneath a root and report what each one is.

    Responsibilities: reuse the library scan to find albums, measure their
    tracks in parallel, and judge each album as a whole. Boundaries: it never
    renames, tags, or writes — the survey produces findings and stops there.
    Dependencies: an injected ``LibraryScanner`` and ``FfmpegQualityAnalyzer``.
    Collaborators: ``LibraryApi``, which turns findings into what the window
    shows. Constraints: albums are found by the same scanner the organizer
    uses, so a folder that merely shelves albums is never surveyed as one, and
    identical audio is measured once however many folders hold it — the
    content signature survives a retag and a rename.
    """

    def __init__(
        self,
        scanner: LibraryScanner,
        analyzer: FfmpegQualityAnalyzer,
        logger: logging.Logger,
        workers: int = DEFAULT_WORKERS,
        store: LibraryStore | None = None,
    ) -> None:
        """Create a survey over one scanner, one analyzer, and a store to remember."""
        self._scanner = scanner
        self._analyzer = analyzer
        self._logger = logger
        self._workers = max(1, workers)
        self._store = store

    def known(self, units: Sequence[AlbumUnit]) -> dict[tuple[str, str | None], StoredQuality]:
        """Return what is already measured for these files, from the database.

        Measuring costs minutes of decoding, so it is done once per recording
        and remembered. This is what lets the organizer ask what a file is
        without paying for the answer twice.

        **Keyed by the file's audio and not only by its declared shape.** A
        content signature is codec, sample count, rate, channels and depth, and
        two recordings of one track — an honest master and the same track
        laundered from an MP3 — agree on every one of them. A lookup by
        signature alone returns the row of whichever was measured first, so an
        honest album would be accused, every track, of being the transcode
        sitting beside it. ``quality_for_files`` is the reading built for this.

        A file whose audio cannot be keyed still reads the signature's row,
        which is the ordinary case.
        """
        if self._store is None:
            return {}
        return measurements_for(
            self._store,
            (
                (file.content_signature, file.audio_key)
                for unit in units
                for file in unit.audio_files
            ),
        )

    def albums(self, root: Path, excluded: Iterable[Path] = ()) -> tuple[AlbumUnit, ...]:
        """Return the albums a survey of this root would measure."""
        return tuple(unit for unit in self._scanner.scan(root, excluded) if not unit.is_loose_track)

    def measure(
        self,
        units: Sequence[AlbumUnit],
        on_album: Callable[[int, int, AlbumQuality], None] | None = None,
    ) -> tuple[AlbumQuality, ...]:
        """Measure every album, reporting each one as it is finished.

        ``on_album`` receives the album's position, the total, and the finding,
        so a window can fill in as the work proceeds rather than waiting for a
        library-sized silence.
        """
        # Both maps are keyed by the file's audio, never by its declared shape
        # alone: two recordings of one track share a content signature, and
        # keying by it hands one file the measurement of the other.
        measured: dict[tuple[str, str | None], TrackAnalysis | None] = {}
        # A row from before a rule lacks the numbers that rule asks for, so it
        # is measured again rather than judged on what it has. **Which numbers
        # those are is `StoredQuality`'s to say**, beside the columns
        # themselves. A condition spelled out here would name the columns known
        # when it was written, and every measurement added afterwards would
        # leave older rows remembered without it — so a file already surveyed
        # could never be reached by a newer check.
        remembered = {
            key: quality
            for key, quality in self.known(units).items()
            if quality.answers_the_rules_in_force
        }
        findings: list[AlbumQuality] = []
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            for index, unit in enumerate(units, start=1):
                # Kept per album rather than for the whole run: measuring a
                # library is hours of decoding, and holding all of it in memory
                # until the last album would let a crash, a quit, or one
                # unreadable file cost every measurement taken so far.
                fresh: dict[str, StoredQuality] = {}
                try:
                    finding = self._measure_album(unit, pool, measured, remembered, fresh)
                except Exception as error:
                    # One album must never cost the library. Whatever this was,
                    # it is this album's problem: it is reported unmeasured, it
                    # is named in the log, and the survey carries on.
                    self._logger.exception(
                        "An album could not be measured; the survey continues without it.",
                        extra={
                            "operation": "quality.survey.album_failed",
                            "folder": str(unit.folder_path),
                            "error": str(error),
                        },
                    )
                    finding = _unmeasured(unit)
                if self._store is not None and fresh:
                    self._store.record_quality(fresh)
                findings.append(finding)
                if on_album is not None:
                    on_album(index, len(units), finding)
        self._logger.info(
            "Quality survey completed.",
            extra={
                "operation": "quality.survey",
                "albums": len(findings),
                "suspect": sum(1 for finding in findings if finding.is_suspect),
            },
        )
        return tuple(findings)

    def _measure_album(
        self,
        unit: AlbumUnit,
        pool: ThreadPoolExecutor,
        measured: dict[tuple[str, str | None], TrackAnalysis | None],
        remembered: Mapping[tuple[str, str | None], StoredQuality],
        fresh: dict[str, StoredQuality],
    ) -> AlbumQuality:
        pending = [
            file
            for file in unit.audio_files
            if _key_of(file) not in measured and _key_of(file) not in remembered
        ]
        if pending:
            for file, analysis in zip(
                pending,
                pool.map(self._analyzer.analyze, [file.path for file in pending]),
                strict=True,
            ):
                measured[_key_of(file)] = analysis
        # Paired with the signature of the file that asked, never with the path
        # the analysis happens to carry. `measured` is shared across the whole
        # run, so an album holding audio another album measured first is given
        # that album's analysis — whose path is not in this folder. Looking the
        # file up by that path finds nothing, and a fallback to the file *name*
        # would write the measurement under a key no lookup can ever produce.
        present = [
            (file.content_signature, analysis)
            for file in unit.audio_files
            if (analysis := measured.get(_key_of(file))) is not None
        ]
        analyses = [analysis for _, analysis in present]
        verdict = judge_album(analyses)
        # `strict=True`: `judge_album` returns one verdict per analysis, and if
        # that ever stopped being true the rows would go on being written with
        # the last files silently left out.
        for (signature, analysis), track in zip(present, verdict.tracks, strict=True):
            fresh[signature] = _stored(track, analysis)
        # An album measured earlier is rebuilt from the database rather than
        # decoded again, so a second scan of a library costs no audio at all.
        replayed = [
            remembered[_key_of(file)] for file in unit.audio_files if _key_of(file) in remembered
        ]
        if replayed:
            # Every track, not the ones that happened to need decoding. An
            # album is judged by the median of its tracks, and a median of the
            # fresh half is a statement about that half printed as a statement
            # about the album: with ten lossless tracks on record and one
            # decoded again, the album would be sentenced on that one track.
            # An album judged a transcode names every one of its files a
            # transcode, so the error reaches all of them.
            verdict = verdict_from_stored(
                [
                    quality
                    for file in unit.audio_files
                    if (
                        quality := fresh.get(file.content_signature)
                        or remembered.get(_key_of(file))
                    )
                    is not None
                ]
            )
        return AlbumQuality(
            folder_path=unit.folder_path,
            unit_signature=unit.unit_signature,
            verdict=verdict,
            tracks=unit.track_count,
            analyzed=len(analyses) + len(replayed),
        )


def _unmeasured(unit: AlbumUnit) -> AlbumQuality:
    """Report an album nothing could be learned about, without claiming it is fine."""
    return AlbumQuality(
        folder_path=unit.folder_path,
        unit_signature=unit.unit_signature,
        verdict=AlbumVerdict(
            encoding=Encoding.UNDECIDED,
            reason="This album could not be measured; the log says what stopped it.",
            tracks=(),
            median_drop_db=0.0,
        ),
        tracks=unit.track_count,
        analyzed=0,
    )


def _stored(track: TrackVerdict, analysis: TrackAnalysis) -> StoredQuality:
    """Render one track's verdict in the shape the database keeps.

    The row names the audio it was taken from. Without that it answers for every
    file of its content signature, and a large share of a real library shares
    one with a provably different song — so a number read from an unkeyed row
    is withheld from every filename, and a track that had earned a bitrate
    would wear `[Lossy]` instead.

    It is written here because this is where the audio was just decoded: the
    file is open, and a FLAC answers from its own header without reading a byte
    of audio. Reading the key costs well under a millisecond a file for FLAC
    and about a millisecond for MP3. Deciding per file whether the key *could*
    change an answer is the wrong shape: ambiguity is not a stable property,
    and a file that becomes ambiguous later has already been written without
    a key.
    """
    return StoredQuality(
        encoding=str(track.encoding),
        effective_bitrate_kbps=track.effective_bitrate_kbps,
        cutoff_hertz=track.cutoff_hertz,
        steepest_drop_db=analysis.spectral.steepest_drop_db,
        decay_db=analysis.spectral.decay_db,
        ceiling_db=analysis.spectral.ceiling_db,
        findings=tuple(str(name) for name in track.findings),
        reason=track.reason,
        audio_key=audio_key(analysis.path),
        # And which ruler measured it. Present means the wall was found as a
        # step; absent means this row is older than that rule and is left to
        # the conclusion it was given.
        wall_low_hertz=analysis.spectral.wall_low_hertz,
        # And where the audio stops changing, so that the fall-and-emptiness
        # rule stops at the same frequency the wall rule does.
        floor_hertz=analysis.spectral.floor_from_hertz,
        # And what the encoder's own framing said, so that a row caught by it
        # is still caught when this row is read back and re-judged.
        frame_grid_z=None if analysis.frame_grid is None else analysis.frame_grid.peak_z,
        frame_grid_agrees=(
            None if analysis.frame_grid is None else analysis.frame_grid.readings_agree
        ),
        # And how many of the six named the alignment, which is the question the
        # rule asks. The flag above stays because rows written before the count
        # existed are judged by it.
        frame_grid_agreeing=(
            None if analysis.frame_grid is None else analysis.frame_grid.agreeing_readings
        ),
    )


def verdict_from_stored(stored: Sequence[StoredQuality]) -> AlbumVerdict:
    """Rebuild an album's verdict from measurements already in the database.

    The album is judged the same way it was the first time — by the median of
    its tracks — so a replayed verdict and a fresh one agree. That agreement
    is the whole contract of this function, and it is what a test asserts
    directly: every scan after the first reads the verdict from here, so a
    disagreement is a folder renamed on a conclusion no measurement reached.
    """
    # **Nothing measured is not a verdict.** With no rows at all the album
    # would fall through to the lossy branch — there are no lossless containers
    # among no measurements — and the screen would announce `lossy` about a
    # record nothing had measured. Albums with no track measured, or fewer than
    # half, are an ordinary case and not an edge. `undecided` is what this enum
    # has for *no answer*, it draws grey, and the reason says so.
    if not stored:
        return AlbumVerdict(
            encoding=Encoding.UNDECIDED,
            reason="Nothing here has been measured yet, so there is nothing to replay.",
            tracks=(),
            median_drop_db=0.0,
            median_ceiling_db=0.0,
        )
    tracks = tuple(
        TrackVerdict(
            encoding=_stored_encoding(item),
            reason=item.reason,
            findings=(),
            effective_bitrate_kbps=item.effective_bitrate_kbps,
            cutoff_hertz=item.cutoff_hertz,
            proved_by=stored_track_proof(item),
        )
        for item in stored
    )
    # Which rows were measured inside a lossless container, which is the only
    # place a lossy origin can hide. Everything the transcode question reads
    # comes from these and from nothing else: otherwise, in a folder of ten
    # honest MP3s and one FLAC, the MP3s' walls would convict the album and the
    # FLAC would be renamed `[Lossy]` with it. Kept in step with `judge_album`,
    # because a replayed verdict agreeing with a fresh one is this function's
    # contract. Named rather than written as a complement, deliberately: an
    # `undecided` row answers nothing about a container, so it belongs to
    # neither side. The three siblings of this line say the same.
    inside_lossless = [item for item in stored if item.encoding in ("lossless", "transcoded")]
    decays = [item.decay_db for item in inside_lossless if item.decay_db is not None]
    ceilings = [item.ceiling_db for item in inside_lossless if item.ceiling_db is not None]
    median_drop = median(decays) if decays else 0.0
    # Zero reads as a full top band, so an album replayed from rows taken before
    # the two-sign rule is never marked borderline on a number nobody measured.
    median_ceiling = median(ceilings) if ceilings else 0.0
    # Where the typical track of this album stops. Only rows that recorded it
    # count, so an album of older measurements keeps answering as it did — the
    # same rule the track path follows.
    floors = [item.floor_hertz for item in inside_lossless if item.floor_hertz is not None]
    # `median_low` for the reason the fresh path uses it: a floor is a rung, and
    # the ordinary median of an even split invents one between two of them —
    # 19.5 kHz would clear the trusted line that neither 19 nor a real reading
    # does. The replayed verdict has to ask the identical question.
    median_floor = median_low(floors) if floors else None
    # Only a lossless container can be hiding a lossy origin, and the stored
    # rows say which one this was: a track measured inside a FLAC was written
    # `lossless` or `transcoded`, and one inside an MP3 was not. Asking the
    # transcode question of a lossy album would call most honest MP3 albums
    # fakes — a wall below 19 kHz is not evidence against an MP3, it is what
    # an MP3 is.
    # Declared before the branch that computes them: both are read again by
    # the sentence below, and an album with no lossless container never
    # reaches the branch that sets them.
    walls = 0
    framed = 0
    if not inside_lossless:
        encoding = (
            Encoding.OVERSTATED
            if sum(1 for track in tracks if track.encoding is Encoding.OVERSTATED) * 2 > len(tracks)
            else Encoding.LOSSY
        )
    else:
        walls = sum(1 for item in inside_lossless if _stored_wall(item))
        # And the rows the frame grid caught, counted exactly as the fresh path
        # counts them. Every other number here is a spectral median, and a row
        # caught by its framing reads lossless spectrally — so without this the
        # same album would answer `transcoded` after its scan and `lossless` on
        # the next launch: the row on disk right and the screen wrong.
        framed = sum(1 for item in inside_lossless if convicted_by_frame_grid(_grid_of(item)))
        transcoded = (
            walls * 2 > len(inside_lossless)
            or framed * 2 > len(inside_lossless)
            or (
                bool(decays and ceilings)
                and album_is_transcoded(median_drop, median_ceiling, median_floor)
            )
        )
        # Lossless, not "whatever the first track was". The fresh path answers
        # this exact question with `Encoding.LOSSLESS` — the album has a
        # lossless container and the transcode rule just declined to fire — and
        # the agreement between a replayed verdict and a fresh one is this
        # function's whole contract. Reading `tracks[0]` would break it
        # whenever the first file in a folder listing is a dissenter: an album
        # with a minority of dissenting tracks and a gentle median would be
        # condemned by the one that sorted first, and an album called a
        # transcode names every one of its files a transcode.
        encoding = Encoding.TRANSCODED if transcoded else Encoding.LOSSLESS
    # A bitrate is what a lossy stream is, so an honest lossless album has none
    # to report. Showing one read as a contradiction on screen: "lossless,
    # looks like 320 kbps".
    # Read from whichever set decided the verdict, so the number on screen and
    # the word beside it came from the same tracks.
    deciding = inside_lossless or stored
    rates = [item.effective_bitrate_kbps for item in deciding if item.effective_bitrate_kbps]
    reports_bitrate = encoding is not Encoding.LOSSLESS
    # **The sentence that says what convicted, not that something did.**
    # Every album on the Quality screen is drawn from here. `Replayed from N
    # measurements already on record.` names no evidence at all, and beside a
    # spectrogram that looks healthy it leaves nothing on the screen to say
    # why the album was condemned. So the replayed sentence names its proof,
    # as the fresh one does.
    proof = (
        album_proof(framed, walls, len(inside_lossless))
        if encoding is Encoding.TRANSCODED and inside_lossless
        else None
    )
    return AlbumVerdict(
        encoding=encoding,
        reason=_replayed_reason(
            proof, encoding, framed, walls, inside_lossless, decays, median_drop, len(stored)
        ),
        tracks=tracks,
        median_drop_db=median_drop,
        effective_bitrate_kbps=round(median(rates)) if rates and reports_bitrate else None,
        median_ceiling_db=median_ceiling,
        proved_by=proof,
    )


def _replayed_reason(
    proof: Proof | None,
    encoding: Encoding,
    framed: int,
    walls: int,
    inside_lossless: Sequence[StoredQuality],
    decays: Sequence[float],
    median_drop: float,
    rows: int,
) -> str:
    """Say what the rows on record add up to, in the fresh path's own words.

    The same two functions the fresh verdict uses, so a replayed sentence and
    a fresh one cannot drift apart — the agreement between them is what
    ``verdict_from_stored`` exists to keep.

    An album outside a lossless container, or one with no spectral numbers at
    all, keeps a sentence about what is on record rather than borrowing one
    about a measurement it does not have.
    """
    if proof is not None:
        return album_transcode_reason(
            proof, framed, walls, len(inside_lossless), median_drop, len(decays)
        )
    if encoding is Encoding.LOSSLESS and decays:
        return (
            f"Across {len(decays)} tracks the energy fades gradually, "
            f"falling {median_drop:.1f} dB at the median."
        )
    return f"Replayed from {rows} measurements already on record."


def measurements_for(
    store: LibraryStore, files: Iterable[tuple[str, str | None]]
) -> dict[tuple[str, str | None], StoredQuality]:
    """Return each file's own measurement, refusing one taken from other audio.

    **The one place this application asks the store what it knows about files
    in hand.** ``_preferred`` falls back to another recording's row on purpose
    and says in its own docstring that the caller must decide what that is
    worth — and every caller here decides the same thing, so the decision is
    made once. A row taken from audio that is provably not this file's is not
    an answer about this file: measuring costs seconds, and being wrong costs
    a false accusation against an honest album.

    A file with no audio key of its own, or a row written before keys existed,
    reads the signature's row. That is the ordinary case.

    **Keyed by the pair, and it must stay that way to the last line.** Folding
    the answer back onto the signature undoes the whole method inside one
    dictionary comprehension: two files of one signature and different audio
    write into one slot and the last one wins, which takes the `[Lossy]` flag
    off a transcoded album as soon as the honest album beside it is measured.
    """
    rows = store.quality_for_files(files)
    return {
        (signature, key): row
        for (signature, key), row in rows.items()
        if key is None or row.audio_key is None or row.audio_key == key
    }


def stored_track_proof(quality: StoredQuality) -> Proof | None:
    """Say which measurement convicted this row, or ``None`` if none did.

    Asked in the order the rule asks it: the frame grid answers before the
    spectrum, and a wall answers before the fall-and-emptiness pair. The
    distinction is not bookkeeping — a wall is a cliff anybody can see in a
    spectrogram, and a grid is invisible in one, so a screen that says only
    *transcoded* about both sends half its readers to a picture that appears
    to clear the file.
    """
    if not stored_track_is_transcoded(quality):
        return None
    if convicted_by_frame_grid(_grid_of(quality)):
        return Proof.GRID
    if _stored_wall(quality):
        return Proof.WALL
    return Proof.SPECTRUM


def the_picture_shows_the_proof(quality: StoredQuality) -> bool:
    """Whether a spectrogram of this file shows something that convicted it.

    `proved_by` names one proof and a file can carry more than one — `album_proof`
    says so in its own docstring, and it is why the grid is asked first: it is
    the finding a screen has to warn about. The sentence that warning is made of
    says the picture will not show this one, and over a file that also carries a
    wall that is false about the picture the reader is looking at. Files
    convicted by the grid that also carry a wall inside the trusted range are
    common in a real library.

    So the question the note depends on is asked as itself, here, beside the
    proof it qualifies — never reassembled in the window out of `cutoff` and a
    threshold this side owns. A wall is the one proof a picture shows; the grid
    is invisible in one, and the fall-and-emptiness pair is a shape read off
    numbers.
    """
    return _stored_wall(quality)


def _stored_wall(quality: StoredQuality) -> bool:
    """Report whether a stored row records a wall inside the trusted range."""
    return quality.cutoff_hertz is not None and quality.cutoff_hertz <= CUTOFF_TRUST_HZ


def _grid_of(quality: StoredQuality) -> FrameGrid | None:
    """Rebuild the framing reading a row carries, or ``None`` if it carries none.

    The offset is not stored: it is what makes the six readings comparable
    while they are being taken, and afterwards only their agreement matters.
    A placeholder is used so that one rule reads both a fresh measurement and a
    stored row — two rules would be two places for this to drift.
    """
    if quality.frame_grid_z is None:
        return None
    # A row written since the count exists answers with it. An older one has
    # only the flag, and is judged by exactly the question it was measured
    # under: unanimous means six, and anything else is one — the count it
    # would have had is unknown, and inventing a four here would convict rows
    # nobody measured for that.
    if quality.frame_grid_agreeing is not None:
        agreeing = quality.frame_grid_agreeing
    elif quality.frame_grid_agrees is None:
        return None
    else:
        agreeing = READINGS if quality.frame_grid_agrees else 1
    return FrameGrid(peak_z=quality.frame_grid_z, offset=-1, agreeing_readings=agreeing)


def _key_of(file: AudioFileFacts) -> tuple[str, str | None]:
    """Name one file by its audio, falling back to its declared shape.

    The pair is what tells two recordings of one track apart — an honest master
    and the same track laundered from an MP3 agree on codec, sample count,
    rate, channels and depth, so the signature alone hands one of them the
    other's verdict. ``None`` for the key keeps the signature-only reading for
    a file whose audio cannot be named, which is the ordinary case.
    """
    return (file.content_signature, file.audio_key)


def stored_track_is_transcoded(quality: StoredQuality) -> bool:
    """Ask the current rule of a stored row, never the verdict written with an old one.

    A run under the slope-only rule recorded genuine tracks as ``transcoded``,
    and those rows would poison every replay and every rename that trusted the
    stored word. The numbers were measured correctly all along — only the
    conclusion moved — so the conclusion is re-drawn from the numbers.

    **Rows measured under the older wall rule are judged by that rule.** Their
    `cutoff_hertz` came from the absolute floor, and ``track_is_transcoded``
    reads it as such. The later rule changed how a *fresh* measurement finds a
    wall, and a fresh measurement is judged where it is taken: the correction
    applies from then on and is not retroactive. So a row convicted under the
    older rule keeps its conviction until that album is measured again.
    """
    # Named, not complemented — see `_what_stands_about`: `undecided` is
    # returned before any container is looked at, so it cannot join either
    # side without carrying a claim nobody measured.
    if quality.encoding not in ("transcoded", "lossless"):
        return False
    # **The frame grid is asked before the spectrum, and answers on its own.**
    # A file caught by its framing reads *lossless* spectrally — that is the
    # whole reason the measurement exists — so a re-judgement that asked only
    # the spectrum would absolve on the next launch what the scan had just
    # caught. A row with no grid columns predates the question and falls
    # through to the rule it was measured for.
    if convicted_by_frame_grid(_grid_of(quality)):
        return True
    if quality.decay_db is None or quality.ceiling_db is None:
        return quality.encoding == "transcoded"
    return track_is_transcoded(
        quality.cutoff_hertz,
        quality.decay_db,
        quality.ceiling_db,
        quality.floor_hertz,
    )


def _stored_encoding(item: StoredQuality) -> Encoding:
    """Return a stored row's encoding, re-judged where the rule has moved."""
    # The same named pair as its three siblings: a re-judgement is a statement
    # about a lossless container, and `undecided` does not say whether it was
    # measured inside one.
    if item.encoding in ("transcoded", "lossless"):
        return Encoding.TRANSCODED if stored_track_is_transcoded(item) else Encoding.LOSSLESS
    return Encoding(item.encoding)
