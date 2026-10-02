"""Persisting what was scanned, identified, planned, and applied."""

import json
import logging
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from diglibrary.application.contracts import ReleaseMetadata, written_credit
from diglibrary.database.connection import Database
from diglibrary.database.serialization import (
    UnreadableReleaseError,
    deserialize_release,
    serialize_release,
)
from diglibrary.library.executor import AppliedOperation
from diglibrary.library.fingerprint import AudioFingerprint
from diglibrary.library.models import AlbumUnit
from diglibrary.library.planner import ChangeOperation, OperationKind


@dataclass(frozen=True, slots=True)
class StoredUnit:
    """Purpose: identify a persisted album unit and report what is known about it.

    Responsibilities: carry the row identity, the content signature, and the
    state the library has recorded. Boundaries: it holds no plan and no release.
    Dependencies: none. Collaborators: ``LibraryStore`` and the workflow that
    decides what still needs work. Constraints: the signature, not the path, is
    what identifies the album across runs, because it survives a rename.
    """

    unit_id: int
    unit_signature: str
    folder_path: Path
    state: str
    # What brought this album in: `scanned` or `downloaded`. Both persist, so
    # it is not inferable from which of them survived a restart.
    origin: str = "scanned"
    # The user's rating, 1 to 5, or `None` for an album not yet rated. `None`
    # is not zero and never renders as an empty rating: an unrated card draws
    # nothing at all.
    rating: int | None = None


@dataclass(frozen=True, slots=True)
class StoredHarmonics:
    """What one recording is in, as it was persisted.

    Both halves may be absent independently: a track can have a readable key
    and no settled tempo. Absent is `None` and never a zero, because a screen
    printing `0 BPM` would be stating something the audio never said.
    """

    audio_key: str
    content_signature: str
    tonic: str | None
    mode: str | None
    key_confidence: float | None
    bpm: float | None
    bpm_confidence: float | None
    # The other octave when the audio does not decide. Distinct from an absent
    # `bpm`, which is "not measured".
    bpm_alternative: float | None
    # How far ahead of the runner-up the key came in, and which key that was.
    # A key with no margin was measured before the column existed, and
    # therefore with the earlier key profile, which is a different claim from
    # a key that won by nothing.
    key_margin: float | None = None
    runner_up_tonic: str | None = None
    runner_up_mode: str | None = None

    @property
    def key_name(self) -> str | None:
        """The key as a musician writes it, or `None` when it was not found."""
        return f"{self.tonic} {self.mode}" if self.tonic and self.mode else None


@dataclass(frozen=True, slots=True)
class StoredQuality:
    """What one measurement of a file concluded, as it was persisted."""

    encoding: str
    effective_bitrate_kbps: int | None
    cutoff_hertz: int | None
    steepest_drop_db: float | None
    findings: tuple[str, ...]
    reason: str
    decay_db: float | None = None
    ceiling_db: float | None = None
    audio_key: str | None = None
    """Which audio this measurement was actually taken from.

    ``None`` means the row predates the question and answers for every file of
    its content signature. A row that carries a key answers only for the file
    whose audio matches it, and is the one preferred when both are on record.
    """

    wall_low_hertz: int | None = None
    """The last rung that still held audio below the wall.

    Set only where there *is* a wall, so it is not a mark of which ruler
    measured the row: a clean file carries no wall and no rung whichever rule
    measured it. What it does say is unambiguous where it matters — a row with
    a `cutoff_hertz` and no rung below it was measured when a wall was the
    first band under an absolute floor, and the screen says so rather than
    drawing an interval it cannot know.

    Nothing re-judges on it. A rule change applies from the change onwards:
    the old rows are read by the rule they were measured for, which is what
    `stored_track_is_transcoded` does, and the new rule lives where a fresh
    measurement is judged.
    """

    frame_grid_z: float | None = None
    """How far one alignment of 576 stood above the rest, when that was measured.

    Recorded because a row is re-judged from its numbers every time it is
    read, and a file caught by its frame grid has a spectrum that reads
    lossless — that is the point of the measurement. Without this column the
    re-judgement would absolve, on the next launch, exactly what the scan
    caught.

    ``None`` is a row measured before this question was asked, and it keeps the
    answer its own numbers give, as with every rule change here.
    """

    frame_grid_agrees: bool | None = None
    frame_grid_agreeing: int | None = None
    """Whether all six readings named the same alignment.

    The other half of the rule, and stored separately for the same reason it is
    asked separately: a tall peak nobody corroborated and a corroborated peak
    that is merely noise are two different things, and a single column could
    not tell them apart afterwards.
    """

    floor_hertz: int | None = None
    """Where the audio stops changing — the frequency the floor begins at.

    Recorded so that the fall-and-emptiness rule can consult the same ceiling
    the wall rule does. Without it that rule convicts recordings ending at
    20 kHz, which is where an honest converter's filter lands and where
    `CUTOFF_TRUST_HZ` is deliberately set to stop.

    ``None`` is a row measured before the column existed, and it keeps its old
    answer rather than being absolved on a number nobody took.
    """

    @property
    def is_transcoded(self) -> bool:
        """Report whether this file was lossy before it reached its container."""
        return self.encoding == "transcoded"

    @property
    def answers_the_rules_in_force(self) -> bool:
        """Report whether this row carries every measurement today's rules read.

        **The one place that says what a complete row is**, because a survey
        skips a file whose row is complete and a rule added later is a rule that
        never reaches a library already measured. A row that lacks the frame
        grid reading but counts as complete is never swept, so the one
        measurement that reaches a 320 kbps transcode laundered into FLAC
        cannot run on it, and an album ends up with some tracks convicted by
        the grid and others reading lossless at the same wall.

        **A lossy container is complete without a grid.** The sweep is asked
        only of a file that promises to have lost nothing, so a `lossy` row has
        no column to be missing — demanding one would re-decode every lossy
        file on every pass to learn nothing.

        The cost is stated where it is paid: a lossless file the sweep cannot
        read — too short to hold one — has no grid honestly and reads here like
        a row from before the sweep, so it is measured again every time its
        album is scanned. The durable cure is a column recording which ruler
        wrote the row.

        **When a measurement is added, it is added here.** This property is the
        list, and nothing else may keep a second copy of it. The grid counts
        how many readings named the alignment rather than asking whether all
        six agreed: a row carrying only the flag answers the old question and
        cannot answer the new one, so it is incomplete until the file is swept
        again.
        """
        if self.decay_db is None or self.ceiling_db is None:
            return False
        # Asked as *could this row have carried one*, not as a list of the
        # encodings that do. Only a file whose container promised to
        # have lost nothing is swept, and `lossy` is the one word that says the
        # container did not — so every verdict that exists now or is added later
        # falls on the side that expects a reading.
        if self.encoding == "lossy":
            return True
        return self.frame_grid_z is not None and self.frame_grid_agreeing is not None

    @property
    def is_this_file(self) -> bool:
        """Report whether this measurement is provably of the file that asked.

        A row without a key may have been measured from a different recording
        that happens to share the signature, so a number read from it is never
        written into a filename.
        """
        return self.audio_key is not None


@dataclass(frozen=True, slots=True)
class AcousticAnswer:
    """What the fingerprint service said about one file, as it was persisted.

    ``asked`` false means this audio has a fingerprint and was never put to the
    service. ``asked`` true with no ``recording_id`` is the answer that used to
    be invisible: it was asked, and the service has never heard of this
    recording — an ordinary case for music outside the mainstream catalogues.

    ``is_this_audio`` is the same question ``StoredQuality.is_this_file``
    answers, one table over: a row written before the audio had a name may have
    been drawn from a different recording of the same declared shape, so the
    identity it carries is not proof about the file in hand.
    """

    recording_id: str | None
    score: float | None
    asked: bool
    is_this_audio: bool = False


@dataclass(frozen=True, slots=True)
class BenchRoot:
    """A folder the user put on the quality bench, and what it holds."""

    root_id: int
    path: Path
    walked_at: str | None
    albums: int


def word_for(
    words: Mapping[tuple[str, str | None], str],
    content_signature: str,
    audio_key: str | None,
) -> str | None:
    """Return the user's word about *this* file, out of the words about its album.

    The word written against this exact audio wins; the one written before the
    audio had a name answers otherwise, which is how every word given before
    migration 22 goes on working. Written once and shared, because the Library,
    the bench and the planner all ask it and three copies of this rule is three
    chances for two screens to disagree about what was said.
    """
    if audio_key is not None and (content_signature, audio_key) in words:
        return words[(content_signature, audio_key)]
    return words.get((content_signature, None))


@dataclass(frozen=True, slots=True)
class HeldCopy:
    """One place a piece of audio is held, as some walk of this library found it."""

    path: Path
    album: str
    audio_key: str | None
    content_signature: str
    where: str
    """Which register found it — `library` for a scan, `bench` for a walk."""

    album_path: Path | None = None
    album_signature: str = ""
    """The album's own content signature, which is what a decision about it is
    keyed by: organising renames the folder and filing moves it."""

    unit_id: int | None = None


@dataclass(frozen=True, slots=True)
class DuplicateAudio:
    """Purpose: name one recording this library holds more than once.

    Responsibilities: carry the copies and say what proves they are the same
    thing. Boundaries: it proposes nothing and deletes nothing — which copy to
    keep is the user's decision, and this application says what it measured
    and moves no file. Dependencies: ``HeldCopy``.
    Collaborators: the duplicate map. Constraints: ``proof`` is what was
    actually compared, because the two kinds are not the same claim — the same
    audio key is the same stream byte for byte, and the same acoustic print is
    the same performance possibly encoded twice.
    """

    proof: str
    """`audio` for an identical stream, `print` for an identical fingerprint."""

    identity: str
    copies: tuple[HeldCopy, ...]


@dataclass(frozen=True, slots=True)
class BenchFile:
    """One file of a bench album, as the last walk found it."""

    name: str
    content_signature: str
    audio_key: str | None = None


@dataclass(frozen=True, slots=True)
class BenchAlbum:
    """Purpose: hold what the bench remembers of one album, minus its verdict.

    Responsibilities: name the folder and carry the files that make it up.
    Boundaries: it states no verdict and no confidence — those are re-judged
    from the measurements on every read, because a stored verdict is never
    trusted across a rule change. Dependencies: ``BenchFile``.
    Collaborators: the bench workflow and the window. Constraints: the files are
    the album's membership as the last walk saw it, which is what a median of
    its tracks needs in order to be a statement about the album.
    """

    album_id: int
    root_id: int
    folder_path: Path
    unit_signature: str
    track_count: int
    files: tuple[BenchFile, ...]


def _as_integer(answer: bool | None) -> int | None:
    """Store a yes, a no, or the absence of the question — three states, not two.

    SQLite has no boolean, and `int(None)` raises rather than storing NULL, so
    the row that predates a question needs saying out loud.
    """
    return None if answer is None else int(answer)


def _preferred(rows: Sequence[StoredQuality], key: str | None) -> StoredQuality | None:
    """Return which of a signature's measurements answers for the file in hand.

    The order is the point. A row measured from *this* audio wins outright.
    Failing that, the legacy row answers, because a library measured before
    the key existed is made of those and nothing is taken away from it. Only
    when neither exists does another recording's row answer, and it does so
    rather than leaving the file unmeasured — every caller of this can see the
    key it got back and decide what the answer is worth.
    """
    if not rows:
        return None
    if key is not None:
        exact = next((row for row in rows if row.audio_key == key), None)
        if exact is not None:
            return exact
    legacy = next((row for row in rows if row.audio_key is None), None)
    if legacy is not None:
        return legacy
    # Rows arrive newest first, so this is the most recent thing measured of a
    # signature nobody has ever measured without a key.
    return rows[0]


def _both_spellings(path: Path | str) -> tuple[str, ...]:
    """Return the ways macOS writes one path, so a lookup by text finds it.

    An accented name reaches this application in two spellings: composed (NFC)
    from a directory listing, and decomposed (NFD) from a folder dragged onto
    the window. They are the same path to the file system — APFS compares
    normalization-insensitively, so both open the same folder — and two
    different strings to SQLite, which compares bytes.

    Without this, a folder with an accented name dragged back in after being
    moved misses its row in the lookup by path; the fallback by content
    signature finds the row and asks whether its folder is still on disk — it
    is, because APFS answers for either spelling — and the album is recorded a
    second time: two cards for one folder.

    Both spellings are offered rather than one being imposed on the database:
    what is stored stays exactly as the file system spelled it, which is what a
    file system that *does* distinguish the two would need.
    """
    text = str(path)
    composed = unicodedata.normalize("NFC", text)
    decomposed = unicodedata.normalize("NFD", text)
    return (text,) if composed == decomposed else tuple(dict.fromkeys((text, composed, decomposed)))


def _holes(values: Sequence[object]) -> str:
    """Return the `?` placeholders for a list of bound values."""
    return ",".join("?" * len(values))


def _catalogue_ever_answered_sql(unit: str) -> str:
    """Return the condition *some catalogue has answered about this album, ever*.

    One expression, handed to the two places that ask: the shelf drawn from
    columns and the album read back into memory. A card must not change its
    badge on being read back, which is the promise `_shelf_summary` makes in so
    many words — and two spellings of one condition is how that promise breaks
    without anything failing.

    ``unit`` is how the surrounding statement names the album: a bound `?`, or
    the outer query's own column. It is a literal written in this module and
    never a value from anywhere else.
    """
    return f"""EXISTS (
        SELECT 1 FROM identifications ident
          JOIN metadata_releases rel ON rel.id = ident.metadata_release_id
         WHERE ident.album_unit_id = {unit} AND rel.source != 'tags')"""


class LibraryStore:
    """Purpose: record the library's history so work is never repeated or lost.

    Responsibilities: upsert scanned units and files, cache source releases,
    record identifications, plans, operations, and their outcomes. Boundaries:
    it decides nothing — no matching, no threshold, no filesystem access — and
    it never deletes a row, because a rejected identification is evidence that
    the same wrong answer should not be proposed again. Dependencies:
    ``Database`` and the serializer. Collaborators: the application workflows.
    Constraints: an album is recognized by its content signature, so renaming it
    updates the existing row rather than creating a second one.
    """

    def __init__(self, database: Database, logger: logging.Logger) -> None:
        """Create a store over an already-initialized database."""
        self._database = database
        self._logger = logger

    # --- what is on disk --------------------------------------------------

    def record_unit(self, unit: AlbumUnit) -> int:
        """Insert or update one album unit and its files, and return its row id."""
        with self._database.connect() as connection:
            unit_id = self._upsert_unit(connection, unit)
            self._upsert_files(connection, unit, unit_id)
            return unit_id

    def record_units(self, units: Iterable[AlbumUnit]) -> dict[str, int]:
        """Record many units and return their row ids **by folder path**.

        By folder and not by signature. A signature is a fact about audio and a
        row is a fact about a folder, and the two stop being one the moment a
        second copy of an album is kept — which `_upsert_unit` allows on
        purpose, refusing to collapse two folders that both exist. Keyed by
        signature, the second folder read in a run would overwrite the first in
        this map, and both would answer with one row id.
        """
        recorded: dict[str, int] = {}
        for unit in units:
            recorded[str(unit.folder_path)] = self.record_unit(unit)
        return recorded

    def highest_unit_id(self) -> int:
        """Return the newest album row's id, or zero for a library with none.

        This is how *the shelf as it stands right now* is written down: ids are
        handed out in arrival order, so one number separates the albums that
        were already here from the ones that come next. Read once, at startup,
        before anything new can be recorded.
        """
        with self._database.connect() as connection:
            row = connection.execute("SELECT MAX(id) FROM album_units").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def unit_by_signature(self, unit_signature: str) -> StoredUnit | None:
        """Return what is known about an album, found by its audio content."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, unit_signature, folder_path, state, origin, rating FROM album_units "
                "WHERE unit_signature = ?",
                (unit_signature,),
            ).fetchone()
        return StoredUnit(row[0], row[1], Path(row[2]), row[3], row[4], row[5]) if row else None

    def files_were_written_for(self, unit_id: int) -> bool:
        """Whether a plan for this album has been applied and not reverted.

        A fact about the disk, which is what distinguishes *this album's files
        are where this application put them* from every flag that answers about
        the workflow instead. `album_units.state` is the workflow's word and it
        moves — a typed correction gives the album something to write and the
        row becomes `identified` again — so a guard about the files cannot read
        it.
        """
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM change_plans WHERE album_unit_id = ? AND state = 'applied' "
                "AND reverted_at IS NULL LIMIT 1",
                (unit_id,),
            ).fetchone()
        return row is not None

    def albums_holding_all_of(self, unit_id: int) -> tuple[StoredUnit, ...]:
        """Return the other albums on the shelf that hold every stream of this one.

        Byte for byte, by audio key, and over the files each album's folder holds
        now — a row naming a file by a name it no longer has is history, not a
        place (the reading `duplicate_audio` already makes). An album with a file
        whose audio has no key answers nothing: *every stream* cannot be claimed
        of a file nobody named.

        Not the signature, which is a shape many different recordings share.
        """
        with self._database.connect() as connection:
            folder_row = connection.execute(
                "SELECT folder_path FROM album_units WHERE id = ?", (unit_id,)
            ).fetchone()
            if folder_row is None:
                return ()
            folder = str(folder_row[0])
            keys = connection.execute(
                "SELECT audio_key FROM audio_files WHERE album_unit_id = ? "
                "AND substr(path, 1, length(?) + 1) = ? || '/'",
                (unit_id, folder, folder),
            ).fetchall()
            wanted = {key for (key,) in keys}
            if not wanted or None in wanted:
                return ()
            rows = connection.execute(
                "SELECT u.id, u.unit_signature, u.folder_path, u.state, u.origin, u.rating "
                "FROM album_units u JOIN audio_files f ON f.album_unit_id = u.id "
                "WHERE u.cleared_at IS NULL AND u.id != ? "
                "AND substr(f.path, 1, length(u.folder_path) + 1) = u.folder_path || '/' "
                f"AND f.audio_key IN ({_holes(tuple(wanted))}) "
                "GROUP BY u.id HAVING COUNT(DISTINCT f.audio_key) = ? ORDER BY u.id",
                (unit_id, *wanted, len(wanted)),
            ).fetchall()
        return tuple(
            StoredUnit(row[0], row[1], Path(row[2]), row[3], row[4], row[5]) for row in rows
        )

    def units_by_signature(self, unit_signature: str) -> tuple[StoredUnit, ...]:
        """Return every album row carrying this audio, oldest first.

        `unit_by_signature` answers with one, which is what a scan needs. Two
        rows can legitimately hold one signature — two copies of an album in two
        folders, which `_upsert_unit` keeps apart on purpose — and the question
        *where else is this audio* has no answer while only the first row is
        readable.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, unit_signature, folder_path, state, origin, rating FROM album_units "
                "WHERE unit_signature = ? ORDER BY id",
                (unit_signature,),
            ).fetchall()
        return tuple(
            StoredUnit(row[0], row[1], Path(row[2]), row[3], row[4], row[5]) for row in rows
        )

    def unit_by_id(self, unit_id: int) -> StoredUnit | None:
        """Return what is known about one album, found by its row.

        The shelf hands the window a unit id and nothing else, so opening a
        card needs a way back to the row it came from.
        """
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, unit_signature, folder_path, state, origin, rating FROM album_units "
                "WHERE id = ?",
                (unit_id,),
            ).fetchone()
        return StoredUnit(row[0], row[1], Path(row[2]), row[3], row[4], row[5]) if row else None

    # --- what the Library holds between sessions ---------------------------

    def library_units(self) -> tuple[StoredUnit, ...]:
        """Return every album the Library still shows, oldest row first.

        Cleared albums are left out: the ✕ says the album has been dealt with,
        and that survives a restart.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, unit_signature, folder_path, state, origin, rating FROM album_units "
                "WHERE cleared_at IS NULL ORDER BY id"
            ).fetchall()
        return tuple(
            StoredUnit(row[0], row[1], Path(row[2]), row[3], row[4], row[5]) for row in rows
        )

    def audio_sizes(self, unit_id: int, folder: Path) -> tuple[int, ...]:
        """Return what this album weighed the last time it was seen, sorted.

        How an album whose folder was renamed is found again: sizes come free
        with a directory listing, so a folder holding exactly these can be
        offered to the signature without a byte of audio being read.

        **Only the files recorded under the folder the row names.** An album that
        has lived in more than one place keeps a row per path it was seen at,
        and weighing all of them would describe an album twice the size of any
        that exists.

        **Anywhere under it, not only directly in it.** A two-disc album keeps
        every file one level down, in `CD1` and `CD2`, so asking for the rows
        whose *parent* is the album folder answers nothing at all for it. The
        prefix carries its separator, or `…/Name` would take `…/Name Longer`
        with it.

        **And only the rows of the last sighting.** A row is a path a file was
        once seen at, on purpose — so a renamed track leaves the row it had, and
        adding both would describe an album twice its own size.
        `_upsert_files` stamps every row of one walk with one `last_seen_at`
        for this, so *the last sighting* is a value and not an estimate.

        The worst this can do is answer with part of an album, which finds
        nothing — never a different album, because the folder that fits is still
        read and kept only when its signature is this album's.
        """
        prefixes = tuple(
            str(Path(spelling)).rstrip("/") + "/" for spelling in _both_spellings(folder)
        )
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT path, file_size_bytes, last_seen_at FROM audio_files "
                "WHERE album_unit_id = ? AND file_size_bytes IS NOT NULL",
                (unit_id,),
            ).fetchall()
        inside = [row for row in rows if str(Path(row[0])).startswith(prefixes)]
        if not inside:
            return ()
        latest = max(row[2] or "" for row in inside)
        return tuple(sorted(int(row[1]) for row in inside if (row[2] or "") == latest))

    def record_audio_sizes(self, weighed: Mapping[Path, tuple[int, datetime]]) -> int:
        """Write down what these files weigh now, and return how many rows took it.

        The other half of ``audio_sizes``: if only a scan wrote
        `file_size_bytes`, an apply that embedded a cover would leave every row
        describing the bytes the file had *before* it ran.

        **A row is its path.** Whatever file sits at that path is the file the
        row stands for, so recording what it weighs is true by construction —
        which is why this updates and never inserts. A path with no row is a
        file this application has not registered under that name, and inventing
        a row for it here would be a second, blinder ``_upsert_files``.

        `content_signature` is not touched and cannot need to be: it is the
        digest of the *stream's* declared properties, and reaches for the size
        only when the stream cannot be parsed at all — a value only
        ``scanner.py`` ever computes, from a size it has just read itself.

        **`last_seen_at` is not touched either, and that is load-bearing.** This
        corrects a sighting rather than making one, and `audio_sizes` weighs the
        rows of the *latest* sighting as a group — so stamping the
        files this happened to reach would split an album down the middle the
        moment one of them could not be read.
        """
        if not weighed:
            return 0
        moved = 0
        with self._database.connect(write=True) as connection:
            for path, (size, changed_at) in weighed.items():
                spellings = _both_spellings(path)
                cursor = connection.execute(
                    "UPDATE audio_files SET file_size_bytes = ?, modified_at = ? "
                    f"WHERE path IN ({_holes(spellings)})",
                    (size, changed_at.isoformat(), *spellings),
                )
                moved += int(cursor.rowcount or 0)
        return moved

    def shelf(self) -> tuple[dict[str, object], ...]:
        """Return what every card needs, from columns, without reading one file.

        The shelf as the database already holds it: `metadata_releases` keeps
        the title, the credit and the date in columns of their own, so drawing
        the Library asks for no JSON payload, no folder walk and no matcher.
        Reading every album back is orders of magnitude slower, and it is work
        the first sight of the window does not need.

        Oldest row first, like `library_units`, because the window reverses it.
        """
        with self._database.connect() as connection:
            rows = connection.execute(f"""
                SELECT u.id, u.folder_path, u.state, u.track_count, u.origin, u.rating,
                       r.title, r.primary_artist,
                       -- The album's own year first, exactly as the settled card
                       -- reads it: a shelf whose year changes when the
                       -- album is read back is a shelf that was lying in one of
                       -- the two states.
                       COALESCE(r.original_released_on, r.released_on), r.source,
                       i.confidence,
                       -- Whether a catalogue ever answered, which is not what
                       -- the join above says: the join is this album's standing
                       -- answer, and an arrangement from its own tags is one.
                       {_catalogue_ever_answered_sql("u.id")}
                FROM album_units u
                LEFT JOIN identifications i
                       ON i.id = (
                           SELECT MAX(id) FROM identifications
                           WHERE album_unit_id = u.id AND state != 'superseded'
                       )
                LEFT JOIN metadata_releases r ON r.id = i.metadata_release_id
                WHERE u.cleared_at IS NULL
                ORDER BY u.id
                """).fetchall()
        return tuple(
            {
                "unit_id": row[0],
                "folder_path": row[1],
                "state": row[2],
                "tracks": row[3],
                "origin": row[4],
                # The user's rating, read here so the first sight of the shelf
                # already carries it.
                "rating": row[5],
                "title": row[6],
                "artist": row[7],
                # Only the year reaches a card, and `released_on` is a date
                # string; parsing it here keeps the window from learning what
                # shape this column has.
                "year": int(row[8][:4]) if row[8] and row[8][:4].isdigit() else None,
                "source": row[9],
                "confidence": row[10],
                "catalogue_asked": bool(row[11]),
            }
            for row in rows
        )

    def known_parents(self) -> tuple[Path, ...]:
        """Return the folders albums have been found in, most recently seen first.

        Where this app should look for an album that is no longer where it was
        recorded. It is not a guess about the disk: every one of these is a
        folder that has actually held a recorded album, which is what makes
        looking there cheap and honest.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT folder_path FROM album_units ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        seen: dict[Path, None] = {}
        for row in rows:
            seen.setdefault(Path(row[0]).parent, None)
        return tuple(seen)

    def units_by_folder(self) -> dict[Path, int]:
        """Return every recorded folder and the album row that holds it.

        Every row, cleared ones included: `folder_path` is UNIQUE over the whole
        table, so a folder still named by an album that was let go is a folder
        no other row can be moved onto.
        """
        with self._database.connect() as connection:
            rows = connection.execute("SELECT id, folder_path FROM album_units").fetchall()
        return {Path(folder): int(unit_id) for unit_id, folder in rows}

    def catalogue_answered_for(self, unit_id: int) -> bool:
        """Whether any catalogue has ever answered about this album.

        A different question from ``identification_for``, which returns what the
        album *is* right now: this one is about its history, so a look-up that
        was superseded, rejected or set aside still counts. An album arranged
        from its own tags after a scan has been asked about; one dropped on the
        window and arranged has not, however many rows its arrangement wrote.

        Written as ``source != 'tags'`` rather than as a list of the catalogues
        this build knows, so a source added later is a catalogue by default.
        The one source that is not a catalogue is the files' own tags, and it
        is the one that names itself.
        """
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_catalogue_ever_answered_sql('?')}", (unit_id,)
            ).fetchone()
        return bool(row[0])

    def identification_for(self, unit_id: int) -> dict[str, object] | None:
        """Return the identification standing for one album, newest first.

        Superseded attempts are kept for the record but are not what the album
        *is*: restoring the Library reads this to rebuild an outcome from the
        cached release rather than asking a source again.
        """
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, metadata_release_id, confidence, explanation, state, method,
                       verification
                FROM identifications
                WHERE album_unit_id = ? AND state != 'superseded'
                  AND metadata_release_id IS NOT NULL
                ORDER BY id DESC LIMIT 1
                """,
                (unit_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "identification_id": int(row[0]),
            "release_row": int(row[1]),
            "confidence": float(row[2]),
            "explanation": row[3],
            "state": row[4],
            # How the album was found. `acoustic` is what the fingerprint wrote,
            # and without reading it back a restart turns an identification the
            # audio made into one indistinguishable from a text search.
            "method": row[5],
            # What the catalogues and the witness said. Left behind, a restored
            # album reports "not asked" about the very source it is using.
            "verification": json.loads(row[6]) if row[6] else None,
        }

    def relocate_unit(self, was: str, now: str) -> int:
        """Follow an album to the folder it now lives in, in every table that names it.

        The same failure as ``relocate_download``, one table over. The Library
        restores itself from these rows, so a stale path is read while it is
        wrong rather than corrected by whatever scan comes next.

        **The bench moves with it.** `quality_bench_albums` keeps a folder path
        too. Organising an album renames its folder and filing it away moves it
        again, so a bench row that is not carried leaves the Quality screen
        pointing at a name that no longer exists.

        **And the files inside it.** `audio_files` keeps a whole path, so every
        registered file of a moved album would name a file that is no longer
        there. That is not a stale row nobody reads: a file registered twice,
        under the name it had and the name it has, is two differently-named rows
        over one content signature, which is exactly what
        `ambiguous_signatures` is looking for — so the library would withhold
        measurements from itself.

        **One folder is one row, even when two rows claim it.** `folder_path` is
        UNIQUE, and offering both spellings of an accent can match two rows at
        once: a library may hold a composed row and a decomposed one for the
        same folder. Moving them together puts two rows on one path and raises
        `UNIQUE constraint failed: album_units.folder_path`, which ends the
        whole sweep — no album is followed and none is let go. So exactly one
        row moves: the one the caller named, and failing that the one still on
        the shelf.

        Done here rather than at each caller because this is the one place every
        path that follows a folder already comes through, and a table added
        later must not be able to be forgotten by one of them.
        """
        spellings = _both_spellings(was)
        holes = _holes(spellings)
        with self._database.connect(write=True) as connection:
            unit = connection.execute(
                f"SELECT id FROM album_units WHERE folder_path IN ({holes}) "
                # The row spelled the way the caller spelled it is the one being
                # talked about; when nothing is spelled that way, a row still on
                # the shelf is worth more than one already cleared.
                "ORDER BY (folder_path = ?) DESC, (cleared_at IS NULL) DESC, id LIMIT 1",
                (*spellings, was),
            ).fetchone()
            cursor = connection.execute(
                # `OR IGNORE` because another album may already be recorded at
                # the destination; the row that is there describes the folder
                # that is there, and this one is left where it stands rather
                # than an exception ending the sweep it was part of.
                "UPDATE OR IGNORE album_units SET folder_path = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (now, unit[0] if unit else None),
            )
            if unit is not None and not cursor.rowcount:
                # The `OR IGNORE` above is right, and it must not be silent. An
                # album that cannot take the name its own apply just gave the
                # folder keeps a path that no longer exists, and an album whose
                # folder is gone is taken off the shelf — it would disappear
                # with nothing anywhere saying why.
                self._say_a_row_could_not_move("album_units", was, now, connection)
            connection.execute(
                "UPDATE OR IGNORE quality_bench_albums SET folder_path = ? "
                f"WHERE folder_path IN ({holes})",
                (now, *spellings),
            )
            # And the root, because pointing the bench at one album's own folder
            # is an ordinary gesture. Moving the album row and leaving the root
            # behind would only trade `Not a folder` for a re-walk that finds
            # nothing.
            connection.execute(
                f"UPDATE OR IGNORE quality_bench_roots SET path = ? WHERE path IN ({holes})",
                (now, *spellings),
            )
            self._move_audio_paths(connection, spellings, now, inside=True)
            return int(cursor.rowcount or 0)

    def relocate_file(self, was: str, now: str) -> int:
        """Follow one registered file to the name it now carries.

        The other half of ``relocate_unit``, and it is needed because an apply
        renames the files *before* it renames the folder: an organized album is
        one `rename_folder` and one `rename_file` per track, and following the
        folder alone would leave every file row naming a file that no longer
        exists.

        Returns how many rows moved, which is 0 when the destination is already
        registered — a scan may have reached the new name first, and its row is
        the one that describes the file that is there.
        """
        with self._database.connect() as connection:
            return self._move_audio_paths(connection, _both_spellings(was), now, inside=False)

    def _move_audio_paths(
        self,
        connection: sqlite3.Connection,
        spellings: Sequence[str],
        now: str,
        *,
        inside: bool,
    ) -> int:
        """Move the registered path of one file, or of everything under a folder.

        `OR IGNORE` because `path` is unique and the destination may already be
        registered — the row that names the file that is actually there stays,
        and the one left behind is a historical row, not a correction being
        dropped.

        **What it must not be is quiet about it.** A row the `OR IGNORE`
        declines to move keeps a name that no longer exists, so the very act of
        following the album is what makes that row dead; the file itself is
        never lost, because the row already holding the destination is the one
        describing it, but the refusal has to be counted and logged.

        The prefix carries its separator: without it, moving `…/Name` would
        also take `…/Name Longer` along. One statement per spelling because
        SQLite counts characters, and NFC and NFD do not have the same number
        of them.
        """
        moved = 0
        for spelling in spellings:
            prefix = spelling.rstrip("/") + "/" if inside else spelling
            destination = now.rstrip("/") + "/" if inside else now
            cursor = connection.execute(
                "UPDATE OR IGNORE audio_files SET path = ? || substr(path, ?) "
                "WHERE substr(path, 1, ?) = ?",
                (destination, len(prefix) + 1, len(prefix), prefix),
            )
            moved += int(cursor.rowcount or 0)
            if destination == prefix:
                # A move to where it already is moves nothing and leaves
                # everything answering to the old name, which is not a refusal.
                continue
            # Whatever still answers to the old name after the statement that
            # was meant to take it away is a row the destination refused.
            left = connection.execute(
                "SELECT path FROM audio_files WHERE substr(path, 1, ?) = ?",
                (len(prefix), prefix),
            ).fetchall()
            if left:
                self._logger.warning(
                    "Registered files could not follow their album: the names they would "
                    "take are already registered, so these rows now name files that are "
                    "not there.",
                    extra={
                        "operation": "database.relocate.declined",
                        "table": "audio_files",
                        "rows": len(left),
                        "first": left[0][0],
                        "destination": now,
                    },
                )
        return moved

    def _say_a_row_could_not_move(
        self, table: str, was: str, now: str, connection: sqlite3.Connection
    ) -> None:
        """Report a move the destination refused, naming who is already there.

        One place, because the two tables that follow a folder fail the same way
        and a second copy of this sentence would be a second chance to forget it.
        """
        occupant = connection.execute(
            "SELECT id FROM album_units WHERE folder_path = ?", (now,)
        ).fetchone()
        self._logger.warning(
            "An album could not follow its folder: that path is already recorded.",
            extra={
                "operation": "database.relocate.declined",
                "table": table,
                "was": was,
                "destination": now,
                "occupied_by": occupant[0] if occupant else None,
            },
        )

    def clear_unit(self, unit_id: int) -> None:
        """Take one album off the Library screen, for good, touching no file."""
        with self._database.connect(write=True) as connection:
            connection.execute(
                "UPDATE album_units SET cleared_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (unit_id,),
            )

    def set_unit_origin(self, unit_id: int, origin: str) -> None:
        """Record what brought this album in — `scanned` or `downloaded`.

        A collected download is read off the disk by the ordinary scan, so it
        arrives recorded as scanned and is promoted here once the caller knows
        which it was. Origin is how an album first came in, so it is only ever
        set to `downloaded`, never back.
        """
        with self._database.connect(write=True) as connection:
            connection.execute(
                "UPDATE album_units SET origin = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (origin, unit_id),
            )

    # --- measured quality --------------------------------------------------

    def record_harmonics(self, measurements: Iterable[StoredHarmonics]) -> None:
        """Remember what each recording is in, keyed by the audio it came from.

        **`audio_key`, never `content_signature`**: the signature identifies a
        shape, and different songs of the same length and format share one. A
        key stored against the signature would be shown on a recording it was
        never measured from.
        """
        rows = [
            (
                item.audio_key,
                item.content_signature,
                item.tonic,
                item.mode,
                item.key_confidence,
                item.bpm,
                item.bpm_confidence,
                item.bpm_alternative,
                item.key_margin,
                item.runner_up_tonic,
                item.runner_up_mode,
            )
            for item in measurements
            if item.audio_key
        ]
        if not rows:
            return
        with self._database.connect(write=True) as connection:
            connection.executemany(
                "INSERT INTO track_harmonics "
                "(audio_key, content_signature, tonic, mode, key_confidence, bpm, "
                " bpm_confidence, bpm_alternative, key_margin, runner_up_tonic, "
                " runner_up_mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(audio_key) DO UPDATE SET "
                "  tonic = excluded.tonic, "
                "  mode = excluded.mode, "
                "  key_confidence = excluded.key_confidence, "
                "  bpm = excluded.bpm, "
                "  bpm_confidence = excluded.bpm_confidence, "
                "  bpm_alternative = excluded.bpm_alternative, "
                "  key_margin = excluded.key_margin, "
                "  runner_up_tonic = excluded.runner_up_tonic, "
                "  runner_up_mode = excluded.runner_up_mode, "
                "  measured_at = CURRENT_TIMESTAMP",
                rows,
            )

    def harmonics_for(self, audio_keys: Iterable[str]) -> dict[str, StoredHarmonics]:
        """Return what is already known about these recordings, by audio key."""
        wanted = [key for key in dict.fromkeys(audio_keys) if key]
        if not wanted:
            return {}
        found: dict[str, StoredHarmonics] = {}
        with self._database.connect() as connection:
            # Chunked for the reason `_quality_rows` is: SQLite caps bound
            # parameters, and a bench holding a library asks about thousands.
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                for row in connection.execute(
                    "SELECT audio_key, content_signature, tonic, mode, key_confidence, bpm, "
                    "       bpm_confidence, bpm_alternative, key_margin, runner_up_tonic, "
                    "       runner_up_mode "
                    f"FROM track_harmonics WHERE audio_key IN ({placeholders})",
                    chunk,
                ):
                    found[row[0]] = StoredHarmonics(
                        audio_key=row[0],
                        content_signature=row[1],
                        tonic=row[2],
                        mode=row[3],
                        key_confidence=row[4],
                        bpm=row[5],
                        bpm_confidence=row[6],
                        bpm_alternative=row[7],
                        key_margin=row[8],
                        runner_up_tonic=row[9],
                        runner_up_mode=row[10],
                    )
        return found

    def record_quality(self, measurements: Mapping[str, StoredQuality]) -> None:
        """Remember what each file measured, keyed by its content signature.

        Measuring a library costs minutes of decoding, so it is done once. The
        signature survives a retag and a rename, which is what lets a
        measurement outlive the organizer renaming the folder it describes.

        **A mapping holds one row per signature, and that is a ceiling.** Two
        recordings of one track share every declared thing a signature is made
        of, so a caller with both in hand silently loses one of them on the way
        in. Where a caller may hold two, it uses ``record_measurements`` and
        passes the pairs.
        """
        self.record_measurements(measurements.items())

    def record_measurements(self, measurements: Iterable[tuple[str, StoredQuality]] = ()) -> None:
        """Remember these measurements, one row for each, keyed by signature and audio.

        The pair is the unique index migration 13 declares, so two recordings of
        one shape are two rows and neither overwrites the other. Written as an
        iterable of pairs rather than a mapping precisely so that nothing is
        dropped between the caller and the statement.
        """
        measurements = list(measurements)
        if not measurements:
            return
        with self._database.connect(write=True) as connection:
            connection.executemany(
                "INSERT INTO track_quality "
                "(content_signature, audio_key, encoding, effective_bitrate_kbps, cutoff_hertz, "
                " wall_low_hertz, steepest_drop_db, findings, reason, decay_db, ceiling_db, "
                " floor_hertz, frame_grid_z, frame_grid_agrees, frame_grid_agreeing) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                # The conflict target is spelled exactly as the unique index of
                # migration 13, because that is how SQLite matches one: a row
                # carrying an audio key never overwrites the legacy row of the
                # same signature, and never overwrites another recording's.
                "ON CONFLICT(content_signature, IFNULL(audio_key, '')) DO UPDATE SET "
                "  encoding = excluded.encoding, "
                "  effective_bitrate_kbps = excluded.effective_bitrate_kbps, "
                "  cutoff_hertz = excluded.cutoff_hertz, "
                "  wall_low_hertz = excluded.wall_low_hertz, "
                "  steepest_drop_db = excluded.steepest_drop_db, "
                "  findings = excluded.findings, "
                "  reason = excluded.reason, "
                "  decay_db = excluded.decay_db, "
                "  ceiling_db = excluded.ceiling_db, "
                "  floor_hertz = excluded.floor_hertz, "
                "  frame_grid_z = excluded.frame_grid_z, "
                "  frame_grid_agrees = excluded.frame_grid_agrees, "
                "  frame_grid_agreeing = excluded.frame_grid_agreeing, "
                "  measured_at = CURRENT_TIMESTAMP",
                [
                    (
                        signature,
                        quality.audio_key,
                        quality.encoding,
                        quality.effective_bitrate_kbps,
                        quality.cutoff_hertz,
                        quality.wall_low_hertz,
                        quality.steepest_drop_db,
                        ",".join(quality.findings),
                        quality.reason,
                        quality.decay_db,
                        quality.ceiling_db,
                        quality.floor_hertz,
                        quality.frame_grid_z,
                        _as_integer(quality.frame_grid_agrees),
                        quality.frame_grid_agreeing,
                    )
                    for signature, quality in measurements
                ],
            )
        self._logger.info(
            "Measured quality recorded.",
            extra={"operation": "quality.record", "files": len(measurements)},
        )

    def fingerprint_for(
        self, content_signature: str, audio_key: str | None = None
    ) -> AudioFingerprint | None:
        """Return the fingerprint computed from *this* audio, if any.

        A caller that can name its audio is answered only by a row that names
        the same audio. The row left over from before — `audio_key` NULL — is
        never handed to it, because a fingerprint is what becomes an
        identification, and one signature can cover two different recordings.
        Recomputing costs a fraction of a second and no request; being wrong
        costs an album wearing another album's name.

        Asked without a key, the NULL row answers.
        """
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT fingerprint, duration_seconds, algorithm FROM acoustic_prints "
                "WHERE content_signature = ? AND audio_key IS ?",
                (content_signature, audio_key),
            ).fetchone()
        if row is None:
            return None
        return AudioFingerprint(
            fingerprint=row[0],
            duration_seconds=int(row[1]),
            algorithm=row[2],
        )

    def remember_lookup(
        self,
        content_signature: str,
        audio_key: str | None,
        recording_id: str | None,
        score: float | None,
    ) -> None:
        """Record what the service answered about this audio, including nothing.

        Keeping the fingerprint and discarding every answer drawn from it would
        leave no screen able to say what the acoustic engine has done.
        `looked_up_at` is what tells "never asked" from "asked, and it has
        never heard of this recording", and the second is an ordinary answer.

        Written against the audio the print was drawn from, so an answer never
        lands on the row of another recording that shares the shape.
        """
        with self._database.connect(write=True) as connection:
            connection.execute(
                "UPDATE acoustic_prints SET external_recording_id = ?, "
                "  recording_score = ?, looked_up_at = CURRENT_TIMESTAMP "
                "WHERE content_signature = ? AND audio_key IS ?",
                (recording_id, score, content_signature, audio_key),
            )

    def acoustic_answers(
        self, files: Iterable[tuple[str, str | None]]
    ) -> dict[tuple[str, str | None], AcousticAnswer]:
        """Return what the audio answered for each of these files, where it was asked.

        Asked *and answered* by the pair the caller has in hand — the shape of
        the file and the identity of its audio — because those are two different
        questions and only the second one is proof. Keyed by the pair for the
        same reason: two files of one album can share a signature, and an answer
        filed under the signature alone would hand both of them one identity,
        which is the very defect this is here to end.

        A row that names the same audio is this file's answer; the legacy row
        answers for a file whose audio cannot be named, and says so through
        ``is_this_audio``.
        """
        wanted = list(dict.fromkeys(files))
        found: dict[tuple[str, str | None], AcousticAnswer] = {}
        if not wanted:
            return found
        with self._database.connect() as connection:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                signatures = list(dict.fromkeys(signature for signature, _ in chunk))
                placeholders = ",".join("?" * len(signatures))
                rows = connection.execute(
                    "SELECT content_signature, audio_key, external_recording_id, "
                    "       recording_score, looked_up_at FROM acoustic_prints "
                    f"WHERE content_signature IN ({placeholders})",
                    signatures,
                ).fetchall()
                answers = {(row[0], row[1]): row[2:] for row in rows}
                for signature, key in chunk:
                    named = answers.get((signature, key)) if key is not None else None
                    row = named if named is not None else answers.get((signature, None))
                    if row is None:
                        continue
                    recording, score, asked_at = row
                    found[(signature, key)] = AcousticAnswer(
                        recording_id=recording,
                        score=float(score) if score is not None else None,
                        asked=asked_at is not None,
                        is_this_audio=named is not None,
                    )
        return found

    def remember_fingerprint(
        self, content_signature: str, audio_key: str | None, value: AudioFingerprint
    ) -> None:
        """Store a fingerprint against the audio it was computed from.

        Keyed by the signature and not by the file, so organizing — which
        renames every file of an album — does not discard the most expensive
        operation this application performs; and by the audio as well as
        the shape (migration 19), so one recording's print never overwrites
        another's.

        **The identical print claims the legacy row.** A row from before the
        audio had a name is not thrown away and not left orphaned: when the
        fingerprint computed from a named audio comes back byte for byte the
        same, that row was always about this audio, and it is adopted along with
        the lookup it spent a request on. That is the judge — the print settles
        what the shape could not.
        """
        with self._database.connect(write=True) as connection:
            if audio_key is not None:
                claimed = connection.execute(
                    # `OR IGNORE` because this audio may already have a row of
                    # its own; the insert below then refreshes that one.
                    "UPDATE OR IGNORE acoustic_prints SET audio_key = ? "
                    "WHERE content_signature = ? AND audio_key IS NULL AND fingerprint = ?",
                    (audio_key, content_signature, value.fingerprint),
                )
                if claimed.rowcount:
                    return
            connection.execute(
                "INSERT INTO acoustic_prints "
                "(content_signature, audio_key, algorithm, fingerprint, duration_seconds) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(content_signature, IFNULL(audio_key, '')) DO UPDATE SET "
                "  algorithm = excluded.algorithm, "
                "  fingerprint = excluded.fingerprint, "
                "  duration_seconds = excluded.duration_seconds, "
                "  computed_at = CURRENT_TIMESTAMP",
                (
                    content_signature,
                    audio_key,
                    value.algorithm,
                    value.fingerprint,
                    value.duration_seconds,
                ),
            )

    def quality_for(self, signatures: Iterable[str]) -> dict[str, StoredQuality]:
        """Return every measurement already known for these files.

        Asked without an audio key, so it answers with the row that speaks for
        the whole signature. Where only keyed rows exist, the most recent one
        answers rather than nothing: a file measured in depth and never
        measured before still has a verdict, and it is the one that was taken
        from its own audio. Callers that hold the file itself should ask
        ``quality_for_files``, which can tell the recordings apart.
        """
        found: dict[str, StoredQuality] = {}
        for signature, rows in self._quality_rows(signatures).items():
            chosen = _preferred(rows, key=None)
            if chosen is not None:
                found[signature] = chosen
        return found

    def quality_for_files(
        self, files: Iterable[tuple[str, str | None]]
    ) -> dict[tuple[str, str | None], StoredQuality]:
        """Return each file's measurement, told apart by the audio it holds.

        ``files`` pairs a content signature with that file's audio key, and the
        answer is keyed by that same pair. The row measured from this very
        audio wins; without one, the signature's legacy row answers. This is
        the reading that stops a file from wearing a verdict measured from
        another recording of the same length.

        **Keyed by the pair, not by the signature.** An answer indexed by the
        signature alone undoes the method in its last line: two files of one
        signature and different audio write into one slot, the last pair
        processed wins, and both files then read it.
        """
        pairs = list(dict.fromkeys(files))
        by_signature = self._quality_rows(signature for signature, _ in pairs)
        found: dict[tuple[str, str | None], StoredQuality] = {}
        for signature, key in pairs:
            chosen = _preferred(by_signature.get(signature, ()), key=key)
            if chosen is not None:
                found[signature, key] = chosen
        return found

    def keyed_signatures(self, signatures: Iterable[str]) -> set[str]:
        """Return which of these signatures have a measurement of a named audio.

        The bench asks this first so it computes an audio key only for the files
        where one could change the answer. Every other file costs a database
        lookup and no read at all.
        """
        wanted = list(dict.fromkeys(signatures))
        if not wanted:
            return set()
        found: set[str] = set()
        with self._database.connect() as connection:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = connection.execute(
                    "SELECT DISTINCT content_signature FROM track_quality "
                    f"WHERE audio_key IS NOT NULL AND content_signature IN ({placeholders})",
                    chunk,
                ).fetchall()
                found.update(row[0] for row in rows)
        return found

    def _quality_rows(self, signatures: Iterable[str]) -> dict[str, list[StoredQuality]]:
        """Return every stored measurement for these signatures, newest first."""
        wanted = list(dict.fromkeys(signatures))
        if not wanted:
            return {}
        found: dict[str, list[StoredQuality]] = {}
        with self._database.connect() as connection:
            # Chunked because SQLite caps the number of bound parameters, and a
            # library-sized scan asks about tens of thousands of files at once.
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = connection.execute(
                    "SELECT content_signature, encoding, effective_bitrate_kbps, cutoff_hertz, "
                    "       steepest_drop_db, findings, reason, decay_db, ceiling_db, audio_key, "
                    "       wall_low_hertz, floor_hertz, frame_grid_z, frame_grid_agrees, "
                    "       frame_grid_agreeing "
                    f"FROM track_quality WHERE content_signature IN ({placeholders}) "
                    "ORDER BY measured_at DESC, id DESC",
                    chunk,
                ).fetchall()
                for row in rows:
                    found.setdefault(row[0], []).append(
                        StoredQuality(
                            encoding=row[1],
                            effective_bitrate_kbps=row[2],
                            cutoff_hertz=row[3],
                            steepest_drop_db=row[4],
                            findings=tuple(name for name in (row[5] or "").split(",") if name),
                            reason=row[6] or "",
                            decay_db=row[7],
                            ceiling_db=row[8],
                            audio_key=row[9],
                            wall_low_hertz=row[10],
                            floor_hertz=row[11],
                            frame_grid_z=row[12],
                            frame_grid_agrees=None if row[13] is None else bool(row[13]),
                            frame_grid_agreeing=row[14],
                        )
                    )
        return found

    # --- the quality bench -------------------------------------------------

    def add_bench_root(self, path: Path) -> int:
        """Put a folder on the bench, or return the one already there."""
        with self._database.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO quality_bench_roots (path) VALUES (?) " "ON CONFLICT(path) DO NOTHING",
                (str(path),),
            )
            row = connection.execute(
                "SELECT id FROM quality_bench_roots WHERE path = ?", (str(path),)
            ).fetchone()
        return int(row[0])

    def bench_roots(self) -> tuple[BenchRoot, ...]:
        """Return every folder on the bench, oldest first."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT root.id, root.path, root.walked_at, "
                "       (SELECT COUNT(*) FROM quality_bench_albums AS album "
                "        WHERE album.root_id = root.id) "
                "FROM quality_bench_roots AS root ORDER BY root.id"
            ).fetchall()
        return tuple(
            BenchRoot(root_id=row[0], path=Path(row[1]), walked_at=row[2], albums=int(row[3]))
            for row in rows
        )

    def remove_bench_root(self, root_id: int) -> None:
        """Take a folder off the bench. No measurement and no file is touched.

        The albums and their membership go with it, by cascade. What was
        measured stays in `track_quality`, keyed by audio and not by any of
        this, so putting the folder back costs a walk and no decoding.
        """
        with self._database.connect(write=True) as connection:
            connection.execute("DELETE FROM quality_bench_roots WHERE id = ?", (root_id,))

    def record_bench_albums(self, root_id: int, units: Sequence[AlbumUnit]) -> None:
        """Replace what this root holds with what the walk just found.

        Replace rather than merge, because the walk is the answer to *what is
        under this folder now*: an album moved or deleted has to leave the
        bench, and one added has to arrive. The albums that stayed keep their
        row, so a remembered audio key is not thrown away by a re-walk.
        """
        wanted = {str(unit.folder_path): unit for unit in units}
        with self._database.connect(write=True) as connection:
            existing = {
                row[1]: int(row[0])
                for row in connection.execute(
                    "SELECT id, folder_path FROM quality_bench_albums WHERE root_id = ?",
                    (root_id,),
                ).fetchall()
            }
            gone = [album_id for path, album_id in existing.items() if path not in wanted]
            for start in range(0, len(gone), 500):
                chunk = gone[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                connection.execute(
                    f"DELETE FROM quality_bench_albums WHERE id IN ({placeholders})", chunk
                )
            connection.executemany(
                "INSERT INTO quality_bench_albums "
                "(root_id, folder_path, unit_signature, track_count) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(root_id, folder_path) DO UPDATE SET "
                "  unit_signature = excluded.unit_signature, "
                "  track_count = excluded.track_count, "
                "  seen_at = CURRENT_TIMESTAMP",
                [
                    (root_id, path, unit.unit_signature, unit.track_count)
                    for path, unit in wanted.items()
                ],
            )
            ids = {
                row[1]: int(row[0])
                for row in connection.execute(
                    "SELECT id, folder_path FROM quality_bench_albums WHERE root_id = ?",
                    (root_id,),
                ).fetchall()
            }
            connection.executemany(
                "INSERT INTO quality_bench_files "
                "(album_id, file_name, content_signature, audio_key) "
                "VALUES (?, ?, ?, ?) "
                # The name is the identity inside an album, and the signature is
                # refreshed rather than left: a file replaced in place is the
                # same slot holding different audio.
                "ON CONFLICT(album_id, file_name) DO UPDATE SET "
                "  content_signature = excluded.content_signature, "
                # The walk names the audio as it goes (migration 20), so the
                # key comes from this walk when it could be read. Failing that,
                # a key already remembered is kept when the shape has not
                # changed, and dropped when it has.
                "  audio_key = COALESCE("
                "    excluded.audio_key,"
                "    CASE WHEN quality_bench_files.content_signature = "
                "              excluded.content_signature "
                "         THEN quality_bench_files.audio_key END)",
                [
                    (ids[path], file.path.name, file.content_signature, file.audio_key)
                    for path, unit in wanted.items()
                    for file in unit.audio_files
                ],
            )
            connection.execute(
                "UPDATE quality_bench_roots SET walked_at = CURRENT_TIMESTAMP WHERE id = ?",
                (root_id,),
            )
        self._logger.info(
            "The bench recorded what a walk found.",
            extra={
                "operation": "quality.bench.walked",
                "root": root_id,
                "albums": len(wanted),
            },
        )

    def bench_albums(self, root_id: int | None = None) -> tuple[BenchAlbum, ...]:
        """Return the bench's albums with their files, without touching a disk."""
        clause = "WHERE album.root_id = ?" if root_id is not None else ""
        arguments = (root_id,) if root_id is not None else ()
        with self._database.connect() as connection:
            albums = connection.execute(
                "SELECT album.id, album.root_id, album.folder_path, album.unit_signature, "
                "       album.track_count FROM quality_bench_albums AS album "
                f"{clause} ORDER BY album.folder_path",
                arguments,
            ).fetchall()
            files = connection.execute(
                "SELECT file.album_id, file.file_name, file.content_signature, file.audio_key "
                "FROM quality_bench_files AS file "
                "JOIN quality_bench_albums AS album ON album.id = file.album_id "
                f"{clause} ORDER BY file.file_name",
                arguments,
            ).fetchall()
        held: dict[int, list[BenchFile]] = {}
        for row in files:
            held.setdefault(int(row[0]), []).append(
                BenchFile(name=row[1], content_signature=row[2], audio_key=row[3])
            )
        return tuple(
            BenchAlbum(
                album_id=int(row[0]),
                root_id=int(row[1]),
                folder_path=Path(row[2]),
                unit_signature=row[3],
                track_count=int(row[4]),
                files=tuple(held.get(int(row[0]), ())),
            )
            for row in albums
        )

    def shared_signatures(self) -> set[str]:
        """Return the signatures more than one recording on this bench answers to.

        **Asked of the audio, not of the file name.** A different name is only
        a stand-in for a different recording: it excludes two copies of one
        rip, which usually share a name and are the same audio, and it misses
        two folders of the *same album*, one honest and one transcoded from
        MP3, which hold identical file names over provably different
        recordings — no doubt is raised and each album wears the other's
        verdict.

        ``audio_key`` answers it exactly (migration 22), so it is what is
        asked. Where a file has no key the question cannot be answered about it
        and the name stands in for that signature — and where every file has
        one, a single distinct key is proof the doubt would be false, so it is
        not raised.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT content_signature FROM quality_bench_files "
                "GROUP BY content_signature "
                # `COUNT(DISTINCT audio_key)` skips NULLs, which is why the
                # second half exists: one key beside one unkeyed file is not
                # one recording, it is one recording and one unanswered
                # question, and that is where the name goes on standing in.
                "HAVING COUNT(DISTINCT audio_key) > 1 "
                "   OR (COUNT(*) > COUNT(audio_key) AND COUNT(DISTINCT file_name) > 1)"
            ).fetchall()
        return {row[0] for row in rows}

    def remember_audio_keys(self, keys: Mapping[tuple[int, str], str]) -> None:
        """Remember which audio each bench file holds, so nothing reads it twice.

        Keyed by album and file name, because that is what a bench row is; the
        key itself is about the audio, and reading one costs a seek and a read
        that there is no reason to pay for again.
        """
        if not keys:
            return
        with self._database.connect(write=True) as connection:
            connection.executemany(
                "UPDATE quality_bench_files SET audio_key = ? "
                "WHERE album_id = ? AND file_name = ?",
                [(key, album_id, name) for (album_id, name), key in keys.items()],
            )

    def record_quality_override(
        self,
        unit_signature: str,
        verdict: str,
        content_signature: str = "",
        audio_key: str | None = None,
    ) -> None:
        """Record the user overruling a measurement.

        ``content_signature`` empty means the whole album. The word is kept per
        album, because the same recording can sit in two of them and a verdict
        is about one album — and per *audio* as well (migration 22),
        because a signature is a declared shape and two files of one album can
        share it while holding different recordings.
        """
        with self._database.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO quality_words "
                "(unit_signature, content_signature, audio_key, verdict) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(unit_signature, content_signature, IFNULL(audio_key, '')) "
                "DO UPDATE SET verdict = excluded.verdict, created_at = CURRENT_TIMESTAMP",
                (unit_signature, content_signature, audio_key, verdict),
            )

    def clear_quality_override(
        self,
        unit_signature: str,
        content_signature: str = "",
        audio_key: str | None = None,
    ) -> None:
        """Withdraw one word and let the measurement speak again.

        The word written before the audio had a name goes too: what is taken
        back is the word about *this file*, and leaving a legacy row that
        answers for it would make the withdrawal do nothing on screen.
        """
        with self._database.connect(write=True) as connection:
            connection.execute(
                "DELETE FROM quality_words "
                "WHERE unit_signature = ? AND content_signature = ? "
                "  AND (audio_key IS ? OR audio_key IS NULL)",
                (unit_signature, content_signature, audio_key),
            )

    def quality_words(self, unit_signature: str) -> dict[tuple[str, str | None], str]:
        """Return this album's words, by file signature and the audio it was said about.

        `''` for the signature is the album as a whole. A NULL audio key is a
        word said before the audio had a name, and it answers for every file of
        that signature.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT content_signature, audio_key, verdict FROM quality_words "
                "WHERE unit_signature = ?",
                (unit_signature,),
            ).fetchall()
        return {(row[0], row[1]): row[2] for row in rows}

    def quality_overrides(self, unit_signature: str) -> dict[str, str]:
        """Return this album's words by signature alone, for what cannot name an audio.

        The keyed word wins where there is one, and the legacy row answers
        otherwise. Callers that hold the files should ask ``quality_words`` and
        resolve per file — this one cannot tell two recordings of one shape
        apart, which is the whole point of migration 22.
        """
        found: dict[str, str] = {}
        for (signature, audio_key), verdict in self.quality_words(unit_signature).items():
            if audio_key is None and signature in found:
                continue
            found[signature] = verdict
        return found

    def ambiguous_signatures(
        self,
        signatures: Iterable[str],
        still_there: Callable[[str], bool] | None = None,
    ) -> set[str]:
        """Return which of these signatures more than one differently-named file holds.

        Asked before a measured bitrate is allowed into a filename. A
        signature held by two files of the same name is two copies of one rip and
        is not ambiguous; held by files of different names it may be two
        recordings, and a number read from one of them would be written onto the
        other.

        Both places the app knows files from are asked — the Library's registry
        and the bench — because a collision does not care which screen brought
        the file in. It cannot see a file the app has never read, and that limit
        is the reason the answer is only ever allowed to *withhold* a number.

        **A name nothing answers to is not a second file.** ``still_there`` is
        the caller's way of saying which registered paths are still on disk; a
        row naming a file that is gone is history, not a rival, and counting it
        withholds a bitrate from files that have no rival at all. Injected
        rather than checked here, because this class reaches no filesystem;
        without it every registered name counts.
        """
        wanted = list(dict.fromkeys(signatures))
        if not wanted:
            return set()
        found: set[str] = set()
        with self._database.connect() as connection:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = connection.execute(
                    "SELECT content_signature, name FROM ("
                    "  SELECT content_signature, path AS name, 1 AS on_disk FROM audio_files"
                    f"   WHERE content_signature IN ({placeholders})"
                    "  UNION"
                    # The bench walked these names moments ago and keeps no path
                    # to check, so they are taken as they stand.
                    "  SELECT content_signature, file_name AS name, 0 AS on_disk"
                    "    FROM quality_bench_files"
                    f"   WHERE content_signature IN ({placeholders})"
                    ")",
                    chunk + chunk,
                ).fetchall()
                names: dict[str, set[str]] = {}
                for signature, name in rows:
                    if still_there is not None and "/" in name and not still_there(name):
                        continue
                    # `audio_files` keeps a whole path and the bench keeps a bare
                    # name, so they are compared on the last segment: the same
                    # rip in two folders is one name twice, not ambiguity.
                    names.setdefault(signature, set()).add(name.rsplit("/", 1)[-1])
                found.update(signature for signature, seen in names.items() if len(seen) > 1)
        return found

    def duplicate_audio(
        self, still_there: Callable[[str], bool] | None = None
    ) -> tuple[DuplicateAudio, ...]:
        """Return every piece of audio this library is holding in more than one place.

        Read from what the walks already wrote and never decoded, so this costs a
        query and no audio. Two kinds of proof, and they are two different
        claims:

        - **the same audio key** is the same stream, byte for byte — one rip
          filed twice. It is free, because the walk names the audio of every
          file it reads (migration 20).
        - **the same acoustic print** is the same performance, which may have
          been encoded twice at different bitrates. It costs a fingerprint per
          file, so it answers only about the files something has already asked
          about.

        A file that appears in both registers under one name is one file, not
        two: the same album can be on the Library and on the bench at once. What
        makes a duplicate is two different *places*.

        **A place nothing answers to is not a place.** ``still_there`` is the
        caller's way of saying which registered paths are on disk; a row naming a
        file that is gone is history, and counting it makes the map accuse a
        folder of holding one recording twice when it holds it once: the extra
        rows are the names the same files carried before they were organised.
        Asking the disk about every registered row is cheap.

        Injected rather than checked here, because this class reaches no
        filesystem; without it every registered row counts. Nothing is
        deleted, so a volume that comes back brings its rows back with it.

        Nothing is proposed and nothing is moved. Which copy to keep is the
        user's decision; this application says what it measured and leaves the
        files alone.
        """
        with self._database.connect() as connection:
            rows = connection.execute("""
                SELECT file.path, unit.folder_path, file.audio_key, file.content_signature,
                       'library', unit.unit_signature, unit.id
                  FROM audio_files AS file
                  LEFT JOIN album_units AS unit ON unit.id = file.album_unit_id
                 WHERE unit.cleared_at IS NULL
                UNION ALL
                SELECT album.folder_path || '/' || bench.file_name, album.folder_path,
                       bench.audio_key, bench.content_signature, 'bench',
                       album.unit_signature, NULL
                  FROM quality_bench_files AS bench
                  JOIN quality_bench_albums AS album ON album.id = bench.album_id
                """).fetchall()
            prints = connection.execute(
                "SELECT content_signature, audio_key, fingerprint FROM acoustic_prints "
                "WHERE audio_key IS NOT NULL"
            ).fetchall()
        held: dict[str, HeldCopy] = {}
        by_audio: dict[str, list[str]] = {}
        for path, folder, key, signature, where, album_signature, unit_id in rows:
            if path in held:
                continue
            if still_there is not None and not still_there(path):
                continue
            held[path] = HeldCopy(
                path=Path(path),
                album=Path(folder).name if folder else "",
                audio_key=key,
                content_signature=signature,
                where=where,
                album_path=Path(folder) if folder else None,
                album_signature=album_signature or "",
                unit_id=int(unit_id) if unit_id is not None else None,
            )
            if key:
                by_audio.setdefault(key, []).append(path)
        found = [
            DuplicateAudio(
                proof="audio",
                identity=key,
                copies=tuple(held[path] for path in sorted(paths)),
            )
            for key, paths in sorted(by_audio.items())
            if len(paths) > 1
        ]
        # And the same performance encoded twice, which the stream digest cannot
        # see and the print can. Grouped by print, then by the audio behind it,
        # so two copies of one rip do not read as two recordings.
        by_print: dict[str, set[str]] = {}
        for _, key, fingerprint in prints:
            by_print.setdefault(fingerprint, set()).add(key)
        for fingerprint, keys in sorted(by_print.items()):
            if len(keys) < 2:
                continue
            copies = tuple(
                held[path] for key in sorted(keys) for path in sorted(by_audio.get(key, ()))
            )
            if len(copies) > 1:
                found.append(DuplicateAudio(proof="print", identity=fingerprint, copies=copies))
        return tuple(found)

    def duplicate_decisions(self) -> dict[tuple[str, str], str]:
        """Return the user's word about each pair of albums that share audio.

        Keyed by the two albums' content signatures, ordered, so one pair is one
        answer however the two are named — and so that organising or filing an
        album does not lose what was already decided about it.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT first_signature, second_signature, verdict FROM duplicate_decisions"
            ).fetchall()
        return {(row[0], row[1]): row[2] for row in rows}

    def record_duplicate_decision(self, first: str, second: str, verdict: str | None) -> None:
        """Write down the user's word about two albums that hold some of the same audio.

        `both` is *they belong in both places* and `handled` is *I have dealt
        with it*; ``None`` takes the answer back, which puts the pair on screen
        again. **No file is touched by any of them**, and the screen says so:
        what to do with the audio is the user's decision, and this only stops
        the application asking again.
        """
        pair = tuple(sorted((first, second)))
        with self._database.connect(write=True) as connection:
            if verdict is None:
                connection.execute(
                    "DELETE FROM duplicate_decisions "
                    "WHERE first_signature = ? AND second_signature = ?",
                    pair,
                )
                return
            connection.execute(
                "INSERT INTO duplicate_decisions (first_signature, second_signature, verdict) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(first_signature, second_signature) DO UPDATE SET "
                "  verdict = excluded.verdict, decided_at = CURRENT_TIMESTAMP",
                (*pair, verdict),
            )

    def all_quality_overrides(self) -> dict[str, dict[tuple[str, str | None], str]]:
        """Return every word the user has given about the audio, by album.

        Read in one pass because the bench applies them to every row it holds
        at once, and asking per album would be one round trip per album to
        answer for the few rows that exist.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT unit_signature, content_signature, audio_key, verdict FROM quality_words"
            ).fetchall()
        found: dict[str, dict[tuple[str, str | None], str]] = {}
        for unit_signature, content_signature, audio_key, verdict in rows:
            found.setdefault(unit_signature, {})[(content_signature, audio_key)] = verdict
        return found

    def ids_in_state(self, state: str) -> set[int]:
        """Return the row ids of every album in one state.

        Asked of the folder rather than of the audio. Leaving a finished album
        alone is a promise about *the folder this app organized*, and keeping
        it by signature would extend it to every other folder holding the same
        recording — a second copy would arrive on the shelf already wearing
        `organized`, with nothing to plan and a button that does nothing.

        An album renamed by hand still keeps its row, because `_upsert_unit`
        adopts the old row exactly when the folder it named is gone. The row
        follows the album; the signature does not have to.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM album_units WHERE state = ?", (state,)
            ).fetchall()
        return {int(row[0]) for row in rows}

    def set_unit_state(self, unit_id: int, state: str) -> None:
        """Record the album's current place in the workflow."""
        with self._database.connect(write=True) as connection:
            connection.execute(
                "UPDATE album_units SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (state, unit_id),
            )

    def rate_unit(self, unit_id: int, rating: int | None) -> None:
        """Record the user's rating of this album, or withdraw it.

        ``None`` withdraws, and it is the only way back to unrated: the control
        offers five values and no sixth, so a zero would have to be invented
        here to mean *never mind*. `updated_at` is deliberately left alone — a
        rating says nothing about when the album was last looked at by the
        workflow, and the shelf reads that column.
        """
        with self._database.connect(write=True) as connection:
            connection.execute("UPDATE album_units SET rating = ? WHERE id = ?", (rating, unit_id))

    # --- what the sources said --------------------------------------------

    def record_release(self, release: ReleaseMetadata) -> int:
        """Cache one source release in full and return its row id."""
        payload = serialize_release(release)
        primary_artist = written_credit(release.artists) or None
        with self._database.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO metadata_releases
                    (source, source_release_id, title, primary_artist, released_on,
                     original_released_on, track_count, payload, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (source, source_release_id) DO UPDATE SET
                    title = excluded.title,
                    primary_artist = excluded.primary_artist,
                    released_on = excluded.released_on,
                    original_released_on = excluded.original_released_on,
                    track_count = excluded.track_count,
                    payload = excluded.payload,
                    fetched_at = CURRENT_TIMESTAMP
                """,
                (
                    str(release.source),
                    release.source_release_id,
                    release.title,
                    primary_artist,
                    release.released_on.isoformat() if release.released_on else None,
                    (
                        release.original_released_on.isoformat()
                        if release.original_released_on
                        else None
                    ),
                    len(release.tracks),
                    payload,
                ),
            )
            row = connection.execute(
                "SELECT id FROM metadata_releases WHERE source = ? AND source_release_id = ?",
                (str(release.source), release.source_release_id),
            ).fetchone()
        return int(row[0])

    def release(self, release_row_id: int) -> ReleaseMetadata | None:
        """Return a cached release, so a better matcher can re-run without the network."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM metadata_releases WHERE id = ?", (release_row_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return deserialize_release(row[0])
        except UnreadableReleaseError:
            # One cached release this build cannot read is one lookup that has
            # to be asked again, not a library that fails to open.
            self._logger.warning(
                "A cached release could not be read and was passed over.",
                extra={
                    "operation": "metadata.release.unreadable",
                    "release_row_id": release_row_id,
                },
            )
            return None

    # --- what was decided --------------------------------------------------

    def record_identification(
        self,
        unit_id: int,
        release_row_id: int | None,
        method: str,
        confidence: float,
        explanation: str,
        state: str = "proposed",
        decided_by: str | None = None,
    ) -> int:
        """Record one identification attempt, kept whether or not it was accepted."""
        with self._database.connect(write=True) as connection:
            connection.execute(
                "UPDATE identifications SET state = 'superseded' "
                "WHERE album_unit_id = ? AND state = 'proposed'",
                (unit_id,),
            )
            cursor = connection.execute(
                """
                INSERT INTO identifications
                    (album_unit_id, metadata_release_id, method, confidence, explanation,
                     state, decided_by, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL
                                                  ELSE CURRENT_TIMESTAMP END)
                """,
                (
                    unit_id,
                    release_row_id,
                    method,
                    confidence,
                    explanation,
                    state,
                    decided_by,
                    decided_by,
                ),
            )
            # Read inside the block, like every other `lastrowid` in this file.
            # Outside it the connection is already closed, and reading it there
            # works only because CPython happens not to guard that attribute —
            # if it did, every identification would silently get row id 0.
            return int(cursor.lastrowid or 0)

    def record_plan(
        self,
        unit_id: int,
        identification_id: int | None,
        operations: Sequence[ChangeOperation],
    ) -> int:
        """Record a simulated plan and every operation it would perform."""
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                "INSERT INTO change_plans (album_unit_id, identification_id, state) "
                "VALUES (?, ?, 'simulated')",
                (unit_id, identification_id),
            )
            plan_id = int(cursor.lastrowid or 0)
            connection.executemany(
                """
                INSERT INTO change_operations
                    (change_plan_id, sequence, kind, target_path, before_state, after_state)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        plan_id,
                        operation.sequence,
                        str(operation.kind),
                        str(operation.target_path),
                        json.dumps(dict(operation.before_state), ensure_ascii=False),
                        json.dumps(dict(operation.after_state), ensure_ascii=False),
                    )
                    for operation in operations
                ],
            )
        return plan_id

    def record_plan_outcome(
        self, plan_id: int, state: str, applied_sequences: Iterable[int] = ()
    ) -> None:
        """Record what became of a plan, and which of its operations took effect."""
        sequences = list(applied_sequences)
        with self._database.connect(write=True) as connection:
            if state == "applied":
                # `reverted_at` is cleared, on both rows. An operation that has
                # just been applied again is not a reverted one, and the trail a
                # reversal reads is *what still stands* — so a plan reverted,
                # redone, and reverted again (the two alternate without limit)
                # would otherwise present an empty trail the second time and
                # answer that there was nothing to undo.
                connection.execute(
                    "UPDATE change_plans SET state = ?, applied_at = CURRENT_TIMESTAMP, "
                    "reverted_at = NULL WHERE id = ?",
                    (state, plan_id),
                )
                connection.executemany(
                    "UPDATE change_operations SET applied_at = CURRENT_TIMESTAMP, "
                    "reverted_at = NULL WHERE change_plan_id = ? AND sequence = ?",
                    [(plan_id, sequence) for sequence in sequences],
                )
            elif state == "reverted":
                connection.execute(
                    "UPDATE change_plans SET state = ?, reverted_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (state, plan_id),
                )
                connection.executemany(
                    "UPDATE change_operations SET reverted_at = CURRENT_TIMESTAMP "
                    "WHERE change_plan_id = ? AND sequence = ?",
                    [(plan_id, sequence) for sequence in sequences],
                )
            else:
                connection.execute(
                    "UPDATE change_plans SET state = ? WHERE id = ?", (state, plan_id)
                )

    def record_execution(
        self, plan_id: int, applied: Sequence[AppliedOperation], complete: bool = True
    ) -> None:
        """Store the executed trail, which is what a later reversal replays.

        The planning-time before-state is a preview; the executed one is
        authoritative. Each operation's row is updated with what was really
        replaced, the backup an image went to, and the directories the move
        created, so the trail outlives the process that applied it.

        A run that stopped partway is recorded as ``failed`` rather than
        ``applied``. It really did write what its trail lists, so it
        keeps its timestamp and stays revertible — it is the state of the plan
        that is different, and the history has to say so.
        """
        with self._database.connect(write=True) as connection:
            connection.execute(
                "UPDATE change_plans SET state = ?, applied_at = CURRENT_TIMESTAMP WHERE id = ?",
                ("applied" if complete else "failed", plan_id),
            )
            for entry in applied:
                before = dict(entry.before_state)
                if entry.created_directories:
                    before["created_directories"] = [
                        str(path) for path in entry.created_directories
                    ]
                connection.execute(
                    "UPDATE change_operations SET before_state = ?, backup_path = ?, "
                    "applied_at = CURRENT_TIMESTAMP WHERE change_plan_id = ? AND sequence = ?",
                    (
                        json.dumps(before, ensure_ascii=False),
                        str(before["backup"]) if "backup" in before else None,
                        plan_id,
                        entry.operation.sequence,
                    ),
                )

    def record_operation_applied(self, plan_id: int, entry: AppliedOperation) -> None:
        """Write one operation's trail down the moment it takes effect.

        The end-of-run record remains authoritative and rewrites all of this;
        what this adds is survival. Without it, a process killed between the
        first write and the last leaves operations on disk with no row to
        revert from.

        The plan is marked ``failed`` while it runs, and ``applied`` only when
        the run finishes, so an interrupted apply is already in the state the
        history knows how to draw: partial, counted, and revertible.
        The guard keeps a plan that already reached a verdict from being pulled
        back to ``failed``.
        """
        before = dict(entry.before_state)
        if entry.created_directories:
            before["created_directories"] = [str(path) for path in entry.created_directories]
        with self._database.connect(write=True) as connection:
            connection.execute(
                "UPDATE change_plans SET state = 'failed', "
                "applied_at = COALESCE(applied_at, CURRENT_TIMESTAMP) "
                "WHERE id = ? AND state = 'simulated'",
                (plan_id,),
            )
            connection.execute(
                # `reverted_at` cleared here too, and for the same reason as in
                # ``record_plan_outcome``: this is the other place an operation
                # is written down as applied, and a rule stated in one of the
                # two is stated in one place too few.
                "UPDATE change_operations SET before_state = ?, backup_path = ?, "
                "applied_at = CURRENT_TIMESTAMP, reverted_at = NULL "
                "WHERE change_plan_id = ? AND sequence = ?",
                (
                    json.dumps(before, ensure_ascii=False),
                    str(before["backup"]) if "backup" in before else None,
                    plan_id,
                    entry.operation.sequence,
                ),
            )

    def record_operation_reverted(self, plan_id: int, sequence: int) -> None:
        """Write one operation down the moment it comes back, for the same reason.

        The mirror of ``record_operation_applied``. ``record_plan_outcome`` is
        still what settles the plan, and it runs only once the whole reversal
        returns — so without this a reversal that stops partway through writes
        nothing at all, and the rows go on saying every operation is applied
        while some of them have already been undone on disk.

        The rehearsal is what makes this rare. This is what makes it *true* when
        it happens anyway: a disk that fills, a file locked by another program,
        a volume that goes away between one operation and the next.
        """
        with self._database.connect(write=True) as connection:
            connection.execute(
                "UPDATE change_operations SET reverted_at = CURRENT_TIMESTAMP "
                "WHERE change_plan_id = ? AND sequence = ?",
                (plan_id, sequence),
            )

    def applied_trail(self, plan_id: int) -> tuple[AppliedOperation, ...]:
        """Rebuild the still-standing trail of one applied plan from its rows.

        An operation that has already come back is left out, so a reversal that
        stopped partway through can be finished rather than started again — the
        second attempt would otherwise try to undo, a second time, what is
        already undone, and every one of those undos raises.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT sequence, kind, target_path, before_state, after_state "
                "FROM change_operations WHERE change_plan_id = ? AND applied_at IS NOT NULL "
                "AND reverted_at IS NULL ORDER BY sequence",
                (plan_id,),
            ).fetchall()
        trail: list[AppliedOperation] = []
        for sequence, kind, target_path, before_json, after_json in rows:
            before = json.loads(before_json) if before_json else {}
            created = tuple(Path(entry) for entry in before.pop("created_directories", ()))
            trail.append(
                AppliedOperation(
                    operation=ChangeOperation(
                        sequence=sequence,
                        kind=OperationKind(kind),
                        target_path=Path(target_path),
                        after_state=json.loads(after_json),
                        before_state=before,
                    ),
                    before_state=before,
                    created_directories=created,
                )
            )
        return tuple(trail)

    def record_download(
        self,
        source: str,
        directory: str,
        folder: str,
        files: int,
        size_bytes: int,
        whole_folder: bool,
    ) -> int:
        """Remember that music was fetched from the network, and from whom.

        What this app renamed is half of what happened to a folder; the other
        half is that it arrived, and History answers for both.
        """
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO downloads
                    (source, directory, folder, files, size_bytes, whole_folder)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source, directory, folder, files, size_bytes, 1 if whole_folder else 0),
            )
            return int(cursor.lastrowid or 0)

    def download_history(self) -> tuple[dict[str, object], ...]:
        """Return everything fetched from the network, newest first.

        Which album a download became is answered by **where it is, and failing
        that by what it is**. The path is tried first because it is exact and it
        is what a freshly collected download has. It stops being true the moment
        the organised folder is moved into the library by hand — a move this
        application never performs and therefore never records.

        The signature is the fallback, and it is a shape rather than an
        identity: `unit_signature` collides across copies of the same album by
        design. So it answers only when exactly one album carries it, and a
        shared one answers nothing rather than naming a copy at random.
        """
        with self._database.connect() as connection:
            rows = connection.execute("""
                WITH anchored AS (
                    SELECT downloads.id AS download_id,
                           COALESCE(
                               (SELECT units.id FROM album_units AS units
                                 WHERE units.folder_path = downloads.landed_path),
                               (SELECT min(units.id) FROM album_units AS units
                                 WHERE downloads.unit_signature != ''
                                   AND units.unit_signature = downloads.unit_signature
                                HAVING count(*) = 1)
                           ) AS unit_id
                      FROM downloads
                )
                SELECT downloads.id, downloads.source, downloads.directory,
                       downloads.folder, downloads.files, downloads.size_bytes,
                       downloads.whole_folder, downloads.requested_at,
                       downloads.collected_at, downloads.cleared_at,
                       downloads.landed_path, units.id,
                       releases.primary_artist, releases.title,
                       downloads.unit_signature
                FROM downloads
                JOIN anchored ON anchored.download_id = downloads.id
                -- Which album this download became, when it has become one, so
                -- History can show the sleeve the Library shows. Null until the
                -- folder has been collected, and null again if the album was
                -- cleared away — a row without a picture, not a broken one.
                LEFT JOIN album_units AS units
                       ON units.id = anchored.unit_id
                -- And what that album turned out to be, so a download reads
                -- like the album it is rather than like a remote path.
                LEFT JOIN identifications
                       ON identifications.album_unit_id = units.id
                      AND identifications.state != 'superseded'
                LEFT JOIN metadata_releases AS releases
                       ON releases.id = identifications.metadata_release_id
                GROUP BY downloads.id
                ORDER BY downloads.requested_at DESC, downloads.id DESC
                """).fetchall()
        return tuple(
            {
                "download_id": row[0],
                "source": row[1],
                "directory": row[2],
                "folder": row[3],
                "files": row[4],
                "size": row[5],
                "whole_folder": bool(row[6]),
                "requested_at": row[7],
                "collected_at": row[8],
                "cleared_at": row[9],
                "landed_path": row[10] or "",
                "unit_id": row[11],
                "artist": row[12],
                "title": row[13],
                "unit_signature": row[14],
            }
            for row in rows
        )

    def mark_download_collected(
        self, download_id: int, landed_path: str, unit_signature: str = ""
    ) -> None:
        """Record that this download has arrived, where from, and what it is.

        The signature is what survives the folder being moved into the library
        by hand, which the path does not. It is empty when the collect read more
        than one album out of the folder — there is one column here and no way
        to say *these two* — and empty is the same "not anchored" every
        uncollected row already carries.
        """
        with self._database.connect(write=True) as connection:
            connection.execute(
                """
                UPDATE downloads
                   SET collected_at = CURRENT_TIMESTAMP, landed_path = ?,
                       unit_signature = ?
                 WHERE id = ?
                """,
                (landed_path, unit_signature, download_id),
            )

    def relocate_download(self, was: str, now: str) -> int:
        """Follow a collected download to the folder an apply renamed it to.

        Organising a download is the point of this application, and organising
        renames its folder — so the path recorded when it arrived stops being
        true the moment it is used for anything. Left stale, the album is
        looked for under a name nothing has any more and quietly vanishes from
        the library on the next start.
        """
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE downloads SET landed_path = ? WHERE landed_path = ?",
                (now, was),
            )
            return int(cursor.rowcount or 0)

    def clear_collected_downloads(self) -> int:
        """Take every collected download off the screen, touching no file.

        It records that they have been seen. The rows stay, so History still
        answers for what arrived, and nothing on disk is read or written.
        """
        with self._database.connect(write=True) as connection:
            cursor = connection.execute("""
                UPDATE downloads
                   SET cleared_at = CURRENT_TIMESTAMP
                 WHERE collected_at IS NOT NULL AND cleared_at IS NULL
                """)
            return cursor.rowcount or 0

    def clear_download(self, download_id: int) -> bool:
        """Take one download off the waiting list, touching no file.

        The companion of `clear_collected_downloads`, which only ever reaches
        the ones that *arrived* — without this, a download that never arrives
        (a peer that went away) cannot be dismissed by anything and stays
        pending for the life of the database.

        One row, named by the caller: nothing sweeps. The row stays so History
        still answers for what was asked for.
        """
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE downloads SET cleared_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND cleared_at IS NULL",
                (download_id,),
            )
            return bool(cursor.rowcount)

    def plan_history(self) -> tuple[dict[str, object], ...]:
        """Return every plan that reached the disk, newest first, for the history screen.

        A partial run is included and counted twice — what it planned and what
        it actually wrote — because "N of M" is the whole story of that row.
        """
        with self._database.connect() as connection:
            rows = connection.execute("""
                SELECT plans.id, plans.state, plans.applied_at, plans.reverted_at,
                       units.id, units.folder_path,
                       identifications.confidence, identifications.explanation,
                       (SELECT COUNT(*) FROM change_operations
                         WHERE change_operations.change_plan_id = plans.id),
                       (SELECT COUNT(*) FROM change_operations
                         WHERE change_operations.change_plan_id = plans.id
                           AND change_operations.applied_at IS NOT NULL),
                       -- Who it is by and what it is called. A folder name
                       -- alone answers neither on a screen of forty rows.
                       releases.primary_artist, releases.title
                FROM change_plans AS plans
                JOIN album_units AS units ON units.id = plans.album_unit_id
                LEFT JOIN identifications
                    ON identifications.id = plans.identification_id
                LEFT JOIN metadata_releases AS releases
                    ON releases.id = identifications.metadata_release_id
                WHERE plans.state IN ('applied', 'reverted', 'failed')
                ORDER BY plans.applied_at DESC, plans.id DESC
                """).fetchall()
        return tuple(
            {
                "plan_id": row[0],
                "state": row[1],
                "applied_at": row[2],
                "reverted_at": row[3],
                "unit_id": row[4],
                "folder_path": row[5],
                "confidence": row[6],
                "explanation": row[7],
                "operations": row[8],
                "applied_operations": row[9],
                "artist": row[10],
                "title": row[11],
            }
            for row in rows
        )

    def record_identification_state(
        self, identification_id: int, state: str, decided_by: str
    ) -> None:
        """Record the user's or the threshold's verdict on one identification."""
        with self._database.connect(write=True) as connection:
            connection.execute(
                "UPDATE identifications SET state = ?, decided_by = ?, "
                "decided_at = CURRENT_TIMESTAMP WHERE id = ?",
                (state, decided_by, identification_id),
            )

    def change_log(self) -> tuple[dict[str, object], ...]:
        """Return every operation this library ever applied, oldest first.

        The from/to report is built from this: the executed
        before-state is authoritative, so what a row reports is what actually
        happened rather than what was once planned.
        """
        with self._database.connect() as connection:
            rows = connection.execute("""
                SELECT units.folder_path, operations.kind, operations.target_path,
                       operations.before_state, operations.after_state,
                       operations.applied_at, operations.reverted_at,
                       operations.change_plan_id
                FROM change_operations AS operations
                JOIN change_plans AS plans ON plans.id = operations.change_plan_id
                JOIN album_units AS units ON units.id = plans.album_unit_id
                WHERE operations.applied_at IS NOT NULL
                ORDER BY operations.applied_at, operations.change_plan_id, operations.sequence
                """).fetchall()
        return tuple(
            {
                "folder_path": row[0],
                "kind": row[1],
                "target_path": row[2],
                "before_state": json.loads(row[3]) if row[3] else {},
                "after_state": json.loads(row[4]) if row[4] else {},
                "applied_at": row[5],
                "reverted_at": row[6],
                "plan_id": row[7],
            }
            for row in rows
        )

    def record_proof(self, plan_id: int, kind: str, ok: bool, payload: dict[str, object]) -> None:
        """Store one apply-time proof — audio fingerprints or write read-back."""
        with self._database.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO apply_proofs (change_plan_id, kind, ok, payload) VALUES (?, ?, ?, ?)",
                (plan_id, kind, 1 if ok else 0, json.dumps(payload, ensure_ascii=False)),
            )

    def proofs(self, plan_id: int) -> dict[str, dict[str, object]]:
        """Return the recorded proofs of one plan, newest of each kind."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT kind, ok, payload FROM apply_proofs "
                "WHERE change_plan_id = ? ORDER BY id",
                (plan_id,),
            ).fetchall()
        found: dict[str, dict[str, object]] = {}
        for kind, ok, payload in rows:
            found[kind] = {"ok": bool(ok), **json.loads(payload)}
        return found

    def proof_flags(self) -> dict[int, dict[str, bool]]:
        """Return each plan's proof verdicts, for the report's certificate columns."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT change_plan_id, kind, ok FROM apply_proofs ORDER BY id"
            ).fetchall()
        flags: dict[int, dict[str, bool]] = {}
        for plan_id, kind, ok in rows:
            flags.setdefault(plan_id, {})[kind] = bool(ok)
        return flags

    def album_witnesses(self) -> dict[str, dict[str, object]]:
        """Return each album's stored witness verdicts, by folder path."""
        with self._database.connect() as connection:
            rows = connection.execute("""
                SELECT units.folder_path, identifications.verification
                FROM identifications
                JOIN album_units AS units ON units.id = identifications.album_unit_id
                WHERE identifications.verification IS NOT NULL
                  AND identifications.state IN ('accepted', 'proposed')
                ORDER BY identifications.id
                """).fetchall()
        return {row[0]: json.loads(row[1]) for row in rows}

    def proof_summary(self) -> dict[str, object]:
        """Return the library-wide certificate counts the window shows."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT kind, ok, COUNT(*) FROM apply_proofs "
                "JOIN change_plans ON change_plans.id = apply_proofs.change_plan_id "
                "WHERE change_plans.state = 'applied' GROUP BY kind, ok"
            ).fetchall()
        summary = {"audio_ok": 0, "audio_failed": 0, "writes_ok": 0, "writes_failed": 0}
        for kind, ok, count in rows:
            summary[f"{kind}_{'ok' if ok else 'failed'}"] = count
        return summary

    def assign_run(self, plan_ids: Sequence[int], run_id: str) -> None:
        """Stamp the plans one gesture applied, so the whole run can be undone."""
        with self._database.connect(write=True) as connection:
            connection.executemany(
                "UPDATE change_plans SET run_id = ? WHERE id = ?",
                [(run_id, plan_id) for plan_id in plan_ids],
            )

    def run_plans(self, run_id: str) -> tuple[int, ...]:
        """Return the still-applied plans of one run, newest first, for reversal.

        A plan that stopped partway is included: it wrote part of what it
        planned, and it is the one a reversal is most needed for.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM change_plans WHERE run_id = ? AND state IN ('applied', 'failed') "
                "ORDER BY id DESC",
                (run_id,),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def reverted_run_plans(self, run_id: str) -> tuple[int, ...]:
        """Return the reverted plans of one run, oldest first, to apply again.

        The mirror of ``run_plans``, and its order is the mirror too: a reversal
        undoes the newest first, so re-applying starts from the oldest and walks
        forward through the run exactly as it originally ran.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM change_plans WHERE run_id = ? AND state = 'reverted' "
                "ORDER BY id",
                (run_id,),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def plan_operations(self, plan_id: int) -> tuple[ChangeOperation, ...]:
        """Return every operation one plan holds, in the order it planned them.

        Not ``applied_trail``: that answers "what did this actually do", filtered
        to what took effect and carrying the before-state read at the time, and
        it is what a reversal replays backwards. This answers "what did this plan
        say to do", which is what re-applying a reverted run needs.

        The order is the plan's own and it matters: the planner emits operations
        so that every later path is still valid — files inside a folder before
        the folder itself — and applying them in any other order renames a file
        that is no longer where the next operation looks for it.
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT sequence, kind, target_path, before_state, after_state "
                "FROM change_operations WHERE change_plan_id = ? ORDER BY sequence",
                (plan_id,),
            ).fetchall()
        return tuple(
            ChangeOperation(
                sequence=sequence,
                kind=OperationKind(kind),
                target_path=Path(target_path),
                after_state=json.loads(after_json),
                # The planning-time before-state, minus the bookkeeping a real
                # apply writes into it: `created_directories` is a fact about a
                # run that happened, not part of what the plan says to do.
                before_state={
                    key: value
                    for key, value in (json.loads(before_json) if before_json else {}).items()
                    if key != "created_directories"
                },
            )
            for sequence, kind, target_path, before_json, after_json in rows
        )

    def plan_release(self, plan_id: int) -> int | None:
        """Return the cached release row one plan was built around, if it had one.

        A cover-only plan identifies nothing and has none.
        """
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT identifications.metadata_release_id FROM change_plans "
                "JOIN identifications ON identifications.id = change_plans.identification_id "
                "WHERE change_plans.id = ?",
                (plan_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def record_verification(self, identification_id: int, verification: dict[str, object]) -> None:
        """Store the comparison results the witnesses produced: the verdicts, not their data."""
        with self._database.connect(write=True) as connection:
            connection.execute(
                "UPDATE identifications SET verification = ? WHERE id = ?",
                (json.dumps(verification, ensure_ascii=False), identification_id),
            )

    def verification(self, identification_id: int) -> dict[str, object] | None:
        """Return one identification's stored witness results."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT verification FROM identifications WHERE id = ?",
                (identification_id,),
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def album_sources(self) -> dict[str, tuple[str, str]]:
        """Return the release each album was identified as, by album folder path.

        The report links to the page that justified the changes, and an
        accepted identification is what justified them.
        """
        with self._database.connect() as connection:
            rows = connection.execute("""
                SELECT units.folder_path, releases.source, releases.source_release_id
                FROM identifications
                JOIN album_units AS units ON units.id = identifications.album_unit_id
                JOIN metadata_releases AS releases
                    ON releases.id = identifications.metadata_release_id
                WHERE identifications.state IN ('accepted', 'proposed')
                ORDER BY identifications.id
                """).fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}

    def plan_unit(self, plan_id: int) -> int | None:
        """Return the album a plan belongs to."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT album_unit_id FROM change_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        return int(row[0]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Store one user-chosen setting; the window's values live here."""
        with self._database.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def setting(self, key: str) -> str | None:
        """Return one stored setting, or ``None`` when the user never chose it."""
        with self._database.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def record_correction(
        self,
        unit_id: int,
        field: str,
        value: str,
        replaced: str | None,
        track_position: int | None = None,
        release_key: str | None = None,
    ) -> None:
        """Record one manual correction, keeping what it replaced.

        The delete-then-insert exists because a UNIQUE constraint treats two
        NULL positions as distinct rows, and a folder-name correction has no
        position — upserting would quietly accumulate corrections instead of
        replacing them.

        ``release_key`` names the release a pairing was made against, and is
        NULL for every field that does not depend on one.
        """
        with self._database.connect(write=True) as connection:
            connection.execute(
                "DELETE FROM manual_corrections WHERE album_unit_id = ? AND field = ? "
                "AND track_position IS ?",
                (unit_id, field, track_position),
            )
            connection.execute(
                "INSERT INTO manual_corrections "
                "(album_unit_id, field, track_position, value, replaced, release_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (unit_id, field, track_position, value, replaced, release_key),
            )

    def clear_correction(self, unit_id: int, field: str, track_position: int | None) -> None:
        """Withdraw one manual correction, restoring the catalogue's authority."""
        with self._database.connect(write=True) as connection:
            connection.execute(
                "DELETE FROM manual_corrections WHERE album_unit_id = ? AND field = ? "
                "AND track_position IS ?",
                (unit_id, field, track_position),
            )

    def clear_corrections_of(self, unit_id: int, field: str) -> int:
        """Withdraw every correction of one field, and say how many went.

        The count is what lets the window report a discard as a fact — *four
        pairings dropped* — rather than as a claim nobody checked.
        """
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                "DELETE FROM manual_corrections WHERE album_unit_id = ? AND field = ?",
                (unit_id, field),
            )
            return int(cursor.rowcount or 0)

    def rekey_corrections(self, unit_id: int, field: str, release_key: str) -> int:
        """Move every correction of one field onto the release now identified.

        This is the user accepting suspended pairings for a release adopted
        later: the positions are kept exactly as they were recorded, and only
        the release they answer to changes.
        """
        with self._database.connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE manual_corrections SET release_key = ? "
                "WHERE album_unit_id = ? AND field = ?",
                (release_key, unit_id, field),
            )
            return int(cursor.rowcount or 0)

    def corrections(self, unit_id: int) -> tuple[dict[str, object], ...]:
        """Return every manual correction recorded for one album."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT field, track_position, value, replaced, release_key "
                "FROM manual_corrections WHERE album_unit_id = ? ORDER BY field, track_position",
                (unit_id,),
            ).fetchall()
        return tuple(
            {
                "field": row[0],
                "track_position": row[1],
                "value": row[2],
                "replaced": row[3],
                "release_key": row[4],
            }
            for row in rows
        )

    # --- internals ---------------------------------------------------------

    def _upsert_unit(self, connection: sqlite3.Connection, unit: AlbumUnit) -> int:
        spellings = _both_spellings(unit.folder_path)
        row = connection.execute(
            # Oldest first, so that a database which already holds both spellings
            # always answers with the same row instead of whichever the page
            # order happened to yield.
            f"SELECT id FROM album_units WHERE folder_path IN ({_holes(spellings)}) "
            "ORDER BY id LIMIT 1",
            spellings,
        ).fetchone()
        if row is None:
            # The album may simply have been renamed since the last scan, which
            # its audio content still identifies. But a signature match
            # whose folder is still on disk is a second copy of the same album,
            # not the same one moved, and collapsing the two would lose one.
            candidate = connection.execute(
                "SELECT id, folder_path FROM album_units WHERE unit_signature = ?",
                (unit.unit_signature,),
            ).fetchone()
            if candidate is not None and not Path(candidate[1]).exists():
                row = (candidate[0],)
        if row is not None:
            # `cleared_at = NULL` keeps the promise the ✕ makes: it hides a row,
            # it does not remember a decision, and a scan that reaches the
            # folder again brings the album back.
            connection.execute(
                "UPDATE album_units SET folder_path = ?, unit_signature = ?, track_count = ?, "
                "total_duration_ms = ?, cleared_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    str(unit.folder_path),
                    unit.unit_signature,
                    unit.track_count,
                    unit.total_duration_ms,
                    row[0],
                ),
            )
            return int(row[0])
        cursor = connection.execute(
            "INSERT INTO album_units (folder_path, unit_signature, track_count, total_duration_ms) "
            "VALUES (?, ?, ?, ?)",
            (
                str(unit.folder_path),
                unit.unit_signature,
                unit.track_count,
                unit.total_duration_ms,
            ),
        )
        return int(cursor.lastrowid or 0)

    def _upsert_files(self, connection: sqlite3.Connection, unit: AlbumUnit, unit_id: int) -> None:
        # **One sighting, one stamp.** Every row this walk touches carries the
        # same `last_seen_at`, because *what the album weighed when it was last
        # seen* is the question `audio_sizes` asks, and it can only be answered
        # if the rows of one walk are one group. `CURRENT_TIMESTAMP` per
        # statement would group them only by accident of resolution: it is
        # fixed within a statement and not across them, so a walk that
        # straddles a second would be two sightings and half an album.
        #
        # Milliseconds, so two walks in quick succession are never one — and
        # the format is the whole-second one with a fraction appended, which
        # sorts after it, so a row stamped without the fraction reads as older
        # than every row written here.
        seen_at = connection.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')").fetchone()[0]
        for file in unit.audio_files:
            # The row that already stands for this file may spell its accents the
            # other way (see `_both_spellings`). Re-spelled to what the file
            # system just said, the insert below finds it instead of adding a
            # second row for the same file.
            spellings = _both_spellings(file.path)
            if len(spellings) > 1:
                connection.execute(
                    "UPDATE OR IGNORE audio_files SET path = ? "
                    f"WHERE path IN ({_holes(spellings)})",
                    (str(file.path), *spellings),
                )
            properties = file.properties
            connection.execute(
                """
                INSERT INTO audio_files
                    (path, content_signature, file_size_bytes, modified_at, album_unit_id,
                     container_format, codec, duration_ms, sample_rate, channels, bit_depth,
                     bitrate, disc_position, audio_key, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (path) DO UPDATE SET
                    content_signature = excluded.content_signature,
                    file_size_bytes = excluded.file_size_bytes,
                    modified_at = excluded.modified_at,
                    album_unit_id = excluded.album_unit_id,
                    duration_ms = excluded.duration_ms,
                    -- Which audio this is, from the walk that just read it
                    -- (migration 20). A scan that could not name it leaves the
                    -- name that stands rather than erasing it: `None` means
                    -- *this walk could not say*, never *it is not that one*.
                    audio_key = COALESCE(excluded.audio_key, audio_files.audio_key),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    str(file.path),
                    file.content_signature,
                    file.file_size_bytes,
                    file.modified_at.isoformat(),
                    unit_id,
                    file.path.suffix.lstrip(".").lower(),
                    properties.codec if properties else None,
                    properties.duration_ms if properties else None,
                    properties.sample_rate if properties else None,
                    properties.channels if properties else None,
                    properties.bit_depth if properties else None,
                    properties.bitrate if properties else None,
                    file.disc_hint,
                    file.audio_key,
                    seen_at,
                ),
            )
