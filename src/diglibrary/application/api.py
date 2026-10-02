"""The application API the window calls: every gesture, and nothing else.

This is the boundary between the interface and the application: the interface
renders what this class reports and forwards gestures back. No business rule
lives on the other side of the bridge, and nothing here talks to pywebview — the
window injects the one platform capability it owns (the folder dialog) as a
plain callable.
"""

import base64
import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from diglibrary.application.artwork import ArtworkService
from diglibrary.application.bench import BenchFinding, DeepAlbum, DeepAnalysis, QualityBench
from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSourceId,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
    written_credit,
)
from diglibrary.application.identification import (
    Decision,
    IdentificationOutcome,
    IdentificationWorkflow,
)
from diglibrary.application.quality import (
    AlbumQuality,
    QualitySurvey,
    measurements_for,
    stored_track_is_transcoded,
    stored_track_proof,
    the_picture_shows_the_proof,
    verdict_from_stored,
)
from diglibrary.cachedir import MEGABYTE, clear_folder, enforce_folder_limit, folder_bytes
from diglibrary.config import credentials
from diglibrary.connectors.models import SearchProgress, TransferFile, TransferState
from diglibrary.database.copies import (
    DEFAULT_COPY_LIMIT_BYTES,
    enforce_copy_limit,
    take_copy,
)
from diglibrary.database.copies import (
    held_bytes as database_copies_bytes,
)
from diglibrary.database.library_store import (
    AcousticAnswer,
    HeldCopy,
    LibraryStore,
    StoredHarmonics,
    StoredQuality,
    StoredUnit,
    word_for,
)
from diglibrary.harmonic.analyzer import HarmonicAnalyzer
from diglibrary.harmonic.keys import camelot_for
from diglibrary.library.artwork import CACHE_SUFFIXES as ARTWORK_SUFFIXES
from diglibrary.library.artwork import DEFAULT_BACKUP_LIMIT_BYTES, ArtworkStore, extension_for
from diglibrary.library.artwork import DEFAULT_CACHE_LIMIT_BYTES as DEFAULT_ARTWORK_CACHE_BYTES
from diglibrary.library.audio import AUDIO_EXTENSIONS
from diglibrary.library.audio_key import audio_key
from diglibrary.library.casing import (
    normalize_album_names,
    normalize_release,
    normalize_track,
)
from diglibrary.library.executor import (
    AppliedOperation,
    ChangeExecutor,
    ExecutionState,
    StalePlanError,
    rerooted_trail,
)
from diglibrary.library.extras import (
    ExtraTrack,
    claims,
    next_free_position,
    renumbers_the_catalogue,
    with_extra_tracks,
    without_extra_tracks,
)
from diglibrary.library.hints import SearchHints, files_in_track_order, local_track_titles
from diglibrary.library.integrity import AudioProof, audio_proof
from diglibrary.library.matching import (
    DURATION_TOLERANCE_MS,
    NO_ALBUM_TITLES,
    MatchCandidate,
    album_credit,
    album_titles,
    explain_evidence,
    track_title_similarity,
)
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.library.naming import (
    DEFAULT_TRANSCODED_LABEL,
    NamingError,
    NamingPolicy,
    facts_from_properties,
    format_label,
    is_various,
)
from diglibrary.library.planner import ChangeOperation, ChangePlan, ChangePlanner, OperationKind
from diglibrary.library.report import AlbumReport, Ink, Run, change, write_change_report
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.spelling import a_whole_word_apart, one_word_apart
from diglibrary.library.tagrelease import read_tags
from diglibrary.library.tags import WRITTEN_FIELDS, TagStore, as_the_container_writes
from diglibrary.metadata.cache import DEFAULT_LIMIT_BYTES as DEFAULT_METADATA_CACHE_BYTES
from diglibrary.metadata.cache import JsonMetadataCache
from diglibrary.metadata.itunes import WitnessThrottledError
from diglibrary.metadata.spotify import SpotifyError, SpotifyPointer, parse_spotify_link
from diglibrary.providers.models import ProviderOperationResult, ProviderRequest
from diglibrary.quality.analysis import PROBE_FREQUENCIES
from diglibrary.quality.models import Encoding
from diglibrary.quality.spectrogram import DEFAULT_CACHE_LIMIT_BYTES, SpectrogramRenderer
from diglibrary.quality.verdict import REFERENCE_WALLS, is_borderline

FolderPicker = Callable[[], str | None]
ImagePicker = Callable[[], str | None]
"""Ask the platform for one image file, the way it is asked for a folder."""
PathOpener = Callable[[str], None]
# Where the window fetches a cover's bytes. Declared as a shape rather than
# imported, because the thing that serves them belongs to the interface and
# this layer never imports upward — the same arrangement the folder dialog and
# the Finder opener already have, kept by the boundary test.
ImageUrls = Callable[[Path, Path], str | None]

_NAME_PIECES: tuple[dict[str, object], ...] = (
    # The pieces a name can be built from, in the order the composer offers
    # them. `wrap` is what the piece brings with it when it is added — nothing,
    # parentheses, or brackets — so an arrangement can be composed without
    # writing a template: two bare pieces join with " - ", and a wrapped one is
    # preceded by a space.
    {"token": "albumartist", "label": "Artist", "wrap": "", "folder": True, "track": True},
    {"token": "album", "label": "Album", "wrap": "", "folder": True, "track": True},
    {"token": "year", "label": "Year", "wrap": "(", "folder": True, "track": True},
    {
        "token": "format",
        "label": "Format",
        "wrap": "[",
        "folder": True,
        "track": False,
    },
    {"token": "edition", "label": "Pressing year", "wrap": "(", "folder": True, "track": False},
    {"token": "kind", "label": "Designator", "wrap": "(", "folder": True, "track": False},
    {
        "token": "label",
        "label": "Record label",
        "wrap": "(",
        "folder": True,
        "track": False,
    },
    {
        "token": "catalog",
        "label": "Catalogue no.",
        "wrap": "(",
        "folder": True,
        "track": False,
    },
    {
        "token": "country",
        "label": "Country",
        "wrap": "[",
        "folder": True,
        "track": False,
    },
    {"token": "genre", "label": "Genre", "wrap": "[", "folder": True, "track": False},
    {"token": "style", "label": "Style", "wrap": "[", "folder": True, "track": False},
    # Track pieces.
    {"token": "track", "label": "Track no.", "wrap": "", "folder": False, "track": True},
    {"token": "disc", "label": "Disc no.", "wrap": "", "folder": False, "track": True},
    {"token": "title", "label": "Title", "wrap": "", "folder": False, "track": True},
    {"token": "artist", "label": "Track artist", "wrap": "", "folder": False, "track": True},
    {
        "token": "bitdepth",
        "label": "Bit depth",
        "wrap": "[",
        "folder": False,
        "track": True,
    },
    {
        "token": "samplerate",
        "label": "Sample rate",
        "wrap": "[",
        "folder": False,
        "track": True,
    },
    {"token": "bitrate", "label": "Bitrate", "wrap": "[", "folder": False, "track": True},
    {
        "token": "duration",
        "label": "Length",
        "wrap": "[",
        "folder": False,
        "track": True,
    },
    # The measured three. They are the only pieces here that depend on work
    # having been done, and `measured` lets the composer say so rather than
    # letting an arrangement be built that renders nothing on unmeasured files.
    {
        "token": "key",
        "label": "Key (Camelot)",
        "wrap": "",
        "folder": False,
        "track": True,
        "measured": True,
    },
    {
        "token": "keyname",
        "label": "Key (A minor)",
        "wrap": "(",
        "folder": False,
        "track": True,
        "measured": True,
    },
    {
        "token": "bpm",
        "label": "BPM",
        "wrap": "[",
        "folder": False,
        "track": True,
        "measured": True,
    },
)

_PIECE_LABELS: dict[str, str] = {str(piece["token"]): str(piece["label"]) for piece in _NAME_PIECES}
"""One name per piece, so a report about a piece reads like the picker does."""

_PLACEHOLDER_IN_TEMPLATE = re.compile(r"%([a-z]+)%")

_LISTING_HOLDS_FOR = 60.0
"""Seconds a listing of the places albums live stays good enough to reuse.

One restore looks for every album whose folder did not answer, and the listing
is the same for all of them, so taking it once per missing album would make a
restart slow on a large library. Short, because it is only ever a way to *find*
candidates — the audio is read from the folder itself afterwards, so a listing
that has gone stale costs a failed signature check and nothing else.
"""


def _render_shape(template: str) -> str:
    """Draw the arrangement itself, with every piece standing in for its value.

    Nothing optional disappears here — that is the point. The rendered name
    below it shows what this album actually produces, and the difference between
    the two lines is the answer to *why is my year not there*.
    """
    shape = template.replace("{", "").replace("}", "")
    return _PLACEHOLDER_IN_TEMPLATE.sub(
        lambda match: _PIECE_LABELS.get(match.group(1), match.group(1)), shape
    ).strip()


_UI_SETTING_KEYS = (
    "naming_style",
    # The composed arrangement, when one has been composed. It wins over the
    # style — a style is a ready-made template, and these two are the template
    # built out of the same pieces. Empty means none was, and the style answers.
    "folder_template",
    "track_template",
    # The pieces the composer was holding, so reopening it shows what was built
    # rather than a template that has to be read backwards. The templates above
    # are what the engine uses; these are what the screen reopens.
    "folder_parts",
    "track_parts",
    "include_edition",
    "artwork_enabled",
    "threshold",
    "keep_trees_open",
    "ui_density",
    # How much disk the drawn spectrograms may hold. A ceiling rather than
    # forever, and a setting, because the pictures occupy the user's disk.
    "spectrogram_cache_mb",
    # The same ceiling, for the two folders that never had one.
    "metadata_cache_mb",
    "artwork_cache_mb",
    "backup_mb",
    # The ceiling on the copies of the database taken as the window opens.
    "database_copies_mb",
    # Whether the welcome has been seen. One-way: it is set, never unset.
    "welcomed",
    # The order the shelf is drawn in. Kept where every other preference is, so
    # the Library opens the way it was left — and stored as a name rather than
    # as a comparator, because the sorting is the window's and the memory of it
    # is the application's.
    "library_sort",
    # The widths dragged on the tables that have headings, as JSON keyed by
    # table. One string rather than a setting per table: what is stored is a
    # string either way, and a shape this layer does not have to understand is
    # a shape it cannot get half right.
    "column_widths",
)

_RELEASE_LINKS = {
    MetadataSources.DISCOGS: "https://www.discogs.com/release/{id}",
    MetadataSources.MUSICBRAINZ: "https://musicbrainz.org/release/{id}",
}

_TAGS_AFTER = "tags.not_before_unit"

# The name a job carries when it renames or retags files, which is the one kind
# of job a folder read waits for. A job that writes to the user's files and
# starts without it would let a read record paths that are about to move.
_WRITES_FILES = "writes-files"
_READ_WAITS_FOR_WRITES = (
    "Albums are being renamed right now, so that folder was not read. "
    "Add it again when they finish."
)
"""The album row the tag mode starts after, so it is never retroactive.

A setting rather than a column: nothing about an album changes here — what is
recorded is where the library stood when the mode was first used.
"""

_LINK_PATTERNS = (
    (MetadataSources.DISCOGS, re.compile(r"discogs\.com/(?:[^/]+/)?release/(\d+)")),
    (MetadataSources.MUSICBRAINZ, re.compile(r"musicbrainz\.org/release/([0-9a-f-]{36})")),
)

_MASTER_PATTERNS = (
    # Discogs sends a search result to the master page, and MusicBrainz to the
    # release group, so the link a user actually copies is very often not a
    # release at all. Both are followed to a concrete edition rather than
    # refused, because refusing would be correct and useless.
    (MetadataSources.DISCOGS, re.compile(r"discogs\.com/(?:[^/]+/)?master/(\d+)")),
)

_WRITE_KINDS = ("write_tags", "rename_file", "rename_folder", "write_image", "embed_image")

CONNECTABLE = {
    "discogs": credentials.DISCOGS,
    "acoustid": credentials.ACOUSTID,
    "slskd": credentials.SLSKD,
    "spotify_id": credentials.SPOTIFY_ID,
    "spotify_secret": credentials.SPOTIFY_SECRET,
}
"""Every service the window may hand a key to, and the variable each one is.

Out here rather than inside `connect` so that a test can read it. The list of
what the backend accepts and the list of what the window offers are written in
two files by hand, and a service missing from one of them is a tab that does
nothing with no error anywhere — so a test compares the two.
"""

FFMPEG_MISSING = (
    "ffmpeg was not found, so no audio can be measured on this machine. "
    "Install it and reopen DigLibrary."
)
"""Why a measuring gesture cannot run, said in one sentence and in one place.

Every gesture that decodes audio refuses with this, so that no screen gives its
own account of the same absence — one hiding its button silently while another
reports that no audio could be read.
"""


@dataclass
class _AlbumState:
    """Everything the window may ask about one scanned album."""

    unit: AlbumUnit
    unit_id: int
    outcome: IdentificationOutcome
    identification_id: int | None = None
    plan_id: int | None = None
    alternatives: tuple[MatchCandidate, ...] = ()
    applied: bool = False
    # Whether the plan **currently held** has been written, which is not the same
    # question as whether this album was ever applied. With one flag for both,
    # re-planning an organized album produces writes that the screen treats as
    # already done: the names editor hides itself for a plan that has run, and
    # Approve disables itself for the same reason. A re-plan clears this and
    # leaves `applied` standing.
    written: bool = False
    # Whether the plan this album is holding was asked for with `Plan this album
    # again`. An organized album comes back from a restart with names on screen
    # and no such request behind them, and those names must not be editable: they
    # would be typed into a table no catalogue has been asked about since the
    # last session. Only that one gesture counts; the other planning gestures do
    # not. Per session on purpose — the request is a gesture, not a property of
    # the album, so it does not outlive the window.
    planned_again: bool = False
    rejected: bool = False
    held: bool = False
    # Organized by an earlier run and left alone by this scan, until it is asked
    # for by name.
    organized: bool = False
    # The two words this album had before an edit took them away, so that
    # withdrawing the edit gives them back. Without it, typing a name and
    # withdrawing it leaves the album out of `Organized` for the rest of the
    # session, with a plan that has gone back to changing nothing.
    words_it_had: tuple[bool, bool] | None = None
    # Whether anything has been looked up for this album yet. False for one that
    # is on screen because its folder was opened and is waiting to be marked and
    # scanned.
    looked_at: bool = True
    cover_plan: ChangePlan | None = None
    # **Whether the picture was pointed at by the user** or worked out from the
    # album's own files. Two gestures write the same field, and only the first
    # may put a notice on the album's screen saying *a cover you chose is
    # waiting*, with a button to write it. The automatic plan only ever writes
    # the folder, so a notice over it would announce a choice nobody made.
    cover_chosen_by_user: bool = False
    cover_plan_id: int | None = None
    cover_applied: bool = False
    audio_ok: bool | None = None
    writes_ok: bool | None = None


@dataclass
class _LastAction:
    """What one gesture replaced, kept so that `Cancel` can put it back.

    Two gestures on an open album re-ask a catalogue about a record that already
    had an answer: `Plan this album again` and `Scan this album again`. Neither
    touches a file, so neither has anything for `Revert` to undo — and without
    this the only ways out are `Approve & apply`, which writes and discards any
    manual adjustment, and `Reject`, which records the identification as wrong
    and drops the album to `skipped`.

    The album itself is what is kept, not a description of it: `_identify` builds
    a **new** `_AlbumState` and remembers it over the old one, so anything read
    out of the live map after the gesture is already the answer that replaced the
    one being undone. Held outside `_AlbumState` for the same reason.
    """

    gesture: str
    album: _AlbumState
    # `album_units.state` as the database held it before the gesture. An
    # organized album keeps that word through a re-plan and loses it to a
    # re-scan, so the row is read rather than assumed.
    unit_state: str | None
    # The corrections on record when the photograph was taken, so a `Cancel` can
    # tell whether anything was typed **since**. One typed before the gesture is
    # already in the reading being put back and needs no explaining; one typed
    # after it goes out of sight, and that is the only part of this a person
    # could read as their word having been thrown away.
    words: tuple[dict[str, object], ...]


@dataclass
class _Pipeline:
    """The engine pieces the API drives, rebuilt together when settings change."""

    scanner: LibraryScanner
    workflow: IdentificationWorkflow
    executor: ChangeExecutor
    threshold: float
    planner: ChangePlanner | None = None
    artwork: ArtworkService | None = None
    tag_store: TagStore | None = None
    quality: QualitySurvey | None = None
    bench: QualityBench | None = None


@dataclass
class _Search:
    """One open search tab: its own thread, its own rows, its own identifiers.

    A folder identifier means something only inside the search that minted it.
    Global identifiers would let a second search renumber the first's rows
    underneath it.
    """

    identifier: str
    query: str
    kind: str
    job: threading.Thread | None = None
    closed: bool = False
    request_id: str | None = None
    folders: dict[str, object] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _result: dict[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Start out as a tab that is searching and has found nothing yet."""
        self._result = {
            "id": self.identifier,
            "query": self.query,
            "searching": True,
            "folders": [],
            "sources": 0,
            "files": 0,
        }

    @property
    def result(self) -> dict[str, object]:
        """Return a snapshot the window can have, taken under the lock.

        The worker thread writes this while the window's poll reads it, and a
        dictionary being written to is not one that can be safely walked.
        """
        with self._lock:
            return dict(self._result)

    def publish(self, values: dict[str, object]) -> None:
        """Merge what has just been learned into what the window will read next."""
        with self._lock:
            self._result.update(values)


class _TabWatch:
    """Report a search's progress into its tab, and say when the tab has gone.

    The connector calls both of these from its own thread, so neither does
    anything but touch this tab's own state.
    """

    def __init__(self, search: _Search) -> None:
        """Watch on behalf of one open tab."""
        self._search = search

    def observed(self, progress: SearchProgress) -> None:
        """Show the count as it grows, instead of a mute wait of several seconds."""
        self._search.publish({"sources": progress.sources, "files": progress.files})

    def abandoned(self) -> bool:
        """Answer whether the tab this belongs to has been closed."""
        return self._search.closed


class LibraryApi:
    """Purpose: expose the whole engine to the window as gestures and reports.

    Responsibilities: run scans on a worker thread, keep the per-album state
    the screens render, persist every decision through the library store, and
    translate results into JSON-safe dictionaries. Boundaries: it owns no
    business rule — identification, planning, and execution belong to the
    engines it drives — and it never imports the interface layer.
    Dependencies: a pipeline factory, the store, an artwork store, and an
    injected folder picker. Collaborators: the window's JavaScript, through
    pywebview's bridge. Constraints: every public method returns JSON-safe
    data, because everything it returns crosses the bridge.
    """

    def __init__(
        self,
        pipeline_factory: Callable[[dict[str, object]], _Pipeline],
        store: LibraryStore,
        artwork_store: ArtworkStore,
        logger: logging.Logger,
        folder_picker: FolderPicker | None = None,
        image_picker: ImagePicker | None = None,
        cover_cache: Path | None = None,
        path_opener: PathOpener | None = None,
        icon_maker: Callable[[], dict[str, object]] | None = None,
        acquisition: object | None = None,
        keep_awake: Callable[[bool], None] | None = None,
        spectrograms: SpectrogramRenderer | None = None,
        metadata_cache: JsonMetadataCache | None = None,
        artwork_cache: tuple[Path, ...] = (),
        backups: tuple[Path, ...] = (),
        spotify: SpotifyPointer | None = None,
        database: Path | None = None,
        database_copies: Path | None = None,
        harmonic_analyzer: HarmonicAnalyzer | None = None,
        images: ImageUrls | None = None,
        ffmpeg: bool = False,
    ) -> None:
        """Create the API from an assembled engine and the store that remembers."""
        self._pipeline_factory = pipeline_factory
        self._store = store
        self._artwork_store = artwork_store
        self._logger = logger
        self._folder_picker = folder_picker
        self._image_picker = image_picker
        self._cover_cache = cover_cache
        # Absent when this machine has no ffmpeg, and the window is told so
        # rather than shown an empty frame.
        self._spectrograms = spectrograms
        # The two folders that would otherwise grow without a ceiling: the
        # catalogue answers, and the staged pictures with the sleeves the window
        # draws. Both are made again on demand; neither is a fact that would be
        # lost.
        self._metadata_cache = metadata_cache
        self._artwork_cache = artwork_cache
        # The replaced images. Bounded, never emptied by a button: discarding
        # one costs the ability to revert a run, not disk time.
        self._backups = backups
        # The database itself, and where its copies go. Both or neither: a
        # folder with nothing to copy into it is a folder that would quietly
        # never hold a copy.
        self._database = database
        self._database_copies = database_copies
        # Key and tempo. Absent when this machine has no ffmpeg, and the screen
        # says so rather than offering a button that does nothing.
        self._harmonic_analyzer = harmonic_analyzer
        # Whether audio can be measured on this machine at all — one fact, and
        # the same one for every screen that asks. Worked out separately by each
        # screen, from the survey on one and from the analyzer on the other, each
        # could only speak for its own half, and one of them blamed the audio for
        # what the missing binary had done. Resolved once in composition, beside
        # the objects built from it.
        self._ffmpeg = ffmpeg
        self._images = images
        # Wrapped once, here, because ten gestures reach the Finder, and a
        # window that opens *successfully* at the wrong folder raises nothing
        # and shows no toast. The wrapper logs which gesture opened which path.
        self._path_opener = _opener_that_says_so(path_opener, logger)
        self._icon_maker = icon_maker
        # Whether this machine may doze off. Injected, because asking a Mac to
        # stay awake is the platform's business and not this layer's; a build
        # that passes nothing simply never asks.
        self._keep_awake = keep_awake or (lambda _wanted: None)
        # Set when the application is going away, so the sleep watch stops
        # waiting rather than being killed mid-question.
        self._closing = threading.Event()
        # The acquisition provider, when this build composed one. Absent is an
        # ordinary state — the window shows the area as unavailable and says so.
        self._acquisition = acquisition
        # Reads a pasted Spotify link as words and nothing else. It is not a
        # metadata source and is never asked to be one: no value it returns is
        # stored, compared, or written.
        self._spotify = spotify
        # One entry per open search tab. With single fields, a second search
        # would overwrite the first's folder identifiers, and a download started
        # from the older tab would fetch an album from the newer one.
        self._searches: dict[str, _Search] = {}
        self._acquisition_lock = threading.Lock()
        self._searches_opened = 0
        self._collect_job: threading.Thread | None = None
        # Albums that arrived by download rather than by a folder being pointed
        # at. Both origins persist, so this is the in-memory face of the unit's
        # recorded `origin` rather than the reason one of them survives.
        self._downloaded: set[int] = set()
        # Folders that were seen to be short and taken anyway. Held for the
        # session rather than written down: it is an answer about one folder at
        # one moment, the collect poll picks it up within seconds, and a stored
        # flag would go on overruling the completeness rule for a download whose
        # acceptance is long past.
        self._accepted_short: set[str] = set()
        self._settings = self._load_settings()
        # Where the library stood the first time this build opened it. Read
        # here, in the constructor, and nowhere else: the tag mode must not be
        # retroactive, so albums already in the library are left untouched — and
        # asking this question later would be asking it after a dropped folder
        # had already been recorded, which would put the new album on the wrong
        # side of its own watermark.
        self._tags_after = self._watermark()
        self._pipeline = pipeline_factory(self._settings)
        self._albums: dict[int, _AlbumState] = {}
        # The shelf as the database already holds it, drawn before a single file
        # is read. Every album is on screen at once, and an album is read back
        # from disk at the moment it is touched. A row leaves this map the
        # instant the real one arrives, so the two can never both answer for one
        # album.
        self._shelf: dict[int, dict[str, object]] = {}
        # The scan runs in its own thread and fills this map while the window
        # polls `state()` from another. Reading one album by id is safe on its
        # own, but walking the map is not: an insert partway through raises
        # "dictionary changed size during iteration" and the window's poll dies
        # mid-scan. Every walk takes a snapshot under this lock.
        self._albums_lock = threading.Lock()
        # What the last undoable gesture on one album replaced, by album id. At
        # most one per album: `Cancel` says *before the last thing you asked
        # for*, and a stack of them would be offering a history this screen does
        # not draw. An apply drops the entry, because a gesture that has reached
        # the files is `Revert`'s to undo and not this one's.
        self._undoable: dict[int, _LastAction] = {}
        # The places albums live, weighed, so that an album whose folder does
        # not answer can be looked for by what is inside it. Held for a moment
        # because one restore asks for it once per missing album.
        self._folder_sizes: tuple[float, dict[Path, tuple[int, ...]]] | None = None
        # Which albums are out with the witness right now, so that opening the
        # same dialog twice does not spend two requests of a limited allowance.
        self._asking: set[int] = set()
        self._asking_lock = threading.Lock()
        self._events: list[dict[str, object]] = []
        self._events_lock = threading.Lock()
        # One apply at a time, across every gesture that starts one. The
        # executor refuses to overwrite an existing path, and that refusal is a
        # check followed by a rename with nothing between them: two applies
        # running together — a double-click on `Approve & apply`, or
        # `Apply N automatic` pressed while one is still going — can have a
        # destination appear in that gap, and `Path.rename` replaces silently.
        # The window is milliseconds wide and the cost is a file with no trail
        # entry, which is the outcome this project rates worst. Applies are
        # gestured and rare, so serialising them costs nothing anyone can feel.
        self._apply_lock = threading.RLock()
        self._job: threading.Thread | None = None
        # **Reading a folder has its own worker.** A folder that is opened or
        # dropped is read off the disk and nothing is asked of any catalogue, so
        # it does not wait for a scan. Reads queue behind each other on
        # `_read_lock`, so two drops in a row are read in order instead of the
        # second being refused.
        self._reads: list[threading.Thread] = []
        self._read_lock = threading.Lock()
        # The folders a run is identifying right now, counted because two runs
        # can hold one folder at once (a collect and a scan). A read leaves these
        # albums to the run: its answer is the newer one.
        self._identifying_folders: Counter[str] = Counter()
        self._search_job: threading.Thread | None = None
        self._root: str | None = None
        self._reach: dict[str, object] | None = None
        self._quality: list[dict[str, object]] = []
        self._quality_job: threading.Thread | None = None
        # The bench reads folders on a thread of its own: a walk of a large
        # library takes long enough that a window which stops answering for it
        # cannot be told from a window that died.
        self._bench_job: threading.Thread | None = None
        self._harmonics_job: threading.Thread | None = None
        # A finished deep measurement that has not been written down. It waits
        # here to be accepted, exactly like a plan waiting for `Approve &
        # apply`, and it expires with the session.
        self._bench_analysis: DeepAnalysis | None = None
        # What the last scan measured as transcoded, per album and kept, so that
        # re-planning one album later still names it for what its audio is.
        # Keyed by unit signature: one album's verdict is not a fact about a
        # file that also lives somewhere else.
        self._transcoded: dict[str, frozenset[str]] = {}
        # What the measurement alone says, before the user's word is applied to
        # it. Kept apart so a control can tell "the app marked this" from "you
        # marked this", and withdraw a word instead of stacking another.
        self._measured: dict[str, frozenset[str]] = {}
        # What a lossy-origin file's audio measured, by signature, and ONLY
        # where that measurement is provably that file's. A number nobody can
        # attribute never gets in here, so nothing downstream has to remember to
        # check before writing it into a name.
        self._rates: dict[str, int] = {}
        # The Library is read back on a thread, because a window must open now
        # and reading a shelf of album folders takes a moment. Held rather than
        # started and forgotten: something has to be able to wait for it, and a
        # thread nobody can join is a race nobody can test.
        # Set the instant the restore has *reported*, which is not the same
        # moment the thread stops existing. Asking whether the thread is alive
        # leaves the `Reading your library…` banner up for ever: the event
        # arrives, the window clears the banner and asks for a fresh snapshot,
        # the snapshot still finds the thread alive and turns the flag back on,
        # and the one event that could turn it off has already been drained.
        self._restore_reported = threading.Event()
        # Drawn before the thread starts, so the window's very first `state()`
        # already has the whole library in it — whatever the thread is doing.
        self._draw_the_shelf()
        self._restore_job = threading.Thread(target=self._restore_library, daemon=True)
        self._restore_job.start()

    def _draw_the_shelf(self) -> None:
        """Fill the light shelf from the database's own columns.

        **A folder that does not answer is drawn as missing, not left out.**
        Omitting it lets the restore behind it clear the row for good, and an
        album disappears from the Library while it is still on the disk.

        A folder that does not answer at this instant is not an album that is
        gone. An external disk is unmounted, a cloud folder has not synced, a
        rename happened in the Finder. Every album the Library has shown
        survives the session, and this is one of the places that keeps it true.

        `is_dir` is cheap enough to ask for every row of a large library.
        """
        try:
            rows = self._store.shelf()
        except Exception:
            # A shelf that cannot be drawn is not a window that cannot open. The
            # restore behind it will fill the map the long way.
            self._logger.exception(
                "The Library could not be drawn from the database.",
                extra={"operation": "api.shelf.failure"},
            )
            return
        # Every row, including one whose folder does not answer — it is drawn
        # wearing that fact rather than left out. Whether it answers is asked
        # where the row is turned into a card, so there is one truth about it.
        self._shelf = {int(row["unit_id"]): row for row in rows}

    # --- acquisition -------------------------------------------------------

    def acquisition_state(self) -> dict[str, object]:
        """Report whether music can be acquired at all, and why not when it cannot.

        An absent or unreachable provider is an ordinary state that the window
        must be able to explain: acquisition runs through a slskd server the
        user runs, and nothing else, and that server may simply be switched
        off.

        **The reason is the provider's, and this hands it over unread.**
        Answering `unreachable` for every exception is a claim about the
        network made without measuring one — a running slskd with no API key
        would be reported as a server that had stopped answering. Credential
        failures are states, not exceptions, so what is left here is genuinely
        unexpected and says so: `error` carries the failure's own text rather
        than a diagnosis this method cannot make.
        """
        if self._acquisition is None:
            return {"available": False, "reason": "no_provider"}
        try:
            health = self._acquisition.health()
        except Exception as error:
            self._logger.exception(
                "Asking the acquisition provider for its state failed.",
                extra={"operation": "api.acquisition.state"},
            )
            return {
                "available": False,
                "reason": "error",
                "detail": f"{type(error).__name__}: {error}",
            }
        available = health.status.value == "ready"
        return {
            "available": available,
            "reason": health.status.value,
            "detail": health.message,
        }

    def acquisition_search(self, query: str, kind: str = "album") -> dict[str, object]:
        """Open one search on its own worker thread, and answer with its identifier.

        A Soulseek search is not a request with an answer; it is a question the
        network replies to for as long as it cares to. Blocking the window on
        that would freeze it for the whole wait — and several may now be running
        at once, one per tab, so each carries its own everything.
        """
        if self._acquisition is None:
            return {"ok": False, "error": "No acquisition provider is composed."}
        if not query.strip():
            return {"ok": False, "error": "A search needs something to look for."}
        # A Spotify link copied from the player can be dropped here. What
        # crosses is words — the same words that could have been typed — and
        # the link itself is not kept anywhere.
        seeded: dict[str, object] = {}
        link = parse_spotify_link(query)
        if link is not None:
            if self._spotify is None:
                return {"ok": False, "error": "This build cannot read Spotify links."}
            try:
                words = self._spotify.words_for(link)
            except SpotifyError as error:
                return {"ok": False, "error": str(error)}
            query = words.as_query()
            seeded = {"seeded_from": "spotify", "exact": words.exact}
            self._logger.info(
                "A Spotify link was read as words and searched for.",
                extra={"operation": "api.acquisition.seeded", "exact": words.exact},
            )
        with self._acquisition_lock:
            self._searches_opened += 1
            identifier = f"s{self._searches_opened}"
            search = _Search(identifier=identifier, query=query, kind=kind)
            self._searches[identifier] = search
        search.job = threading.Thread(
            target=self._run_acquisition_search, args=(search,), daemon=True
        )
        search.job.start()
        return {"ok": True, "id": identifier, "query": query, **seeded}

    def acquisition_results(self, search_id: str) -> dict[str, object]:
        """Return what one search has gathered, running or finished.

        A search the window does not know about any more is not an error: a tab
        can be closed while its poll is in flight, and the poll arriving after
        is the ordinary shape of that, not a fault to report.
        """
        with self._acquisition_lock:
            search = self._searches.get(search_id)
            if search is None:
                return {"searching": False, "closed": True, "folders": []}
            return dict(search.result)

    def acquisition_close(self, search_id: str) -> dict[str, object]:
        """Close one search: stop it if it is running, and drop it from the service.

        Closing is not only forgetting. A search still gathering is stopped at
        the service, because a tab that is gone should stop costing the network
        something, and the record is dropped so that searches do not accumulate
        in the slskd server.
        """
        with self._acquisition_lock:
            search = self._searches.pop(search_id, None)
        if search is None:
            return {"ok": True}
        # The running thread reads this and abandons the search itself, which is
        # the only place that knows the service's identifier for it while it is
        # still in flight.
        search.closed = True
        request_id = search.request_id
        if request_id and self._acquisition is not None:
            try:
                self._acquisition.forget(request_id)
            except Exception:
                self._logger.warning(
                    "The finished search could not be dropped from the service.",
                    extra={"operation": "api.acquisition.close"},
                )
        return {"ok": True}

    def acquisition_folder(self, search_id: str, folder_id: str) -> dict[str, object]:
        """Read one folder in full from the source that offered it.

        A search answers with the files that matched the words; the folder holds
        the rest of the album, the cover and the log. Reading it replaces the
        row's file list with the real one, so what is shown is the folder and
        not the query's shadow of it.

        A source that has gone offline cannot be asked, and that is an ordinary
        outcome rather than a failure: the window keeps the matched files and
        says the folder could not be read.
        """
        if self._acquisition is None:
            return {"ok": False, "error": "No acquisition provider is composed."}
        with self._acquisition_lock:
            search = self._searches.get(search_id)
            folder = search.folders.get(folder_id) if search else None
        if folder is None or search is None:
            return {"ok": False, "error": "That result is no longer on screen; search again."}
        try:
            files = self._acquisition.folder_contents(folder.source, folder.directory)
        except Exception as error:
            self._logger.info(
                "A folder could not be read from its source.",
                extra={"operation": "api.acquisition.folder"},
            )
            return {"ok": False, "error": str(error), "offline": True}
        if not files:
            return {"ok": False, "error": "That source offered nothing in this folder."}
        # The row keeps its identity and gains its real contents, so a download
        # started from it afterwards asks for the whole folder rather than for
        # the handful of names the query happened to match.
        filled = replace(folder, files=files)
        with self._acquisition_lock:
            if search.folders.get(folder_id) is folder:
                search.folders[folder_id] = filled
        return {
            "ok": True,
            "files": filled.file_count,
            "size": filled.total_size_bytes,
            "tracks": [
                {
                    "name": file.basename,
                    "size": file.size_bytes or 0,
                    "attributes": _attributes(file),
                }
                for file in filled.files
            ],
        }

    def acquisition_download(
        self, search_id: str, folder_id: str, names: list[str] | None = None
    ) -> dict[str, object]:
        """Fetch a folder, or only the files named within it.

        ``names`` empty or absent means the whole folder, which is the usual
        wish. Naming files fetches exactly those and nothing else travels with
        them — one track is a legitimate thing to want.

        The folder is looked up inside its own search. With global identifiers,
        opening a second search would renumber the first one's rows and a
        download from the older tab would fetch a different album.
        """
        if self._acquisition is None:
            return {"ok": False, "error": "No acquisition provider is composed."}
        with self._acquisition_lock:
            search = self._searches.get(search_id)
            folder = search.folders.get(folder_id) if search else None
        if folder is None:
            return {"ok": False, "error": "That result is no longer on screen; search again."}
        wanted = set(names or ())
        files = tuple(
            file for file in folder.transfer_files() if not wanted or file.basename in wanted
        )
        if not files:
            return {"ok": False, "error": "None of those files are in this folder."}
        try:
            result = self._acquisition.download(ProviderRequest(folder.source, {"files": files}))
        except Exception as error:
            self._logger.exception(
                "The download could not be requested.",
                extra={"operation": "api.acquisition.download"},
            )
            return {"ok": False, "error": str(error)}
        if result.successful:
            # History answered only for what this app renamed, which is half of
            # what happens to a folder: the other half is that it arrived.
            self._store.record_download(
                source=folder.source,
                directory=folder.directory,
                folder=folder.name or folder.directory,
                files=len(files),
                size_bytes=sum(file.size_bytes or 0 for file in files),
                whole_folder=not wanted,
            )
        # What the service actually took, and not only what was asked of it. The
        # two are not the same number: a service may queue one file of a folder
        # of many, and reporting the number requested would announce files that
        # never arrive. The provider counts what was queued, and that count is
        # passed on.
        transfers = result.values.get("transfers")
        queued = len(transfers) if isinstance(transfers, Sequence) else None
        if queued is not None and queued < len(files):
            self._logger.warning(
                "The service took fewer files than were asked of it.",
                extra={
                    "operation": "api.acquisition.download",
                    "requested": len(files),
                    "queued": queued,
                },
            )
        return {
            "ok": result.successful,
            "requested": len(files),
            "queued": queued,
            "message": result.explanation,
        }

    def forget_download(self, download_id: int) -> dict[str, object]:
        """Stop waiting for one download, without touching a file.

        `acquisition_clear_downloads` only ever reaches the ones that arrived,
        so a request whose files never came could not be dismissed by anything
        and would stay pending for the life of the database. One row, named
        from its own menu — nothing here sweeps, and nothing is deleted: the
        row stays and History still answers for what was asked for.
        """
        if not self._store.clear_download(download_id):
            return {"ok": False, "error": "That download is not waiting for anything."}
        self._logger.info(
            "A download was taken off the waiting list.",
            extra={"operation": "acquisition.download.forgotten", "download_id": download_id},
        )
        return {"ok": True}

    def open_download(self, download_id: int) -> dict[str, object]:
        """Open the folder one download landed in, as the service names it.

        Where downloads land belongs to the acquisition service, so it is asked
        rather than remembered — a copy kept here would drift the moment the
        folder was moved. The service creates a folder named after the remote
        one's last segment; when that folder is not there yet, because nothing
        has finished arriving, the downloads folder itself is opened rather
        than nothing.
        """
        if self._path_opener is None:
            return {"ok": False, "error": "No opener is available."}
        if self._acquisition is None:
            return {"ok": False, "error": "No acquisition provider is composed."}
        record = next(
            (
                entry
                for entry in self._store.download_history()
                if entry["download_id"] == download_id
            ),
            None,
        )
        if record is None:
            return {"ok": False, "error": "That download is not in the history."}
        root, refusal = self._download_root()
        if root is None:
            return refusal
        landed = _landed_folder(root, record["directory"])
        # A name that is not a leaf opens the download folder itself rather than
        # wherever it pointed, and the folder is what was asked for anyway.
        opened = landed if landed is not None and landed.is_dir() else root
        self._path_opener(str(opened))
        return {"ok": True, "path": str(opened), "arrived": landed.is_dir()}

    def _download_root(self) -> tuple[Path | None, dict[str, object]]:
        """Where downloads land on *this* machine, or why that cannot be said.

        **The service's path is the service's, not this machine's.** slskd
        answers with the folder *it* writes to, which may be relative to its own
        working directory or inside a container. A relative one resolves against
        this process's directory, so "Open folder" would open the application's
        own folder.

        **One function, so the guard cannot be on one opener and missing from
        another.** Every path that turns the service's answer into a folder on
        this disk comes through here.
        """
        if self._acquisition is None:
            return None, {"ok": False, "error": "No acquisition provider is composed."}
        try:
            destination = self._acquisition.download_directory()
        except Exception as error:
            return None, {"ok": False, "error": str(error)}
        if not destination:
            return None, {
                "ok": False,
                "error": "The acquisition service did not say where it downloads.",
            }
        root = _a_folder_on_this_disk(destination)
        if root is None:
            return None, {
                "ok": False,
                "error": (
                    f"Your slskd reports its download folder as “{destination}”, which is not "
                    "a folder on this machine — a path relative to wherever slskd runs, or one "
                    "inside a container. Set an absolute download folder in slskd."
                ),
            }
        return root, {}

    def acquisition_transfers(self, source: str | None = None) -> dict[str, object]:
        """Return where transfers have got to, grouped by source and folder.

        Without a source it asks for everything, which is what the Transfers
        screen needs: it cannot name the sources in advance, because asking is
        how it finds out which they are.
        """
        if self._acquisition is None:
            return {"ok": False, "folders": []}
        try:
            transfers = self._acquisition.progress(source)
        except Exception as error:
            return {"ok": False, "error": str(error), "folders": []}
        grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
        for transfer in transfers:
            grouped.setdefault((transfer.source, transfer.directory), []).append(
                {
                    # The service's own handle, which is the only way to name
                    # this transfer again when stopping or resuming it.
                    "id": transfer.identifier,
                    "name": transfer.basename,
                    # The remote name in full, because asking for this file
                    # again means asking for it the way its source spells it.
                    "remote": transfer.name,
                    "state": transfer.state.value,
                    "size": transfer.size_bytes or 0,
                    "moved": transfer.transferred_bytes or 0,
                    "at": transfer.requested_at or "",
                    # What the service measures, passed on as measured. This
                    # application computes no speed and no estimate of its own:
                    # it is not the one moving the bytes, and a second opinion
                    # about them would only ever be a worse one.
                    "rate": transfer.average_speed or 0,
                    "eta": transfer.remaining_time or "",
                    "place": transfer.place_in_queue,
                }
            )
        folders = [
            {
                "source": identifier,
                "folder": directory.rsplit("\\", 1)[-1],
                # Kept whole as well as trimmed: asking for the folder again
                # needs the path its source published, not its last segment.
                "directory": directory,
                "files": files,
                "size": sum(int(f["size"]) for f in files),
                "moved": sum(int(f["moved"]) for f in files),
                "done": all(TransferState(f["state"]).has_stopped for f in files),
                # Stopped, and not all of it here. The folder stays on this
                # screen saying so rather than joining the Library short, and
                # `Take it as it is` is the way to overrule that.
                "stopped_short": _stopped_short(files),
                # A folder is as recent as the first thing asked for in it.
                "at": min((str(f["at"]) for f in files if f["at"]), default=""),
                # An album moves as fast as whatever of it is moving now.
                "rate": sum(float(f["rate"] or 0) for f in files if f["state"] == "in_progress"),
                # **How many bytes of this folder have still to arrive**, so the
                # screen can say when the folder ends rather than when one file
                # of it does. Counted here because `is_moving` lives here: the
                # window has no copy of which states are ends, and a second copy
                # of that set would drift from the first (`models.py`).
                #
                # Written as the complement — everything that has not stopped —
                # so a slskd wording this vocabulary has never met is counted as
                # still coming rather than silently as nothing.
                "still_coming": sum(
                    max(int(f["size"]) - int(f["moved"]), 0)
                    for f in files
                    if TransferState(f["state"]).is_moving
                ),
            }
            for (identifier, directory), files in grouped.items()
        ]
        # Newest first: the thing just asked for is the thing being watched. A
        # folder whose service did not say when goes last rather than being
        # sorted as though it were the oldest thing here — the empty string is
        # not a time.
        dated = sorted((f for f in folders if f["at"]), key=lambda f: str(f["at"]), reverse=True)
        undated = [f for f in folders if not f["at"]]
        # Inside a folder the order is the album's, which is its file names
        # read with their numbers as numbers. Sorted as text, `010` slots
        # between `01` and `02` — a space orders before a digit — so a rip
        # numbered past nine would list its tenth track second.
        for folder in folders:
            tracks = folder["files"]
            assert isinstance(tracks, list)
            tracks.sort(key=lambda track: _numbered_name(str(track["name"])))
        ordered = [*dated, *undated]
        return {"ok": True, "folders": ordered, "users": _by_user(ordered)}

    def accept_incomplete_download(self, directory: str) -> dict[str, object]:
        """Take a download that stopped short, on request, exactly as it stands.

        A peer that goes away leaves a folder that will never be whole, and
        refusing it for ever is not this application's decision to make. What
        this does *not* do is pretend: the folder joins the Library with the
        files that arrived, and the ones that were cut off arrive cut off —
        `Retry` is what fetches the rest, and it is offered on the same menu.

        The answer lasts the session. The collect poll reaches it within
        seconds, and a stored flag would go on overruling the completeness rule
        for a download whose acceptance is long past.
        """
        if not directory:
            return {"ok": False, "error": "That transfer has no folder to take."}
        self._accepted_short.add(directory)
        self._logger.info(
            "A download that stopped short was taken as it stands, on request.",
            extra={"operation": "api.acquisition.accepted_short", "directory": directory},
        )
        return {"ok": True}

    def acquisition_open_transfer(
        self, directory: str, source: str | None = None
    ) -> dict[str, object]:
        """Reveal on disk the album folder one transfer belongs to.

        **The album's own folder, not the folder above it.** Guessing the leaf
        under the download root is right only while nothing has happened to the
        album, and something usually has: organising renames it, which is the
        whole point of this application. The guess then misses, and opening the
        downloads folder instead is not what "open this album" means.

        So the recorded landing is asked first. It is kept true across a
        rename, which makes it the only thing here that knows where the album
        actually is. The guess is the fallback, and the downloads folder is the
        fallback to that, for an album that has genuinely not arrived yet.
        """
        if self._acquisition is None:
            return {"ok": False, "error": "No acquisition provider is composed."}
        if self._path_opener is None:
            return {"ok": False, "error": "This build cannot open folders."}
        landed = self._recorded_landing(directory, source)
        if landed is not None:
            self._path_opener(str(landed))
            return {"ok": True, "path": str(landed), "arrived": True}
        try:
            destination = self._acquisition.download_directory()
        except Exception as error:
            return {"ok": False, "error": str(error)}
        if not destination:
            return {"ok": False, "error": "The acquisition service did not say where it downloads."}
        root, refusal = self._download_root()
        if root is None:
            return refusal
        landed = _landed_folder(root, directory)
        if landed is not None and landed.is_dir():
            self._path_opener(str(landed))
            return {"ok": True, "path": str(landed), "arrived": True}
        # Nothing has arrived yet, so the folder it will appear in is opened —
        # which is what "where is this going" asks. It is said out loud, because
        # an empty folder opening in the Finder is indistinguishable from an
        # album that downloaded into nothing.
        self._path_opener(str(root))
        return {
            "ok": True,
            "path": str(root),
            "arrived": False,
            "message": (
                "Nothing of this album has arrived yet. This is the folder it will appear in."
            ),
        }

    def _recorded_landing(self, directory: str, source: str | None) -> Path | None:
        """Where this download actually is, if this application wrote it down.

        Matched on the remote folder the source published, which is the one name
        that survives everything done to the album afterwards. The source
        narrows it when two peers offered folders of the same name.
        """
        for record in self._store.download_history():
            if str(record["directory"]) != directory:
                continue
            if source is not None and str(record["source"]) != source:
                continue
            # **`str(Path(""))` is `"."`, not `""`.** An empty string does not
            # stay empty through `Path`, so a download whose landing has not
            # been written down yet — every one that has not been collected —
            # would answer with this process's own directory, and `Open folder`
            # would open that. The helper refuses an empty value before it
            # becomes a path.
            landed = _a_folder_on_this_disk(record["landed_path"])
            if landed is not None:
                return landed
        return None

    def close(self) -> None:
        """Say that the application is going away, so its watches stop waiting."""
        self._closing.set()

    def acquisition_watch_sleep(self) -> None:
        """Follow what is moving: bring in what arrived, hold the machine awake,
        and resume what stopped.

        A machine that sleeps while an album is coming in wakes to find every
        transfer failed — the network stopped existing under a download that
        had no way to know.

        So while anything is queued or moving, the machine is asked to stay
        awake. And whatever failed while nobody was watching is asked for again
        once, at the start, because the partial files are still on disk and
        asking is how this network resumes.

        **And an album that has finished arriving joins the Library by itself.**
        A collect asked for only by the Transfers screen runs only while that
        tab is in front, so a download finishing behind any other tab would sit
        complete on disk and uncollected.

        Here rather than on a timer of its own, because this thread already asks
        the service what is moving — the answer that says something finished is
        the one already in hand.

        Run on its own thread and deliberately slow: this is a question about
        whether the machine may doze, not a progress bar.
        """
        if self._acquisition is None:
            return
        self._resume_what_stopped()
        while not self._closing.wait(_SLEEP_WATCH_SECONDS):
            try:
                transfers = self._acquisition.progress()
            except Exception:
                # The service being unreachable is not a reason to keep a
                # machine awake, and it is not worth a line every thirty
                # seconds either.
                self._keep_awake(False)
                continue
            self._keep_awake(any(t.state.is_moving for t in transfers))
            # Cheap when there is nothing waiting: it is one query against the
            # download history and returns. Every refusal it can give — a scan
            # running, the Library still being restored, a collect already under
            # way — is its own, and it answers them before touching a folder.
            self.acquisition_collect()

    def _resume_what_stopped(self) -> None:
        """Ask again, once, for whatever was left failed — usually by a sleep.

        Only failures. A cancelled transfer is one that was stopped on purpose,
        and asking for it again would be this application overruling that.
        """
        try:
            transfers = self._acquisition.progress()
        except Exception as error:
            # Not on screen: this runs before the window is up and again every
            # thirty seconds, and a server that is merely switched off would
            # then shout once a minute forever. The log is the right place for
            # something that repeats on a timer, and the Transfers screen says
            # the same thing plainly when it is opened.
            self._logger.info(
                "What had stopped could not be asked about.",
                extra={"operation": "api.acquisition.resume", "error": str(error)},
            )
            return
        stopped: dict[str, list[str]] = {}
        for transfer in transfers:
            if transfer.state.value == "failed":
                stopped.setdefault(transfer.source, []).append(transfer.name)
        for source, names in stopped.items():
            result = self.acquisition_resume(source, names)
            self._logger.info(
                (
                    "Asked again for what had failed."
                    if result.get("ok")
                    else "What had failed could not be asked for again."
                ),
                extra={
                    "operation": "api.acquisition.resume",
                    "source": source,
                    "files": len(names),
                },
            )
        if stopped:
            self._emit("resumed", {"files": sum(len(n) for n in stopped.values())})

    def acquisition_stop(
        self, source: str, identifiers: list[str], remove: bool = False
    ) -> dict[str, object]:
        """Stop these transfers, keeping what has already been written to disk.

        This is both *pause* and *cancel*, because on this network they are the
        same act with different intentions. Soulseek has no pause: a transfer is
        running or it is not, and what makes stopping reversible is that the
        service keeps the partial file — resuming asks for it again and it
        carries on from what is already there.

        So the difference is only what is left behind. ``remove`` false keeps the
        row, which is a pause: it stays on screen and can be resumed from where
        it stopped. ``remove`` true takes the row away, which is a cancel. In
        neither case is anything deleted from disk by this application.
        """
        if self._acquisition is None:
            return {"ok": False, "error": "No acquisition provider is composed."}
        try:
            stopped = self._acquisition.cancel(source, tuple(identifiers), remove)
        except Exception as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "stopped": stopped}

    def acquisition_resume(self, source: str, names: list[str]) -> dict[str, object]:
        """Ask for these files again, which is how this network resumes.

        There is nothing else to ask: the service was told to stop and forgot
        the transfer, but not the bytes. Requesting the same file from the same
        source picks up the partial one already on disk.

        It is also what answers a failure, and failures here are ordinary —
        a peer going offline, a queue expiring, and a laptop that slept.
        """
        if self._acquisition is None:
            return {"ok": False, "error": "No acquisition provider is composed."}
        wanted = tuple(TransferFile(name) for name in names if name)
        if not wanted:
            return {"ok": False, "error": "There is nothing to ask for again."}
        result = self._acquisition.download(ProviderRequest(source, {"files": wanted}))
        if not result.successful:
            return {"ok": False, "error": result.summary}
        return {"ok": True, "requested": len(wanted)}

    def acquisition_clear(self) -> dict[str, object]:
        """Clear the finished transfers from the acquisition service's list.

        Nothing on disk moves. The files this cleared are wherever the service
        put them, which is the folder "Organize what I downloaded" points at.
        """
        if self._acquisition is None:
            return {"ok": False, "error": "No acquisition provider is composed."}
        try:
            self._acquisition.clear_finished()
        except Exception as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True}

    def acquisition_collect(self) -> dict[str, object]:
        """Bring every finished download into the library, and leave it there.

        A downloaded album is the one folder in this library whose arrival this
        application already knows about, so requiring a scan to find it would
        be asking to be told something it was told. It joins by itself once it
        has finished arriving, and stays across sessions.

        Finished means the service says so — every transfer of that folder in a
        terminal state — or, when the service's list has been cleared and can no
        longer say, that the folder is on disk with audio in it.
        """
        if self._acquisition is None:
            return {"ok": False, "collected": 0}
        if self._collect_job is not None and self._collect_job.is_alive():
            return {"ok": True, "collected": 0, "busy": True}
        if self._restore_job is not None and self._restore_job.is_alive():
            # The restore fills the same map this reads twice, and it is not
            # `self._job` — so the guard below, which covers the scan, does not
            # cover it. A collect that begins during the restore would claim
            # albums the restore is still bringing in.
            return {"ok": True, "collected": 0, "busy": True}
        # This starts a scan, and every entry point that starts one refuses
        # while a scan is running — `scan`, `open_folder`, `scan_selected`,
        # `adopt_folders` all check `self._job`. Without the same check here a
        # collect could identify albums concurrently with a scan or with the
        # library being restored. Two identify passes
        # also rebind `self._transcoded`, which is what a later re-plan reads to
        # decide whether a folder is named [FLAC] or [Lossy] — so the wrong word
        # could reach a folder name on disk.
        if self._job is not None and self._job.is_alive():
            return {"ok": True, "collected": 0, "busy": True}
        waiting = [
            record
            for record in self._store.download_history()
            if not record["collected_at"] and not record["cleared_at"]
        ]
        if not waiting:
            return {"ok": True, "collected": 0}
        self._collect_job = threading.Thread(target=self._run_collect, args=(waiting,), daemon=True)
        self._collect_job.start()
        return {"ok": True, "started": len(waiting)}

    def acquisition_clear_downloads(self) -> dict[str, object]:
        """Take the collected downloads off the library, touching no file.

        It records that they have been seen. History still answers for what
        arrived, and every file stays exactly where it was downloaded.
        """
        landed = {
            str(record["landed_path"])
            for record in self._store.download_history()
            if record["collected_at"] and not record["cleared_at"] and record["landed_path"]
        }
        cleared = self._store.clear_collected_downloads()
        with self._albums_lock:
            taken = [
                unit_id
                for unit_id in list(self._downloaded)
                if (state := self._albums.get(unit_id)) is None
                or _under_any(state.unit.folder_path, landed)
            ]
            for unit_id in taken:
                self._albums.pop(unit_id, None)
                self._downloaded.discard(unit_id)
            # The unit rows too, or every one of them returns on the next start:
        # the Library is restored from them now, and clearing the download
        # history alone would take the row off the screen for one session.
        for unit_id in taken:
            self._store.clear_unit(unit_id)
        return {"ok": True, "cleared": cleared}

    def _run_collect(self, waiting: list[dict[str, object]]) -> None:
        """Read each finished download off the disk, on the worker thread.

        It ends by saying what joined, always — including nothing. Reading the
        disk takes as long as it takes, and the window that asked for this has
        finished reading the library long before: without the event the album is
        in the library and nothing on screen knows, so a finished download would
        appear only after "Organize what I downloaded" was pressed by hand.
        """
        collected = 0
        arrived = ""
        arrivals: list[int] = []
        same_audio: list[dict[str, object]] = []
        try:
            try:
                destination = self._acquisition.download_directory()
            except Exception:
                self._logger.info(
                    "The acquisition service could not say where it downloads.",
                    extra={"operation": "api.acquisition.collect"},
                )
                return
            if not destination:
                return
            root = Path(destination).expanduser()
            try:
                transfers = self._acquisition.progress()
            except Exception:
                transfers = ()
            for record in waiting:
                landed = _landed_folder(root, record["directory"])
                if landed is None:
                    self._logger.warning(
                        "A download's folder name is not a plain name; it was not followed.",
                        extra={"operation": "acquisition.collect.refused"},
                    )
                    continue
                if not _download_finished(record, transfers, landed, self._accepted_short):
                    continue
                # Read under the lock, both times. `_run_adopt` does exactly
                # this and says why: walking the map while another thread fills
                # it raises "dictionary changed size during iteration". Worse
                # than the crash is the quiet half: an album another thread
                # added between the two reads lands in `joined`, is marked
                # `downloaded`, and is then deleted from the Library by `Clear
                # downloaded` — an album that was scanned by hand.
                with self._albums_lock:
                    before = set(self._albums)
                self._run_scan(landed, announce=False, same_audio=same_audio)
                with self._albums_lock:
                    # **Under this folder, and not merely new.** A set
                    # difference over a map two other threads write to says
                    # "appeared while I was working", which is not the same
                    # question: a collect that begins while the Library is
                    # still being restored would claim every album the restore
                    # put on the shelf between the two reads as a download, and
                    # `Clear downloads` would then sweep them off the shelf.
                    #
                    # Asking whether the album is *inside the folder that was
                    # collected* is a rule about the album, so no amount of
                    # concurrency can make it wrong.
                    joined = {
                        unit_id
                        for unit_id in set(self._albums) - before
                        if _inside(self._albums[unit_id].unit.folder_path, landed)
                    }
                    self._downloaded.update(joined)
                # A collected download is read off the disk by the ordinary
                # scan, so it is recorded as scanned and promoted here, where
                # the caller is the one thing that knows it arrived.
                for unit_id in joined:
                    self._store.set_unit_origin(unit_id, "downloaded")
                if not joined:
                    # Nothing was read, so nothing was collected. Marking it
                    # anyway takes the record out of the waiting list for good,
                    # and no later attempt ever looks at it again — the album
                    # can never arrive. It stays waiting instead, and says why.
                    self._logger.info(
                        "A finished download held no album this app could read.",
                        extra={
                            "operation": "api.acquisition.collect",
                            "folder": str(landed),
                        },
                    )
                    continue
                # What it is, beside where it landed. The path is exact and
                # becomes false when the organised folder is moved elsewhere;
                # the signature is what History still recognises it by then.
                # Only when the folder held exactly one album — two albums have
                # two signatures and this column holds one, so it holds neither
                # rather than half an answer.
                signature = ""
                if len(joined) == 1:
                    with self._albums_lock:
                        only = self._albums.get(next(iter(joined)))
                    signature = "" if only is None else only.unit.unit_signature
                self._store.mark_download_collected(
                    int(record["download_id"]), str(landed), signature
                )
                collected += len(joined)
                # Which albums, and not only how many. Every way an album can
                # arrive marks it, and a count cannot be marked. `opened` and
                # `adopted` carry their ids as well, and the three of them are
                # what makes it one rule.
                arrivals.extend(sorted(joined))
                arrived = landed.name
                self._logger.info(
                    "A finished download joined the library.",
                    extra={"operation": "api.acquisition.collect"},
                )
        finally:
            # In a `finally`, because a window left waiting on an event that a
            # raised exception swallowed is a screen that never updates, and
            # that silence reads as a result.
            self._emit(
                "collected",
                {
                    "albums": collected,
                    "folder": arrived,
                    "unit_ids": arrivals,
                    "same_audio": same_audio,
                },
            )

    def _restore_library(self) -> None:
        """Read the whole Library back, offline, so it survives a restart.

        Every album this app has been shown returns, whatever brought it in. No
        source is queried: an album is re-read off its folder and re-matched
        against the release already cached in ``metadata_releases``, which is
        what that cache is for. Opening the window is not the moment to spend a
        catalogue's rate limit on answers this app already has.

        A folder that is no longer where it was recorded is not a fault: an
        album may be scanned in one folder and filed into a collection
        afterwards. It is looked for where this application has been shown, and
        when nothing answers its row is kept and marked as missing.
        """
        restored = 0
        try:
            restored = self._restore_each()
        finally:
            # In a `finally`, because a window left waiting on an event that a
            # raised exception swallowed is a screen that never updates — and
            # the one thing that can raise out here is the read of the album
            # rows themselves, which is every album at once.
            #
            # Reported *before* the event, never after: the window is told once,
            # and a snapshot taken between the two would have contradicted it.
            self._restore_reported.set()
            self._emit("restored", {"albums": restored})
        # The library is back and nothing is running. This is the moment the
        # discardable folders can be brought under their ceilings without
        # reaching into work in progress.
        #
        # And the moment to copy the database, for the same reason: the restore
        # is what reads every album, so a copy taken here is a copy of a
        # database nothing is mid-sentence in. It runs on this thread, which is
        # the worker and not the window, and it is skipped entirely when
        # nothing has been written since the last one.
        self._copy_the_database()
        self._enforce_cache_limits()

    def _copy_the_database(self) -> None:
        """Keep a compressed version of the database from this moment.

        Failure is logged and swallowed by `take_copy` itself: a copy that
        cannot be written is a copy that was not written, and it must never be
        what stops the library from opening.
        """
        if self._database is None or self._database_copies is None:
            return
        take_copy(self._database, self._database_copies, self._logger)

    def _restore_each(self) -> int:
        """Read every recorded album back, counting the ones that came.

        **The ones that still need an answer go first.** The shelf is already
        on screen from the database, so this pass exists to fill in what a
        column cannot hold — and the only thing a column cannot hold is the
        pairing, which the window draws for an album that is *not* organized.
        Those are usually few, so what is being waited for arrives early. The
        organized ones follow behind, where finishing early costs nothing and
        finishing late costs nothing either.
        """
        restored = 0
        # An album whose folder is *missing* comes first of all. Its card is
        # drawn as missing until the search below finds where it was filed, so
        # it reads wrongly for exactly as long as this pass takes to reach it.
        units = sorted(
            self._store.library_units(),
            key=lambda u: (u.folder_path.is_dir(), u.state == "organized"),
        )
        for stored in units:
            try:
                if not stored.folder_path.is_dir():
                    # It may simply have been filed somewhere this app knows.
                    # Following it costs a listing and, at most, one folder
                    # read; only when nothing answers is the row marked.
                    moved = self._look_for_moved(stored)
                    if moved is not None:
                        self._logger.info(
                            "A recorded album was found where it had been filed.",
                            extra={
                                "operation": "api.library.followed",
                                "folder": str(moved),
                            },
                        )
                        # Written down, not merely believed. Correcting only the
                        # copy in memory would leave the row stale until
                        # `library_present` happened to run — and every table
                        # keyed by that path with it.
                        self._store.relocate_unit(str(stored.folder_path), str(moved))
                        stored = replace(stored, folder_path=moved)
                if not stored.folder_path.is_dir():
                    # **Kept, and said.** Filing an album elsewhere is a normal
                    # end of its journey, but clearing its row is permanent,
                    # and a folder that does not answer at this instant is not
                    # an album that is gone: an unmounted disk, a cloud folder
                    # mid-sync and a rename in the Finder all look identical
                    # here. Clearing would take albums still on the disk out of
                    # the Library.
                    #
                    # It stays on the shelf, marked, with `Relocate…` to hand.
                    # Nothing leaves the Library unless the user says so.
                    self._logger.info(
                        "An album's folder did not answer; its row was kept and marked.",
                        extra={
                            "operation": "api.library.missing",
                            "folder": str(stored.folder_path),
                        },
                    )
                    continue
                if self._restore_unit(stored):
                    restored += 1
            except Exception:
                # One unreadable album must not cost the rest of the shelf.
                self._logger.exception(
                    "An album could not be read back into the Library.",
                    extra={
                        "operation": "api.library.restore",
                        "folder": str(stored.folder_path),
                    },
                )
        return restored

    def _organized_outcome(self, unit: AlbumUnit, unit_id: int) -> IdentificationOutcome:
        """Say what an already-organized album *is*, without planning it again.

        Such an album is left exactly as it is, and having nothing — no
        candidate, no plan — is what keeps every batch gesture from reaching
        it. That alone would leave the card with nothing to show but a folder
        name, and the dialog unable to say what the record is, though the
        database has held the answer since it was organized.

        So the answer comes back and the plan does not. The candidate names the
        album and the alignment fills the match badge; the plan stays ``None``,
        which is what keeps it out of ``apply_automatic``, and the decision stays
        ``REVIEW`` for the same reason.
        """
        base = _already_organized(unit)
        recorded = self._store.identification_for(unit_id)
        release = self._store.release(int(recorded["release_row"])) if recorded else None
        if release is None:
            return base
        try:
            named = self._pipeline.workflow.adopt_release(
                unit, release.source, release, offline=True
            )
        except Exception:
            self._logger.exception(
                "An organized album could not be named from its cached release.",
                extra={"operation": "api.organized.name", "folder": str(unit.folder_path)},
            )
            return base
        return replace(base, candidate=named.candidate, alignment=named.alignment)

    def _followed_folder(self, stored: StoredUnit) -> Path | None:
        """Return where this recorded album is now, following it by its audio.

        The one place that answers *where did this album go*. Answering it
        inside the gestures is one level too deep: everything the window can
        press goes through `_bring_in` first, which reads the recorded path —
        so for an album that is no longer in memory, a following done inside
        the gestures could not be reached at all, and opening the album would
        fail at the door.

        Only where this app has been shown, never by searching the disk, and
        adopted only when the audio says it is the same album.
        """
        if stored.folder_path.is_dir():
            return stored.folder_path
        moved = self._look_for_moved(stored)
        if moved is None:
            return None
        if not any(
            unit.folder_path == moved and unit.unit_signature == stored.unit_signature
            for unit in self._pipeline.scanner.scan(moved)
        ):
            return None
        self._store.relocate_unit(str(stored.folder_path), str(moved))
        self._logger.info(
            "An album was followed to where it now lives.",
            extra={"operation": "api.followed", "album": stored.unit_id, "now": str(moved)},
        )
        return moved

    def _where_it_is_now(self, state: _AlbumState) -> Path | None:
        """Make sure this album is where the window thinks, and follow it if not.

        An album moved by hand leaves the copy in memory naming the old place,
        and planning it then reports that tags could not be read from a file
        that is intact and sitting in another folder — an absence wearing the
        words of damage.

        So the album is re-read from disk before it is planned, the same way
        `scan_selected` re-reads what was marked, and an album that is no
        longer there is looked for the way a restart looks for it: only in
        folders this app has actually been shown, never by searching the disk,
        and adopted only when the audio says it is the same album.

        Returns where it is, or ``None`` when it is nowhere this app can see.
        """
        folder = state.unit.folder_path
        if folder.is_dir():
            fresh = [
                found
                for found in self._pipeline.scanner.scan(folder)
                if found.folder_path == folder
            ]
            if fresh:
                # Re-read rather than trusted: files may have been added,
                # removed or renamed since this album was put on screen, and
                # planning a stale unit plans against files that are not there.
                state.unit = fresh[0]
                return folder
        stored = self._store.unit_by_signature(state.unit.unit_signature)
        # The row may already know, because a scan reached the album where it now
        # lives; only when nothing knows is it worth looking.
        moved = (
            stored.folder_path
            if stored is not None and stored.folder_path != folder and stored.folder_path.is_dir()
            else self._look_for_moved(stored) if stored is not None else None
        )
        if moved is None:
            return None
        found = [
            unit
            for unit in self._pipeline.scanner.scan(moved)
            if unit.folder_path == moved and unit.unit_signature == state.unit.unit_signature
        ]
        if not found:
            return None
        state.unit = found[0]
        self._store.relocate_unit(str(folder), str(moved))
        self._logger.info(
            "An album was followed to where it now lives before being planned again.",
            extra={"operation": "api.replan.followed", "album": state.unit_id},
        )
        return moved

    def _read_again(self, unit: AlbumUnit, folder: Path) -> AlbumUnit:
        """Return this album as it reads in the folder it has just been followed to.

        Following an album is not renaming it: the folder was moved outside
        this application, which has no plan to replay over the paths it held.
        So the folder is read, and the album whose signature is this one is
        adopted whole — its folder, its tracks and its companions at once.

        **The tracks matter as much as the folder.** The cover is read out of
        the first track by path (`_embedded_cover`), and so is every gesture
        that reaches a file rather than the album; carrying the folder alone
        leaves those naming a folder that no longer exists.

        The signature is asked again rather than assumed: `_look_for_moved` read
        this folder to decide, and between that read and this one its contents
        can have changed. When nothing there answers to this album, the folder
        is carried on its own, which is still better than leaving the unit
        where it was.
        """
        found = next(
            (
                scanned
                for scanned in self._pipeline.scanner.scan(folder)
                if scanned.folder_path == folder and scanned.unit_signature == unit.unit_signature
            ),
            None,
        )
        return found if found is not None else replace(unit, folder_path=folder)

    def _look_for_moved(self, stored: StoredUnit) -> Path | None:
        """Find an album that is no longer where it was recorded.

        Albums are commonly filed into a collection after being organized, and
        dropping the row then would empty the Library of most of what it had
        recorded. So the app looks for the album first, and only in places it
        has been shown: the configured library folder, the folder last scanned,
        and the parents of albums it has already seen. It never searches the
        disk.

        **The name finds the candidate; the audio decides.** Two albums can
        share a folder name, and adopting the wrong one would attach an
        identification, a plan and the user's corrections to a record that is
        not the one they were made about. So a candidate is read and kept only
        when its unit signature is the same album — which is one folder read,
        and only when a name matched.

        **And when no name matches, the files inside are the name to search
        by.** The folder's own name finds an album that moved and structurally
        cannot find one that was *renamed* in the Finder, for instance by
        adding a note to the end of it. What is not renamed by hand is the
        audio inside, because that is this application's own job and it wrote
        down every file's size.

        Listing the folders that hold audio reads no audio at all, and matching
        the recorded albums against them by their file sizes is cheap. A folder
        may fit an album that is not its own when both are copies of one
        record, so the signature still decides, on the one folder that is read.

        **A folder another row holds is never an answer, by either door.**
        Refused by the sizes and not by the name, a deleted copy would find the
        copy that was kept under the same name, every caller would be handed a
        folder the database would not let it take — and a `Revert` of the
        deleted copy's plan would be pointed at the kept one.
        """
        name = stored.folder_path.name
        taken = self._taken_by_another(stored)
        tried: set[Path] = set()
        for parent in self._places_albums_live():
            candidate = parent / name
            if candidate == stored.folder_path or candidate in tried:
                continue
            tried.add(candidate)
            if _normalized(candidate) in taken or not candidate.is_dir():
                continue
            if self._is_this_album(candidate, stored):
                return candidate
        for candidate in self._folders_that_fit(stored, taken):
            if candidate in tried:
                continue
            tried.add(candidate)
            if self._is_this_album(candidate, stored):
                return candidate
        return None

    def _is_this_album(self, candidate: Path, stored: StoredUnit) -> bool:
        """Read one folder and say whether the album in it is this one."""
        unit = next(
            (
                scanned
                for scanned in self._pipeline.scanner.scan(candidate)
                if scanned.folder_path == candidate
            ),
            None,
        )
        return unit is not None and unit.unit_signature == stored.unit_signature

    def _taken_by_another(self, stored: StoredUnit) -> set[str]:
        """The folders some other row is recorded under, in one spelling each."""
        return {
            _normalized(folder)
            for folder, unit_id in self._store.units_by_folder().items()
            if unit_id != stored.unit_id
        }

    def _folders_that_fit(self, stored: StoredUnit, taken: set[str]) -> tuple[Path, ...]:
        """Folders whose audio weighs exactly what this album's audio weighs.

        A candidate filter and nothing more: sizes come free with the directory
        listing, so this costs no audio read, and what it hands back is then put
        to the signature. Sizes rather than file names because the files may be
        renamed too — and this application does rename them, which is the whole
        reason a search by name is not enough.

        A folder another album is recorded under is never offered: that album is
        where it says it is, and two rows pointing at one folder is the defect
        this whole search exists to avoid.
        """
        wanted = self._store.audio_sizes(stored.unit_id, stored.folder_path)
        if not wanted:
            return ()
        return tuple(
            folder
            for folder, sizes in self._folders_by_audio_size().items()
            if sizes == wanted and _normalized(folder) not in taken
        )

    def _folders_by_audio_size(self) -> dict[Path, tuple[int, ...]]:
        """List the places this app has been shown, and weigh the audio in each.

        Held for a short while because one restore asks this once per album whose
        folder does not answer, and the answer is the same for all of them, so
        listing once per missing album would multiply the cost by their number.
        Anything stale is harmless: a folder that has moved on since the
        listing simply fails the signature read that follows.

        **A folder holding no audio is asked about its own children**, because
        a two-disc album keeps every file in `CD1` and `CD2` and would
        otherwise weigh nothing and be offered to nobody. Only when the folder
        itself is empty of audio, so the ordinary album costs exactly what it
        did.
        """
        now = time.monotonic()
        if self._folder_sizes is not None and now - self._folder_sizes[0] < _LISTING_HOLDS_FOR:
            return self._folder_sizes[1]
        sizes: dict[Path, tuple[int, ...]] = {}
        for parent in self._places_albums_live():
            try:
                children = [entry.path for entry in os.scandir(parent) if entry.is_dir()]
            except OSError:
                continue
            for child in children:
                weighed = _audio_weighed_in(Path(child))
                if not weighed:
                    weighed = _audio_weighed_a_level_down(Path(child))
                if weighed:
                    sizes[Path(child)] = weighed
        self._folder_sizes = (now, sizes)
        return sizes

    def _places_albums_live(self) -> tuple[Path, ...]:
        """The folders this app may look in for an album that moved.

        Every one of them is somewhere it has been told about: the root last
        scanned, and the parents of albums already recorded — a collection is
        among those the moment one album of it has been seen.
        """
        places: list[Path] = []
        if self._root:
            places.append(Path(self._root))
        places.extend(self._store.known_parents())
        seen: dict[Path, None] = {}
        for place in places:
            seen.setdefault(place, None)
        return tuple(seen)

    def _restore_unit(self, stored: StoredUnit) -> bool:
        """Re-read one recorded album off the disk and rebuild what is known of it.

        An album the window already holds is left alone. The restore runs on a
        thread while a folder can already be opened or dropped, and those albums
        are recorded as they arrive — so a restore that read the table
        afterwards would put its own older answer on top of what was just asked
        for, including replacing "waiting to be scanned" with a plan.
        """
        with self._albums_lock:
            if stored.unit_id in self._albums:
                return False
        # **Followed before it is read.** Scanning the recorded path would
        # raise on an album that was moved, and this is the door every gesture
        # goes through — so any following done later would be unreachable for
        # exactly the albums that need it.
        folder = self._followed_folder(stored)
        if folder is None:
            self._logger.info(
                "A recorded album is not where it was and was not found; not restored.",
                extra={"operation": "api.library.restore", "folder": str(stored.folder_path)},
            )
            return False
        units = [unit for unit in self._pipeline.scanner.scan(folder) if unit.folder_path == folder]
        if not units:
            # The folder is there and holds no album this app can read. Said,
            # not skipped: "it is not there" and "there is nothing in it" are
            # different answers, and only one of them is about a missing folder.
            self._logger.info(
                "A recorded album folder held nothing readable; not restored.",
                extra={"operation": "api.library.restore", "folder": str(stored.folder_path)},
            )
            return False
        unit = units[0]
        unit_id = self._store.record_unit(unit)
        # What this album's audio already measured, before anything is planned
        # for it. A restore plans, and the map the names are built from is
        # otherwise filled only by a scan: without this read every album on the
        # shelf after a restart would lose its transcode verdict, the user's
        # word about it, and every bitrate that is provably its own. This is a
        # read of the store — no ffmpeg, no network.
        self._facts_from_store([unit])
        organized = stored.state == "organized"
        # An organized album restored with a full outcome would come back as
        # `automatic` with a plan, and one press of "Apply N automatic" would
        # then reach albums this app had already finished with.
        outcome = (
            self._organized_outcome(unit, unit_id)
            if organized
            else self._restored_outcome(unit, unit_id, stored)
        )
        # The row says this album was not automatic, so it does not come back as
        # one. `hold` writes `needs_review` when an album is pulled back to be
        # looked at, and `_persist_outcome` writes the same value for every
        # album the app itself did not call automatic — the two cannot be told
        # apart, and the conclusion the row records is the same either way.
        # Reading it back is faithful to the record; the cost is that lowering
        # the threshold later does not release these until they are touched,
        # which is the safe direction for a gesture whose whole point was to
        # stop something being renamed.
        if stored.state == "needs_review" and outcome.decision is Decision.AUTOMATIC:
            outcome = replace(
                outcome,
                decision=Decision.REVIEW,
                reason="Held for review; this album was not automatic when it was last decided.",
            )
        state = _AlbumState(
            unit=unit,
            unit_id=unit_id,
            outcome=outcome,
            organized=organized,
            applied=organized,
            # An album restored with no identification on record comes back
            # arranged from its own tags, and that is not a look-up. An
            # organized one keeps the word whatever its cache still holds:
            # something answered for it once, and it is the row saying so.
            looked_at=organized or _a_catalogue_answered(outcome),
            # `reject` writes on the album's own row that its identification
            # was refused. Unless that is read back here the refusal lasts only
            # as long as the window stays open: the album returns as an
            # ordinary automatic one, and `Apply N automatic` is a single
            # click.
            rejected=stored.state == "skipped",
        )
        # Checked again, at the moment of writing. The guard at the top of this
        # method is the right rule in the wrong place: between it and here a whole
        # folder is read off the disk, and the scan can put that album in during
        # the read.
        if not self._remember_if_absent(unit_id, state):
            return False
        with self._albums_lock:
            if stored.origin == "downloaded":
                self._downloaded.add(unit_id)
        return True

    def _restored_outcome(
        self, unit: AlbumUnit, unit_id: int, stored: StoredUnit
    ) -> IdentificationOutcome:
        """Rebuild an album's outcome from what was decided, without the network."""
        on_record = self._outcome_on_record(unit, unit_id)
        if on_record is not None:
            return on_record
        # Never identified, or identified against a release the cache no
        # longer holds. No catalogue is asked on the way in — scanning is how
        # identification is asked for — but its own tags cost nothing and
        # reach nobody, so the album comes back proposed rather than blank.
        # The proposal is there when a folder is dropped, when one is
        # opened, and when the application is reopened.
        proposed = self._from_its_own_tags(unit, unit_id, frozenset())
        if proposed is not None:
            return proposed
        return IdentificationOutcome(
            unit=unit,
            hints=SearchHints(),
            decision=Decision.UNIDENTIFIED,
            reason="This album has not been identified yet.",
        )

    def _outcome_on_record(self, unit: AlbumUnit, unit_id: int) -> IdentificationOutcome | None:
        """Plan this album around the release on record for it, asking nobody.

        ``None`` when no identification stands for the album, or when the
        release it names is no longer in the cache.

        **One function for the two gestures that need it**: a restart, and
        `Plan this album again`. Both want the album as it was last decided,
        which is the release standing on its record with every correction and
        pairing put back on it, and a search cannot promise that. A search
        answers with whichever release ranks first, which is another record
        whenever the one in use was chosen by hand, and answers with nothing at
        all while a catalogue is unreachable.
        """
        recorded = self._store.identification_for(unit_id)
        release = self._store.release(int(recorded["release_row"])) if recorded else None
        if recorded is None or release is None:
            return None
        outcome = self._pipeline.workflow.adopt_release(
            unit,
            release.source,
            release,
            offline=True,
            transcoded=self._transcoded_for(unit),
            measured_rates=self._rates,
        )
        if recorded.get("method") == "acoustic":
            outcome = replace(outcome, acoustic="named")
        # What the catalogues and the witness said about this album, read back
        # with it. Without this a restart turns every album into one whose
        # sources have "not asked" beside them, including the one in use.
        if recorded.get("verification"):
            kept = dict(recorded["verification"])
            outcome = replace(outcome, verification=kept)
            # What the audio answered, including when the answer was no. If
            # only the positive one survived, through `method='acoustic'`, a
            # reopened window would say "not asked" about an album it had asked
            # about and paid a rate-limited request for. For music the
            # fingerprint service does not hold, the negative is the ordinary
            # answer, and it is a fact about the album rather than an absence.
            if kept.get("acoustic"):
                outcome = replace(outcome, acoustic=str(kept["acoustic"]))
        # Offline, like everything else in a restore: an album that carries a
        # correction is re-planned here, and re-planning must not reach the
        # network for its cover.
        return self._with_corrections(unit_id, unit, outcome, frozenset(), offline=True)

    def acquisition_organize(self) -> dict[str, object]:
        """Point the organiser at the folder finished downloads land in.

        Where that is belongs to the acquisition service, so it is asked rather
        than remembered: a copy kept here would drift the moment the folder was
        moved. This is the ordinary scan, on an ordinary folder — the plan is
        reviewed and approved exactly like any other.
        """
        # Through the same door as every other reader of that setting: a
        # relative answer handed to `scan` would point a scan at this
        # application's own folder.
        root, refusal = self._download_root()
        if root is None:
            self._logger.info(
                "Organizing what was downloaded was refused before it started.",
                extra={
                    "operation": "api.acquisition.organize",
                    # Named even here, because "not a folder this app can reach"
                    # is a sentence about a folder.
                    "folder": None,
                    "started": False,
                    "refused": refusal.get("error"),
                },
            )
            return refusal
        destination = str(root)
        # Logged every time. This gesture reaches `scan`, `scan` can refuse, and
        # a refusal that is only a toast with no folder in it makes a press
        # that did nothing look exactly like a press that worked.
        answer = self.scan(destination)
        self._logger.info(
            "Organizing what was downloaded.",
            extra={
                "operation": "api.acquisition.organize",
                "folder": destination,
                "started": bool(answer.get("ok")),
                "refused": answer.get("error"),
            },
        )
        if not answer.get("ok"):
            # The folder belongs in the sentence: "a scan is already running"
            # over a shelf that is not moving says nothing about which folder
            # was not read.
            answer = dict(answer)
            answer["error"] = f"{answer.get('error')} ({destination})"
        return answer

    def _run_acquisition_search(self, search: _Search) -> None:
        """Search on the worker thread and publish what answered, into its own tab."""
        try:
            result = self._acquisition.search(
                ProviderRequest(search.query, {"kind": search.kind, "watch": _TabWatch(search)})
            )
        except Exception as error:
            if search.closed:
                # The tab was closed while this was in flight. The provider
                # abandoned the search at the service, which is the whole
                # intention — there is nobody left to tell.
                return
            self._logger.exception(
                "The acquisition search failed.", extra={"operation": "api.acquisition.search"}
            )
            search.publish({"searching": False, "folders": [], "error": str(error)})
            return
        if search.closed:
            return
        try:
            self._publish_search(search, result)
        except Exception as error:
            # Everything after the provider call can raise too: the download
            # history is a SQLite read against three writers, and the relevance
            # and attribute readings walk a payload that came from strangers.
            # `searching: False` is written on the last line alone, so a raise
            # above it would leave the tab spinning with nothing on screen
            # saying why.
            self._logger.exception(
                "The acquisition search answered but could not be read.",
                extra={"operation": "api.acquisition.search.publish"},
            )
            if not search.closed:
                search.publish({"searching": False, "folders": [], "error": str(error)})

    def _publish_search(self, search: _Search, result: ProviderOperationResult) -> None:
        """Turn one provider answer into what the tab shows."""
        folders = result.values.get("folders", ()) if result.successful else ()
        search.request_id = str(result.values.get("request_id") or "") or None
        terms = _terms(search.query)
        # What has already been asked for, so a result taken earlier is marked
        # as taken rather than left to be remembered. Read once, here, rather
        # than per row.
        requested = {
            (str(entry["source"]), str(entry["directory"]))
            for entry in self._store.download_history()
        }
        payload = []
        found: dict[str, object] = {}
        for index, folder in enumerate(folders):
            identifier = f"f{index}"
            found[identifier] = folder
            payload.append(
                {
                    "id": identifier,
                    "source": folder.source,
                    "folder": folder.name or folder.directory,
                    "files": folder.file_count,
                    "size": folder.total_size_bytes,
                    "rate": folder.transfer_rate or 0,
                    "queue": folder.queue_depth or 0,
                    "free": folder.availability.value == "available",
                    "relevance": _relevance(folder, terms),
                    "requested": (folder.source, folder.directory) in requested,
                    "tracks": [
                        {
                            "name": file.basename,
                            "size": file.size_bytes or 0,
                            "attributes": _attributes(file),
                        }
                        for file in folder.files
                    ],
                }
            )
        search.folders = found
        search.publish({"searching": False, "folders": payload, "message": result.explanation})

    # --- gestures ----------------------------------------------------------

    def choose_folder(self) -> str | None:
        """Ask the platform for a folder, through the injected picker."""
        if self._folder_picker is None:
            return None
        return self._folder_picker()

    def choose_cover(self, unit_id: int) -> dict[str, object]:
        """Plan the cover the user points at, and write nothing yet.

        The Cover Art Archive is addressed through MusicBrainz, so an album that
        no MusicBrainz release matched has no cover to be offered, and the
        automatic rule has nothing to choose between. This is how a cover
        reaches those albums at all.

        Two steps on purpose. What comes back here is a plan, and a second
        gesture writes it: a picture replacing a picture is exactly the write
        that must be read before it runs.

        **A finished album is not asked to be planned again first.** A cover
        names nothing and asks no catalogue, so a re-plan would buy nothing the
        cover needs — and it would leave a proposal standing on the album,
        sending a finished album back to `Auto` for the sake of a picture. The
        cover plan stands on its own and has its own way back in History.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        artwork = self._pipeline.artwork
        planner = self._pipeline.planner
        if artwork is None or planner is None or self._image_picker is None:
            return {"ok": False, "error": "This build cannot choose a cover."}
        picked = self._image_picker()
        if not picked:
            return {"ok": True, "cancelled": True, "album": self._album_payload(state)}
        staged = artwork.stage_chosen(Path(picked).expanduser())
        if staged is None:
            return {"ok": False, "error": f"That file is not an image this app can read: {picked}"}
        plan = planner.chosen_cover_plan(state.unit, staged)
        if plan.is_empty:
            return {"ok": False, "error": "There is nothing in this album for a cover to reach."}
        if state.cover_plan_id is not None and not state.cover_applied:
            # A second choice replaces the first rather than joining it.
            self._store.record_plan_outcome(state.cover_plan_id, "discarded")
        state.cover_plan = plan
        state.cover_plan_id = self._store.record_plan(state.unit_id, None, plan.operations)
        state.cover_applied = False
        state.cover_chosen_by_user = True
        self._logger.info(
            "A cover was chosen for one album.",
            extra={
                "operation": "artwork.chosen.planned",
                "unit_id": unit_id,
                "operations": len(plan.operations),
            },
        )
        return {"ok": True, "album": self._album_payload(state)}

    def write_chosen_cover(self, unit_id: int) -> dict[str, object]:
        """Run the cover plan that was shown, over the album it was shown on."""
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if state.cover_plan is None or state.cover_applied:
            return {"ok": False, "error": "There is no cover waiting for this album."}
        failures: list[str] = []
        written: list[str] = []
        batch: list[int] = []
        covers = self._apply_pending_covers(failures, batch, written, {unit_id})
        if batch:
            self._store.assign_run(batch, f"run-{batch[0]}-{len(batch)}")
        if failures:
            return {"ok": False, "error": "; ".join(failures)}
        if not covers:
            # **Never "written" for a write that did not happen.** The count is
            # what the executor did, and a gesture that reports the intention
            # instead is the screen naming something it did not measure — for
            # instance when the plan was discarded before it could run.
            return {
                "ok": False,
                "error": "That cover could not be written; nothing was changed.",
                "album": self._album_payload(state),
            }
        return {"ok": True, "covers": covers, "album": self._album_payload(state)}

    def discard_chosen_cover(self, unit_id: int) -> dict[str, object]:
        """Put the chosen cover down again, having written nothing."""
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if state.cover_plan_id is not None and not state.cover_applied:
            self._store.record_plan_outcome(state.cover_plan_id, "discarded")
        state.cover_plan = None
        state.cover_plan_id = None
        state.cover_chosen_by_user = False
        return {"ok": True, "album": self._album_payload(state)}

    def subfolders(self, root: str) -> dict[str, object]:
        """List a root's direct subfolders and which are excluded."""
        path = Path(root).expanduser()
        if not path.is_dir():
            return {"ok": False, "error": f"Not a folder: {root}"}
        excluded = set(self._stored_exclusions(path))
        names = sorted(
            (
                entry.name
                for entry in path.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            ),
            key=str.casefold,
        )
        return {
            "ok": True,
            "folders": [{"name": name, "excluded": name in excluded} for name in names],
        }

    def scan(self, root: str, excluded: list[str] | None = None) -> dict[str, object]:
        """Start scanning and identifying a folder on the worker thread.

        ``excluded`` names the root's subfolders to leave alone, relative to
        the root. The choice is remembered per root, so pointing at the same
        folder again comes back with the same carve-outs.
        """
        if self._job is not None and self._job.is_alive():
            return {"ok": False, "error": "A scan is already running."}
        if self._collect_job is not None and self._collect_job.is_alive():
            # The collect identifies albums too, and `acquisition_collect`
            # refuses while a scan is running for the same reason: two identify
            # passes rebind `self._transcoded`, which a later re-plan reads to
            # decide whether a folder is named `[FLAC]` or `[Lossy]` — so the
            # wrong word could reach a folder name on disk. The guard has to
            # exist in both directions, and this one covers the gesture that
            # points a scan at the downloads folder while the downloads are
            # being brought in.
            return {"ok": False, "error": "Downloads are still being brought in."}
        path = Path(root).expanduser()
        if not path.is_dir():
            return {"ok": False, "error": f"Not a folder: {root}"}
        names = [str(name) for name in (excluded or [])]
        self._store.set_setting(f"exclusions.{path}", json.dumps(names, ensure_ascii=False))
        self._root = str(path)
        # Nothing is swept. The Library holds what it has been shown, so a scan
        # that replaced whatever an earlier scan had found would empty the
        # shelf around the one album scanned. Pointing at a folder is a question
        # about that folder and about nothing else — an album already here is
        # re-read and updated in place, because units are keyed by identity.
        self._reach = None
        excluded_paths = tuple(path / name for name in names)
        self._job = threading.Thread(
            target=self._run_scan, args=(path, excluded_paths), daemon=True
        )
        self._job.start()
        return {"ok": True}

    def open_folder(self, root: str) -> dict[str, object]:
        """Put a folder's albums on screen without identifying any of them.

        If the albums appeared only once Scan had been pressed and the run was
        over, choosing a folder would change nothing a person could see, and
        the gesture would look like it had failed. The albums arrive at once,
        and Scan is what looks them up.

        Reading the disk is cheap next to identifying; identifying is the part
        that costs a catalogue's rate limit, and it is asked for explicitly.
        """
        if self._writing_files():
            return {"ok": False, "error": _READ_WAITS_FOR_WRITES}
        path = Path(root).expanduser()
        if not path.is_dir():
            return {"ok": False, "error": f"Not a folder: {root}"}
        self._root = str(path)
        # The reach panel describes the run that is going; a folder read beside
        # it does not make that description stale.
        if not (self._job is not None and self._job.is_alive()):
            self._reach = None
        self._start_read(self._run_open, (path,))
        return {"ok": True}

    def _start_read(self, target: Callable[..., None], args: tuple[object, ...]) -> None:
        """Read folders on a worker of their own, one read after another."""

        def queued() -> None:
            with self._read_lock:
                target(*args)

        thread = threading.Thread(target=queued, daemon=True, name="read")
        self._reads = [read for read in self._reads if read.is_alive()] + [thread]
        thread.start()

    def _reading(self) -> bool:
        """Whether a folder is being read, or is waiting to be."""
        return any(read.is_alive() for read in self._reads)

    def _writing_files(self) -> bool:
        """Whether the running job renames or retags files.

        A read waits for this and for nothing else: reading a folder that is
        being renamed under it would record paths that are about to stop
        existing. Named on the thread where the job starts, so the answer is a
        fact about the job and not a list kept here of the jobs that write.
        """
        return self._job is not None and self._job.is_alive() and self._job.name == _WRITES_FILES

    def _run_open(self, root: Path) -> None:
        """Read a folder on the worker thread and show what is in it, tag-proposed."""
        opened: list[int] = []
        came: list[int] = []
        known: list[str] = []
        same_audio: list[dict[str, object]] = []
        try:
            arrived: list[int] = []
            moved: list[dict[str, object]] = []
            opened = self._read_into_library(root, moved, arrived, same_audio)
            # The same rule as the drop, because it is the same gesture.
            came, known = self._what_came(opened, arrived, moved)
        except Exception as error:
            # `reading`, so the window lets a scan beside it keep its bar: this
            # error is about the folder, not about the run.
            self._emit("error", {"message": str(error), "reading": True})
        finally:
            # Always, including nothing: a window waiting on an event that an
            # exception swallowed is a screen that never updates.
            self._emit(
                "opened",
                {
                    "albums": came,
                    "folder": str(root),
                    "same_audio": same_audio,
                    "known_albums": known,
                },
            )

    def _read_into_library(
        self,
        root: Path,
        moved: list[dict[str, object]] | None = None,
        arrived: list[int] | None = None,
        same_audio: list[dict[str, object]] | None = None,
    ) -> list[int]:
        """Put a folder's albums on screen, each proposed by its own tags.

        Nothing is asked of any catalogue here, and nothing is written. Arriving
        is not the same as having no answer: an album whose files carry an
        artist, a title, a year and a title per track is named by them the
        moment it lands, and the dialog opens on that proposal.

        An album the scan has already answered is left as it was: the scan
        happened *because* the tags were in doubt, so its answer supersedes
        theirs and the tags do not get to propose over it.

        ``moved`` collects the albums that were already known and are not where
        the registry had them. Asked *before* the units are recorded, because
        recording is what updates the path: the store follows an album by its
        audio when the old folder is gone, and this is what lets the window say
        that the album was re-anchored rather than only that it is already in
        the library.

        **Nothing that arrives here is marked**, whichever way it arrived.
        Marking is the user's gesture, and this method has no opinion about it.

        ``arrived`` collects the albums that are **new to the library**, decided
        by the database and not by watching a map. Diffing ``self._albums``
        around this call would count every album the restore reads back from
        another thread while the read is in flight, and a drop of one folder
        shortly after launch would report many.
        """
        opened: list[int] = []
        units = list(self._pipeline.scanner.scan(root))
        # **New is a row the database has just issued, not an audio it has not
        # seen.** Asking whether the signature is on record would count a second
        # copy of an album in a folder of its own — which has a row of its own —
        # as the album already here, and would announce a copy whose twin is
        # still on the disk as that twin having *moved*.
        newer_than = self._store.highest_unit_id()
        if moved is not None:
            for unit in units:
                stored = self._store.unit_by_signature(unit.unit_signature)
                # Only a row whose folder is gone is adopted by a new one
                # (`_upsert_unit`), so only that one has moved.
                if (
                    stored is not None
                    and stored.folder_path != unit.folder_path
                    and not stored.folder_path.exists()
                ):
                    moved.append(
                        {
                            "album": unit.folder_path.name,
                            "was": str(stored.folder_path),
                            "now": str(unit.folder_path),
                        }
                    )
        recorded = self._store.record_units(units)
        if moved is not None:
            for entry in moved:
                if "unit_id" not in entry and entry["now"] in recorded:
                    entry["unit_id"] = recorded[entry["now"]]
        if arrived is not None:
            arrived.extend(
                recorded[str(unit.folder_path)]
                for unit in units
                if recorded[str(unit.folder_path)] > newer_than
            )
        if same_audio is not None:
            same_audio.extend(self._same_audio_on_shelf(recorded, newer_than))
        # What these albums' audio already measured, off the record and with no
        # ffmpeg: a proposal that did not know a file was proven lossy would
        # name the folder `[FLAC]`, which is the one error that must never
        # reach the disk. The same read a restore makes for the same reason.
        self._facts_from_store(units)
        organized = self._store.ids_in_state("organized")
        # Two pressings of one album render one name, and neither is on disk
        # yet when the second is proposed. A collision is not a defect here —
        # it is the honest signal that this album wants a scan.
        claimed: set[str] = set()
        for unit in units:
            unit_id = recorded[str(unit.folder_path)]
            if unit_id in organized:
                state = _AlbumState(
                    unit=unit,
                    unit_id=unit_id,
                    outcome=self._organized_outcome(unit, unit_id),
                    organized=True,
                    applied=True,
                )
                claimed.add(unit.folder_path.name)
                self._remember_unless_identifying(unit_id, state)
                opened.append(unit_id)
                continue
            outcome = self._from_its_own_tags(unit, unit_id, frozenset(claimed))
            # Never looked up either way: a folder that was opened or dropped
            # has had no catalogue asked about it, whether or not its own tags
            # had enough to propose a name. Left at the default, the album would
            # claim a look-up that never happened.
            state = (
                _AlbumState(
                    unit=unit,
                    unit_id=unit_id,
                    outcome=_not_looked_at(unit),
                    looked_at=False,
                )
                if outcome is None
                else _AlbumState(unit=unit, unit_id=unit_id, outcome=outcome, looked_at=False)
            )
            claimed.add(_planned_folder_name(unit, state.outcome.plan))
            self._remember_unless_identifying(unit_id, state)
            opened.append(unit_id)
        return opened

    def _watermark(self) -> int:
        """Where the library stood when the tag mode arrived, written down once.

        The tag mode is not retroactive: albums already in the library are not
        touched. So the first time a build carrying it opens a database, it
        records how far the shelf went, and every album already on it is left
        exactly as it is — including at the next launch, which is the moment
        the mode would otherwise repaint every card on the shelf.

        Stored rather than derived, and derived only once: the number has to mean
        *before this existed*, and a fresh maximum taken later would mean
        *before that later moment*, quietly swallowing everything added since.
        """
        stored = self._store.setting(_TAGS_AFTER)
        if stored is not None and stored.strip().lstrip("-").isdigit():
            return int(stored)
        highest = self._store.highest_unit_id()
        self._store.set_setting(_TAGS_AFTER, str(highest))
        return highest

    def _from_its_own_tags(
        self, unit: AlbumUnit, unit_id: int, taken_names: frozenset[str]
    ) -> IdentificationOutcome | None:
        """Propose this album from its own tags, or answer ``None``.

        ``None`` for the three albums that must not be proposed at: one that was
        already on the shelf before this mode existed, one whose files do not
        say enough, and one a scan has already answered — the user's
        corrections and the pressing a catalogue settled are worth more than a
        re-reading of the tags.

        Failure here is never allowed to cost the album its card. This runs
        while a folder is being read, and an unreadable tag on one file must
        not be the difference between a card on screen and a folder that
        vanishes silently.
        """
        if unit_id <= self._tags_after:
            return None
        if self._store.identification_for(unit_id) is not None:
            return None
        try:
            return self._pipeline.workflow.name_from_tags(
                unit,
                taken_names,
                self._transcoded_for(unit),
                self._rates,
            )
        except Exception:
            self._logger.exception(
                "An album could not be named from its own tags.",
                extra={"operation": "api.tags.name", "folder": str(unit.folder_path)},
            )
            return None

    def scan_selected(self, unit_ids: list[int]) -> dict[str, object]:
        """Identify exactly the albums that are marked, and nothing else.

        Marking cards takes the place of an exclusions dialog: unchecking a
        folder in a modal and unmarking the card on screen are the same act,
        and only one of them can be seen while it is decided.
        """
        if self._job is not None and self._job.is_alive():
            return {"ok": False, "error": "A scan is already running."}
        wanted = [int(unit_id) for unit_id in unit_ids]
        with self._albums_lock:
            states = [self._albums[unit_id] for unit_id in wanted if unit_id in self._albums]
        if not states:
            return {"ok": False, "error": "No album is marked."}
        for state in states:
            # Only an album that has been looked up before has a reading to go
            # back to. A first scan's *before* is an album waiting to be
            # scanned, and a `Cancel` that threw away the only answer this
            # album has ever had would be offering harm as a way back.
            if state.looked_at:
                self._keep_for_cancel(state, "scanned_again")
        self._reach = None
        self._job = threading.Thread(target=self._run_selected, args=(states,), daemon=True)
        self._job.start()
        return {"ok": True, "albums": len(states)}

    def _run_selected(self, states: list[_AlbumState]) -> None:
        """Re-read the marked albums off the disk, then identify them.

        **Where they are now, not where they were.** Re-reading the recorded
        folder and nothing else makes a marked album that was moved raise
        `NotADirectoryError` from the scanner and drop out of the run, while
        the run goes on to report success about the albums that had not moved.
        `_where_it_is_now` is the same following that `Plan this album again`
        does.

        An album that nothing can find is **said**, never skipped: it is flagged
        for its card and counted into the run's ending. If "the ones that were
        read" were the only list, the album that belongs to no list would be
        invisible.
        """
        fresh: list[AlbumUnit] = []
        lost: list[_AlbumState] = []
        for state in states:
            try:
                # Re-read rather than trusted: the folder may have changed since
                # it was put on screen, and identifying a stale unit would plan
                # against files that are no longer there.
                where = self._where_it_is_now(state)
            except Exception:
                self._logger.exception(
                    "A marked album could not be read.",
                    extra={
                        "operation": "api.scan.selected",
                        "folder": str(state.unit.folder_path),
                    },
                )
                where = None
            if where is None:
                lost.append(state)
                continue
            fresh.append(state.unit)
        if lost:
            self._logger.info(
                "Marked albums are no longer where they were recorded.",
                extra={"operation": "api.scan.misplaced", "albums": len(lost)},
            )
        if not fresh:
            self._emit("finished", {"albums": 0, "covers": 0, "misplaced": len(lost)})
            return
        self._identify(fresh, misplaced=len(lost))

    def relocate_album(self, unit_id: int, folder: str) -> dict[str, object]:
        """Point one album at the folder it now lives in, named by the user.

        The automatic search looks for the album's folder *under its own name*
        in every place this app has been shown. That finds a folder that was
        moved and structurally cannot find one that was **renamed**, even when
        it sits in plain sight one level up.

        So the folder is pointed at, and the audio decides. The folder named is
        read, and it is adopted only when its album is **this** album — the
        unit signature, the same test the search applies to its own candidates,
        because a folder with the right name holding a different record would
        attach this album's identification, plan and corrections to music it is
        not about.

        **It works from the row alone, because that is the only album that ever
        needs it.** An album whose folder does not answer is never read back into
        memory — the restore keeps its row, marks it, and stops there — so a
        version that required the album in memory would refuse *every* album
        this exists for, and the album would sit on the shelf marked and
        unreachable by every gesture that could repair it.

        The recorded row carries the only two facts this needs: where the album
        was, and which audio it is. Nothing else in the state is consulted.
        """
        with self._albums_lock:
            state = self._albums.get(int(unit_id))
        stored = self._store.unit_by_id(int(unit_id)) if state is None else None
        if state is None and stored is None:
            return {"ok": False, "error": "Unknown album."}
        signature = state.unit.unit_signature if state is not None else stored.unit_signature
        chosen = Path(folder).expanduser()
        if not chosen.is_dir():
            return {"ok": False, "error": f"Not a folder: {folder}"}
        was = state.unit.folder_path if state is not None else stored.folder_path
        if chosen == was:
            return {"ok": False, "error": "That is where this album is already recorded."}
        try:
            found = [
                unit for unit in self._pipeline.scanner.scan(chosen) if unit.folder_path == chosen
            ]
        except Exception as error:
            return {"ok": False, "error": str(error)}
        if not found:
            return {"ok": False, "error": f"No album this app can read is in “{chosen.name}”."}
        if found[0].unit_signature != signature:
            # Named, not merely refused: two folders are in play, and the useful
            # answer is which one this is not.
            return {
                "ok": False,
                "error": (
                    f"“{chosen.name}” holds a different album — {found[0].track_count} "
                    f"tracks that are not this record's. Nothing was changed."
                ),
            }
        if not self._store.relocate_unit(str(was), str(chosen)):
            # **The database said no, so this says no.** The folder is another
            # row's, and the audio already said it is the same record — so this
            # card is a second copy of an album on the shelf. Answering `ok`
            # over the refusal would draw the card at the new folder and leave
            # the row naming the old one. The answer offers to let the second
            # copy go.
            twin_id = self._store.units_by_folder().get(chosen)
            return {
                "ok": False,
                "error": (
                    f"“{chosen.name}” is already on your shelf as another album with "
                    "the same audio, so this card is a second copy of it. Nothing "
                    "was changed."
                ),
                "second_copy": {
                    "unit_id": int(unit_id),
                    "folder": chosen.name,
                    "twin_id": twin_id,
                },
            }
        if state is None:
            # It answers again, so it is read back the way every other album is —
            # its identification, its plan and its corrections were never about
            # the path, and this is the moment they come back with it.
            self._restore_unit(replace(stored, folder_path=chosen))
            with self._albums_lock:
                state = self._albums.get(int(unit_id))
            if state is None:
                return {
                    "ok": False,
                    "error": f"“{chosen.name}” could not be read back. Nothing was changed.",
                }
        else:
            state.unit = found[0]
        self._logger.info(
            "An album was pointed at where it now lives.",
            extra={"operation": "api.relocate", "album": state.unit_id},
        )
        return {
            "ok": True,
            "album": self._album_payload(state),
            "moved": _followed(was, chosen),
        }

    def quality_scan(self, root: str, excluded: list[str] | None = None) -> dict[str, object]:
        """Start measuring what every album under a root actually is.

        This is a survey of its own, deliberately separate from the organizing
        scan: it reads the audio rather than the names, it writes nothing, and
        it is the slow one — every file is decoded.
        """
        if self._quality_job is not None and self._quality_job.is_alive():
            return {"ok": False, "error": "A quality survey is already running."}
        path = Path(root).expanduser()
        if not path.is_dir():
            return {"ok": False, "error": f"Not a folder: {root}"}
        names = [str(name) for name in (excluded or self._stored_exclusions(path))]
        self._quality = []
        self._quality_job = threading.Thread(
            target=self._run_quality,
            args=(path, tuple(path / name for name in names)),
            daemon=True,
        )
        self._quality_job.start()
        return {"ok": True}

    def adopt_folders(self, folders: list[str]) -> dict[str, object]:
        """Add dropped album folders to the library, keeping what is already there.

        Dragging folders in is the same arrival as scanning them, minus the
        instruction that a scan carries: pointing at a root says *this is the
        library now*, and dropping says *these as well*. So this adds, and it
        never takes the place of what a scan found or of a downloaded album.

        Several at once is the ordinary case, so they are read one after another
        on one worker thread, and the screen is told when they have all landed.

        **Every way out of here announces itself**, including the refusals. The
        window begins waiting the instant something lands on it, and the native
        handler that calls this throws the answer away — so a value returned to
        nobody is a screen that waits forever. A refusal that emits nothing
        leaves `Reading what you dropped…` spinning until the window is
        restarted.
        """
        if self._writing_files():
            return self._refuse_drop(len(folders), _READ_WAITS_FOR_WRITES)
        wanted = [Path(folder).expanduser() for folder in folders]
        missing = [str(path) for path in wanted if not path.is_dir()]
        if missing:
            return self._refuse_drop(len(folders), f"Not a folder: {missing[0]}")
        if not wanted:
            # Still announced. The window starts waiting the moment something
            # lands on it, and a drop that carried no folder has to release it
            # or the screen waits for an answer that was never coming.
            self._emit("adopted", {"albums": 0, "folders": 0})
            return {"ok": True, "adopted": 0}
        self._start_read(self._run_adopt, (tuple(wanted),))
        return {"ok": True, "started": len(wanted)}

    def _refuse_drop(self, folders: int, message: str) -> dict[str, object]:
        """Say why a drop was not read, and let go of the window that is waiting.

        Two events, because they answer different questions: the error says what
        happened, and the release says the waiting is over. Sending only the
        first would leave the spinner turning under a toast that had already
        faded.
        """
        self._emit("error", {"message": message, "reading": True})
        self._emit("adopted", {"albums": 0, "known": 0, "folders": folders, "refused": True})
        return {"ok": False, "error": message}

    def _run_adopt(self, folders: tuple[Path, ...]) -> None:
        """Read each dropped folder into the library, on the worker thread.

        Two numbers go back, not one. ``adopted`` counts albums that were not
        here before, and a drop of albums this library already holds makes it
        zero — which on its own reads as *nothing here looked like an album*,
        about a folder the scanner recognises perfectly well. ``known`` is what
        tells the two apart, the same way ``looked_up_at`` separates *never
        asked* from *asked and unknown*.
        """
        adopted = 0
        known = 0
        joined: list[int] = []
        moved: list[dict[str, object]] = []
        same_audio: list[dict[str, object]] = []
        known_names: list[str] = []
        # How long the reading itself took. Everything between this returning
        # and the album being drawn is the window's, and none of that is timed
        # here; so the part that *is* measurable is logged, and a difference
        # between it and what is seen on screen names where to look.
        started = time.monotonic()
        try:
            for folder in folders:
                # Which of this folder's albums are new is a question for the
                # database, asked inside the read. Diffing `self._albums` around
                # the call would answer it with whatever the restore thread had
                # remembered in the meantime.
                arrived: list[int] = []
                try:
                    # Read and shown, not looked up. Dropping is the same
                    # gesture as choosing a folder, so it means the same thing:
                    # the albums arrive and Scan is what spends a catalogue's
                    # rate limit on them. Identifying here would also leave the
                    # Scan button with nothing to do.
                    opened = self._read_into_library(folder, moved, arrived, same_audio)
                except Exception as error:
                    self._logger.exception(
                        "A dropped folder could not be read.",
                        extra={"operation": "api.adopt", "folder": str(folder)},
                    )
                    # Said on screen, not only in the log. A dropped folder that
                    # never appears, with no message, reads as nothing having
                    # been dropped.
                    self._emit(
                        "error",
                        {"message": f"{folder.name} could not be read: {error}", "reading": True},
                    )
                    continue
                adopted += len(arrived)
                came, here = self._what_came(opened, arrived, moved)
                known += len(here)
                known_names.extend(here)
                joined.extend(came)
                self._root = str(folder)
            # A dropped album belongs where it will be seen. One that was
            # already on the shelf would otherwise keep the place it took when
            # it first arrived, possibly far down the list, and the gesture
            # would look like it had done nothing — so it goes to the front.
            self._bring_to_front(joined)
        finally:
            # In a `finally` for the reason the collect event is: a window left
            # waiting on an answer that never comes is a screen that never
            # updates.
            # Which albums, not only how many. The window reads this twice: to
            # name the album that was dropped, which a drop of one already here
            # needs, and to mark the arrivals.
            reading = time.monotonic() - started
            self._logger.info(
                "Dropped folders were read into the library.",
                extra={
                    "operation": "api.adopt.timing",
                    "folders": len(folders),
                    "albums": adopted + known,
                    "seconds": round(reading, 3),
                },
            )
            self._emit(
                "adopted",
                {
                    "albums": adopted,
                    "known": known,
                    "folders": len(folders),
                    "unit_ids": joined,
                    # What the reading cost, so the window can say what the rest
                    # of the wait was. Everything after this event is drawing.
                    "seconds": round(reading, 3),
                    # The albums that were already here and have moved since,
                    # each naming where it was and where it is now. The move has
                    # already been written down by the time this is read; saying
                    # it is what makes a silent re-anchoring into an answer.
                    "moved": moved,
                    # New albums whose every stream is already on the shelf in
                    # another folder, each with the one that holds it.
                    "same_audio": same_audio,
                    # The albums that were already here, where they are: named,
                    # because they are no longer marked or floated to say which.
                    "known_albums": known_names,
                },
            )

    def quality_state(self) -> dict[str, object]:
        """Return every album measured so far, and whether the survey is running."""
        running = self._quality_job is not None and self._quality_job.is_alive()
        return {"albums": list(self._quality), "running": running}

    # --- the bench ---------------------------------------------------------

    def bench_state(self) -> dict[str, object]:
        """Return the whole bench: its folders and what is known of their albums.

        No disk is touched. Albums are judged from what is on record, which is
        fast enough for this to be the answer to opening a tab rather than
        something that has to be asked for.
        """
        bench = self._pipeline.bench
        if bench is None:
            return {"roots": [], "albums": [], "running": False}
        return {
            "roots": [
                {
                    "id": root.root_id,
                    "path": str(root.path),
                    "name": root.path.name or str(root.path),
                    "albums": root.albums,
                    "walked_at": root.walked_at,
                }
                for root in bench.roots()
            ],
            "albums": [_bench_payload(finding) for finding in bench.findings()],
            "running": self._bench_job is not None and self._bench_job.is_alive(),
            # The machine's one answer, not this screen's own reading of it.
            "can_measure": self._ffmpeg,
        }

    def bench_add(self, folder: str) -> dict[str, object]:
        """Put a folder on the bench and read what is under it, on a worker thread.

        The walk is the honest cost, and on a whole library it is a long one,
        so it is announced, reported as it goes, and never silently attached to
        opening a screen.
        """
        bench = self._pipeline.bench
        if bench is None:
            return {"ok": False, "error": "The bench is not available."}
        if self._bench_job is not None and self._bench_job.is_alive():
            return {"ok": False, "error": "The bench is already reading a folder."}
        path = Path(folder).expanduser()
        if not path.is_dir():
            return {"ok": False, "error": f"Not a folder: {folder}"}
        self._bench_job = threading.Thread(target=self._run_bench_add, args=(path,), daemon=True)
        self._bench_job.start()
        return {"ok": True}

    def _run_bench_add(self, path: Path) -> None:
        bench = self._pipeline.bench
        if bench is None:
            return
        self._emit("bench_started", {"folder": path.name})
        try:
            root = bench.add(
                path,
                on_progress=lambda found: self._emit("bench_walking", {"albums": found}),
            )
        except Exception as error:
            self._logger.exception(
                "A folder could not be read onto the bench.",
                extra={"operation": "api.bench.add", "folder": str(path)},
            )
            self._emit("bench_error", {"message": f"{path.name} could not be read: {error}"})
            return
        self._emit("bench_finished", {"folder": path.name, "albums": root.albums})

    def bench_remove(self, root_id: int) -> dict[str, object]:
        """Take a folder off the bench. No file and no measurement is touched."""
        bench = self._pipeline.bench
        if bench is None:
            return {"ok": False, "error": "The bench is not available."}
        bench.remove(int(root_id))
        return {"ok": True}

    def bench_walk(self, root_id: int) -> dict[str, object]:
        """Read one of the bench's folders off the disk again."""
        bench = self._pipeline.bench
        if bench is None:
            return {"ok": False, "error": "The bench is not available."}
        if self._bench_job is not None and self._bench_job.is_alive():
            return {"ok": False, "error": "The bench is already reading a folder."}
        self._bench_job = threading.Thread(
            target=self._run_bench_walk, args=(int(root_id),), daemon=True
        )
        self._bench_job.start()
        return {"ok": True}

    def _run_bench_walk(self, root_id: int) -> None:
        bench = self._pipeline.bench
        if bench is None:
            return
        root = next((entry for entry in bench.roots() if entry.root_id == root_id), None)
        name = root.path.name if root is not None else ""
        self._emit("bench_started", {"folder": name})
        try:
            found = bench.walk(
                root_id, on_progress=lambda albums: self._emit("bench_walking", {"albums": albums})
            )
        except Exception as error:
            self._logger.exception(
                "A bench folder could not be read again.",
                extra={"operation": "api.bench.walk", "root": root_id},
            )
            self._emit("bench_error", {"message": f"{name} could not be read: {error}"})
            return
        self._emit("bench_finished", {"folder": name, "albums": found})

    def harmonics(self) -> dict[str, object]:
        """Every recording whose key and tempo are known, for the mixing screen.

        Only what has been measured: the wheel shows the tracks that were asked
        for and never the shelf waiting to be asked. A screen listing what it
        has not measured would be offering mixes it cannot vouch for.
        """
        rows: list[dict[str, object]] = []
        for state in self._album_snapshot():
            unit = state.unit
            keys = [file.audio_key for file in unit.audio_files if file.audio_key]
            if not keys:
                continue
            known = self._store.harmonics_for(keys)
            # An album is named by the release it was identified as, and by its
            # folder when nothing has been. `_AlbumState` carries neither as a
            # field — it holds the outcome, and the name is read off that.
            release = _shown(state.outcome.candidate.release if state.outcome.candidate else None)
            album_name = (release.title if release else None) or unit.folder_path.name
            artist_name = written_credit(release.artists) if release else ""
            # The same rule as the queue's, because it is the same claim: this
            # column says where the track sits on the record. Applied here too,
            # since a rule that reaches one of two callers is not a rule.
            for position, file in enumerate(
                files_in_track_order(unit, self._pipeline.tag_store), start=1
            ):
                measured = known.get(file.audio_key or "")
                if measured is None:
                    continue
                rows.append(
                    {
                        "audio_key": measured.audio_key,
                        "unit_id": state.unit_id,
                        "album": album_name,
                        "artist": artist_name,
                        "track": file.path.name,
                        "position": position,
                        "key_name": measured.key_name,
                        "camelot": _camelot_of(measured),
                        # How decided the reading was, and the key it beat.
                        # `null` here is not "won by nothing" — it is a reading
                        # taken before the margin was measured at all, and the
                        # window says so instead of giving it a word calibrated
                        # on a different profile.
                        "key_margin": measured.key_margin,
                        "runner_up_camelot": (
                            camelot_for(measured.runner_up_tonic, measured.runner_up_mode)
                            if measured.runner_up_tonic and measured.runner_up_mode
                            else None
                        ),
                        "bpm": measured.bpm,
                        "bpm_confidence": measured.bpm_confidence,
                        "bpm_alternative": measured.bpm_alternative,
                    }
                )
        return {"ok": True, "tracks": rows}

    def measure_harmonics(self, unit_ids: list[int]) -> dict[str, object]:
        """Measure the key and tempo of the albums that were pointed at.

        A list, because the gesture has two origins — one album from its own
        dialog, and the marked albums from the shelf. Never *every* album: this
        application does not sweep a library, and a gesture that measured
        everything would be that sweep. Nothing is written to a file: the
        answers land in the database and on the screen.
        """
        if self._harmonics_job is not None and self._harmonics_job.is_alive():
            return {"ok": False, "error": "A measurement is already running."}
        # **Refused with its reason, rather than run and reported as nothing.**
        # Without ffmpeg the worker would emit `measured: 0`, which is byte for
        # byte the event a real measurement that read no audio emits — so the
        # window would say "no audio here could be read" about files it never
        # opened, and never mention the binary.
        if self._harmonic_analyzer is None:
            return {"ok": False, "error": FFMPEG_MISSING}
        # One album or many, and both spellings accepted. **The window's script
        # is cached by WebKit across restarts** — the trap `index.html` opens
        # with — so a fresh Python can meet a stale `app.js` that still sends a
        # bare id, which would raise `'int' object is not iterable`. A bridge
        # method that reads either shape costs one line and removes the whole
        # class.
        if isinstance(unit_ids, int):
            unit_ids = [unit_ids]
        wanted = [int(unit_id) for unit_id in unit_ids or []]
        if not wanted:
            return {"ok": False, "error": "Nothing is marked to measure."}
        known = {state.unit_id for state in self._album_snapshot()}
        found = [unit_id for unit_id in wanted if unit_id in known]
        if not found:
            return {"ok": False, "error": "Those albums are not on the shelf."}
        self._harmonics_job = threading.Thread(
            target=self._run_measure_harmonics, args=(found,), daemon=True
        )
        self._harmonics_job.start()
        return {"ok": True, "albums": len(found)}

    def _run_measure_harmonics(self, unit_ids: list[int]) -> None:
        """Decode each album's tracks and remember what they are in."""
        analyzer = self._harmonic_analyzer
        if analyzer is None:
            # Unreachable from the window, which refuses before starting a
            # thread — and still named, because an event that says `measured: 0`
            # and nothing else cannot be told from a measurement that found
            # nothing.
            self._emit("harmonics", {"albums": 0, "measured": 0, "no_ffmpeg": True})
            return
        total = 0
        # Said album by album. Decoding several albums takes minutes, and a
        # screen with no progress for that long is indistinguishable from a
        # screen with nothing to do.
        self._emit("harmonics-started", {"albums": len(unit_ids)})
        for done, unit_id in enumerate(unit_ids, start=1):
            measured = self._measure_one_album(analyzer, unit_id)
            total += measured
            # After each album, so the wheel fills as they land rather than all
            # at once at the end.
            self._emit(
                "harmonics-album",
                {"done": done, "total": len(unit_ids), "measured": total},
            )
        self._emit("harmonics", {"albums": len(unit_ids), "measured": total})

    def _measure_one_album(self, analyzer: HarmonicAnalyzer, unit_id: int) -> int:
        """Measure one album and store what it found, returning how many tracks."""
        state = next((entry for entry in self._album_snapshot() if entry.unit_id == unit_id), None)
        if state is None:
            return 0
        measured: list[StoredHarmonics] = []
        for file in state.unit.audio_files:
            # A file whose audio cannot be named is skipped rather than stored
            # against its signature: a signature is shared by different songs
            # of the same length and format, and a key stored against it could
            # be shown for the wrong recording.
            if not file.audio_key:
                continue
            found = analyzer.analyze(file.path, file.audio_key, file.content_signature)
            if found is None:
                continue
            measured.append(
                StoredHarmonics(
                    audio_key=found.audio_key,
                    content_signature=found.content_signature,
                    tonic=found.key.tonic if found.key else None,
                    mode=found.key.mode if found.key else None,
                    key_confidence=found.key.confidence if found.key else None,
                    bpm=found.tempo.bpm if found.tempo else None,
                    bpm_confidence=found.tempo.confidence if found.tempo else None,
                    bpm_alternative=found.tempo.alternative_bpm if found.tempo else None,
                    key_margin=found.key.margin if found.key else None,
                    runner_up_tonic=found.key.runner_up_tonic if found.key else None,
                    runner_up_mode=found.key.runner_up_mode if found.key else None,
                )
            )
        try:
            self._store.record_harmonics(measured)
        except Exception:
            self._logger.exception(
                "The key and tempo measurements could not be stored.",
                extra={"operation": "harmonic.store.failure", "unit_id": unit_id},
            )
            return 0
        return len(measured)

    def bench_analyze(self, album_ids: list[int]) -> dict[str, object]:
        """Measure the marked albums, properly, and write down nothing.

        Started on a worker thread and reported album by album: this is minutes
        of decoding, and it is the one thing on this screen that costs anything.
        """
        bench = self._pipeline.bench
        if bench is None:
            return {"ok": False, "error": "The bench is not available."}
        if self._bench_job is not None and self._bench_job.is_alive():
            return {"ok": False, "error": "The bench is already busy."}
        wanted = [int(album_id) for album_id in album_ids or []]
        if not wanted:
            return {"ok": False, "error": "Nothing is marked to analyze."}
        self._bench_analysis = None
        self._bench_job = threading.Thread(
            target=self._run_bench_analyze, args=(wanted,), daemon=True
        )
        self._bench_job.start()
        return {"ok": True, "albums": len(wanted)}

    def _run_bench_analyze(self, album_ids: list[int]) -> None:
        bench = self._pipeline.bench
        if bench is None:
            return
        # No pre-emit of its own. A first frame counting *albums* would
        # disagree with every frame after it, since the bench reports files; the
        # bench opens with `done=0` over the real total, a store read away. One
        # statement of one fact.
        try:
            analysis = bench.analyze_in_depth(
                album_ids,
                on_progress=lambda done, total, folder: self._emit(
                    "bench_analyzing", {"done": done, "total": total, "folder": folder}
                ),
            )
        except Exception as error:
            self._logger.exception(
                "A deep measurement could not be finished.",
                extra={"operation": "api.bench.analyze"},
            )
            self._emit("bench_error", {"message": str(error)})
            return
        self._bench_analysis = analysis
        # What it found, what it would change, and what it cost — the three
        # things to be told before anything is asked.
        self._emit(
            "bench_analyzed",
            {
                "albums": len(analysis.albums),
                "files": analysis.files,
                "seconds": round(analysis.seconds, 1),
                "differing": len(analysis.differing),
            },
        )

    def bench_identify(self, album_ids: list[int]) -> dict[str, object]:
        """Ask the audio about the marked albums, on a worker thread.

        Every file is one request against a service that allows about three a
        second, so this is minutes rather than seconds and it says so as it goes.
        It is the only way the acoustic answer ever grows: identification asks
        about an album only when its names failed, which leaves nearly every
        file never asked.
        """
        bench = self._pipeline.bench
        if bench is None:
            return {"ok": False, "error": "The bench is not available."}
        if self._bench_job is not None and self._bench_job.is_alive():
            return {"ok": False, "error": "The bench is already busy."}
        wanted = [int(album_id) for album_id in album_ids or []]
        if not wanted:
            return {"ok": False, "error": "Nothing is marked to identify."}
        self._bench_job = threading.Thread(
            target=self._run_bench_identify, args=(wanted,), daemon=True
        )
        self._bench_job.start()
        return {"ok": True, "albums": len(wanted)}

    def _run_bench_identify(self, album_ids: list[int]) -> None:
        bench = self._pipeline.bench
        if bench is None:
            return
        self._emit("bench_identifying", {"done": 0, "total": len(album_ids), "folder": ""})
        try:
            swept = bench.identify_in_depth(
                album_ids,
                on_progress=lambda done, total, folder: self._emit(
                    "bench_identifying", {"done": done, "total": total, "folder": folder}
                ),
            )
        except Exception as error:
            self._logger.exception(
                "The audio could not be asked about these albums.",
                extra={"operation": "api.bench.identify"},
            )
            self._emit("bench_error", {"message": str(error)})
            return
        # What it cost and what came back, both. `named` is the only honest
        # measure of whether this service knows the music it was asked about.
        self._emit(
            "bench_identified",
            {
                "albums": swept.albums,
                "files": swept.files,
                "named": swept.named,
                "seconds": round(swept.seconds, 1),
            },
        )

    def bench_analysis(self) -> dict[str, object]:
        """Return the deep measurement waiting to be accepted, if there is one."""
        analysis = self._bench_analysis
        if analysis is None:
            return {"ok": True, "pending": False}
        return {
            "ok": True,
            "pending": True,
            "files": analysis.files,
            "seconds": round(analysis.seconds, 1),
            "albums": [_deep_payload(album) for album in analysis.albums],
        }

    def bench_adopt(self, accept: bool = True) -> dict[str, object]:
        """Write the deep measurement down, or drop it, as the caller says.

        Dropping costs nothing but the time already spent: what is stored is
        untouched, and the album goes back to reading whatever it read before.
        """
        bench = self._pipeline.bench
        analysis = self._bench_analysis
        if bench is None or analysis is None:
            return {"ok": False, "error": "There is no measurement waiting."}
        self._bench_analysis = None
        if not accept:
            return {"ok": True, "written": 0}
        written = bench.adopt(analysis)
        # The point of measuring in depth is that the number becomes provably
        # this file's, so it is no longer withheld from a name. Writing the row
        # alone is not enough: unless the store is read again, the plans go on
        # naming the album from the withheld number until the next full scan,
        # and the measurement changes nothing on screen.
        return {"ok": True, "written": written, "replanned": self._replan_from_the_store()}

    def _replan_from_the_store(self) -> int:
        """Re-read what the store now says about every album on the shelf, and re-plan.

        Reaches no ffmpeg and no network — it is the reading of
        ``_facts_from_store`` over the albums the window is holding, and then the
        ordinary rebuild. Called after a gesture that changed what is on record
        rather than what is on disk.
        """
        states = [
            state
            for state in self._album_snapshot()
            if not state.applied and not state.organized and state.outcome.candidate is not None
        ]
        if not states:
            return 0
        self._facts_from_store([state.unit for state in states])
        return sum(1 for state in states if self._replan(state))

    def send_to_bench(self, unit_id: int) -> dict[str, object]:
        """Put one Library album's folder on the bench, from its own right-click.

        The folder rather than the album, because a bench holds folders — and
        pointing at the album's own folder is what makes it arrive alone instead
        of with the whole shelf around it.

        **Wherever that folder is now.** Reading the recorded path and refusing
        when nothing is there would shut this door on an album that was
        organized and then moved, while the other gestures follow it by its
        audio — every file still on the disk, every size still matching the
        row.
        """
        bench = self._pipeline.bench
        if bench is None:
            return {"ok": False, "error": "The bench is not available."}
        state = self._bring_in(int(unit_id))
        if state is None:
            # Same door, same sentence. `That album is no longer on screen`
            # describes the map this call reads and not the album, and an album
            # whose folder moved is exactly the case where the two differ.
            return self._why_it_will_not_open(int(unit_id))
        folder = self._where_it_is_now(state)
        if folder is None:
            return {
                "ok": False,
                "error": (
                    f"{state.unit.folder_path.name} is not where it was, and it was not in any "
                    "folder this app has seen. Drop it on the window and it will be found."
                ),
            }
        try:
            root = bench.add(folder)
        except Exception as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "root": root.root_id, "albums": root.albums}

    def quality_tracks(self, folder: str) -> dict[str, object]:
        """Return what each track of one album measured, and any override on it.

        Fetched per album rather than carried in the survey payload: a large
        library is tens of thousands of tracks, and the screen shows one
        album's worth at a time. The folder is read again here — one folder,
        milliseconds — instead of holding every surveyed unit in memory.
        """
        path = Path(folder).expanduser()
        if not path.is_dir():
            return {"ok": False, "error": f"Not a folder: {folder}"}
        try:
            units = self._pipeline.scanner.scan(path)
        except Exception as error:
            return {"ok": False, "error": str(error)}
        unit = next((entry for entry in units if entry.folder_path == path.resolve()), None)
        if unit is None:
            return {"ok": False, "error": "That folder no longer holds one album."}
        # Asked with the audio key beside the signature, because the files are
        # in hand and their keys were just read off the disk. A signature is a
        # *shape*, worn by provably different recordings, so asking without the
        # key could show one song the wall of another, on the screen used to
        # decide what to keep.
        measured = self._store.quality_for_files(
            (file.content_signature, file.audio_key) for file in unit.audio_files
        )
        words = self._store.quality_words(unit.unit_signature)
        heard = self._answers_about(unit)
        tracks = []
        for file in unit.audio_files:
            quality = measured.get((file.content_signature, file.audio_key))
            answer = heard.get(file.path)
            said = word_for(words, file.content_signature, file.audio_key)
            stands, encoding, reason = _what_stands_about(quality, said)
            tracks.append(
                {
                    "file": file.path.name,
                    "signature": file.content_signature,
                    "encoding": encoding,
                    "transcoded": stands,
                    "borderline": bool(quality and _stored_is_borderline(quality)),
                    "bitrate": quality.effective_bitrate_kbps if quality else None,
                    "cutoff": quality.cutoff_hertz if quality else None,
                    # The rung the audio was still coming out of, so the screen
                    # can say the interval the cut fell in rather than a single
                    # number the measurement does not have. Absent on rows taken
                    # before that was recorded, and the screen says so rather
                    # than drawing an interval it cannot know.
                    "wall_low": quality.wall_low_hertz if quality else None,
                    "decay_db": (
                        round(quality.decay_db, 1)
                        if quality and quality.decay_db is not None
                        else None
                    ),
                    "ceiling_db": (
                        round(quality.ceiling_db, 1)
                        if quality and quality.ceiling_db is not None
                        else None
                    ),
                    "findings": list(quality.findings) if quality else [],
                    "reason": reason,
                    # **Which reading convicted this file, and never the word
                    # alone.** A wall is a cliff anybody can see in the picture
                    # the button beside this row draws; the encoder's frame
                    # grid is invisible in one, and a file caught by it draws a
                    # spectrum that looks perfectly healthy. Saying only `was
                    # lossy` about both makes a grid conviction look like a
                    # mistake to anyone who checks the spectrum.
                    "proved_by": (
                        (str(proof) if (proof := stored_track_proof(quality)) else None)
                        if quality
                        else None
                    ),
                    # **And whether the picture beside it shows any of it.**
                    # `proved_by` names one proof and a file can carry two: the
                    # note that warns *the picture will not show this one* must
                    # not be drawn on a grid conviction that also stops at a
                    # wall the picture itself shows. Computed where the
                    # proof is, and sent named, so the window never rebuilds it
                    # from `cutoff` and a threshold that lives on this side.
                    "picture_shows_it": bool(quality and the_picture_shows_the_proof(quality)),
                    # The numbers the grid sentence quotes, so the screen states
                    # them rather than asserting a conclusion nobody can check.
                    "frame_grid_z": (
                        round(quality.frame_grid_z, 1)
                        if quality and quality.frame_grid_z is not None
                        else None
                    ),
                    "frame_grid_agrees": (quality.frame_grid_agrees if quality else None),
                    # Whether the row that answered was taken from *this* audio.
                    # A row with no audio key beside it may have been measured
                    # from another recording of the same shape, and the screen
                    # needs this to say so.
                    "measured_from_this_file": bool(quality and quality.is_this_file),
                    "override": said,
                    # Which audio the word would be about, so two files of one
                    # album that share a signature can be told apart when a
                    # word is said about one of them.
                    "audio_key": file.audio_key,
                    # Three states, not two: never asked, asked and named, asked
                    # and unknown. Collapsing the last two hides that the
                    # service was asked at all.
                    "heard": (
                        "named"
                        if answer and answer.recording_id
                        else "unknown" if answer and answer.asked else ""
                    ),
                    "recording_id": answer.recording_id if answer else None,
                    "recording_score": answer.score if answer else None,
                }
            )
        return {
            "ok": True,
            "signature": unit.unit_signature,
            "album_override": words.get(("", None)),
            "tracks": tracks,
            # What the audio managed to say about this album as a whole, so
            # that music the service does not hold is visible as a number
            # rather than as a blank column.
            "acoustic": {
                "named": sum(1 for track in tracks if track["heard"] == "named"),
                "unknown": sum(1 for track in tracks if track["heard"] == "unknown"),
                "tracks": len(tracks),
            },
        }

    def say_about_every_track(self, folder: str, verdict: str) -> dict[str, object]:
        """Record one word about every track of an album at once.

        **It writes track words, never an album word.** Only a file can have
        been lossy before it arrived, so only a file is asked. That rule is
        kept; what this removes is the labour of marking every track of an
        album one at a time, when the album is the majority of its tracks
        either way.

        So this is exactly those presses, and the album ends in the same state
        it would have. ``clear`` withdraws the word from every track, which is
        the way back the single-track control already has.

        One replan at the end rather than one per track: the replan is what
        makes a word reach the name a plan would write, and doing it once per
        track would spend that many rebuilds to arrive at the answer the last
        one gives.
        """
        if verdict not in {"honest", "transcoded", "clear"}:
            return {"ok": False, "error": f"Unknown verdict: {verdict}"}
        path = Path(folder).expanduser()
        if not path.is_dir():
            return {"ok": False, "error": f"Not a folder: {folder}"}
        try:
            units = self._pipeline.scanner.scan(path)
        except Exception as error:
            return {"ok": False, "error": str(error)}
        unit = next((entry for entry in units if entry.folder_path == path.resolve()), None)
        if unit is None:
            return {"ok": False, "error": "That folder no longer holds one album."}
        for file in unit.audio_files:
            if verdict == "clear":
                self._store.clear_quality_override(
                    unit.unit_signature, file.content_signature, file.audio_key
                )
            else:
                self._store.record_quality_override(
                    unit.unit_signature, verdict, file.content_signature, file.audio_key
                )
        self._logger.info(
            "A quality verdict was set on every track of an album.",
            extra={
                "operation": "quality.override.every_track",
                "verdict": verdict,
                "tracks": len(unit.audio_files),
            },
        )
        replanned = self._replan_everything_named_by(unit.unit_signature)
        return {
            "ok": True,
            "tracks": len(unit.audio_files),
            "overrides": self._store.quality_overrides(unit.unit_signature),
            "replanned": replanned,
        }

    def track_spectrogram(self, folder: str, file_name: str) -> dict[str, object]:
        """Draw one track's spectrogram and hand back the picture and its scale.

        Nothing is drawn until this is called, and it is only ever called by a
        press: no picture is generated automatically, not even for a track the
        measurement marked. So the cost is always one file that was asked
        about, and the cache is filled only by request.

        The reading beside the picture is the one the analyzer already made.
        Nothing here reads a verdict off pixels — a flat-floor reading of the
        picture was refuted by measurement and deliberately not built.
        """
        if self._spectrograms is None:
            return {
                "ok": False,
                "error": (
                    "ffmpeg was not found, so no spectrogram can be drawn. "
                    "Install it and reopen DigLibrary."
                ),
            }
        path = Path(folder).expanduser()
        if not path.is_dir():
            return {"ok": False, "error": f"Not a folder: {folder}"}
        try:
            units = self._pipeline.scanner.scan(path)
        except Exception as error:
            return {"ok": False, "error": str(error)}
        unit = next((entry for entry in units if entry.folder_path == path.resolve()), None)
        if unit is None:
            return {"ok": False, "error": "That folder no longer holds one album."}
        audio = next((file for file in unit.audio_files if file.path.name == file_name), None)
        if audio is None:
            # Named, not merely refused: a file that has been renamed or moved
            # since the screen was drawn is the ordinary reason to be here.
            return {"ok": False, "error": f"This album no longer holds a file named {file_name}."}
        drawn = self._spectrograms.render(
            audio.path,
            audio.content_signature,
            self._spectrogram_limit_bytes(),
            # Handed over from the scan that just read this folder, because the
            # renderer would otherwise only learn the rate from ffmpeg's own
            # report — which arrives after the picture it decides the shape of.
            sample_rate=audio.properties.sample_rate if audio.properties else None,
        )
        if drawn is None:
            return {"ok": False, "error": f"The spectrogram of {file_name} could not be drawn."}
        # With the key, because this is the highest-stakes place in the
        # application for a borrowed number: these become markers drawn *on
        # this file's picture*, and the picture is what a verdict is overruled
        # with. A 16 kHz line on a spectrogram whose energy plainly runs to 22
        # kHz does not merely lie — it makes the lie look confirmed by the
        # reader's own eye.
        measured = self._store.quality_for_files(((audio.content_signature, audio.audio_key),)).get(
            (audio.content_signature, audio.audio_key)
        )
        return {
            "ok": True,
            "file": file_name,
            "signature": audio.content_signature,
            "image": _data_uri(drawn.path),
            "width": drawn.width,
            "height": drawn.height,
            # What turns a frequency into a row: the height spans zero to here,
            # linearly. Measured, not assumed (see `quality/spectrogram.py`).
            # **Named for what it is, which is the top drawn and not always the
            # stream's Nyquist limit** — above `DRAWN_TOP_HERTZ` the picture is
            # capped, because a 96 kHz stream would spend more than half of its
            # height on frequencies music does not reach.
            "top_hertz": drawn.top_hertz,
            # The wall the analyzer found, so the window can mark it on the
            # picture and the two can be seen to agree — or not.
            "cutoff": measured.cutoff_hertz if measured else None,
            # And the rungs it could have landed on. `cutoff_hertz` is the lowest
            # *probed* band that measured silent, so it is one of five values and
            # not a frequency: a cutoff of 20 kHz means the 19 kHz band still had
            # energy and the 20 kHz band did not, so the audio stops somewhere
            # between them. A hairline drawn at the number would be a precision
            # the measurement does not have, so the window is given the rungs
            # and draws the interval.
            "probes": list(PROBE_FREQUENCIES),
            # Where the common encoders habitually stop, so the picture can be
            # read against them. Sent from here rather than written into the
            # window, because these are the same numbers the verdict uses
            # (`REFERENCE_WALLS`) — two copies would be two answers once one of
            # them moves. Only the rungs this stream can actually hold: a mark
            # above a file's own Nyquist limit would be a mark placed off the top
            # of its picture.
            "encoder_walls": [
                {"hertz": hertz, "bitrate": bitrate}
                for hertz, bitrate in REFERENCE_WALLS
                if hertz < drawn.top_hertz
            ],
            "decay_db": (
                round(measured.decay_db, 1) if measured and measured.decay_db is not None else None
            ),
            "ceiling_db": (
                round(measured.ceiling_db, 1)
                if measured and measured.ceiling_db is not None
                else None
            ),
            "from_cache": drawn.from_cache,
            "cache_bytes": self._spectrograms.held_bytes(),
        }

    def clear_spectrograms(self) -> dict[str, object]:
        """Delete every drawn spectrogram, and say how much disk came back.

        The pictures only. No audio file, no album folder, nothing in the
        library — the same line `Clear downloaded` holds, and it is worth
        repeating because the cost of getting it wrong is the music itself.
        """
        if self._spectrograms is None:
            return {"ok": True, "freed": 0}
        return {"ok": True, "freed": self._spectrograms.clear()}

    def _spectrogram_limit_bytes(self) -> int:
        """How much disk the pictures may hold, as set in Settings."""
        return self._limit_bytes("spectrogram_cache_mb", DEFAULT_CACHE_LIMIT_BYTES)

    def _limit_bytes(self, setting: str, fallback: int) -> int:
        """Read one cache ceiling out of Settings, in bytes.

        A blank field, a word, or a number at or below zero all mean the same
        thing here — *no ceiling has been set* — and all answer with the
        default. A ceiling of zero would be a folder that discards everything
        it writes, which is not a setting anybody wants and is a very expensive
        typo.
        """
        try:
            wanted = int(float(str(self._settings.get(setting))))
        except (TypeError, ValueError):
            return fallback
        return wanted * MEGABYTE if wanted > 0 else fallback

    def connections(self) -> dict[str, object]:
        """Report which optional services have a key, and never what the key is.

        Booleans only. The window has no reason to hold a token and every reason
        not to: it is drawn from a page, and a value that reaches the page is a
        value that can reach a screenshot.
        """
        return {
            "ok": True,
            "connected": credentials.connected(),
            # Whether the welcome has been through once. It is what decides
            # whether the window opens on it, and it is a fact about this
            # installation rather than about any album.
            "welcomed": bool(self._settings.get("welcomed")),
        }

    def connect(self, service: str, value: str) -> dict[str, object]:
        """Store one service key, or remove it when the value is empty.

        Written to `~/.diglibrary/env` and into this process at once, so the
        very next request uses it — see `config.credentials` for why both.
        """
        variable = CONNECTABLE.get(service)
        if variable is None:
            return {"ok": False, "error": f"Unknown service: {service}"}
        try:
            credentials.store(variable, value)
        except Exception as error:
            # Deliberately not `str(error)` alone in the log: an exception raised
            # while handling a token is the one place a token could reach a
            # record. The window is told it failed and the log is told where.
            self._logger.error(
                "A service key could not be stored.",
                extra={"operation": "credentials.store.failure", "service": service},
            )
            return {"ok": False, "error": f"That key could not be saved: {type(error).__name__}"}
        self._logger.info(
            "A service key was set from the window.",
            extra={
                "operation": "credentials.store",
                "service": service,
                "present": bool(value.strip()),
            },
        )
        # The pieces that read a key read it when they are built, and they were
        # built before the key was pasted. Discogs and MusicBrainz resolve
        # theirs per request; the acoustic path is composed from the
        # environment, so without a rebuild `Connected services` would report
        # connected while nothing was connected until a restart. Rebuilding here
        # is what the settings panel does for every other change of composition.
        try:
            self._pipeline = self._pipeline_factory(self._settings)
        except Exception:
            self._logger.exception(
                "A service key was stored but the engine could not be rebuilt around it.",
                extra={"operation": "credentials.recompose.failure", "service": service},
            )
        return {"ok": True, "connected": credentials.connected()}

    def welcomed(self) -> dict[str, object]:
        """Record that the welcome has been through, so it does not open again."""
        self._settings["welcomed"] = True
        self._store.set_setting("ui.welcomed", "True")
        return {"ok": True}

    def cache_sizes(self) -> dict[str, object]:
        """What the bounded folders hold right now, for Settings."""
        return {
            "ok": True,
            "spectrograms": self._spectrograms.held_bytes() if self._spectrograms else 0,
            "metadata": self._metadata_cache.held_bytes() if self._metadata_cache else 0,
            "artwork": folder_bytes(self._artwork_cache, ARTWORK_SUFFIXES),
            "backups": folder_bytes(self._backups, ARTWORK_SUFFIXES),
            # Not discardable like the four above — these are the only record of
            # the library outside the live database — but bounded, so the line
            # that says what is held says this too.
            "database_copies": (
                database_copies_bytes(self._database_copies)
                if self._database_copies is not None
                else 0
            ),
        }

    def clear_metadata_cache(self) -> dict[str, object]:
        """Delete every cached catalogue answer, and say how much disk came back.

        Nothing identified is lost by this. What a release *is* lives in the
        database; this folder holds the HTTP answers that were read to find out,
        so the cost of emptying it is asking Discogs again.
        """
        if self._metadata_cache is None:
            return {"ok": True, "freed": 0}
        return {"ok": True, "freed": self._metadata_cache.clear()}

    def clear_artwork_cache(self) -> dict[str, object]:
        """Delete the staged pictures and the drawn sleeves, and say what came back.

        Only those two folders, and only picture files inside them — never one
        level down, and never the library. A cover already written into an album
        is a file in that album and is not touched here.
        """
        return {"ok": True, "freed": clear_folder(self._artwork_cache, ARTWORK_SUFFIXES)}

    def _enforce_cache_limits(self) -> None:
        """Bring the discardable folders under their ceilings.

        Called where nothing is mid-flight — once the library has finished being
        restored, and again whenever Settings is saved — rather than during a
        run. A scan that evicted its own staged picture between staging it and
        writing it would be this module reaching into work in progress.
        """
        try:
            if self._metadata_cache is not None:
                self._metadata_cache.enforce_limit(
                    self._limit_bytes("metadata_cache_mb", DEFAULT_METADATA_CACHE_BYTES),
                    self._logger,
                )
            if self._artwork_cache:
                enforce_folder_limit(
                    self._artwork_cache,
                    self._limit_bytes("artwork_cache_mb", DEFAULT_ARTWORK_CACHE_BYTES),
                    ARTWORK_SUFFIXES,
                    self._logger,
                    "artwork.cache.evicted",
                )
            if self._backups:
                enforce_folder_limit(
                    self._backups,
                    self._limit_bytes("backup_mb", DEFAULT_BACKUP_LIMIT_BYTES),
                    ARTWORK_SUFFIXES,
                    self._logger,
                    "artwork.backup.evicted",
                )
            # The copies of the database, brought under their ceiling in the
            # same quiet moment as everything else — and by their own function,
            # because the newest one is never discarded here (`copies` says why)
            # and the shared helper would take it.
            if self._database_copies is not None:
                enforce_copy_limit(
                    self._database_copies,
                    self._limit_bytes("database_copies_mb", DEFAULT_COPY_LIMIT_BYTES),
                    self._logger,
                )
        except Exception:
            # Housekeeping must never be what stops the window from opening.
            self._logger.exception(
                "The cache folders could not be brought under their limits.",
                extra={"operation": "cache.limit.failure"},
            )

    def set_quality_verdict(
        self,
        unit_signature: str,
        verdict: str,
        content_signature: str = "",
        audio_key: str = "",
    ) -> dict[str, object]:
        """Record the user's own verdict on an album, or on one track of it.

        ``verdict`` of ``clear`` withdraws that word and lets the measurement
        speak again. Nothing is renamed here: this changes what the next plan
        would write, and the plan is still approved by hand.
        """
        if verdict == "clear":
            self._store.clear_quality_override(unit_signature, content_signature, audio_key or None)
        elif verdict in {"honest", "transcoded"}:
            self._store.record_quality_override(
                unit_signature, verdict, content_signature, audio_key or None
            )
        else:
            return {"ok": False, "error": f"Unknown verdict: {verdict}"}
        self._logger.info(
            "A quality verdict was set by hand.",
            extra={
                "operation": "quality.override",
                "verdict": verdict,
                "whole_album": not content_signature,
            },
        )
        # The word has to reach the screen that acts on it. Written and not
        # replanned, it would leave an album named `[Lossy]` about tracks that
        # had been cleared by hand, one `Approve & apply` away from that word
        # going onto the disk.
        replanned = self._replan_everything_named_by(unit_signature)
        return {
            "ok": True,
            "overrides": self._store.quality_overrides(unit_signature),
            "replanned": replanned,
        }

    def _run_quality(self, root: Path, excluded: tuple[Path, ...]) -> None:
        survey = self._pipeline.quality
        if survey is None:
            self._emit("quality_error", {"message": "ffmpeg is not available to measure audio."})
            return
        try:
            units = survey.albums(root, excluded)
        except Exception as error:
            self._emit("quality_error", {"message": str(error)})
            return
        self._emit("quality_started", {"albums": len(units)})

        def report(index: int, total: int, finding: AlbumQuality) -> None:
            payload = _quality_payload(finding, root)
            self._quality.append(payload)
            self._emit("quality_measured", {"done": index, "total": total, "album": payload})

        try:
            findings = survey.measure(units, on_album=report)
        except Exception as error:
            self._emit("quality_error", {"message": str(error)})
            return
        self._emit(
            "quality_finished",
            {
                "albums": len(findings),
                "suspect": sum(1 for finding in findings if finding.is_suspect),
            },
        )

    def apply_automatic(self, unit_ids: list[int] | None = None) -> dict[str, object]:
        """Apply the automatic, applicable plans that are marked.

        **The marked ones, not every one.** Writing to every automatic album in
        the library would make a button reading `Apply 2 automatic`, over a
        shelf with nothing ticked, offer to organize records that were not
        chosen and cannot be seen. Marking is how this window says *these*
        everywhere else in it; the one gesture that writes to files is not the
        exception.

        `None` still means every one, because the covers-only batch below and the
        window's own older callers ask that way; the Library sends its marks.

        The batch also applies the pending cover-only plans, for the albums it
        did not just organize: an album applied here had its folder renamed and
        its art decided by its own plan, so its stale cover plan is discarded
        instead.
        """
        chosen = None if unit_ids is None else {int(unit_id) for unit_id in unit_ids}
        applied = 0
        failures: list[str] = []
        batch: list[int] = []
        for state in self._album_snapshot():
            ready = (
                (chosen is None or state.unit_id in chosen)
                and state.outcome.decision is Decision.AUTOMATIC
                and not state.applied
                and not state.rejected
                and state.outcome.plan is not None
                and state.outcome.plan.is_applicable
                # A folder that is not there is not a plan that can run, so it
                # is neither counted nor offered.
                and state.unit.folder_path.is_dir()
                # No batch gesture reaches an arrangement. Enforcing that where
                # the arrangement is *built* is not enough, because a restart
                # does not build it; the one place it can be enforced for
                # certain is the gesture that writes.
                and not _is_arrangement(state.outcome)
            )
            if not ready:
                continue
            error = self._apply(state, decided_by="automatic")
            if error is None:
                applied += 1
                if state.plan_id is not None:
                    batch.append(state.plan_id)
            else:
                failures.append(f"{state.unit.folder_path.name}: {error}")
        covered: list[str] = []
        # **The marks govern this half too.** Scoped only on the organizing
        # half, a shelf with nothing ticked would still offer `Write 1 embedded
        # cover` and, on the click, write a picture into an album that was not
        # chosen.
        covers = self._apply_pending_covers(failures, batch, covered, chosen)
        run_id = None
        if batch:
            # One gesture, one run: what this click applied can be undone as
            # one deliberate reversal, in this session or after a restart.
            run_id = f"run-{batch[0]}-{len(batch)}"
            self._store.assign_run(batch, run_id)
        return {
            "ok": not failures,
            "applied": applied,
            "covers": covers,
            # Which folders, not just how many: a count is a fact that cannot
            # be acted on.
            "cover_albums": covered,
            "failures": failures,
            "run_id": run_id,
            "certificate": self._batch_certificate(),
        }

    def revert_run(self, run_id: str, confirmed: bool = False) -> dict[str, object]:
        """Undo everything one apply gesture did, most recent plan first.

        The question about an album that has been filed away is asked **once**
        for the whole run, and before any of it is undone. Asked per plan it
        would be a row of identical dialogs over one gesture, and — worse —
        the first albums would already be back before the question about the
        fourth was asked, which is a run half undone by an unanswered question.
        """
        plan_ids = self._store.run_plans(run_id)
        if not confirmed:
            filed = [
                asked
                for asked in (self._revert_asks_first(plan_id) for plan_id in plan_ids)
                if asked is not None
            ]
            if filed:
                return {
                    "ok": False,
                    "confirm": "filed_away",
                    "run_id": run_id,
                    "albums": len(filed),
                    "folder": filed[0]["folder"],
                    "parent": filed[0]["parent"],
                }
        reverted = 0
        failures: list[str] = []
        for plan_id in plan_ids:
            outcome = self.revert(plan_id, confirmed=True)
            if outcome.get("ok"):
                reverted += 1
            else:
                failures.append(str(outcome.get("error")))
        return {"ok": not failures, "reverted": reverted, "failures": failures}

    def _revert_asks_first(self, plan_id: int) -> dict[str, str] | None:
        """Report the folder a plan's reversal would write in, when it is not where it ran."""
        trail = self._store.applied_trail(plan_id)
        if not trail:
            return None
        was, now = self._where_the_trail_leads(trail, self._store.plan_unit(plan_id))
        if was == now:
            return None
        return {"folder": now.name, "parent": str(now.parent)}

    def redo_run(self, run_id: str) -> dict[str, object]:
        """Apply again, exactly, what reverting one run undid.

        The mirror of ``revert_run``, walking the run forward from its oldest
        plan because that is the order it originally ran in.
        """
        redone = 0
        failures: list[str] = []
        plan_ids = self._store.reverted_run_plans(run_id)
        if not plan_ids:
            return {
                "ok": False,
                "redone": 0,
                "failures": ["Nothing in that run is reverted, so there is nothing to do again."],
            }
        for plan_id in plan_ids:
            outcome = self.redo_plan(plan_id)
            if outcome.get("ok"):
                redone += 1
            else:
                failures.append(str(outcome.get("error")))
        return {"ok": not failures, "redone": redone, "failures": failures}

    def _batch_certificate(self) -> dict[str, object]:
        """Summarize the proofs of everything applied so far: the trust line."""
        snapshot = self._album_snapshot()
        audio_states = [s.audio_ok for s in snapshot if s.audio_ok is not None]
        write_states = [s.writes_ok for s in snapshot if s.writes_ok is not None]
        return {
            "audio_checked": len(audio_states),
            "audio_intact": sum(1 for ok in audio_states if ok),
            "writes_checked": len(write_states),
            "writes_verified": sum(1 for ok in write_states if ok),
        }

    def _apply_pending_covers(
        self,
        failures: list[str],
        batch: list[int] | None = None,
        written: list[str] | None = None,
        chosen: set[int] | None = None,
    ) -> int:
        """Apply the pending cover-only plans, discarding those made stale.

        ``written`` collects the folders a cover actually reached, because a
        count alone does not say which album was touched.

        ``chosen`` is the set of marked albums, and ``None`` means every one —
        the same contract ``apply_automatic`` reads its own argument by. Walking
        the whole library regardless would let a click over a shelf with
        nothing ticked write a picture into a file that was not chosen.
        """
        covers = 0
        for state in self._album_snapshot():
            if chosen is not None and state.unit_id not in chosen:
                continue
            if state.cover_plan is None or state.cover_applied:
                continue
            if not _plan_still_points_at_the_disk(state.cover_plan):
                # **Asked of the disk, not of a flag.** A cover plan made before
                # the album's own plan ran names paths that plan then moved, so
                # it is stale; one made afterwards, from where the files are
                # now, is not — and `state.applied` is true of both. Reading the
                # flag would discard a cover chosen for an album already
                # applied. What makes a plan stale is that what it names is
                # gone, and that is a question with an answer.
                if state.cover_plan_id is not None:
                    self._store.record_plan_outcome(state.cover_plan_id, "discarded")
                state.cover_plan = None
                state.cover_chosen_by_user = False
                continue
            cover_plan_id = state.cover_plan_id
            try:
                with self._apply_lock:
                    result = self._pipeline.executor.apply(
                        state.cover_plan,
                        witness=self._witness(cover_plan_id),
                    )
            except Exception as error:
                failures.append(f"{state.unit.folder_path.name}: {error}")
                continue
            if state.cover_plan_id is not None:
                self._store.record_execution(
                    state.cover_plan_id, result.applied, complete=result.is_complete
                )
            # **A chosen cover reaches inside the files.** The automatic cover
            # plan writes one picture beside them and touches no audio; a chosen
            # one reaches all three places a cover lives, so every track is
            # heavier than the row that describes it the moment this returns.
            # Nothing renames here, so the paths this weighs are the ones the
            # album already stands at.
            #
            # **Weighed whether or not the plan finished.** A cover plan that
            # stops has still put the picture into the tracks it reached, and
            # those files are heavier than their rows either way.
            self._follow_the_bytes(file.path for file in state.unit.audio_files)
            if result.state is ExecutionState.APPLIED:
                state.cover_applied = True
                covers += 1
                if written is not None:
                    written.append(state.unit.folder_path.name)
                if batch is not None and state.cover_plan_id is not None:
                    batch.append(state.cover_plan_id)
                # And the write is certified like any other.
                writes, writes_ok = self._verify_writes(state.cover_plan, {})
                if state.cover_plan_id is not None:
                    self._store.record_proof(
                        state.cover_plan_id, "writes", writes_ok, {"writes": writes}
                    )
            else:
                failures.append(
                    f"{state.unit.folder_path.name}: "
                    f"{result.failure or 'the cover could not be written'}"
                )
        return covers

    def approve(self, unit_id: int, partial: bool = False) -> dict[str, object]:
        """Apply one album's plan because the user said so.

        With ``partial``, the plan applied is the one that renames and tags
        only what paired, leaving every unmatched file exactly as it is.
        Nothing unattended ever reaches this path.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        plan = state.outcome.partial_plan if partial else state.outcome.plan
        if plan is None or not plan.is_applicable:
            return {"ok": False, "error": "This album has no applicable plan."}
        plan_id = state.plan_id
        if partial:
            # The strict plan is what was recorded at identification; the
            # partial one is a different set of operations and gets its own row,
            # so the trail that reverts it is exactly what it did.
            plan_id = self._store.record_plan(
                state.unit_id, state.identification_id, plan.operations
            )
        error = self._apply(state, decided_by="user", plan=plan, plan_id=plan_id)
        return {"ok": error is None, "error": error, "partial": partial}

    def arrange_by_tags(self, unit_id: int) -> dict[str, object]:
        """Propose this one album arranged by its own tags, on request.

        The explicit gesture, and explicitly stronger than the defaults that
        govern the automatic path: it works on an album older than the
        watermark, and on one a scan has answered — it is asked for by name, on
        one album, so the guards that exist to stop *unasked* repainting do not
        get a vote. Only the protection of organized albums holds its shape: an
        organized album gets a proposal to look at, never a batch.

        Proposes only. Nothing is renamed until it is approved.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        # The same rule as the other two gestures that replace a proposal: what
        # can be asked for can be taken back. Leaving this one out would mean
        # some gestures with a way back and others without, told apart by
        # nothing on screen.
        self._keep_for_cancel(state, "arranged_by_tags")
        was = state.unit.folder_path
        found = self._where_it_is_now(state)
        if found is None:
            return {
                "ok": False,
                "error": (
                    f"{state.unit.folder_path.name} is not where it was, and it was not in any "
                    "folder this app has seen. Drop it on the window and it will be found."
                ),
            }
        self._facts_from_store([state.unit])
        try:
            outcome = self._pipeline.workflow.name_from_tags(
                state.unit,
                frozenset(),
                self._transcoded_for(state.unit),
                self._rates,
            )
        except Exception as error:
            self._logger.exception(
                "An album could not be arranged from its tags.",
                extra={"operation": "api.arrange.failure", "album": unit_id},
            )
            return {"ok": False, "error": str(error)}
        if outcome is None:
            return {"ok": False, "error": self._tags_say_too_little(state.unit)}
        state.outcome = outcome
        state.applied = False
        state.written = False
        state.rejected = False
        state.held = False
        # **Whether a catalogue has ever answered for this album, not whether
        # this proposal came from one.** Arranging by tags asks nobody anything,
        # so the album must not come out of it claiming a catalogue answered.
        # But `_a_catalogue_answered` reads the outcome, which is the
        # arrangement — so on its own it would make an album that *had* been
        # looked up claim it never had, and its card would go back to `not
        # scanned`.
        #
        # An album dropped on the window arrives arranged from its own tags
        # with no identification on record, and `not scanned` is exactly what
        # it is. The row is what tells the two apart.
        state.looked_at = (
            _a_catalogue_answered(outcome)
            or self._store.identification_for(state.unit_id) is not None
        )
        # The ids of whatever stood before are not this proposal's: applying
        # under a scan's plan row would hang the arrangement's renames on an
        # identification that says something else. `_apply` earns fresh ones at
        # the moment of writing, which records `existing_tags`.
        state.identification_id = None
        state.plan_id = None
        return {"ok": True, "album": self._album_payload(state), "moved": _followed(was, found)}

    def _tags_say_too_little(self, unit: AlbumUnit) -> str:
        """Say which words these files lack, for an arrangement that cannot be made."""
        reading = read_tags(unit, self._pipeline.tag_store)
        wanting = "; ".join(reading.missing) or "the tags do not say enough"
        return f"These tags cannot arrange this album: {wanting}."

    def arrange_marked(self, unit_ids: list[int]) -> dict[str, object]:
        """Arrange the marked albums by their own tags and apply them.

        The offline batch: no catalogue is asked, folders and files are renamed
        from what the tags already say, and every rename lands in History. It
        never shares a button with the scan's `Apply N automatic`, because the
        two proposals are different claims and are kept apart.

        Three refusals per album, each counted and said: organized stays
        organized (the dialog's own gesture is the way in); an album a scan has
        answered keeps the catalogue's answer; and tags that do not say enough
        arrange nothing. Failures skip, never abort: one unreadable album must
        not cost the rest of the batch.
        """
        wanted = [int(unit_id) for unit_id in unit_ids]
        with self._albums_lock:
            states = [self._albums[unit_id] for unit_id in wanted if unit_id in self._albums]
        if not states:
            return {"ok": False, "error": "No album is marked."}
        if self._job is not None and self._job.is_alive():
            return {"ok": False, "error": "A scan is already running."}
        # A folder being read waits for renames, and renames wait for a folder
        # being read, so neither records a path the other is about to move.
        if self._reading():
            return {"ok": False, "error": "A folder is still being read. Try again in a moment."}
        self._job = threading.Thread(
            target=self._run_arrange, args=(states,), daemon=True, name=_WRITES_FILES
        )
        self._job.start()
        return {"ok": True, "albums": len(states)}

    def _run_arrange(self, states: list["_AlbumState"]) -> None:
        """Arrange and apply each marked album on the worker thread, then report."""
        arranged = 0
        skipped: dict[str, int] = {"organized": 0, "scanned": 0, "wanting": 0, "failed": 0}
        claimed: set[str] = set()
        try:
            for state in states:
                if state.organized or state.applied:
                    skipped["organized"] += 1
                    continue
                if self._store.identification_for(state.unit_id) is not None:
                    skipped["scanned"] += 1
                    continue
                try:
                    if self._where_it_is_now(state) is None:
                        skipped["failed"] += 1
                        continue
                    self._facts_from_store([state.unit])
                    outcome = self._pipeline.workflow.name_from_tags(
                        state.unit,
                        frozenset(claimed),
                        self._transcoded_for(state.unit),
                        self._rates,
                    )
                except Exception:
                    self._logger.exception(
                        "An album could not be arranged from its tags.",
                        extra={"operation": "api.arrange.failure", "album": state.unit_id},
                    )
                    skipped["failed"] += 1
                    continue
                if outcome is None:
                    skipped["wanting"] += 1
                    continue
                claimed.add(_planned_folder_name(state.unit, outcome.plan))
                if outcome.plan is None or not outcome.plan.is_applicable:
                    # A collision, or a plan that cannot close. The proposal is
                    # still put on screen, so what the batch refused is readable
                    # on the album itself.
                    state.outcome = outcome
                    skipped["wanting"] += 1
                    continue
                state.outcome = outcome
                error = self._apply(state, decided_by="user")
                if error is None:
                    arranged += 1
                else:
                    skipped["failed"] += 1
        finally:
            # Always, including nothing — a window waiting on an event an
            # exception swallowed is a screen that never updates.
            self._emit(
                "arranged",
                {"albums": arranged, "skipped": {k: v for k, v in skipped.items() if v}},
            )

    def plan_anyway(self, unit_id: int) -> dict[str, object]:
        """Plan one finished album again, around the release it was organized as.

        The gesture is per album and never in bulk: skipping what is already
        organized exists so that a rename made by hand survives a scan of the
        whole library, and a button that undoes that for everything at once
        would give the guarantee back with one click.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if not (state.organized or state.applied):
            return {"ok": False, "error": "This album is already planned."}
        # Before anything is replaced, and before the folder is even looked for:
        # the photograph is of the album as it stands on screen.
        self._keep_for_cancel(state, "planned_again")
        was = state.unit.folder_path
        found = self._where_it_is_now(state)
        if found is None:
            return {
                "ok": False,
                "error": (
                    f"{state.unit.folder_path.name} is not where it was, and it was not in any "
                    "folder this app has seen. Drop it on the window and it will be found."
                ),
            }
        try:
            # What is on record about this album's audio, including any word
            # said about it since. This is the one gesture that gives an
            # organized album a plan, and the map it reads is filled by a scan:
            # without this read a verdict given afterwards on the Quality bench
            # would be invisible here, and an album marked honest would still
            # plan `[Lossy]`.
            self._facts_from_store([state.unit])
            # **Around the release on record, and no catalogue is asked.** The
            # gesture opens a finished album for editing, so the screen it opens
            # has to be the album as it was applied: that release, those typed
            # names, those pairings. A search promises none of it. It lands on
            # whichever release ranks first, which is another record whenever
            # this one was chosen by hand, and it lands on nothing while a
            # catalogue is unreachable. Another release is still one gesture
            # away, by the search and by the pasted link the dialog offers.
            outcome = self._outcome_on_record(state.unit, state.unit_id)
            if outcome is not None and _is_arrangement(outcome):
                # **An album arranged from its own tags goes back to its
                # arrangement.** What stands on its record is the files' own
                # words, so they are read again and arranged again, with what
                # was typed over them put back. The record's copy of those
                # words is not planned from: handed to the planner as a
                # release, it would write tags back into the files they were
                # read out of, which an arrangement never does.
                arranged = self._pipeline.workflow.name_from_tags(
                    state.unit, frozenset(), self._transcoded_for(state.unit), self._rates
                )
                if arranged is None:
                    return {"ok": False, "error": self._tags_say_too_little(state.unit)}
                outcome = self._with_corrections(state.unit_id, state.unit, arranged, offline=True)
            elif outcome is None:
                # Nothing stands on record for this album: it was organized
                # before any record was kept, or the release it names has left
                # the cache. There is nothing to go back to, so this is where
                # the question is asked.
                outcome = self._with_corrections(
                    state.unit_id,
                    state.unit,
                    self._pipeline.workflow.identify(
                        state.unit, frozenset(), self._transcoded_for(state.unit), self._rates
                    ),
                )
        except Exception as error:
            self._logger.exception(
                "An album could not be identified.",
                extra={"operation": "api.identify.failure"},
            )
            return {"ok": False, "error": f"That album could not be identified: {error}"}
        state.outcome = outcome
        _organized_only_while_nothing_changes(state)
        state.identification_id, state.plan_id = self._persist_outcome(state)
        # This gesture, and only this one, opens an organized album's names for
        # typing. Set after `_persist_outcome`, which clears `written`: the two
        # facts are written in the order the window reads them.
        state.planned_again = True
        return {"ok": True, "album": self._album_payload(state), "moved": _followed(was, found)}

    def _organized_on_record(self, state: _AlbumState) -> bool:
        """Whether this album's files are in place, as the database holds it.

        **Not `state.organized`, which is a different question.** That flag is
        cleared the moment a re-plan produces something to write, by design —
        so on the one screen `Reject` needs guarding, an organized album with a
        fresh proposal on it, the flag is already False and a guard reading it
        would never fire.

        `album_units.state` is the word that survives the re-plan and the word
        `ids_in_state` reads to leave a finished album alone, which is what
        `skipped` would take away. So it is the one to ask.
        """
        if state.applied:
            return True
        stored = self._store.unit_by_id(state.unit_id)
        if stored is not None and stored.state == "organized":
            return True
        # **And the row moves too.** Organize an album, plan it again, type one
        # name — and the row goes from `organized` to `identified`, because
        # giving the album something to write is what clears the flag that
        # decides the word. So the last question is about the disk and nothing
        # else: has a plan for this album been applied and not reverted?
        return self._store.files_were_written_for(state.unit_id)

    def _keep_for_cancel(self, state: _AlbumState, gesture: str) -> None:
        """Photograph one album before a gesture replaces what is held about it.

        Called with the album as it stands, immediately before the gesture that
        replaces it. Reading `album_units.state` here rather than deducing it is
        deliberate: the word an organized album carries survives a re-plan and
        does not survive a re-scan, and a `Cancel` that guessed wrong would put
        back a different album from the one that was on screen.
        """
        # **The first photograph wins while one is standing.** A gesture can be
        # pressed twice, and overwriting would make the way back lead to *after
        # the first press*, with the entry then dropped, so the album the
        # gestures started from could never be reached again. Nothing has
        # been written while an entry stands: an apply drops it, and so does a
        # `Cancel`, which is what makes the next gesture photograph afresh.
        if state.unit_id in self._undoable:
            return
        stored = self._store.unit_by_id(state.unit_id)
        self._undoable[state.unit_id] = _LastAction(
            gesture=gesture,
            album=replace(state),
            unit_state=None if stored is None else stored.state,
            words=self._store.corrections(state.unit_id),
        )

    def cancel(self, unit_id: int) -> dict[str, object]:
        """Put one album back the way it was before the last gesture on it.

        Offered only where there is something to take back, which is the rule the
        footer's other entries keep: a control that is always there and does
        nothing most of the time is not drawn.

        **The rows the gesture wrote are withdrawn, not left standing.** An
        identification is what the album *is*, and `identification_for` reads the
        newest that is not `superseded` — so a cancelled attempt left as
        `proposed` would be the answer this album came back as at the next
        start. The plan it carried is `discarded`, the same way a second cover
        choice discards the first.
        """
        last = self._undoable.get(unit_id)
        if last is None:
            return {"ok": False, "error": "There is nothing to take back on this album."}
        current = self._albums.get(unit_id)
        if current is not None and current.applied and not last.album.applied:
            # It reached the files while this entry was standing. `Revert` is the
            # way back from there, and offering two would be offering one that
            # cannot keep its promise.
            self._undoable.pop(unit_id, None)
            return {"ok": False, "error": "This album has been applied; revert it instead."}
        if current is not None:
            if (
                current.identification_id is not None
                and current.identification_id != last.album.identification_id
            ):
                self._store.record_identification_state(
                    current.identification_id, "superseded", "user"
                )
            if current.plan_id is not None and current.plan_id != last.album.plan_id:
                self._store.record_plan_outcome(current.plan_id, "discarded")
        self._remember(unit_id, last.album)
        if last.unit_state is not None:
            self._store.set_unit_state(unit_id, last.unit_state)
        self._undoable.pop(unit_id, None)
        self._logger.info(
            "One album was put back the way it was before the last gesture.",
            extra={
                "operation": "api.cancel",
                "unit_id": unit_id,
                "gesture": last.gesture,
                "state": last.unit_state,
            },
        )
        # Whether anything typed is being left behind the reading that goes
        # back on screen. Said rather than left to be discovered: a typed word
        # is kept and applied again the next time the album is planned, and the
        # one moment that looks like the opposite is this one.
        return {
            "ok": True,
            "album": self._album_payload(last.album),
            "gesture": last.gesture,
            "kept_words": self._store.corrections(unit_id) != last.words,
        }

    def reject(self, unit_id: int) -> dict[str, object]:
        """Record that the proposed identification is wrong, and leave the files alone.

        **Never about an album that is already organized.** This writes
        `skipped`, and two readers take that word to mean *rejected* — the shelf
        row and the restore — so the card would say `Rejected` over files that are
        organized. Worse and quieter: `ids_in_state("organized")` is what
        makes a scan leave a finished album alone, and an album in `skipped` is
        not in that set, so the next scan of the whole library would plan it
        again and offer to rename what had been settled by hand.

        Refusing is only honest because there is somewhere else to go: `Cancel`
        takes the proposal back and `Revert` undoes files.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if self._organized_on_record(state):
            return {
                "ok": False,
                "error": (
                    "This album is already organized. Cancel takes the proposal back, "
                    "and Revert undoes the files."
                ),
            }
        state.rejected = True
        if state.identification_id is not None:
            self._store.record_identification_state(state.identification_id, "rejected", "user")
        self._store.set_unit_state(state.unit_id, "skipped")
        return {"ok": True}

    def hold(self, unit_id: int) -> dict[str, object]:
        """Pull one automatic album back into review, without calling it wrong.

        Rejection means the proposed release is not this album; holding means
        the answer may well be right and is to be looked at first. The
        identification stays proposed and only the decision moves.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if state.applied:
            return {"ok": False, "error": "This album is already applied; revert it instead."}
        state.held = True
        state.outcome = replace(
            state.outcome,
            decision=Decision.REVIEW,
            reason="Held for review on request.",
        )
        self._store.set_unit_state(state.unit_id, "needs_review")
        return {"ok": True, "album": self._album_summary(state)}

    def correct(
        self,
        unit_id: int,
        folder_name: str | None = None,
        track_titles: dict[str, str] | None = None,
        track_artists: dict[str, str] | None = None,
        track_files: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Apply the user's corrections to names and titles, and re-plan.

        ``track_titles`` maps the release track position to the corrected
        title. An empty string withdraws a correction, restoring the
        catalogue's value. ``track_artists`` is the same for the credit a
        compilation writes into each file name, and ``folder_name`` for the
        album's folder. **What is not sent is left as it stands**: ``None``
        says nothing about a field, and only the empty string withdraws.

        The fields arrive filled with what this app proposes, so a correction is
        what *differs* from that proposal: submitting the form unchanged records
        nothing and overrides nothing. What each field is compared against is
        the value the catalogue published, read off the identified release —
        which stays as the source wrote it, because ``refine`` plans with
        corrections rather than keeping them.

        ``track_files`` maps the release track position to the content signature
        of the file the user says holds it. An empty string withdraws the
        pairing and lets the matcher answer again.

        What is re-planned is every correction that *stands*, read back from the
        database, and not the ones this call happened to carry. A withdrawal is
        an absence, and an absence cannot be sent.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if _holding_the_applied_plan(state):
            return {"ok": False, "error": "This album is already applied; revert it first."}
        if state.outcome.candidate is None:
            return {"ok": False, "error": "This album has no identification to correct."}
        release = state.outcome.candidate.release
        recorded = {
            (str(row["field"]), row["track_position"]): row
            for row in self._store.corrections(state.unit_id)
        }
        # Measured against the title the plan would write, which is the one the
        # field was filled with: against the catalogue's own case, a field the
        # user never touched would come back as a correction.
        catalogue_titles = {
            track.position: track.title for track in normalize_release(release).tracks
        }
        for key, value in (track_titles or {}).items():
            position = int(key)
            value = value.strip()
            if not value or value == catalogue_titles.get(position):
                self._store.clear_correction(state.unit_id, "track_title", position)
                continue
            self._store.record_correction(
                state.unit_id, "track_title", value, catalogue_titles.get(position), position
            )

        # The same shape for the credit, read against the same rule: the credit
        # as this track's file name would write it.
        catalogue_credits = {track.position: _written_credit(track) for track in release.tracks}
        for key, value in (track_artists or {}).items():
            position = int(key)
            value = value.strip()
            if not value or value == catalogue_credits.get(position):
                self._store.clear_correction(state.unit_id, "track_artist", position)
                continue
            self._store.record_correction(
                state.unit_id, "track_artist", value, catalogue_credits.get(position), position
            )
        # A pairing of a file to a track made by hand. It is recorded against
        # the release it was made on and by the file's content signature, never
        # its path: organizing renames every file of this album, and a pairing
        # bound to a name would be thrown away by the app succeeding.
        if track_files:
            refusal = self._record_pairings(state, track_files)
            if refusal is not None:
                return refusal

        # **A folder name that was not sent is not a folder name withdrawn.**
        # Confirming a pairing and keeping one spelling send what they are
        # about and nothing else, and read as an emptied field each of them
        # took a typed folder name away with it. The field emptied is how the
        # name is withdrawn, and that arrives as an empty string.
        if folder_name is not None:
            folder = folder_name.strip() or None
            existing_folder = recorded.get(("folder_name", None))
            proposed_folder = (
                str(existing_folder["replaced"])
                if existing_folder is not None and existing_folder["replaced"]
                else _planned_folder_name(state.unit, state.outcome.plan)
            )
            if folder == proposed_folder:
                folder = None
            if folder is None:
                self._store.clear_correction(state.unit_id, "folder_name", None)
            else:
                self._store.record_correction(state.unit_id, "folder_name", folder, proposed_folder)
        self._replan_after_correction(state)
        return {"ok": True, "album": self._album_payload(state)}

    def _replan_after_correction(self, state: "_AlbumState") -> None:
        """Rebuild the plan from every correction that stands, after one changed.

        Every door that writes or withdraws a correction ends here, and that is
        the point: a withdrawal is a correction changing, so it must leave this
        album in the state a correction would. Two routes doing this each in
        their own way would be two places for one rule.

        Distinct from ``_replan``, which is the sweep: that one refuses an album
        already organized, since a change of naming style must not hand
        ``Apply N automatic`` an album that was renamed by hand. This one is
        always a gesture on one album, so it re-plans and then lets
        ``_organized_only_while_nothing_changes`` decide what the album has
        become.
        """
        # Read back, not accumulated: what re-plans is every correction still
        # standing after the writes above, so a field emptied here is simply not
        # among them and the catalogue's own value is what gets planned.
        folder, standing_titles, standing_artists = self._stored_corrections(state.unit_id)
        release = state.outcome.candidate.release if state.outcome.candidate else None
        pinned, _ = self._stored_pairings(state.unit_id, release)
        # **Offline, with the pictures this album already has** — the same way
        # `_replan` rebuilds a plan. Correcting a name does not change which
        # release this is, so addressing the Cover Art Archive again asks a
        # question whose answer is already in hand, and costs seconds on every
        # blur of every field. Nothing about the art is dropped; it is carried.
        outcome = self._pipeline.workflow.refine(
            state.unit,
            state.outcome.candidate,
            folder_name=folder,
            track_titles=standing_titles or None,
            track_artists=standing_artists or None,
            transcoded=self._transcoded_for(state.unit),
            # **What the audio measured, or the file loses the word that warns
            # about it.** Not the number instead of the word, which is what the
            # name of this argument suggests: without it the track's mark
            # disappears entirely, so a correction to the *folder* name would
            # silently take `[Lossy]` off a proven transcode — and `Approve &
            # apply` writes that name to the disk.
            measured_rates=self._rates,
            track_files=pinned or None,
            offline=True,
            artwork=state.outcome.artwork,
        )
        # The witnesses spoke about this release, and re-planning does not change
        # which release this is — so their verdicts travel with it. Without this,
        # correcting one title would empty the panel that says where the
        # catalogues stand.
        outcome = replace(outcome, verification=state.outcome.verification)
        if state.held and outcome.decision is Decision.AUTOMATIC:
            outcome = replace(outcome, decision=Decision.REVIEW)
        state.outcome = outcome
        # A correction to an organized album's name is the moment that album
        # stops being one that needs nothing: there is something to write now,
        # and `Approve & apply` is what writes it. Nothing has been written
        # here — this is a plan, and it waits like every other.
        _organized_only_while_nothing_changes(state)
        state.identification_id, state.plan_id = self._persist_outcome(state)

    def withdraw_spelling(self, unit_id: int, position: int) -> dict[str, object]:
        """Give one line back to the catalogue, by saying so rather than by typing.

        The way back from ``Keep yours``. ``correct`` clears a correction whose
        value arrives equal to the catalogue's, and an emptied field does the
        same, but both are withdrawal *by coincidence of a value* on a screen
        that shows the catalogue's word nowhere. This is the withdrawal that is
        offered by name.

        The two doors are one door underneath — ``clear_correction`` and then the
        same re-plan ``correct`` ends with — so that one thing is undone one
        way.

        A refusal where nothing stands, rather than an ``ok`` that withdrew
        nothing: a write that succeeds in silence says nothing about what it
        did.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if _holding_the_applied_plan(state):
            return {"ok": False, "error": "This album is already applied; revert it first."}
        if state.outcome.candidate is None:
            return {"ok": False, "error": "This album has no identification to correct."}
        _, standing, _ = self._stored_corrections(unit_id)
        if position not in standing:
            return {"ok": False, "error": "Nothing of yours stands on that line."}
        self._store.clear_correction(unit_id, "track_title", position)
        self._replan_after_correction(state)
        return {"ok": True, "album": self._album_payload(state)}

    def withdraw_title_corrections(
        self, unit_id: int, confirmed: bool = False
    ) -> dict[str, object]:
        """Give every title of one album back to the catalogue, after saying how many.

        The broad withdrawal: it reaches the titles that were typed as well as
        the spellings that were kept, which is exactly why it is the one that
        asks first. The number *is* the question — *withdraw the 4 corrections
        of this album?* — and the server is the side that knows it, so the
        window is told rather than counting rows it was never sent.

        Nothing is written on the unconfirmed call, and the answer carries
        ``confirm`` rather than an error: there is nothing wrong here, there is
        something to decide.

        What is reported afterwards is what ``clear_corrections_of`` deleted,
        which is the count of a fact rather than of an intention.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if _holding_the_applied_plan(state):
            return {"ok": False, "error": "This album is already applied; revert it first."}
        if state.outcome.candidate is None:
            return {"ok": False, "error": "This album has no identification to correct."}
        _, standing, _ = self._stored_corrections(unit_id)
        if not standing:
            return {"ok": False, "error": "Nothing of yours stands on this album's titles."}
        if not confirmed:
            return {"ok": True, "confirm": "withdraw_titles", "count": len(standing)}
        dropped = self._store.clear_corrections_of(unit_id, "track_title")
        self._replan_after_correction(state)
        return {"ok": True, "dropped": dropped, "album": self._album_payload(state)}

    def _record_pairings(
        self, state: "_AlbumState", track_files: Mapping[str, str]
    ) -> dict[str, object] | None:
        """Write the user's pairings, or refuse and say which one is impossible.

        Two refusals, and both are about a claim the app could not keep. A
        signature that is not one of this album's files, and a position this
        release does not publish, would be stored happily and then do nothing
        for ever: the write succeeds and the silence is the bug.

        A signature may hold only one position: pairing a file to a track takes
        it away from wherever it was, which is what makes it possible to repair
        a pairing the app has already made.
        """
        release = state.outcome.candidate.release if state.outcome.candidate else None
        key = _release_key(release)
        here = {file.content_signature: file for file in state.unit.audio_files}
        positions = {track.position for track in release.tracks} if release else set()
        # What the app has paired there right now, so the record says what the
        # correction replaced — the same shape every other correction keeps.
        alignment = state.outcome.alignment
        paired_now = (
            {
                assignment.track.position: assignment.file.content_signature
                for assignment in alignment.assignments
            }
            if alignment
            else {}
        )
        for raw_position, signature in track_files.items():
            position = int(raw_position)
            signature = (signature or "").strip()
            if not signature:
                self._store.clear_correction(state.unit_id, "track_file", position)
                continue
            if signature not in here:
                return {"ok": False, "error": "That file is not one of this album's files."}
            if position not in positions:
                return {"ok": False, "error": "That track is not on this release."}
            # **A file holds one place, whichever way it was given one.** The
            # second field is here and not only in `adopt_extra_track` because
            # this is the single door every pairing comes through — the picker
            # on a track's own row reaches it too, and a guard on one caller of
            # two is a rule applied by half.
            for row in self._store.corrections(state.unit_id):
                if (
                    row["field"] in _FIELDS_ANSWERING_TO_A_RELEASE
                    and str(row["value"]) == signature
                    and not (row["field"] == "track_file" and row["track_position"] == position)
                ):
                    self._store.clear_correction(
                        state.unit_id,
                        str(row["field"]),
                        int(row["track_position"]),  # type: ignore[arg-type]
                    )
            self._store.record_correction(
                state.unit_id,
                "track_file",
                signature,
                paired_now.get(position),
                position,
                release_key=key,
            )
            self._logger.info(
                "A file was paired to a track by hand.",
                extra={"operation": "identification.pairing.by_hand", "position": position},
            )
        return None

    def adopt_extra_track(
        self, unit_id: int, signature: str, position: int | None = None
    ) -> dict[str, object]:
        """Make one file the release does not list a track at the given position.

        ``position`` omitted means the offered default — the one after the
        release's last, which is the case that renumbers nothing. A position
        the release already uses inserts there, and everything from it down
        moves one.

        The file is named by its content signature, never its path, for the same
        reason a pairing is: organizing renames every file of the album, and a
        row bound to a name would be thrown away by this app succeeding.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if _holding_the_applied_plan(state):
            return {"ok": False, "error": "This album is already applied; revert it first."}
        if state.outcome.candidate is None:
            return {"ok": False, "error": "This album has no identification to add a track to."}
        release = state.outcome.candidate.release
        key = _release_key(release)
        if key is None:
            return {"ok": False, "error": "This release cannot be named, so nothing can be kept."}
        if not any(file.content_signature == signature for file in state.unit.audio_files):
            return {"ok": False, "error": "That file is not in this album."}
        # **A place the catalogue already publishes is a pairing, not a new
        # track.** Adopting *into* such a place would insert there and push the
        # catalogue's own track down, so an album whose catalogue lists
        # thirteen tracks would answer the two files nothing had matched by
        # growing to fourteen: the user's file at 4 and the catalogue's own
        # track 4 pushed to 6, still with no file. Putting a file at 4 means
        # that this file *is* track 4.
        #
        # Adoption keeps the case it was built for: a file the release does not
        # list at all, which lands after its last track.
        #
        # Two conditions. **The catalogue's own list, not the one on screen** —
        # `release.tracks` here already carries the user's adoptions, so a second
        # file put at 3 would read 3 as published because the first adoption
        # had just put a track there. And **only a place no file holds**: on a
        # release whose every track is paired, putting a file at 1 says that song
        # comes first, which is an insertion — pairing there would take track
        # 1's file away and hand back the same orphan the user started with.
        catalogue = without_extra_tracks(release)
        published = {track.position for track in catalogue.tracks}
        alignment = state.outcome.alignment
        taken = (
            {assignment.track.position for assignment in alignment.assignments}
            if alignment
            else set()
        )
        if position is not None and int(position) in published and int(position) not in taken:
            # A file cannot be an added track and a pairing at once. That is
            # cleared inside `_record_pairings`, where every pairing is written,
            # rather than here, so that both doors are covered.
            refusal = self._record_pairings(state, {str(int(position)): signature})
            if refusal is not None:
                return refusal
            state.outcome = self._replanned(state)
            _organized_only_while_nothing_changes(state)
            state.identification_id, state.plan_id = self._persist_outcome(state)
            return {"ok": True, "album": self._album_payload(state)}
        # The ceiling is the FINISHED list's next place, not the catalogue's:
        # `next_free_position` strips the added tracks on purpose (the offered
        # default must not drift as tracks are adopted), so the standing
        # adoptions are counted back in here. Computed the same way whether this
        # file is new or moving — a mover's own place is excluded, so 17 files
        # over a 15-track release reach 16 and then 17 instead of fighting for
        # 16 in a circle.
        standing = self._extra_positions(unit_id, release)
        others = {sig: at for sig, at in standing.items() if sig != signature}
        ceiling = next_free_position(release) + len(others)
        where = ceiling if position is None else int(position)
        if where < 1:
            return {"ok": False, "error": "A track's position starts at 1."}
        # Beyond the finished list's last there would be a gap, and a gap is a
        # number nobody can explain: track 14 of an album whose files stop at 11.
        if where > ceiling:
            return {
                "ok": False,
                "error": f"The last position this album can hold is {ceiling}.",
            }
        # **One file holds one place, and no place holds two files.** The
        # correction row is unique per position, so recording over a place
        # another added track holds would silently evict it — choosing 16 for
        # one file would throw the other out, in a circle. Instead, taking a
        # held place does what taking a catalogue track's place does:
        # everything from it down moves one. A move first gives
        # its old place back, so the tracks after it slide up before the
        # insertion pushes any of them down.
        old_place = standing.get(signature)
        shifted: dict[str, int] = {}
        for sig, at in others.items():
            now = at - 1 if old_place is not None and at > old_place else at
            shifted[sig] = now + 1 if now >= where else now
        shifted[signature] = where
        if shifted != standing:
            for at in standing.values():
                self._store.clear_correction(unit_id, "extra_track", at)
        for sig, at in sorted(shifted.items(), key=lambda pair: pair[1]):
            self._store.record_correction(unit_id, "extra_track", sig, None, at, release_key=key)
        self._logger.info(
            "A file the release does not list was made a track of it.",
            extra={
                "operation": "identification.extra_track.adopted",
                "unit_id": unit_id,
                "position": where,
            },
        )
        state.outcome = self._replanned(state)
        _organized_only_while_nothing_changes(state)
        state.identification_id, state.plan_id = self._persist_outcome(state)
        return {"ok": True, "album": self._album_payload(state)}

    def drop_extra_track(self, unit_id: int, position: int) -> dict[str, object]:
        """Undo one adoption, leaving the file exactly as it was found.

        Nothing on disk has been touched by adopting — the plan is read before
        it runs, always — so this only takes the row away and plans again.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if _holding_the_applied_plan(state):
            return {"ok": False, "error": "This album is already applied; revert it first."}
        self._store.clear_correction(unit_id, "extra_track", int(position))
        # The tracks added after this one slide into the gap it leaves — a
        # track at 4 of a list that now stops at 3 is the number the adopt
        # refuses to create, so the drop cannot leave one behind.
        release = state.outcome.candidate.release if state.outcome.candidate else None
        if release is not None:
            key = _release_key(release)
            remaining = self._extra_positions(unit_id, release)
            hanging = {sig: at for sig, at in remaining.items() if at > int(position)}
            for at in hanging.values():
                self._store.clear_correction(unit_id, "extra_track", at)
            for sig, at in sorted(hanging.items(), key=lambda pair: pair[1]):
                self._store.record_correction(
                    unit_id, "extra_track", sig, None, at - 1, release_key=key
                )
        state.outcome = self._replanned(state)
        _organized_only_while_nothing_changes(state)
        state.identification_id, state.plan_id = self._persist_outcome(state)
        return {"ok": True, "album": self._album_payload(state)}

    def reuse_pairings(self, unit_id: int) -> dict[str, object]:
        """Move pairings made on a previous release onto the one now identified.

        The positions are kept exactly as they were recorded: reusing is the
        user's claim that this tracklist is ordered like the one the pairings
        were made against. What comes back is the re-planned album, so the
        table shows immediately whether it was true.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if _holding_the_applied_plan(state):
            return {"ok": False, "error": "This album is already applied; revert it first."}
        if state.outcome.candidate is None:
            return {"ok": False, "error": "This album has no identification to pair against."}
        key = _release_key(state.outcome.candidate.release)
        if key is None:
            return {"ok": False, "error": "This release cannot be named, so nothing can be kept."}
        # **Both fields that answer to a release, not one of them.** Moving
        # pairings only would leave an added track behind on the release the
        # user compared away from, where nothing reads it.
        moved = sum(
            self._store.rekey_corrections(unit_id, field, key)
            for field in _FIELDS_ANSWERING_TO_A_RELEASE
        )
        state.outcome = self._replanned(state)
        _organized_only_while_nothing_changes(state)
        state.identification_id, state.plan_id = self._persist_outcome(state)
        return {"ok": True, "moved": moved, "album": self._album_payload(state)}

    def discard_pairings(self, unit_id: int) -> dict[str, object]:
        """Drop every pairing this album carries, and say how many went."""
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if _holding_the_applied_plan(state):
            return {"ok": False, "error": "This album is already applied; revert it first."}
        # Every field the panel above it listed, or the button empties less than
        # it showed and reports a count for the rest.
        dropped = sum(
            self._store.clear_corrections_of(unit_id, field)
            for field in _FIELDS_ANSWERING_TO_A_RELEASE
        )
        # Re-planned unconditionally, not through `_with_corrections`, which
        # returns the album untouched when nothing is recorded — and after a
        # discard that is precisely the state. The album on screen would keep
        # the pairing it no longer has: what is planned is read back from the
        # database, and an absence cannot be sent.
        state.outcome = self._replanned(state)
        _organized_only_while_nothing_changes(state)
        state.identification_id, state.plan_id = self._persist_outcome(state)
        return {"ok": True, "dropped": dropped, "album": self._album_payload(state)}

    def _replanned(self, state: "_AlbumState") -> IdentificationOutcome:
        """Re-plan this album from every correction standing right now.

        **Offline, with the art already in hand**, like every other rebuild.
        This is the path four gestures of the album dialog take — adopting a
        track, dropping one, reusing pairings, discarding them. The cover
        lookup reaches the archive *through* MusicBrainz, which allows about
        one request a second, so asking here would make each of those gestures
        wait on the network.

        Nothing about which release this is has changed here — a track or a
        pairing moved — so the answer is already in hand.
        """
        if state.outcome.candidate is None:
            return state.outcome
        folder, titles, artists = self._stored_corrections(state.unit_id)
        candidate, pinned, _ = self._planned_from(
            state.unit_id, state.unit, state.outcome.candidate
        )
        return replace(
            self._pipeline.workflow.refine(
                state.unit,
                candidate,
                folder_name=folder,
                track_titles=titles or None,
                track_artists=artists or None,
                transcoded=self._transcoded_for(state.unit),
                # The same reason as every other re-plan: without it the mark
                # comes off the track.
                measured_rates=self._rates,
                track_files=pinned or None,
                offline=True,
                artwork=state.outcome.artwork,
            ),
            # The same release, so the same testimony.
            verification=state.outcome.verification,
        )

    def old_label_preview(self, root: str) -> dict[str, object]:
        """List every name under ``root`` still written with the old word.

        Read-only, and it is the whole point: this renames folders that were
        organized earlier, so what it will do is shown in full — old name and
        new name, one line each — before anything is touched.

        Only a codec followed by the old word, and only inside brackets. Both
        halves matter: `[FLAC Fake]` is this app's own token, while a song title
        may contain the same word, and a plain search for the word would rename
        that album.
        """
        folder = Path(root).expanduser()
        if not folder.is_dir():
            return {"ok": False, "error": "That folder is not there."}
        renames = [
            {"from": str(path), "to": str(path.with_name(renamed))}
            for path, renamed in _old_labelled(folder)
        ]
        return {"ok": True, "renames": renames, "count": len(renames)}

    def rename_old_labels(self, root: str) -> dict[str, object]:
        """Rewrite `[FLAC Fake]` as `[Lossy]` on disk, as one reversible run.

        Applied through the executor like every other change this app makes, so
        each rename lands in History with its trail and the whole sweep reverts
        as one gesture. Files are renamed before the folder that holds them,
        which is the ordering every plan here obeys, or the second rename would
        address a path that no longer exists.

        Albums are recorded as they are swept, because a plan needs the album it
        belongs to; the folders that carry the old word are read for that and no
        others, so pointing this at a whole collection costs the folders it will
        actually change.
        """
        folder = Path(root).expanduser()
        if not folder.is_dir():
            return {"ok": False, "error": "That folder is not there."}
        planned = _old_labelled(folder)
        if not planned:
            return {"ok": True, "renamed": 0, "run_id": None, "failures": []}
        by_album: dict[Path, list[tuple[Path, str]]] = {}
        for path, renamed in planned:
            album = path if path.is_dir() else path.parent
            by_album.setdefault(album, []).append((path, renamed))
        renamed_count = 0
        failures: list[str] = []
        batch: list[int] = []
        for album, entries in by_album.items():
            # Deepest first: a file inside a folder that is itself being renamed
            # must move before its folder does.
            entries.sort(key=lambda entry: (entry[0].is_dir(), -len(entry[0].parts)))
            unit = next(
                (
                    scanned
                    for scanned in self._pipeline.scanner.scan(album)
                    if scanned.folder_path == album
                ),
                None,
            )
            if unit is None:
                failures.append(f"{album.name}: no album could be read there.")
                continue
            unit_id = self._store.record_unit(unit)
            operations = tuple(
                ChangeOperation(
                    sequence=index + 1,
                    kind=(
                        OperationKind.RENAME_FOLDER if path.is_dir() else OperationKind.RENAME_FILE
                    ),
                    target_path=path,
                    after_state={"path": str(path.with_name(new_name))},
                    before_state={"path": str(path)},
                )
                for index, (path, new_name) in enumerate(entries)
            )
            plan = ChangePlan(unit=unit, release=None, operations=operations)
            plan_id = self._store.record_plan(unit_id, None, operations)
            try:
                with self._apply_lock:
                    result = self._pipeline.executor.apply(plan, witness=self._witness(plan_id))
            except Exception as error:
                failures.append(f"{album.name}: {error}")
                continue
            self._store.record_execution(plan_id, result.applied, complete=result.is_complete)
            # The registry is told, exactly as an ordinary apply tells it.
            # Otherwise the sweep renames the folder and leaves the album's own
            # row naming the old one, so the next start looks for a folder that
            # is gone, and looks for it by name — which is the very thing that
            # changed.
            #
            # **Over the trail, so a sweep that stopped is followed too**: this
            # renames only, and a rename that landed is a path nothing else will
            # ever correct.
            landed = replace(plan, operations=tuple(entry.operation for entry in result.applied))
            self._follow_the_rename(landed)
            state = self._albums.get(unit_id)
            if state is not None:
                self._follow_the_album(state, landed)
            if result.state is ExecutionState.APPLIED:
                renamed_count += len(operations)
                batch.append(plan_id)
            else:
                failures.append(f"{album.name}: {result.failure or 'the rename stopped partway'}")
        run_id = None
        if batch:
            run_id = f"run-{batch[0]}-{len(batch)}"
            self._store.assign_run(batch, run_id)
        self._logger.info(
            "The old quality word was rewritten across a chosen folder.",
            extra={"operation": "api.labels.rewritten", "renamed": renamed_count},
        )
        return {
            "ok": not failures,
            "renamed": renamed_count,
            "run_id": run_id,
            "failures": failures,
        }

    def clear_organized(self) -> dict[str, object]:
        """Take every organized album off the Library at once, touching no file.

        The card's ✕, said about a shelf instead of a row. Only albums this app
        organized: those are the ones whose question is answered, and an album
        still waiting for a decision is exactly what must not be swept away by
        a button meant to tidy up.

        Nothing is deleted and nothing moves. History still answers for every
        one of them, and a scan that reaches the folder again brings it back.
        """
        cleared: list[int] = []
        with self._albums_lock:
            for unit_id, state in list(self._albums.items()):
                if not (state.organized or state.applied):
                    continue
                self._albums.pop(unit_id, None)
                self._downloaded.discard(unit_id)
                cleared.append(unit_id)
            # And the organized albums that are still only rows, which is
            # usually nearly all of them. A gesture that swept only what
            # happened to have been read back would clear a different set every
            # time, depending on how far the restore had got.
            for unit_id, row in list(self._shelf.items()):
                if row.get("state") != "organized":
                    continue
                self._shelf.pop(unit_id, None)
                self._downloaded.discard(unit_id)
                cleared.append(unit_id)
        for unit_id in cleared:
            self._store.clear_unit(unit_id)
        self._logger.info(
            "The organized albums were cleared from the Library.",
            extra={"operation": "api.library.clear_organized", "albums": len(cleared)},
        )
        return {"ok": True, "cleared": len(cleared)}

    def _what_the_other_catalogues_said(
        self,
        previous: Mapping[str, object] | None,
        adopted: MetadataSourceId,
    ) -> dict[str, object] | None:
        """Carry into a chosen release what the catalogues had already answered.

        Choosing a release says which record this is. It says nothing about what
        the other catalogues published, and those answers were measured against
        *the album's files* — so they stay true whichever record ends up in use,
        and the header is where they are read.

        ``adopt`` and ``adopt_link`` build a fresh outcome, whose
        ``verification`` is empty, and ``_persist_outcome`` writes that over the
        one on record. Without this, a catalogue that had answered would stop
        being named in the header once a link to another catalogue was pasted:
        not demoted to a witness but gone, because a source with no recorded
        answer reads as *not asked*.

        **The adopted source is not carried, deliberately.** Its own entry is
        recomputed from the candidate a line later in ``_source_positions``, and
        the stored one would win there — ``answers.setdefault`` keeps what is
        already in hand — so the header would name the release just chosen
        beside the *previous* winner's score, which is one catalogue's number
        under another's name.

        ``cross_source`` is dropped for the same reason and the opposite way
        round: it is two catalogues agreeing about *the record that was in use*,
        and after another one is chosen nobody has measured it. ``itunes`` is
        dropped because the witness is asked again when the dialog opens, in the
        album's own words, and its ``track_count_agrees`` is likewise about a
        record that is no longer the one on screen.
        """
        recorded = (previous or {}).get("sources")
        others = [
            dict(entry, winner=False)
            for entry in (recorded if isinstance(recorded, list) else [])
            if isinstance(entry, dict) and str(entry.get("source")) != str(adopted)
        ]
        return {"sources": others} if others else None

    def adopt_link(self, unit_id: int, url: str) -> dict[str, object]:
        """Identify one album with a release the user found themselves.

        The pasted URL names the release; the plan is still computed honestly,
        so an unalignable choice arrives blocked and says why.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        pointed = self._spotify_words(url)
        if pointed is not None:
            words = pointed.get("words")
            if not isinstance(words, dict):
                return pointed
            return self._search_with(state, words)
        parsed = _parse_release_link(url)
        if parsed is None:
            group = _parse_group_link(url)
            if group is None:
                return {
                    "ok": False,
                    "error": (
                        "That is not a Discogs, MusicBrainz or Spotify link this app "
                        "recognizes. Paste the address of a release, a master, a release "
                        "group, or the Spotify album you are listening to."
                    ),
                }
            resolved = self._pipeline.workflow.resolve_group(*group)
            if resolved is None:
                return {
                    "ok": False,
                    "error": (
                        "That master has no main release to follow. "
                        "Open one of its editions and paste that instead."
                    ),
                }
            parsed = (group[0], resolved)
        source, release_id = parsed
        try:
            self._facts_from_store([state.unit])
            outcome = self._pipeline.workflow.adopt(
                state.unit,
                source,
                release_id,
                transcoded=self._transcoded_for(state.unit),
                measured_rates=self._rates,
            )
            outcome = self._with_corrections(state.unit_id, state.unit, outcome)
            outcome = replace(
                outcome,
                verification=self._what_the_other_catalogues_said(
                    state.outcome.verification, source
                ),
            )
        except Exception as error:
            # A raised exception crosses the bridge as a rejected promise, and
            # the window has nothing waiting to catch it: the button stays
            # disabled and the status reads "fetching" forever. A failure the
            # user can see and dismiss is the only acceptable kind here.
            self._logger.exception(
                "A pasted release link could not be adopted.",
                extra={"operation": "identification.adopt_link.failure"},
            )
            return {"ok": False, "error": f"That release could not be fetched: {error}"}
        state.outcome = outcome
        state.rejected = False
        state.identification_id, state.plan_id = self._persist_outcome(
            state, method="external_link"
        )
        return {"ok": True, "album": self._album_payload(state)}

    def _spotify_words(self, url: str) -> dict[str, object] | None:
        """Read a pasted link as Spotify words, or report that it is not one.

        Returns ``None`` when the link is not Spotify's at all, which is how the
        catalogue paths keep working exactly as they did.
        """
        link = parse_spotify_link(url)
        if link is None:
            return None
        if self._spotify is None:
            return {"ok": False, "error": "This build cannot read Spotify links."}
        try:
            words = self._spotify.words_for(link)
        except SpotifyError as error:
            return {"ok": False, "error": str(error)}
        # Nested rather than spread, because `album` at the top level of an
        # answer from this API means *the identified album* everywhere else —
        # and here it is a word to search for. Two meanings on one key is how a
        # later branch reads `response.album.source` off a string.
        return {"words": {"album": words.album, "artist": words.artist or "", "exact": words.exact}}

    def _search_with(self, state: "_AlbumState", words: dict[str, object]) -> dict[str, object]:
        """Search the catalogues with words a pointer supplied.

        A Spotify link cannot *be* an identification: Spotify is not a catalogue
        this application may quote, and no value of its own is written anywhere.
        What it can do is say what to look for, the same way the words of the
        iTunes witness can.

        The same worker `search_again` uses, so the window's existing
        `searching`/`searched` events carry it and nothing new had to learn how
        to render a candidate.
        """
        if self._search_job is not None and self._search_job.is_alive():
            return {"ok": False, "error": "A search is already running."}
        self._emit("searching", {"unit_id": state.unit_id})
        self._search_job = threading.Thread(
            target=self._run_search,
            args=(state, str(words["artist"]) or None, str(words["album"]) or None),
            daemon=True,
        )
        self._search_job.start()
        return {"ok": True, "searching": True, "words": words}

    def export_report(self) -> dict[str, object]:
        """Write a from/to spreadsheet of everything applied, beside the library.

        The report is built from the executed trail, so it states what actually
        happened. A reverted operation stays in it, marked as such — the point
        of a record is that it does not quietly lose things.
        """
        rows = _album_reports(
            self._store.change_log(),
            self._store.album_sources(),
            self._store.proof_flags(),
            self._store.album_witnesses(),
        )
        rows.sort(key=lambda album: album.album.casefold())
        if not rows:
            return {"ok": False, "error": "Nothing has been applied yet."}
        # Beside the library when there is one, and in the home folder when
        # there is not. After a restore the albums come from wherever they live
        # and there is no single root, and refusing for the lack of one would
        # answer a request for the report with "Nothing has been scanned yet".
        folder = Path(self._root) if self._root else Path.home()
        destination = folder / "DigLibrary changes.xlsx"
        try:
            write_change_report(destination, rows)
        except OSError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "path": str(destination), "rows": len(rows)}

    def open_root(self) -> dict[str, object]:
        """Open the scanned folder with the platform's own file browser.

        Only meaningful while one folder is what the screen is about. A run that
        organized albums living in different places has no such folder, and the
        window hides the button rather than offering one that opens nothing.
        """
        if self._path_opener is None or self._root is None:
            return {"ok": False, "error": "There is no folder to open."}
        self._path_opener(self._root)
        return {"ok": True}

    def add_to_applications(self) -> dict[str, object]:
        """Assemble this installation's application icon, and say which it was.

        The point of it existing here at all: installing by name leaves nothing
        to double-click, and a typed command is the one thing somebody who has
        never opened Terminal cannot be told to run. The icon is carried rather
        than drawn, so this is quick and needs no worker thread and no waiting
        state.

        `replaced` is answered by the maker rather than recomputed here: the typed
        command reports the same two words, and a question answered in two places
        is answered differently by one of them.

        **It is not called a Dock icon here.** What this writes is
        `~/Applications/DigLibrary.app`, and macOS only lets a *person* put
        something in a Dock.
        """
        if self._icon_maker is None:
            return {"ok": False, "error": "This installation cannot build the icon."}
        try:
            made = self._icon_maker()
        except Exception as error:
            # Whatever went wrong is quoted rather than named: a cause this
            # code did not measure, such as `iconutil` missing, is not reported
            # as though it had been.
            self._logger.exception(
                "Building the application icon failed.", extra={"operation": "icon.add"}
            )
            return {"ok": False, "error": str(error)}
        self._logger.info(
            "Application icon written.",
            extra={"operation": "icon.add", "replaced": made["replaced"]},
        )
        return {"ok": True, **made}

    def open_album(self, unit_id: int) -> dict[str, object]:
        """Open one album's own folder, wherever it is now.

        Read from the album rather than built from the scanned root and a name:
        an album that has been organised lives under the name this application
        wrote, and one scanned from its own folder may *be* the root.

        *Wherever it is now* is followed by the audio, not read from the row:
        an album moved by hand would otherwise answer with the path it used to
        have, which is a sentence a person can do nothing with.
        """
        if self._path_opener is None:
            return {"ok": False, "error": "There is no opener in this build."}
        state = self._bring_in(unit_id)
        if state is None:
            # The door followed it and could not; the sentence that names the
            # folder and the copy still on disk is built once, there. `Unknown
            # album.` would be the answer to a different question.
            return self._why_it_will_not_open(unit_id)
        folder = self._where_it_is_now(state)
        if folder is None:
            return {
                "ok": False,
                "error": (
                    f"{state.unit.folder_path.name} is not where it was, and it was not in any "
                    "folder this app has seen. Drop it on the window and it will be found."
                ),
            }
        self._path_opener(str(folder))
        return {"ok": True, "path": str(folder)}

    def shared_albums(self) -> dict[str, object]:
        """Return the pairs of albums that hold some of the same audio, and the verdict.

        Grouped by the *pair* rather than by the track, because that is the
        question being decided: two editions of one album can hold several
        identical tracks between them, and deciding per track would be the same
        click repeated for each.

        Only audio that is identical byte for byte is reported here. The store
        also knows how to find one performance encoded twice, through the
        acoustic print, and nothing shows it yet.

        **The application moves nothing and deletes nothing.** The pair carries
        both folders so they can be opened and compared, and the answer given
        only stops this asking again.

        The disk is asked before anything is accused: a registered path that
        names no file is history rather than a second copy, and counting it
        would report an album's files twice.
        """
        decided = self._store.duplicate_decisions()
        pairs: dict[tuple[str, str], dict[str, object]] = {}
        for group in self._store.duplicate_audio(_is_on_disk):
            if group.proof != "audio":
                continue
            copies = sorted(group.copies, key=lambda copy: str(copy.path))
            for index, first in enumerate(copies):
                for second in copies[index + 1 :]:
                    # One album can hold the same audio twice, and that is one
                    # of the useful answers rather than a case to skip: a folder
                    # may keep two differently named versions of a track that
                    # are the same recording, so one of the two names is wrong.
                    # The album is then paired with itself, which is also how
                    # the decision is keyed.
                    same_album = first.album_signature == second.album_signature
                    if same_album and first.path.name == second.path.name:
                        # The same file seen by two registers is one file.
                        continue
                    key = tuple(sorted((first.album_signature, second.album_signature)))
                    # Named in the order the pair is keyed in, so one pair reads
                    # the same way however the two copies were walked.
                    left, right = first, second
                    if not same_album and left.album_signature != key[0]:
                        left, right = right, left
                    pair = pairs.setdefault(
                        key,
                        {
                            "first": _held_album(left),
                            "second": _held_album(right),
                            "tracks": [],
                            "same_album": same_album,
                            "verdict": decided.get(key),
                        },
                    )
                    tracks = pair["tracks"]
                    assert isinstance(tracks, list)
                    tracks.append({"first": left.path.name, "second": right.path.name})
        return {"ok": True, "pairs": sorted(pairs.values(), key=_pair_order)}

    def set_shared_verdict(self, first: str, second: str, verdict: str) -> dict[str, object]:
        """Record the verdict on two albums that share audio, and touch no file.

        `both` is *they belong in both places* — a track that is on the artist's
        album and on a compilation and should stay on both. `handled` is *this
        has been dealt with*. Anything else takes the answer back and puts the
        pair on screen again.
        """
        if verdict not in ("both", "handled"):
            verdict = ""
        self._store.record_duplicate_decision(first, second, verdict or None)
        return {"ok": True, "verdict": verdict}

    def open_folders(self, paths: list[str]) -> dict[str, object]:
        """Show these folders, so that they can be compared by ear.

        The folders are opened in the Finder rather than played here. Nothing
        here reads or writes a file — it hands a path to the system and stops.
        """
        if self._path_opener is None:
            return {"ok": False, "error": "There is no opener in this build."}
        opened: list[str] = []
        for path in paths:
            folder = Path(path).expanduser()
            if folder.is_dir():
                self._path_opener(str(folder))
                opened.append(str(folder))
        if not opened:
            return {"ok": False, "error": "Neither folder is on disk any more."}
        return {"ok": True, "opened": opened}

    def open_downloads(self) -> dict[str, object]:
        """Open the folder finished downloads land in, which the service owns."""
        if self._path_opener is None:
            return {"ok": False, "error": "There is no opener in this build."}
        path, refusal = self._download_root()
        if path is None:
            return refusal
        self._path_opener(str(path))
        return {"ok": True, "path": str(path)}

    def forget_album(self, unit_id: int) -> dict[str, object]:
        """Take one album off the Library, touching nothing on disk.

        It records that the album has been seen. History still answers for
        everything that happened to it, every file stays exactly where it is,
        and a scan that reaches the folder again brings it back — this hides a
        row, it does not remember a decision.

        The same words as Clear fetches and Clear downloaded, and the same
        promise, because the cost of misreading any of the three is the music
        itself.
        """
        with self._albums_lock:
            # Either half is the album being on screen: it has been read back,
            # or it is still one of the shelf's rows. Taking it off must reach
            # both, or the ✕ would remove a card that redraws itself from the
            # row nobody took away.
            if unit_id not in self._albums and unit_id not in self._shelf:
                return {"ok": False, "error": "Unknown album."}
            self._albums.pop(unit_id, None)
            self._shelf.pop(unit_id, None)
            self._downloaded.discard(unit_id)
        # Recorded, because the Library survives a restart. Still not a decision
        # remembered: a scan that reaches the folder again lifts the mark.
        self._store.clear_unit(unit_id)
        return {"ok": True}

    def open_url(self, url: str) -> dict[str, object]:
        """Open a source's release page in the system browser.

        The two catalogues' own pages and the pages a witness published: this is
        a verification gesture, not a general browser. The witness page is
        allowed because a link this app offers must be openable — attribution
        that cannot be followed is not attribution.
        """
        if self._path_opener is None:
            return {"ok": False, "error": "No opener is available."}
        if url in HELP_PAGES:
            # The two pages this application itself offers, by exact address.
            # They are where a key is created, they are not release pages, and
            # the checks below would rightly refuse them — so they are named
            # rather than described: an exact string cannot be widened by a
            # hostname that merely contains the right words.
            self._path_opener(url)
            return {"ok": True}
        if not _is_catalogue_page(url) and not _is_witness_page(url):
            return {
                "ok": False,
                "error": "Only catalogue release pages and witness pages can be opened.",
            }
        self._path_opener(url)
        return {"ok": True}

    def search_again(
        self, unit_id: int, artist: str | None = None, album: str | None = None
    ) -> dict[str, object]:
        """Start a corrected search on a worker thread, and report through events.

        Searching two catalogues can take many seconds — MusicBrainz allows
        roughly one request a second — and doing it on the bridge would freeze
        the window, which makes the gesture feel unresponsive and invites
        repeated clicks.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if self._search_job is not None and self._search_job.is_alive():
            return {"ok": False, "error": "A search is already running."}
        self._emit("searching", {"unit_id": unit_id})
        self._search_job = threading.Thread(
            target=self._run_search,
            args=(state, artist or None, album or None),
            daemon=True,
        )
        self._search_job.start()
        return {"ok": True, "started": True}

    def _run_search(self, state: _AlbumState, artist: str | None, album: str | None) -> None:
        try:
            state.alternatives = self._pipeline.workflow.alternatives(
                state.unit, artist=artist, album=album
            )
        except Exception as error:
            self._logger.exception(
                "A corrected search failed.", extra={"operation": "api.search.failure"}
            )
            self._emit("searched", {"unit_id": state.unit_id, "error": str(error), "found": 0})
            return
        self._emit(
            "searched",
            {
                "unit_id": state.unit_id,
                "found": len(state.alternatives),
                "candidates": [_candidate(c) for c in state.alternatives],
            },
        )

    def candidate_detail(self, unit_id: int, source: str, release_id: str) -> dict[str, object]:
        """Fetch one candidate in full, including the album's own year.

        This is what an expanded candidate shows. It is deliberately lazy: the
        master year costs one request per release, and paying that for every
        candidate, expanded or not, would make searching slow.
        """
        if unit_id not in self._albums:
            return {"ok": False, "error": "Unknown album."}
        try:
            detail = self._pipeline.workflow.detail(MetadataSourceId(source), release_id)
        except Exception as error:
            return {"ok": False, "error": str(error)}
        if detail is None:
            return {"ok": False, "error": "That release could not be fetched in full."}
        return {"ok": True, "candidate": _candidate(detail)}

    def adopt(self, unit_id: int, source: str, release_id: str) -> dict[str, object]:
        """Re-plan the album around the candidate the user chose.

        Choosing a pressing says which release this is and nothing whatever
        about the audio or about the names that were corrected, so both are
        carried into the new plan. Without that, picking another edition would
        silently drop `[Lossy]` and a corrected folder name from what would be
        written.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        self._facts_from_store([state.unit])
        outcome = self._pipeline.workflow.adopt(
            state.unit,
            MetadataSourceId(source),
            release_id,
            transcoded=self._transcoded_for(state.unit),
            measured_rates=self._rates,
        )
        outcome = self._with_corrections(state.unit_id, state.unit, outcome)
        outcome = replace(
            outcome,
            verification=self._what_the_other_catalogues_said(
                state.outcome.verification, MetadataSourceId(source)
            ),
        )
        state.outcome = outcome
        state.rejected = False
        state.held = False
        state.identification_id, state.plan_id = self._persist_outcome(state)
        return {"ok": True, "album": self._album_payload(state)}

    def revert(self, plan_id: int, confirmed: bool = False) -> dict[str, object]:
        """Undo one applied plan from its stored trail, wherever the album is now.

        A trail names absolute paths, and albums are commonly filed away once
        they are organized, so a reversal bound to those paths would stop being
        available at the exact moment the album was moved into a collection.
        ``rerooted_trail`` reads the trail where the album lives today.

        **And it asks first.** Undoing inside a music library renames files that
        are already filed, which is a different thing from undoing inside the
        folder they were downloaded into, so the folder and what would happen in
        it are shown and nothing is written until the answer is yes.
        """
        trail = self._store.applied_trail(plan_id)
        if not trail:
            return {"ok": False, "error": "This plan has no applied trail to revert."}
        unit_id = self._store.plan_unit(plan_id)
        was, now = self._where_the_trail_leads(trail, unit_id)
        if was != now:
            if not confirmed:
                return {
                    "ok": False,
                    "confirm": "filed_away",
                    "plan_id": plan_id,
                    "folder": now.name,
                    "parent": str(now.parent),
                    "operations": len(trail),
                }
            trail = rerooted_trail(trail, was, now)
            self._logger.info(
                "A reversal followed the album to where it had been filed.",
                extra={
                    "operation": "api.revert.followed",
                    "was": str(was),
                    "now": str(now),
                },
            )
        try:
            undone, stopped = self._pipeline.executor.revert_trail(
                trail,
                undone_witness=lambda entry: self._store.record_operation_reverted(
                    plan_id, entry.operation.sequence
                ),
            )
        except Exception as error:
            self._logger.exception(
                "A stored plan could not be reverted.",
                extra={"operation": "api.revert.failure"},
            )
            return {"ok": False, "error": str(error)}
        self._store.record_plan_outcome(
            plan_id, "reverted", [entry.operation.sequence for entry in undone]
        )
        self._follow_the_revert(undone, self._albums.get(unit_id) if unit_id else None)
        # **The same weighing the apply does, pointing the other way.** Taking
        # a picture back out makes every track lighter than the row that
        # describes it, so a reversal that did not do this would trade one stale
        # direction for the other. Read off the trail rather than off the album
        # in memory, because most albums on a shelf are only a row and the
        # reversal has to be as good there.
        self._follow_the_bytes(_audio_the_reversal_left(undone))
        if stopped is not None:
            # **Followed first, then reported.** What came back is on the disk
            # whether or not the walk finished, and the recorded paths go with
            # it — raising here would hand what was already undone to nobody.
            self._logger.error(
                "A reversal stopped partway; what came back was followed.",
                extra={
                    "operation": "api.revert.partial",
                    "undone": len(undone),
                    "of": len(trail),
                },
            )
        # **And it is reported by the answer alone, not by leaving early.**
        # Returning here would skip every line below — so a reversal that
        # stopped would leave `record_plan_outcome` above having already written
        # `reverted` while the album's own row, the shelf row and `_AlbumState`
        # all still said `organized`: a card wearing ✓ organized after an undo,
        # the one thing a revert must never look like. This walk is where the
        # plan was already declared reverted; the three places that say so
        # agree with it, and only the sentence handed back is different.
        outcome: dict[str, object] = (
            {"ok": True, "reverted": len(undone)}
            if stopped is None
            else {"ok": False, "error": stopped, "reverted": len(undone)}
        )
        if unit_id is not None:
            state = self._albums.get(unit_id)
            if state is not None and plan_id == state.cover_plan_id:
                # A reverted cover plan changes nothing about where the album
                # stands in the workflow — only the picture came back out.
                state.cover_applied = False
                return outcome
            self._store.set_unit_state(unit_id, "discovered")
            # **And the row the card is drawn from.** The shelf is built from
            # the database at startup and most albums on it are only a row, so
            # writing the album's state to the store and to `_AlbumState` alone
            # would leave the third place saying `organized`, and the card
            # would go on wearing ✓ organized until the application was
            # reopened. `rate_album` keeps the same rule.
            if (row := self._shelf.get(unit_id)) is not None:
                row["state"] = "discovered"
            if state is not None:
                # **Three places, and `organized` is the one that outlives the
                # session.** `applied` means *this run organized it* and
                # `organized` means *it is organized*, which is the fact the
                # badge reads and the one a restore fills in from the row.
                # Clearing the first two and leaving the third would let a
                # reverted album go on wearing ✓ organized in a window that had
                # read it back off the disk.
                state.organized = False
                state.applied = False
                state.written = False
        return outcome

    def redo_plan(self, plan_id: int) -> dict[str, object]:
        """Apply one reverted plan again, exactly as it was written.

        The undo of an undo, and nothing more than that. The stored operations
        are replayed as they were planned: no source is asked, nothing is
        identified again, and no naming rule is re-run — so what comes back is
        what was reverted, not what this app would decide now. That is the whole
        point of the gesture. Wanting the current answer instead is a different
        wish, and `Plan this album again` is where it lives.

        Refusing is the ordinary outcome when the disk has moved on, and the
        executor already refuses precisely: it rehearses the whole plan against
        what is there now and names every path that has gone, with the advice
        that a fresh scan is the fix (``StalePlanError``).
        """
        state_row = self._store.plan_unit(plan_id)
        if state_row is None:
            return {"ok": False, "error": "That plan is not about an album this library knows."}
        operations = self._store.plan_operations(plan_id)
        if not operations:
            return {"ok": False, "error": "That plan has no operations to apply again."}
        with self._albums_lock:
            state = self._albums.get(state_row)
        if state is None:
            # The row was cleared from the Library, or the folder moved on.
            # Applying blind is possible and wrong: the certificate, the
            # fingerprints and the album's own state all hang off this record,
            # and writing to a folder nothing on screen answers for is how a
            # change becomes unaccountable.
            return {
                "ok": False,
                "error": (
                    "That album is not on the Library screen, so this run cannot be redone. "
                    "Scan the folder it is in to bring it back."
                ),
            }
        release_row = self._store.plan_release(plan_id)
        plan = ChangePlan(
            unit=state.unit,
            release=self._store.release(release_row) if release_row is not None else None,
            operations=operations,
        )
        # `user`, and not a word of its own: the column is constrained to
        # `automatic` or `user`, a published migration is never edited, and
        # `user` is the true answer anyway — a button was pressed. A third word
        # would fail *after* the files were renamed: the disk changed, then the
        # row refused, and the exception surfaced from a half-finished gesture.
        error = self._apply(state, decided_by="user", plan=plan, plan_id=plan_id)
        if error is not None:
            return {"ok": False, "error": error}
        self._logger.info(
            "A reverted plan was applied again, exactly as it was written.",
            extra={"operation": "api.redo", "plan_id": plan_id},
        )
        return {"ok": True, "redone": len(operations)}

    def library_present(self) -> dict[str, object]:
        """Follow albums that moved, and mark the ones that could not be found.

        A folder that is no longer where it was recorded is looked for first, in
        the places this app has been shown, and followed when the audio says it
        is the same record.

        **And nothing leaves.** An album that is not found keeps its row and its
        card, marked as missing. Popping it out of the maps here would make the
        card vanish while the row survived, and from the screen there is no
        difference between a row nobody can see and a row that was deleted.

        This runs whenever the window arrives at the Library, not only while
        the Library is read back at start-up, so an album filed away with the
        window open is noticed without a restart.

        The recorded path is what is checked, never the one held in memory. An
        apply renames the folder and ``relocate_unit`` follows it in the database
        while ``_AlbumState.unit`` keeps the name it was scanned under; checking
        that one would mark as missing every album this app had just organized
        successfully.

        Nothing is deleted. History answers for all of it, and scanning where the
        album lives now brings the row back whole, because an album is identified
        by its audio and not by its path.
        """
        # Not while something is running: a scan reads folders it is about to
        # record and an apply renames them, and a presence check racing either
        # answers about a moment that has already passed.
        #
        # **Answered in the same words as the sweep it stands in for**, so the
        # busy answer carries the same two lists the ordinary one carries, and
        # the window never reads a key that is not there.
        if (
            (self._job is not None and self._job.is_alive())
            or (self._restore_job is not None and self._restore_job.is_alive())
            or self._reading()
        ):
            return {"ok": True, "missing": [], "followed": [], "checked": 0}
        recorded = self._store.library_units()
        missing: list[int] = []
        followed: list[int] = []
        for stored in recorded:
            if stored.folder_path.is_dir():
                continue
            try:
                moved = self._look_for_moved(stored)
                if moved is not None:
                    # Recorded where it is now, and the row on screen follows:
                    # the album kept its identification, its plan and its
                    # corrections, because none of those were ever about the
                    # path.
                    self._store.relocate_unit(str(stored.folder_path), str(moved))
                    with self._albums_lock:
                        state = self._albums.get(stored.unit_id)
                        row = self._shelf.get(stored.unit_id)
                        if row is not None:
                            # The card is drawn from this row, so an album
                            # followed while it is still only a row must move
                            # here too, or the shelf keeps naming a folder this
                            # very call has just corrected.
                            row["folder_path"] = str(moved)
                    # And nothing beside it: `_shelf_summary` reads whether the
                    # folder answers off this very path, every time the card is
                    # drawn. A `folder_missing` written here would be a second
                    # copy of a fact nobody reads from it.
                    if state is not None:
                        # The whole album, not only its folder. The cover is
                        # read out of the *first track*, by path, so following
                        # the folder alone leaves an album on screen with a name
                        # that is right and file names that are not.
                        state.unit = self._read_again(state.unit, moved)
                    followed.append(stored.unit_id)
                    continue
                # Not found is not gone. The same rule as the restore — the row
                # is kept and marked, and only the user takes an album off this
                # shelf.
                # Nothing to write down: whether a folder answers is asked of
                # the disk wherever it is drawn, never remembered. A remembered
                # answer is a second truth that drifts from the first, and this
                # album is about to be found again by hand.
            except Exception:
                # One album that cannot be followed must not cost the others,
                # which is the rule the restore already keeps. Unguarded, one
                # row raising `UNIQUE constraint failed` would take down the
                # whole walk, so nothing would be followed and nothing marked,
                # and all that would show is an error over an unchanged shelf.
                self._logger.exception(
                    "An album could not be followed to where it may have moved.",
                    extra={"operation": "api.library.present.failure", "album": stored.unit_id},
                )
                continue
            missing.append(stored.unit_id)
        if missing:
            self._logger.info(
                "Albums whose folder did not answer were kept and marked.",
                extra={"operation": "api.library.present.missing", "count": len(missing)},
            )
        if followed:
            # Logged, because otherwise this call would move an album and tell
            # nobody: the only trace of a corrected row would be a changed
            # `updated_at`.
            self._logger.info(
                "Albums that had moved were followed to where they are now.",
                extra={"operation": "api.library.present.followed", "count": len(followed)},
            )
        # **What was followed is named, not only what was lost.** The window
        # redraws the shelf from this answer, and a card wearing `not where it
        # was` is drawn from a folder path this call has just corrected — so a
        # sweep that fixed everything and answered with an empty list would
        # leave the chip on screen over albums that never left the disk.
        return {
            "ok": True,
            "missing": missing,
            "followed": followed,
            "checked": len(recorded),
        }

    # --- reports -----------------------------------------------------------

    def events(self) -> list[dict[str, object]]:
        """Drain and return progress events; the window polls this."""
        with self._events_lock:
            drained, self._events = self._events, []
        return drained

    def state(self) -> dict[str, object]:
        """Return the dashboard: every album, grouped by what happened to it.

        **It says how long it took.** `api.album` reports its milliseconds, and
        this is the call every gesture ends with, so it reports them too: a
        claim about the speed of the shelf's redraw should be answerable by the
        log rather than by a number remembered from an earlier measurement.
        """
        started = time.perf_counter()
        albums = [self._album_summary(state) for state in self._album_snapshot()]
        # And the ones still only on the shelf, in the same order the rows came
        # in, so the reverse below puts the newest first for both.
        with self._albums_lock:
            waiting = sorted(self._shelf.values(), key=lambda row: int(row["unit_id"]))
        albums = [_shelf_summary(row, self._origin_of) for row in waiting] + albums
        # Newest first, by the order they entered this session's map. What was
        # just chosen, dropped or downloaded is what is about to be worked on,
        # and alphabetical order would bury it among the finished albums.
        albums.reverse()
        answer = {
            "root": self._root,
            "scanning": self._job is not None and self._job.is_alive(),
            # Whether the Library is still being read back off the disk. The
            # window opens before this finishes and asks once; without being
            # told to keep asking it draws the empty library and stays there.
            "restoring": (
                self._restore_job is not None
                and self._restore_job.is_alive()
                and not self._restore_reported.is_set()
            ),
            "albums": albums,
            "history": self._history(),
            "settings": dict(self._settings),
            # Whether one folder is what the screen is about. After a restore
            # the albums come from wherever they live, and a button offering to
            # open "the folder" then opens nothing.
            "has_root": self._root is not None,
            "reach": self._reach,
            # How many of the albums above arrived by download, so the window
            # can offer to take them off the list.
            "downloads": len(self._downloaded),
            # Whether anything asked for has still not landed. The collect that
            # brings a finished download in runs on the sleep-watch thread every
            # thirty seconds and emits `collected` — but the window only keeps
            # asking for events while it believes something is running, and a
            # collect it did not start sets none of those flags. The album
            # would join the library and the event would wait in the queue
            # until some other gesture happened to drain it, so a downloaded
            # album would appear only after leaving the Library screen and
            # coming back. This is what tells the window that waiting for an
            # event is worth doing.
            "awaiting": sum(
                1
                for record in self._store.download_history()
                if not record["collected_at"] and not record["cleared_at"]
            ),
            # Whether ffmpeg is here, under the same name the bench payload uses,
            # because it is the same fact from the same attribute. It rides here
            # too so that a screen which never asks the bench anything — Mixing —
            # still knows, instead of finding out by measuring and being handed a
            # count of zero.
            "can_measure": self._ffmpeg,
        }
        self._logger.info(
            "The dashboard was read.",
            extra={
                "operation": "api.state.timing",
                "albums": len(albums),
                "milliseconds": round((time.perf_counter() - started) * 1000),
            },
        )
        return answer

    def _history(self) -> list[dict[str, object]]:
        """Return what happened to this library, newest first, of either kind.

        Two things happen to a folder: it arrives, and this app renames it.
        They are one list because they are one story, told in the order it
        happened, and each row carries the kind it is so the screen knows what
        to offer — a plan can be reverted, and a download can be opened.
        """
        entries: list[dict[str, object]] = [
            {**plan, "kind": "plan", "when": plan.get("applied_at") or ""}
            for plan in self._store.plan_history()
        ]
        downloads = self._store.download_history()
        # Asked once for the whole screen rather than once per row, because it
        # is an HTTP call to the service.
        root: Path | None = None
        if any(not d["collected_at"] and not d["cleared_at"] for d in downloads):
            root, _ = self._download_root()
        entries.extend(
            {
                **download,
                "kind": "download",
                "when": download.get("requested_at") or "",
                "arrival": _download_arrival(download, root),
            }
            for download in downloads
        )
        entries.sort(key=lambda entry: str(entry["when"]), reverse=True)
        return entries

    def album(self, unit_id: int) -> dict[str, object]:
        """Return everything the review screen shows for one album.

        The witness is asked here when it has not been asked yet: one request,
        for the one album being opened, which is what makes "always consulted"
        affordable against a service that allows twenty calls a minute. A scan
        asks it only about albums that did not settle on their own; opening one
        covers the rest, at the moment it can be read.
        """
        state = self._bring_in(unit_id)
        if state is None:
            return self._why_it_will_not_open(unit_id)
        # Timed, so that a dialog that is slow to open can be explained from
        # the log. The store reads behind the dialog are fast; the slow part is
        # the witness, which is why it is asked on a thread below.
        started = time.perf_counter()
        payload = self._album_payload(state)
        self._logger.info(
            "The album dialog was answered.",
            extra={
                "operation": "api.album",
                "album": unit_id,
                "milliseconds": round((time.perf_counter() - started) * 1000),
            },
        )
        # The question is still put — it is just not waited for. One request is
        # spent on the album being opened; nothing requires the dialog to wait
        # for it.
        self._ask_the_witness_later(state)
        return {"ok": True, "album": payload}

    def _ask_the_witness_later(self, state: _AlbumState) -> None:
        """Put the album to the witness on a thread, and say so when it answers.

        The screen is drawn from what is already known and gains the witness's
        line when the network gets round to it. One thread per album at a time,
        because the dialog is opened and closed and opened again while a slow
        request is still out, and asking twice would spend two of the twenty
        requests a minute this service allows.
        """
        if self._pipeline.workflow.verifier is None:
            return
        if "itunes" in (state.outcome.verification or {}):
            return
        with self._asking_lock:
            if state.unit_id in self._asking:
                return
            self._asking.add(state.unit_id)
        threading.Thread(target=self._run_the_witness, args=(state,), daemon=True).start()

    def _run_the_witness(self, state: _AlbumState) -> None:
        """Ask, and hand the answer to the window as the album it belongs to."""
        try:
            self._ask_the_witness(state)
            # Sent whole rather than as a verdict on its own: the dialog is
            # rendered from an album, and half an album would be a second shape
            # for the window to know how to draw.
            self._emit("album", {"album": self._album_payload(state)})
        finally:
            with self._asking_lock:
                self._asking.discard(state.unit_id)

    def _ask_the_witness(self, state: _AlbumState) -> None:
        """Put this album to the independent witness, once, and keep the verdict.

        **Especially when the catalogues failed.** Naming files correctly is
        the app's main job, so an album no source could identify is exactly the
        one where a witness that *does* know the record is worth a request.

        **The question is the album's own words, always** — the artist and
        album its folder and tags suggest, which is what a person would type.
        Asking in the candidate's words wherever a candidate exists is the rule
        written for *no candidate* rather than for *no candidate that
        convinced*: a low-confidence guess would be adopted, the witness would
        be asked about *that* record and confirm it, and the record that agrees
        with every file would never be asked about.
        """
        verification = state.outcome.verification or {}
        if "itunes" in verification:
            return
        verifier = self._pipeline.workflow.verifier
        if verifier is None:
            return
        asked = _as_search_terms(state.outcome.hints)
        if asked is None:
            return
        started = time.perf_counter()
        try:
            testimony = verifier.verify(
                asked,
                state.unit.track_durations_ms or (),
                local_track_titles(state.unit, self._pipeline.tag_store),
            )
            self._logger.info(
                "The witness was asked while an album was being opened.",
                extra={
                    "operation": "api.witness.asked",
                    "album": state.unit_id,
                    "milliseconds": round((time.perf_counter() - started) * 1000),
                },
            )
        except Exception as error:
            # *Asked and it did not work out*, which is all this knows. The
            # type is what tells a throttle from an outage, and it is in `why`.
            self._logger.warning(
                "The witness could not be asked about an album being opened.",
                extra={
                    "operation": "api.witness.failure",
                    "unit_id": state.unit_id,
                    "why": type(error).__name__,
                    "said": str(error),
                },
            )
            # Recorded as an attempt that failed, not left absent. An absent
            # entry is how the window says *the answer has not come back yet*,
            # and a failure left absent would be drawn as a request still in
            # flight. Two states, two answers.
            state.outcome = replace(
                state.outcome,
                verification={**verification, "itunes": {"witness": "itunes", "reached": False}},
            )
            return
        # Recorded either way: an answer of "found nothing" is an answer, and
        # asking again every time the album is opened would spend a request on
        # a question already put.
        state.outcome = replace(
            state.outcome,
            verification={**verification, "itunes": testimony or {"witness": "itunes"}},
        )
        if state.identification_id is not None:
            self._store.record_verification(state.identification_id, state.outcome.verification)

    def witness_record(self, unit_id: int) -> dict[str, object]:
        """Return what the witness recognised for this album, fetched now.

        The fold opened on the witness's line. Lazy like ``candidate_detail``
        and for the same reason: it costs two requests against a service that
        allows about twenty a minute, and paying that for a fold nobody opened
        is what would make opening an album slow.

        **Nothing here is kept.** The answer is not written to the outcome, not
        recorded against the identification, and not cached — it is built, drawn,
        and dropped when the dialog closes, because a verification source may be
        read on screen and never persisted. The words are gone by the next
        call, which is also why the button beside it has to search with them
        immediately rather than store them for later.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        verifier = self._pipeline.workflow.verifier
        if verifier is None:
            return {"ok": False, "error": "No witness is configured."}
        asked = _as_search_terms(state.outcome.hints)
        if asked is None:
            return {"ok": True, "record": None}
        try:
            record = verifier.recognised(asked)
        except WitnessThrottledError as error:
            # The one cause here that *was* measured, and it is the opposite of
            # not being reached: the witness answered, and what it answered was
            # `429`. It has its own type because the remedy is the opposite
            # too — wait, rather than check the network.
            self._logger.warning(
                "The witness is throttling the record that was asked for.",
                extra={
                    "operation": "api.witness.record.throttled",
                    "unit_id": unit_id,
                },
            )
            return {
                "ok": False,
                "error": f"iTunes is asking to be left alone for a moment: {error}",
            }
        except Exception as error:
            # Everything else describes nothing and quotes what it was told.
            # A sentence naming a cause would be wrong for every failure that
            # reaches here and has another one — a fault in this application's
            # own code would send the reader to check the network.
            self._logger.warning(
                "The witness could not be asked for the record that was asked for.",
                extra={
                    "operation": "api.witness.record.failure",
                    "unit_id": unit_id,
                    "why": type(error).__name__,
                },
            )
            return {"ok": False, "error": f"iTunes could not be asked: {error}"}
        if record is None:
            return {"ok": True, "record": None}
        return {
            "ok": True,
            "record": {
                "title": record.title,
                "artist": record.artist,
                "year": record.year,
                "track_count": record.track_count,
                "url": record.url,
                "tracks": [
                    {
                        "position": track.position,
                        "name": track.name,
                        "duration_ms": track.duration_ms,
                    }
                    for track in record.tracks
                ],
            },
        }

    def cover(self, unit_id: int) -> str | None:
        """Return a data URI for the album's cover, or ``None`` when it has none.

        The staged image from the plan is preferred — it is what will be
        written — and the picture already embedded in the first track is the
        fallback, so the grid shows the library the user actually has.
        """
        state = self._bring_in(unit_id)
        if state is None:
            return None
        staged = self._staged_cover(state.outcome.plan)
        if staged is not None:
            return _data_uri(staged)
        return self._embedded_cover(state.unit)

    # --- settings ----------------------------------------------------------

    def settings(self) -> dict[str, object]:
        """Return the settings the window exposes."""
        return dict(self._settings)

    def set_settings(self, values: dict[str, object]) -> dict[str, object]:
        """Store the window's settings and rebuild the pipeline around them."""
        accepted = {key: values[key] for key in _UI_SETTING_KEYS if key in values}
        self._settings.update(accepted)
        for key, value in accepted.items():
            self._store.set_setting(f"ui.{key}", str(value))
        try:
            self._pipeline = self._pipeline_factory(self._settings)
        except Exception as error:
            return {"ok": False, "error": str(error)}
        # And the plans already on the shelf, which is where these settings are
        # actually spent. Rebuilding the pipeline changes what the *next* scan
        # would do and nothing about what is on screen, so without this a
        # raised threshold applies to nothing visible and `Apply N automatic`
        # goes on applying the plans decided under the old one. The same holds
        # for every setting that reaches a name: the style, the edition, the
        # artwork, the bar.
        replanned = sum(1 for state in self._album_snapshot() if self._replan(state))
        if replanned:
            self._logger.info(
                "Settings changed, so the plans on the shelf were rebuilt around them.",
                extra={"operation": "api.settings.replanned", "albums": replanned},
            )
        # A lowered ceiling takes effect now rather than at the next restart.
        self._enforce_cache_limits()
        return {"ok": True, "settings": dict(self._settings), "replanned": replanned}

    def naming_composer(self) -> dict[str, object]:
        """Return the pieces a name can be built from, and one album to preview on.

        The arrangement of a name is composed by the user, piece by piece and
        in any order, rather than picked from fixed models.

        The sample is an album from the shelf rather than an invented one: a
        preview built from a made-up record cannot show which pieces the
        library's own catalogue data actually fills in.
        """
        sample = self._composer_sample()
        return {
            "ok": True,
            "folder": [dict(piece) for piece in _NAME_PIECES if piece["folder"]],
            "track": [dict(piece) for piece in _NAME_PIECES if piece["track"]],
            "sample": sample,
        }

    def naming_preview(self, folder_template: str, track_template: str) -> dict[str, object]:
        """Render both arrangements against a real album, or say why one cannot be.

        The refusal matters as much as the preview: a template is validated here,
        where it is being composed, so an arrangement that could not produce a
        name is never saved and never reaches a rename.
        """
        state = self._first_identified()
        release = None if state is None else state.outcome.candidate.release
        if release is None:
            return {"ok": False, "reason": "no_album"}
        # Drawn in the case the plan writes, or the preview names the album one
        # way and the rename another.
        release = normalize_release(release)
        answer: dict[str, object] = {"ok": True}
        for key, template in (("folder", folder_template), ("track", track_template)):
            if not str(template or "").strip():
                answer[key] = ""
                continue
            try:
                policy = NamingPolicy(
                    **{
                        "folder_template" if key == "folder" else "track_template": template,
                    }
                )
            except NamingError as error:
                answer[key] = ""
                answer[f"{key}_error"] = str(error)
                answer["ok"] = False
                continue
            answer[key] = self._render_sample(policy, release, key, state)
            # The shape, always: the same arrangement with each piece drawn as
            # its own name. A year the album does not have renders nothing, and
            # a name with no year looks exactly like an arrangement that was
            # never asked for one, so the chosen piece is shown by name.
            answer[f"{key}_shape"] = _render_shape(template)
            answer[f"{key}_missing"] = self._empty_pieces(policy, release, key, state, template)
        return answer

    def _empty_pieces(
        self, policy: NamingPolicy, release: object, which: str, state: object, template: str
    ) -> list[str]:
        """Name the chosen pieces this particular album has nothing to put in."""
        values = self._sample_values(policy, release, which, state)
        if values is None:
            return []
        return [
            str(_PIECE_LABELS.get(token, token))
            for token in dict.fromkeys(_PLACEHOLDER_IN_TEMPLATE.findall(template))
            if not values.get(token, "").strip()
        ]

    def _best_sample(self) -> object | None:
        """The album that fills the most pieces, so the preview shows the most.

        Not the first on the shelf: the map is walked in insertion order and
        the window draws it reversed, so "first" is the oldest album, which is
        arbitrary. If that album happened to lack a year, the year piece would
        look broken rather than absent.

        Ties go to the album nearest the top of the shelf.
        """
        best, best_score = None, -1
        for state in self._album_snapshot():
            outcome = getattr(state, "outcome", None)
            candidate = getattr(outcome, "candidate", None)
            release = None if candidate is None else getattr(candidate, "release", None)
            if release is None:
                continue
            values = self._sample_values(NamingPolicy(), release, "folder", state) or {}
            score = sum(1 for value in values.values() if str(value).strip())
            if score >= best_score:
                best, best_score = state, score
        return best

    def _sample_values(
        self, policy: NamingPolicy, release: object, which: str, state: object
    ) -> dict[str, str] | None:
        """What every piece would hold for this album, folder side or track side."""
        try:
            if which == "folder":
                container = format_label([file.properties for file in state.unit.audio_files])
                return policy.folder_values(release, container)  # type: ignore[arg-type]
            first = next(iter(state.unit.audio_files), None)
            track = release.tracks[0] if release.tracks else None  # type: ignore[attr-defined]
            return policy.track_values(
                release,  # type: ignore[arg-type]
                title=track.title if track is not None else "Track",
                track_number=1,
                disc_number=1,
                disc_count=1,
                track_artist=None,
                facts=facts_from_properties(first.properties if first is not None else None),
            )
        except (NamingError, AttributeError, IndexError):
            return None

    def _first_identified(self) -> object | None:
        """The album the composer previews against."""
        return self._best_sample()

    def _composer_sample(self) -> dict[str, object]:
        state = self._first_identified()
        if state is None:
            return {"available": False}
        return {
            "available": True,
            "folder": Path(state.unit.folder_path).name,
            "tracks": len(state.unit.audio_files),
        }

    def _render_sample(
        self, policy: NamingPolicy, release: object, which: str, state: object
    ) -> str:
        """Render one line of the preview, or empty when it cannot be rendered."""
        container = format_label([file.properties for file in state.unit.audio_files])
        try:
            if which == "folder":
                return policy.folder_name(release, container)  # type: ignore[arg-type]
            first = next(iter(state.unit.audio_files), None)
            track = release.tracks[0] if release.tracks else None  # type: ignore[attr-defined]
            return policy.track_name(
                release,  # type: ignore[arg-type]
                title=track.title if track is not None else "Track",
                track_number=1,
                disc_number=1,
                disc_count=1,
                track_artist=None,
                extension=Path(first.path).suffix if first is not None else ".flac",
                facts=facts_from_properties(first.properties if first is not None else None),
            )
        except (NamingError, AttributeError, IndexError):
            # A preview that cannot be drawn is not a reason to break the panel:
            # the line goes empty and the arrangement simply cannot be saved.
            return ""

    # --- the worker --------------------------------------------------------

    def _run_scan(
        self,
        root: Path,
        excluded: tuple[Path, ...] = (),
        announce: bool = True,
        same_audio: list[dict[str, object]] | None = None,
    ) -> None:
        """Read a folder, identify what is in it, and add it to what the window shows.

        ``announce`` off is the same work done quietly: a downloaded album joins
        the library by itself, and that must not drive the progress bar or tell
        the window a scan has started and finished when none was asked for.
        """
        try:
            # A loose track is shown and organized where it sits. Dropping it
            # here would leave a file loose in a container out of everything
            # the window reports, silently.
            units = list(self._pipeline.scanner.scan(root, excluded))
        except Exception as error:
            if announce:
                self._emit("error", {"message": str(error)})
            return
        self._identify(units, excluded, announce, same_audio=same_audio)

    def _identify(
        self,
        units: list[AlbumUnit],
        excluded: tuple[Path, ...] = (),
        announce: bool = True,
        misplaced: int = 0,
        same_audio: list[dict[str, object]] | None = None,
    ) -> None:
        """Identify these albums while saying which folders are being identified.

        The folders are held for the whole run, so a read beside it leaves them
        to the run, and released however the run ends.
        """
        folders = Counter(str(unit.folder_path) for unit in units)
        with self._albums_lock:
            self._identifying_folders.update(folders)
        try:
            self._identify_units(units, excluded, announce, misplaced, same_audio)
        finally:
            with self._albums_lock:
                self._identifying_folders.subtract(folders)
                self._identifying_folders += Counter()

    def _identify_units(
        self,
        units: list[AlbumUnit],
        excluded: tuple[Path, ...] = (),
        announce: bool = True,
        misplaced: int = 0,
        same_audio: list[dict[str, object]] | None = None,
    ) -> None:
        """Identify albums already read off the disk, and show what they are.

        Split from reading them, because the two are asked for separately:
        choosing a folder puts its albums on screen at once, and identifying
        them is what pressing Scan does to the ones that are marked.
        """
        if announce:
            # **This run's record of the sources starts empty.** Emptying it
            # only when a run *reports* leaves it standing between runs, and
            # every drop starts a run of its own, so a timeout from before
            # would be inherited by the next album dragged in and announced
            # against that scan. A run that begins owns what it says about its
            # own sources, and says nothing about what happened before it.
            self._forget_source_failures()
            # The reach panel: what this scan may touch, stated in plain
            # numbers, generated from the same scan that will do the work.
            self._reach = {
                "folders": len({unit.folder_path for unit in units if not unit.is_loose_track}),
                "loose_tracks": sum(1 for unit in units if unit.is_loose_track),
                "files": sum(unit.track_count for unit in units),
                "excluded": [str(path.name) for path in excluded],
                "extensions": sorted(AUDIO_EXTENSIONS),
                "writes": list(_WRITE_KINDS),
            }
            self._emit("scanned", {"albums": len(units), "reach": self._reach})
        # How many of them this run deliberately did not answer for, kept out
        # here so that the ending can carry it whatever happens in between.
        left_alone = 0
        # Everything from here to the last album sits inside one guard, because
        # a thread that raises here dies where nothing is watching: `finished`
        # is never emitted, the bar keeps saying "Identifying N of N", and the
        # window will not start another scan while it believes one is running.
        # The per-album guard below covers identification itself; this covers
        # what surrounds it — the store refusing a write, the audio measurement
        # finding no ffmpeg, and a name the filesystem cannot hold.
        # Kept even when nobody asked for it, because the run's own ending says
        # it; a caller that wants it too passes the list.
        arrived_same_audio = same_audio if same_audio is not None else []
        try:
            newer_than = self._store.highest_unit_id()
            recorded = self._store.record_units(units)
            arrived_same_audio.extend(self._same_audio_on_shelf(recorded, newer_than))
            # What is already on record first, and only then the decoding. They
            # are two different questions and the second one is the only one that
            # needs ffmpeg: without this split, a machine where ffmpeg is missing
            # forgot every measurement it had ever taken, because `measure`
            # returns nothing at all when there is no survey to run.
            self._facts_from_store(units)
            # The audio is measured before anything is named, so a folder is
            # never called [FLAC] on the strength of an extension. Measuring is
            # remembered by content signature, so a library pays for this once
            # and a rename does not invalidate it.
            self._measure_for_naming(units)
            # An album this application already organized is left exactly as it
            # is, including whatever has been renamed by hand since. It is
            # planned again only when that album is asked for by name.
            organized = self._store.ids_in_state("organized")
            # Two pressings of one album want one name, and neither has been
            # applied yet when the second is planned, so the claim has to be
            # tracked here rather than read off the disk.
            claimed: set[str] = set()
            for index, unit in enumerate(units, start=1):
                unit_id = recorded[str(unit.folder_path)]
                if unit_id in organized:
                    # Counted, and said at the end. An organized album can be
                    # marked and scanned — the mark is on every card — and the
                    # run reads its folder, matches it, and hands it no plan.
                    # Without the count the ending would say the album was
                    # identified when it is exactly as it was: a run claiming
                    # work it deliberately did not do.
                    left_alone += 1
                    state = _AlbumState(
                        unit=unit,
                        unit_id=unit_id,
                        outcome=self._organized_outcome(unit, unit_id),
                        organized=True,
                    )
                    # Its folder is on disk under that name, so no album planned in
                    # this run may render the same one.
                    claimed.add(unit.folder_path.name)
                    self._remember(unit_id, state)
                    if announce:
                        self._emit(
                            "identified",
                            {
                                "done": index,
                                "total": len(units),
                                "album": self._album_summary(state),
                            },
                        )
                    continue
                try:
                    outcome = self._pipeline.workflow.identify(
                        unit,
                        frozenset(claimed),
                        self._transcoded_for(unit),
                        self._rates,
                    )
                    outcome = self._with_corrections(unit_id, unit, outcome, frozenset(claimed))
                except Exception as error:
                    self._logger.exception(
                        "An album could not be identified.",
                        extra={"operation": "api.identify.failure"},
                    )
                    # A folder that was scanned is a folder the window shows.
                    # Dropping it here would let an album vanish between the
                    # count and the grid with nothing said about it.
                    outcome = IdentificationOutcome(
                        unit=unit,
                        hints=SearchHints(),
                        decision=Decision.UNIDENTIFIED,
                        reason=f"This album could not be identified: {error}",
                    )
                state = _AlbumState(unit=unit, unit_id=unit_id, outcome=outcome)
                claimed.add(_planned_folder_name(unit, outcome.plan or outcome.partial_plan))
                state.identification_id, state.plan_id = self._persist_outcome(state)
                self._remember(unit_id, state)
                if announce:
                    self._emit(
                        "identified",
                        {"done": index, "total": len(units), "album": self._album_summary(state)},
                    )
        except Exception:
            self._logger.exception(
                "The scan could not be finished.",
                extra={"operation": "api.identify.failure"},
            )
        # Nothing after the last album may stop the window from being told the
        # scan is over, or the progress bar sits at "Identifying N of N"
        # forever with no way to start another one.
        covers = 0
        try:
            covers = self._plan_missing_covers(units)
        except Exception:
            self._logger.exception(
                "Cover planning failed after the scan.",
                extra={"operation": "api.cover_plan.failure"},
            )
        finally:
            if announce:
                # What the run did *not* answer for travels with what it did:
                # a run that skips an album and reports only its successes
                # reads as a run that handled everything.
                self._emit(
                    "finished",
                    {
                        "albums": len(units),
                        "covers": covers,
                        "misplaced": misplaced,
                        # What it read and deliberately left as it was. A
                        # number, not a silence: `albums` counts what the run
                        # touched, and without this the ending claims to have
                        # identified albums it had promised not to change.
                        "left_alone": left_alone,
                        # **Which sources were not part of this answer.** A
                        # source that raises is caught and treated as having
                        # found nothing, so the run carries on with what is
                        # left. That is right, and unless it is said here an
                        # album can be settled as the wrong record with
                        # nothing showing that a catalogue was missing.
                        # Emptied where this run *began*, so what is read here
                        # is this run's own record and nothing older
                        # (`_forget_source_failures`).
                        "sources_down": self._sources_down(),
                        # Albums this run brought in whose every stream was
                        # already on the shelf in another folder.
                        "same_audio": arrived_same_audio,
                    },
                )

    def _forget_source_failures(self) -> None:
        """Empty a run's record of its sources, so the next one starts its own.

        Called where a run *begins*, not where one ends. Emptying it on the way
        out of `_sources_down` is the same thing only while runs are the only
        callers, and they are not: identifying one album from its dialog
        searches the same sources and reports nothing, so its failure would sit
        in the map until the next run arrived and reported it as its own.
        """
        workflow = getattr(self._pipeline, "workflow", None)
        if workflow is None:
            return
        workflow.sources_unavailable.clear()
        # Asked of the workflow rather than cleared through it: the count
        # belongs to the Metadata Engine, and `sources_answered` reads it back
        # as a copy — `.clear()` on that would empty the copy and leave the
        # run carrying the previous run's answers.
        forget = getattr(workflow, "forget_answers", None)
        if callable(forget):
            forget()

    def _sources_down(self) -> dict[str, str]:
        """Report the sources that failed and answered nothing.

        The question is always *what was missing from the run that just
        finished*, and what makes the answer about this run is that the record
        was emptied when it started (`_forget_source_failures`).

        **A source that answered even once is not down**, however many times it
        also failed. The sentence this feeds says the source was not consulted
        and that the run's albums were settled without it, which is false for a
        source that answered several searches and then timed out on one. A
        count of answers is what separates a service that is out from a
        request that went wrong.

        **And every kind of answer counts.** The count is kept in the Metadata
        Engine, where every request arrives, rather than beside the search
        loop: a source whose search timed out may still have served the
        releases the acoustic path fetched, and an album identified from those
        was not settled without it.
        """
        workflow = getattr(self._pipeline, "workflow", None)
        failed = dict(getattr(workflow, "sources_unavailable", {}) or {})
        answered = dict(getattr(workflow, "sources_answered", {}) or {})
        return {source: why for source, why in failed.items() if not answered.get(source)}

    def _album_snapshot(self) -> tuple[_AlbumState, ...]:
        """Return every album known right now, safe to walk while a scan fills the map."""
        with self._albums_lock:
            return tuple(self._albums.values())

    def _bring_to_front(self, unit_ids: Sequence[int]) -> None:
        """Put these albums where the shelf starts.

        The window draws this map newest-first, which is its insertion order
        reversed — so *last in* is *first seen*, and moving a row to the end is
        how an album that was just pointed at gets to the top without the
        window having to learn a sort of its own.
        """
        with self._albums_lock:
            for unit_id in unit_ids:
                state = self._albums.pop(unit_id, None)
                if state is not None:
                    self._albums[unit_id] = state

    def _why_it_will_not_open(self, unit_id: int) -> dict[str, object]:
        """Say what this application knows about an album it cannot read.

        `Unknown album` answers two different questions, and it is the wrong
        answer to the second. A row this application does not have is genuinely
        unknown; a row whose *folder* is gone is an album it holds the name, the
        state and the identification of, and saying it knows nothing about that
        one is the screen contradicting the database.

        **The twin is named when there is one**, because it is the difference
        between *this album is lost* and *this card is a copy you can let go of*.
        The case arises when an organized album is brought in a second time and
        the second folder is then deleted. The twin is found by
        `unit_signature` — the same audio, which is what identifies an album
        across runs — and only when the folder it names is actually there,
        since anything else would point at a second missing folder.
        """
        stored = self._store.unit_by_id(unit_id)
        if stored is None:
            return {"ok": False, "error": "Unknown album."}
        if stored.folder_path.exists():
            # The folder answers and the album still could not be read. That is a
            # different failure, and it keeps the sentence it had.
            return {"ok": False, "error": "Unknown album."}
        twin = self._same_audio_elsewhere(stored)
        sentence = (
            f"“{stored.folder_path.name}” is not where it was recorded, and this "
            f"app cannot read it: {stored.folder_path}."
        )
        if twin is not None:
            sentence += (
                f" The same audio is on record at {twin.folder_path}, which is "
                "still there — so this card is a second copy, and ✕ lets it go."
            )
        else:
            sentence += " Point it at the folder it lives in now, or let the card go with ✕."
        return {
            "ok": False,
            "error": sentence,
            "folder_is_gone": True,
            "folder": str(stored.folder_path),
            "same_audio_at": (
                None
                if twin is None
                else {
                    "unit_id": twin.unit_id,
                    "folder": str(twin.folder_path),
                    "organized": twin.state == "organized",
                }
            ),
        }

    def _same_audio_elsewhere(self, stored: StoredUnit) -> StoredUnit | None:
        """The other row carrying this album's audio, if its folder is still there.

        Two copies of one album are two rows on purpose: `_upsert_unit` refuses
        to collapse a signature match whose folder exists, because that would
        lose one. This reads that same fact from the other end, after one of the
        two folders has gone.
        """
        for other in self._store.units_by_signature(stored.unit_signature):
            if other.unit_id != stored.unit_id and other.folder_path.is_dir():
                return other
        return None

    def _what_came(
        self, opened: Sequence[int], arrived: Sequence[int], moved: Sequence[Mapping[str, object]]
    ) -> tuple[list[int], list[str]]:
        """Split what a folder read put on screen into arrivals and albums already here.

        Arrivals are what was new and what was already here but has moved: both
        are marked and brought to the front. **An album read again from the
        folder it already lives in is only said**, neither marked nor floated,
        so it comes back as a name for the sentence, not an id.
        One place for the drop and for opening a folder, which are one gesture.
        """
        followed = {int(str(entry["unit_id"])) for entry in moved if "unit_id" in entry}
        came = set(arrived) | (set(opened) & followed)
        here = sorted(set(opened) - came)
        names = [
            stored.folder_path.name
            for stored in (self._store.unit_by_id(unit_id) for unit_id in here)
            if stored is not None
        ]
        return sorted(came), names

    def _same_audio_on_shelf(
        self, recorded: Mapping[str, int], newer_than: int
    ) -> list[dict[str, object]]:
        """Which of the albums that just arrived hold audio already on the shelf.

        A second copy is accepted and **said at the moment it arrives**, by
        whichever door; joining in silence would let a record sit on the shelf
        twice with nothing saying so. Only rows this arrival created are
        asked, so reading a pair the user already has says nothing again; and
        only a twin whose folder is there, since one that is gone is not a copy
        they still have.
        """
        found: list[dict[str, object]] = []
        for folder, unit_id in recorded.items():
            if unit_id <= newer_than:
                continue
            twin = next(
                (
                    other
                    for other in self._store.albums_holding_all_of(unit_id)
                    if other.folder_path.is_dir()
                ),
                None,
            )
            if twin is None:
                continue
            found.append(
                {
                    "unit_id": unit_id,
                    "album": Path(folder).name,
                    "twin_id": twin.unit_id,
                    "twin": twin.folder_path.name,
                    "twin_folder": str(twin.folder_path),
                }
            )
            self._logger.info(
                "An album arrived holding audio already on the shelf.",
                extra={
                    "operation": "api.arrival.same_audio",
                    "unit_id": unit_id,
                    "twin_id": twin.unit_id,
                },
            )
        return found

    def _bring_in(self, unit_id: int) -> _AlbumState | None:
        """Return one album's full state, reading it back now if it is only a row.

        The door every gesture goes through. The Library is drawn from the
        database, so the album that is clicked may never have been read off the
        disk — and it is read at that moment, for that album, instead of the
        restore reading all of them.

        A gesture that reaches an album this cannot read gets `None`, and says
        the same sentence either way.

        **The album this session is holding is asked the same question as the
        one it is not.** Returning the copy in memory without asking about the
        folder would show the repair screen only for a card that had never been
        read off the disk, while an album scanned or organized earlier in the
        session opened the *full* dialog over files that are not there.

        Followed before it is refused, because an album that merely moved needs
        no repair: `_followed_folder` is the one place that answers *where did
        this album go*, and it is what the restore behind the other branch
        already asks. Only when nothing answers does this hand back `None` and
        let `_why_it_will_not_open` say so.

        The cost is one `stat` per dialog, and only for an album already in
        memory — the other branch reads the disk anyway.
        """
        state = self._albums.get(unit_id)
        if state is not None:
            if state.unit.folder_path.is_dir():
                return state
            stored = self._store.unit_by_id(unit_id)
            moved = None if stored is None else self._followed_folder(stored)
            if moved is None:
                return None
            # Read again rather than carried: following is not renaming, so the
            # names inside are the folder's to state.
            state.unit = self._read_again(state.unit, moved)
            return state
        with self._albums_lock:
            row = self._shelf.get(unit_id)
        if row is None:
            return None
        stored = self._store.unit_by_id(unit_id)
        if stored is None:
            return None
        try:
            self._restore_unit(stored)
        except Exception:
            self._logger.exception(
                "An album could not be read back when it was opened.",
                extra={"operation": "api.shelf.open", "folder": str(stored.folder_path)},
            )
            return None
        return self._albums.get(unit_id)

    def _remember(self, unit_id: int, state: _AlbumState) -> None:
        """Add one album to the map the window walks.

        And take its light row off the shelf in the same breath, under the same
        lock, so that there are never two answers for one album.
        """
        with self._albums_lock:
            self._albums[unit_id] = state
            self._shelf.pop(unit_id, None)

    def _remember_unless_identifying(self, unit_id: int, state: _AlbumState) -> bool:
        """Add a folder's reading unless a run is identifying that album.

        A read runs beside a scan, and both end in `_remember`. The scan's
        answer is the newer one — it asked a catalogue — so a read finishing
        after it must not put the album back to *not looked at*. Asked and
        written under one lock, because a check that far from its write is not a
        guard (see `_remember_if_absent` below).
        """
        with self._albums_lock:
            if self._identifying_folders[str(state.unit.folder_path)]:
                return False
            self._albums[unit_id] = state
            self._shelf.pop(unit_id, None)
        return True

    def _remember_if_absent(self, unit_id: int, state: _AlbumState) -> bool:
        """Add one album only if the window does not already hold it.

        The restore runs on a thread while a scan, a folder being opened or a
        drop may already be under way, and its answer is the older one — so it
        must never land on top. Checking first and then reading a folder off
        the disk before writing is not a guard: the scan slips in during the
        read, a freshly scanned album loses the cover plan the scan has just
        built for it, and `apply_automatic` then has nothing to apply. The
        check and the write are therefore one step under the lock.
        """
        with self._albums_lock:
            if unit_id in self._albums:
                return False
            self._albums[unit_id] = state
            self._shelf.pop(unit_id, None)
            return True

    def _with_corrections(
        self,
        unit_id: int,
        unit: AlbumUnit,
        outcome: IdentificationOutcome,
        taken_names: frozenset[str] = frozenset(),
        offline: bool = False,
    ) -> IdentificationOutcome:
        """Re-apply the corrections this album already carries.

        A correction is the user overruling the catalogue, and that does not
        expire when the album is looked up again. Without this, a second scan
        would plan the catalogue's name over the chosen one, and the only trace
        of the decision would be a row in the database nothing reads.
        """
        if outcome.candidate is None:
            return outcome
        if not self._store.corrections(unit_id):
            return outcome
        folder, titles, artists = self._stored_corrections(unit_id)
        # Added tracks go in before anything is aligned, so the aligner, the
        # planner and the naming all work on a release that simply has them.
        # Read against the release the *catalogue* published, which is what
        # the stored positions were said about.
        candidate, pinned, _ = self._planned_from(unit_id, unit, outcome.candidate)
        # **A correction that does not speak about this release changes nothing
        # here.** Pairings and added tracks are kept against the release they
        # were made on, and are held rather than applied when another one is in
        # use. Planning again on the strength of rows that are only held would
        # rebuild the outcome for nothing, and the rebuild is not neutral: it
        # is the path of an identified album, so an album arranged from its
        # own tags would come out of it planning tag writes and an embedded
        # cover, which an arrangement never does.
        if not (
            folder or titles or artists or pinned or candidate.release != outcome.candidate.release
        ):
            return outcome
        return replace(
            self._pipeline.workflow.refine(
                unit,
                candidate,
                folder_name=folder,
                track_titles=titles or None,
                track_artists=artists or None,
                taken_names=taken_names,
                transcoded=self._transcoded_for(unit),
                measured_rates=self._rates,
                track_files=pinned or None,
                offline=offline,
            ),
            verification=outcome.verification,
        )

    def _transcoded_for(self, unit: AlbumUnit) -> frozenset[str]:
        """Return what the last survey proved lossy *in this album*."""
        return self._transcoded.get(unit.unit_signature, frozenset())

    def set_track_quality(
        self, unit_id: int, content_signature: str, verdict: str
    ) -> dict[str, object]:
        """Take the user's word about one file, in the plan that shows it.

        The same word the Quality screen accepts, said where its consequence is
        visible: the label is on this file's name and on the folder's, and
        re-planning here redraws both immediately. ``clear`` withdraws it and
        lets the measurement speak again. Nothing is written to disk — the plan
        still waits for approval.
        """
        state = self._albums.get(unit_id)
        if state is None:
            return {"ok": False, "error": "Unknown album."}
        if _holding_the_applied_plan(state):
            return {"ok": False, "error": "This album is already applied; revert it first."}
        if state.outcome.candidate is None:
            return {"ok": False, "error": "This album has no plan to change."}
        signature = state.unit.unit_signature
        # The plan names a file by its signature, and the audio it holds is read
        # off the album in hand rather than asked of the window. Where two
        # files of this album share the shape and hold different recordings,
        # the word is written without a key and answers for both: that is all
        # this gesture can name, and the bench is where it can be said about
        # one of them.
        holding = [
            file for file in state.unit.audio_files if file.content_signature == content_signature
        ]
        keys = {file.audio_key for file in holding if file.audio_key}
        audio_key = keys.pop() if len(keys) == 1 else None
        if verdict == "clear":
            self._store.clear_quality_override(signature, content_signature, audio_key)
        elif verdict in {"honest", "transcoded"}:
            self._store.record_quality_override(signature, verdict, content_signature, audio_key)
        else:
            return {"ok": False, "error": f"Unknown verdict: {verdict}"}
        self._logger.info(
            "A track's quality was set by hand, from its plan.",
            extra={"operation": "quality.override.in_plan", "verdict": verdict},
        )
        # By hand, on this album, because the gesture points at this row. The
        # batch rule that leaves organized albums alone is about sweeps — a
        # naming style that changed, a verdict that reaches every album holding
        # the same audio — and applying it here would record the word and
        # repaint nothing on a table that draws an organized album's names.
        self._replan_around_user_word(state, by_hand=True)
        _organized_only_while_nothing_changes(state)
        return {"ok": True, "album": self._album_payload(state)}

    def _replan_around_user_word(self, state: _AlbumState, *, by_hand: bool = False) -> bool:
        """Rebuild one album's plan with the user's verdict in it.

        Re-reading what this album's files *are* is free: every file was
        measured once and is remembered by content signature, so this is a read
        of the store with the overrides on top of it.

        A function of its own so that every screen that takes a verdict calls
        it. A gesture that writes the word to the database and does not re-plan
        leaves the plan naming files `[Lossy]` about tracks that were cleared
        by hand, and that word would be written to the disk.
        """
        if state.outcome.candidate is None:
            return False
        if not by_hand and _holding_the_applied_plan(state):
            return False
        self._measure_for_naming([state.unit])
        return self._replan(state, by_hand=by_hand)

    def _replan(self, state: "_AlbumState", *, by_hand: bool = False) -> bool:
        """Rebuild one album's plan from what is known right now, and say if it moved.

        The rebuild itself, with no opinion about what changed — a verdict, a
        naming style, a threshold, a correction. Everything it plans from is
        read here: the corrections, the pairings, and the maps the measurement
        lives in. Nothing is measured and nothing reaches the network.

        An album this application has already organized is left alone. What
        keeps it out of every batch is having no plan, so building one for it
        here would hand `Apply N automatic` an album that may have been renamed
        by hand since.

        **Offline, and with the art it already had.** A whole shelf can be
        re-planned at once — a naming style changes and every plan on screen is
        rebuilt — and addressing the archive costs a MusicBrainz search and an
        archive request per album, against a source that allows about one a
        second. On a restored shelf, where nothing was fetched, that would be
        live requests every time and a window frozen for the length of them.
        Handing back the art the outcome already carries is what makes the
        offline rebuild keep the cover instead of dropping it from the plan.
        """
        if state.outcome.candidate is None:
            return False
        # A sweep never reaches an organized album; the one a gesture points at
        # does. `by_hand` is the whole difference, and it is false for both
        # callers that walk the shelf.
        if not by_hand and (state.applied or state.organized):
            return False
        folder, titles, artists = self._stored_corrections(state.unit_id)
        pinned, _ = self._stored_pairings(state.unit_id, state.outcome.candidate.release)
        state.outcome = replace(
            self._pipeline.workflow.refine(
                state.unit,
                state.outcome.candidate,
                folder_name=folder,
                track_titles=titles or None,
                track_artists=artists or None,
                transcoded=self._transcoded_for(state.unit),
                measured_rates=self._rates,
                track_files=pinned or None,
                offline=True,
                artwork=state.outcome.artwork,
            ),
            verification=state.outcome.verification,
        )
        state.identification_id, state.plan_id = self._persist_outcome(state)
        return True

    def _replan_everything_named_by(self, unit_signature: str) -> int:
        """Re-plan every Library album a verdict just spoke about, and count them.

        Keyed by `unit_signature` because that is what a quality verdict names,
        and the same album can be on the shelf more than once — the audio is the
        identity here, so one word can reach several rows.

        Returns how many were re-planned, which is what lets the window know
        whether the Library behind it has moved.
        """
        return sum(
            1
            for state in self._album_snapshot()
            if state.unit.unit_signature == unit_signature and self._replan_around_user_word(state)
        )

    def _stored_pairings(
        self, unit_id: int, release: ReleaseMetadata | None
    ) -> tuple[dict[str, int], tuple[dict[str, object], ...]]:
        """Return the pairings that answer to this release, and the ones that do not.

        A pairing names a position on a *particular* tracklist, so adopting
        another release does not carry it over: position 4 is a different song
        there. Those pairings are neither applied nor deleted but suspended,
        and the window asks whether to reuse them: pairing an album by hand is
        work, and comparing two pressings must not discard it.
        """
        key = _release_key(release)
        standing: dict[str, int] = {}
        suspended: list[dict[str, object]] = []
        for row in self._store.corrections(unit_id):
            if row["field"] not in _FIELDS_ANSWERING_TO_A_RELEASE:
                continue
            if row["track_position"] is None:
                continue
            if key is not None and row["release_key"] == key:
                # Only a pairing is a pin: an adoption standing on this release
                # is read by `_extra_positions`, which builds the tracklist.
                if row["field"] == "track_file":
                    standing[str(row["value"])] = int(row["track_position"])  # type: ignore[arg-type]
            else:
                # **An added track suspends exactly as a pairing does.**
                # Otherwise, after another release is compared, the screen
                # goes on reporting the files as unmatched with nothing saying
                # that the positions given to them are being held. Work that
                # is kept has to be reported as kept.
                suspended.append(dict(row))
        return standing, tuple(suspended)

    def _planned_from(
        self, unit_id: int, unit: AlbumUnit, candidate: MatchCandidate
    ) -> tuple[MatchCandidate, dict[str, int], bool]:
        """Return everything a re-plan needs: the release to plan, the pins, and the cost.

        **One function, because every path that re-plans needs all three and
        needs them consistent** — the correction path and `Plan this album
        again` both reach it. With three separate readers a rule ends up
        applied on one path and not on the other.

        An added track is pinned to the position it was given, and not merely
        offered to the aligner: the aligner would be free to hand that position
        to any file of a similar length, and the point of the gesture is that
        *this* file is *that* track. The candidate's confidence and explanation
        are untouched — adding a track the copy holds says nothing about how
        well the release matched.
        """
        pinned, _ = self._stored_pairings(unit_id, candidate.release)
        extras = self._stored_extras(unit_id, candidate.release, unit)
        # **Unconditionally, including when there are none.** The release held
        # here may already carry added tracks, and an early return for an empty
        # list would make `drop` not undo: the row goes and the release keeps
        # the track. `with_extra_tracks` takes them out before putting them
        # back, so this one call answers both directions.
        grown = with_extra_tracks(candidate.release, extras)
        if not extras:
            return replace(candidate, release=grown), pinned, False
        renumbering = renumbers_the_catalogue(candidate.release, extras)
        by_position = {extra.position: extra for extra in extras}
        pins = dict(pinned)
        for signature, position in self._extra_positions(unit_id, candidate.release).items():
            if position in by_position:
                pins[signature] = position
        return replace(candidate, release=grown), pins, renumbering

    def _extra_positions(self, unit_id: int, release: ReleaseMetadata | None) -> dict[str, int]:
        """Map each adopted signature to the position it was given, for this release."""
        key = _release_key(release)
        if key is None:
            return {}
        return {
            str(row["value"]): int(row["track_position"])  # type: ignore[arg-type]
            for row in self._store.corrections(unit_id)
            if row["field"] == "extra_track"
            and row["track_position"] is not None
            and row["release_key"] == key
        }

    def _stored_extras(
        self, unit_id: int, release: ReleaseMetadata | None, unit: AlbumUnit
    ) -> tuple[ExtraTrack, ...]:
        """Return the tracks added to this release, titled from the files themselves.

        Stored the way a pairing is — the file's content signature against the
        position it was given, keyed to the release it was said about — because
        it *is* a pairing, with the one difference that the release does not
        publish the position. Adopting another release drops them for the same
        reason it suspends pairings: position 11 is a claim about one
        tracklist.

        The title is read from the file every time rather than stored beside the
        position. A stored title would be a second copy of something the disk
        already says, and the two would part the first time the file is
        retagged.
        """
        key = _release_key(release)
        if key is None:
            return ()
        by_signature = {file.content_signature: file for file in unit.audio_files}
        extras: list[ExtraTrack] = []
        for row in self._store.corrections(unit_id):
            if row["field"] != "extra_track" or row["track_position"] is None:
                continue
            if row["release_key"] != key:
                continue
            file = by_signature.get(str(row["value"]))
            if file is None:
                # The folder changed since the row was written. Tolerated
                # rather than raised, exactly as a stale pairing is: a row
                # outliving its file must not stop an album from being read.
                continue
            extras.append(
                ExtraTrack(
                    position=int(row["track_position"]),  # type: ignore[arg-type]
                    title=self._title_for(file),
                    duration_ms=file.properties.duration_ms if file.properties else None,
                )
            )
        return tuple(extras)

    def _title_for(self, file: AudioFileFacts) -> str:
        """Name a file from what it already says about itself, and never invent one.

        The title tag first; the file name without its extension when there is
        no tag. Neither is a catalogue's word and neither is guessed.
        """
        tag_store = self._pipeline.tag_store
        if tag_store is not None:
            try:
                values = tag_store.read(file.path).get("title", ())
            except Exception:
                values = ()
            if values and str(values[0]).strip():
                return str(values[0]).strip()
        return file.path.stem

    def _user_titles(self, state: object, outcome: object) -> dict[int, str]:
        """What each paired file calls itself, by the release position it holds.

        Read from the pairing rather than from the file order, because the two
        are not the same thing on any album this question is interesting about.
        Returns empty when there is no pairing to read, which is the arrangement
        case and every unidentified album.
        """
        alignment = getattr(outcome, "alignment", None)
        unit = getattr(state, "unit", None)
        if alignment is None or unit is None:
            return {}
        titles = local_track_titles(unit, self._pipeline.tag_store)
        by_path = dict(zip((file.path for file in unit.audio_files), titles, strict=False))
        return {
            assignment.track.position: mine
            for assignment in alignment.assignments
            if (mine := by_path.get(assignment.file.path))
        }

    def _stored_corrections(
        self, unit_id: int
    ) -> tuple[str | None, dict[int, str], dict[int, str]]:
        """Return every correction standing for this album: folder, titles, credits.

        The whole standing set, because this is what every re-plan is given.
        A caller that passes only what it has just been handed leaves a
        withdrawn correction in force and silently drops a correction made on
        another screen.
        """
        rows = self._store.corrections(unit_id)
        folder = next((str(row["value"]) for row in rows if row["field"] == "folder_name"), None)
        by_position = {
            field: {
                int(position): str(row["value"])
                for row in rows
                if row["field"] == field and (position := row["track_position"]) is not None
            }
            for field in ("track_title", "track_artist")
        }
        return folder, by_position["track_title"], by_position["track_artist"]

    def _measure_for_naming(self, units: Sequence[AlbumUnit]) -> dict[str, frozenset[str]]:
        """Measure every file, and return the signatures that proved to be transcodes.

        The survey is the slow part of a first scan and free on every one after,
        because measurements are keyed by content signature and kept. A library
        with no ffmpeg simply gets no measurement, and every folder is then
        named from what its container declares — degraded, never wrong in a way
        that hides itself, because the log says the survey was unavailable.
        """
        survey = self._pipeline.quality
        if survey is None or not units:
            return {}
        self._emit("measuring", {"albums": len(units)})
        try:
            findings = survey.measure(
                units,
                on_album=lambda done, total, _: self._emit(
                    "measured", {"done": done, "total": total}
                ),
            )
        except Exception:
            self._logger.exception(
                "Audio could not be measured; names will follow what containers declare.",
                extra={"operation": "quality.survey.failure"},
            )
            return {}
        # The survey answers per file, told apart by the audio each one holds,
        # and the answer is carried on **without being folded**. A
        # `{signature: row}` built over a map keyed by the pair is the undoing
        # `quality_for_files` warns about: two files of one signature and
        # different audio write into one slot and the last one wins, so an
        # honest album's `lossless` row can land under the signature a
        # transcoded album reads by and take its `[Lossy]` away.
        return self._absorb(
            units,
            tuple(finding.verdict.encoding for finding in findings),
            survey.known(units),
        )

    def _facts_from_store(self, units: Sequence[AlbumUnit]) -> dict[str, frozenset[str]]:
        """Read back what these albums already measured, decoding nothing at all.

        The other half of ``_measure_for_naming``. Measuring is what a scan
        does; *knowing* is what every screen after it needs, including in a
        session that has not scanned. Without this, restoring the Library,
        adopting a release and pasting a link would all plan as though the
        audio had never been measured, and name a proven transcode `[FLAC]`
        with the measurement on record.

        This reaches no ffmpeg and no network: it is the store's own answer,
        replayed through the same rule a fresh verdict is judged by
        (``verdict_from_stored``), so a replayed name and a scanned one agree.
        An album nothing has measured yields nothing, which is the same
        degraded-never-wrong answer a machine without ffmpeg gets.
        """
        if not units:
            return {}
        # **Asked with the audio key, like every other reading of these rows.**
        # This is the path a launch takes — nothing is decoded, the store is
        # replayed — and it is what the Library dialog draws from. Asked by
        # signature alone it could not tell two recordings of one shape apart:
        # the row that came back first would decide for both, and an honest
        # FLAC and the transcode beside it would be handed one answer.
        known = measurements_for(
            self._store,
            (
                (file.content_signature, file.audio_key)
                for unit in units
                for file in unit.audio_files
            ),
        )
        replayed = tuple(
            verdict_from_stored(
                [
                    quality
                    for file in unit.audio_files
                    if (quality := known.get((file.content_signature, file.audio_key))) is not None
                ]
            ).encoding
            for unit in units
        )
        return self._absorb(units, replayed, known)

    def _absorb(
        self,
        units: Sequence[AlbumUnit],
        encodings: Sequence[Encoding],
        known: Mapping[tuple[str, str | None], StoredQuality],
    ) -> dict[str, frozenset[str]]:
        """Take one reading of these albums into the maps every screen names from.

        ``known`` is keyed by **signature and audio key together**, never by the
        signature alone: two recordings of one track share every declared thing
        a signature is made of, so a map indexed by it answers one of them with
        the other's verdict.

        ``self._transcoded`` is **updated, never replaced**. Rebinding it would
        let a second scan erase the first folder's verdicts: an album already
        on the shelf would lose its `[Lossy]` the moment another folder was
        read — on screen and in what `Apply N automatic` would write.
        """
        # The album is the unit of judgement, so an album judged a transcode
        # names every one of its files a transcode. Its tracks came off one
        # lossy master, and the few that fall under the deliberately cautious
        # per-track threshold are not a second provenance — calling such an
        # album mixed would be an artefact of the threshold, not a fact about
        # the music. Only an otherwise honest album marks its dissenters.
        #
        # The result is per album, not one set for the library. An album's
        # verdict is a statement about that album, and identity here is the
        # audio: the same track sitting in two folders is one signature, so a
        # global set would carry one album's verdict into another's name — a
        # track measured lossless in its own album would be named a fake there
        # because the same audio also sits in a compilation that is one.
        by_album: dict[str, frozenset[str]] = {}
        # `strict=True`: one reading per album is the contract of both callers,
        # and a silent truncation here would leave the last albums of a run
        # wearing whatever the map held before.
        for encoding, unit in zip(encodings, units, strict=True):
            files = [file.content_signature for file in unit.audio_files]
            if encoding is Encoding.TRANSCODED:
                by_album[unit.unit_signature] = frozenset(files)
                continue
            # A lone dissenter is read back from the store by content
            # signature, never by list position: the verdict holds only the
            # files that could be measured, so zipping it against the unit's
            # files marked the wrong track whenever one was unreadable.
            by_album[unit.unit_signature] = frozenset(
                file.content_signature
                for file in unit.audio_files
                if (quality := known.get((file.content_signature, file.audio_key))) is not None
                and stored_track_is_transcoded(quality)
            )
        self._measured.update(by_album)
        self._remember_rates(units, known)
        named = {
            signature: self._with_overrides(signature, marked, units)
            for signature, marked in by_album.items()
        }
        self._transcoded.update(named)
        return named

    def _remember_rates(
        self, units: Sequence[AlbumUnit], known: Mapping[tuple[str, str | None], StoredQuality]
    ) -> None:
        """Keep the bitrate of every file whose measurement is provably its own.

        Written as a filter rather than as a check at the moment of naming, so
        that a number that cannot be attributed never enters the application at
        all. Two ways to earn a number: the row names the audio it came from
        (`audio_key`), or no other file the application has ever read shares
        the signature.

        A row written before `audio_key` existed whose signature is ambiguous
        is therefore withheld, and the name keeps the word `[Lossy]` without a
        number. Analyzing an album in depth is what earns it back.
        """
        audio_files = [file for unit in units for file in unit.audio_files]
        signatures = [file.content_signature for file in audio_files]
        ambiguous = self._store.ambiguous_signatures(signatures, _is_on_disk)
        for file in audio_files:
            quality = known.get((file.content_signature, file.audio_key))
            if quality is None or not quality.effective_bitrate_kbps:
                continue
            if quality.is_this_file or file.content_signature not in ambiguous:
                self._rates[file.content_signature] = quality.effective_bitrate_kbps

    def _with_overrides(
        self, unit_signature: str, marked: frozenset[str], units: Sequence[AlbumUnit]
    ) -> frozenset[str]:
        """Let the user's word outrank the measurement.

        Measuring is evidence, not authority. It already yields to a pasted
        link, a corrected name and a typed title, and it has to yield here too:
        a spectrum cannot know what somebody heard.
        """
        words = self._store.quality_words(unit_signature)
        if not words:
            return marked
        held = [
            file
            for unit in units
            if unit.unit_signature == unit_signature
            for file in unit.audio_files
        ]
        album_verdict = words.get(("", None))
        if album_verdict == "honest":
            decided = set()
        elif album_verdict == "transcoded":
            decided = set(file.content_signature for file in held)
        else:
            decided = set(marked)
        # Asked per file, so a word about one recording never reaches another of
        # the same declared shape. What a *name* can carry is still per
        # signature — two files of one shape are renamed by one rule — and
        # where they disagree the accusation wins, because naming a transcode
        # `[FLAC]` is the error that reaches the disk.
        for file in held:
            said = word_for(words, file.content_signature, file.audio_key)
            if said == "honest" and file.content_signature in decided:
                if not any(
                    word_for(words, other.content_signature, other.audio_key) == "transcoded"
                    for other in held
                    if other.content_signature == file.content_signature
                ):
                    decided.discard(file.content_signature)
            elif said == "transcoded":
                decided.add(file.content_signature)
        return frozenset(decided)

    def _plan_missing_covers(self, units: Sequence[AlbumUnit] | None = None) -> int:
        """Build a cover-only plan for the coverless albums this run read.

        The bytes are the user's own, so no identification is needed — but
        nothing is written here. The plans wait for the batch gesture, and an
        album whose own plan already stages a folder cover is left to it.

        An album this scan left alone gets no cover plan either. Leaving it
        alone has to mean every write: a picture dropped into that folder is
        still a write into a folder the scan said it would not touch.

        **The run's own albums, not the shelf.** Walking the whole library
        after every run would let a scan of two albums arm a cover plan on a
        third, report a coverless album in a folder it never opened, and put
        the batch button on the bar with nothing ticked. ``None`` still means
        every one, for the callers that ask that way.
        """
        artwork = self._pipeline.artwork
        planner = self._pipeline.planner
        if artwork is None or planner is None:
            return 0
        read = None if units is None else {unit.unit_signature for unit in units}
        planned = 0
        for state in self._album_snapshot():
            if read is not None and state.unit.unit_signature not in read:
                continue
            if state.organized:
                continue
            if _stages_a_folder_cover(state.outcome.plan):
                continue
            if (
                state.cover_chosen_by_user
                and state.cover_plan is not None
                and not state.cover_applied
            ):
                # A picture chosen by hand is waiting on this album, and the
                # run that works covers out from the files would replace it
                # silently, with the plan that was read still recorded as
                # waiting. The chosen picture outranks this one.
                continue
            try:
                staged = artwork.extract_local_front(
                    tuple(file.path for file in state.unit.audio_files)
                )
            except Exception:
                self._logger.exception(
                    "An embedded picture could not be staged.",
                    extra={"operation": "api.cover_plan.failure"},
                )
                continue
            if staged is None:
                continue
            plan = planner.cover_only_plan(state.unit, staged)
            if plan.is_empty:
                continue
            state.cover_plan = plan
            # Worked out from the album's own files, so the screen must not call
            # it a chosen cover. Written rather than left at its default: an
            # album whose chosen cover was written already carries `True` here.
            state.cover_chosen_by_user = False
            state.cover_plan_id = self._store.record_plan(state.unit_id, None, plan.operations)
            planned += 1
        return planned

    # --- internals ---------------------------------------------------------

    def _apply(
        self,
        state: _AlbumState,
        decided_by: str,
        plan: ChangePlan | None = None,
        plan_id: int | None = None,
    ) -> str | None:
        plan = plan if plan is not None else state.outcome.plan
        plan_id = plan_id if plan_id is not None else state.plan_id
        assert plan is not None
        if plan_id is None:
            # **No write without a trail.** Every path that produces an outcome
            # persists it and gets an id back — except the restore, which builds
            # an `_AlbumState` directly and leaves `plan_id` at its default, so
            # an album on the shelf after a restart has none. Applying it that
            # way would quietly do less: `_witness(None)` is None, so the
            # executor records no operation; `record_execution` is skipped;
            # nothing lands in a run. The files would be renamed with no row
            # in the History and nothing to revert.
            #
            # Earned here rather than at the restore, because this is the last
            # place before the disk is touched and therefore the only one that
            # cannot be bypassed by a path written later.
            state.identification_id, state.plan_id = self._persist_outcome(state)
            plan_id = state.plan_id
            self._logger.info(
                "A plan was recorded at the moment of applying, so the write has a trail.",
                extra={"operation": "api.apply.trail_earned", "unit_id": state.unit_id},
            )
        # **`Cancel` stops being the way back here**, and here rather than at
        # any caller, for the reason the paragraph above gives: this is the last
        # place before the disk is touched, so it is the only one a path
        # written later cannot slip past. From the moment a write is attempted —
        # including one that fails partway — the way back is `Revert`, which
        # undoes files, and not a gesture that only puts a proposal back.
        self._undoable.pop(state.unit_id, None)
        # The audio is fingerprinted before anything is written, so the
        # certificate compares against what the user actually had.
        fingerprints = {file.path: audio_proof(file.path) for file in state.unit.audio_files}
        try:
            with self._apply_lock:
                # **A plan whose whole trail stands on the disk has been written,
                # and writing it again writes nothing.** Asked under the lock,
                # because that is where a second request waits: two `approve`
                # calls for one album can arrive together, and the second runs
                # the moment the first lets go. It would then rehearse a plan
                # whose every file the first has just renamed, and report each
                # file as gone over an album that was organized correctly. The
                # trail is the fact, not a flag in memory: the witness writes it
                # as each operation lands, inside this lock, and a reversal
                # clears it, so a plan taken back can be applied again. A plan
                # with no operations has an empty trail before it runs as well
                # as after, so it is not asked: it goes on to be marked
                # organized.
                if (
                    plan_id is not None
                    and plan.operations
                    and len(self._store.applied_trail(plan_id)) == len(plan.operations)
                ):
                    self._logger.info(
                        "A plan that already stands on the disk was asked for again; "
                        "nothing was written.",
                        extra={"operation": "api.apply.repeated", "unit_id": state.unit_id},
                    )
                    return None
                result = self._pipeline.executor.apply(plan, witness=self._witness(plan_id))
        except StalePlanError as error:
            # The disk moved under the plan. Saying so — and saying that a
            # fresh scan is the fix — beats a refusal that has to be decoded.
            self._logger.warning(
                "A plan was refused because the folder it describes has moved.",
                extra={"operation": "api.apply.stale", "unit_id": state.unit_id},
            )
            return str(error)
        except Exception as error:
            return str(error)
        if plan_id is not None:
            self._store.record_execution(plan_id, result.applied, complete=result.is_complete)
        if result.state is not ExecutionState.APPLIED:
            # **What landed is followed even so.** A plan that stops is a plan
            # that has already written to the disk: when the last operation,
            # the folder rename, fails, every file rename before it has landed
            # and the recorded paths have to follow them. The opposite order is
            # the one that loses an album outright: the folder moves, the
            # operation after it fails, and `album_units` goes on naming a
            # folder this application itself has just taken away.
            #
            # The trail is what is followed, not the plan, because the plan is a
            # statement of intent and only the trail says what is true of the
            # disk. It is the same four steps over a smaller list, rather than a
            # second way of doing this that would be applied by half.
            self._follow_what_landed(state, plan, result.applied)
            return result.failure or "The plan stopped partway; its trail is recorded."
        if state.identification_id is not None:
            self._store.record_identification_state(state.identification_id, "accepted", decided_by)
        self._store.set_unit_state(state.unit_id, "organized")
        state.applied = True
        state.written = True
        # An organized album is never a rejected one. `reject` refuses to mark
        # an organized album for that reason, and this is the other order: a
        # rejection followed by an approval. Left standing, the mark draws
        # `Rejected` over the files this apply has just organized, for as long
        # as the session lasts.
        state.rejected = False
        self._follow_what_landed(state, plan, result.applied)
        self._certify(state, plan, plan_id, fingerprints)
        if state.plan_id is not None and state.plan_id != plan_id:
            state.plan_id = plan_id
        return None

    def _witness(self, plan_id: int | None) -> Callable[[AppliedOperation], None] | None:
        """Return the recorder that writes one plan's trail as the plan runs.

        The executor stays storage-free and `library/` never imports the
        database; what crosses the boundary is this one callable, made here.
        """
        if plan_id is None:
            return None
        return lambda entry: self._store.record_operation_applied(plan_id, entry)

    def _follow_what_landed(
        self, state: _AlbumState, plan: ChangePlan, trail: Sequence[AppliedOperation]
    ) -> None:
        """Take every recorded path, and the album itself, where the disk now is.

        The four steps an apply is followed by, over **the operations that
        really ran**. A plan that finished and its trail are the same list, so
        the ordinary path is unchanged; a plan that stopped is the reason this
        takes a trail at all.

        `_follow_the_bytes` comes after `_follow_the_album`, because that is what
        settles the paths it weighs, and a weight has to be written at the path
        it belongs to.
        """
        landed = replace(plan, operations=tuple(entry.operation for entry in trail))
        self._follow_the_root(landed)
        self._follow_the_rename(landed)
        self._follow_the_album(state, landed)
        self._follow_the_bytes(file.path for file in state.unit.audio_files)

    def _follow_the_root(self, plan: ChangePlan) -> None:
        """Move the scanned root with the folder, when the folder that moved was it.

        Scanning one album's own folder is a normal gesture, and that album may
        well be renamed — which renames the folder the window is showing as the
        root. Everything afterwards reads that path: the header, "Open folder",
        the next scan. Left behind, they would all point at a folder that no
        longer exists.
        """
        if self._root is None:
            return
        root = Path(self._root)
        for operation in plan.operations:
            if operation.kind is OperationKind.RENAME_FOLDER and operation.target_path == root:
                moved = Path(str(operation.after_state["path"]))
                self._logger.info(
                    "The scanned folder was renamed by its own album; following it.",
                    extra={"operation": "api.apply.root_moved", "folder": str(moved)},
                )
                self._root = str(moved)
                return

    def _follow_the_rename(self, plan: ChangePlan) -> None:
        """Move every recorded path with the folder this plan just renamed.

        The same problem as ``_follow_the_root`` and a worse consequence.
        Organising a download renames its folder — that is what this
        application is for — and the path written when it arrived then names
        something that no longer exists. ``_restore_downloads`` would look it
        up on the next start, find nothing, and drop it without a word, so a
        downloaded album would be in the library for exactly as long as the
        window stays open.

        **The files are followed too, and in the plan's own order.** An album is
        organized by renaming its files while the folder still has its old name
        and renaming the folder last, so each file is followed where it stands
        and the folder then carries all of them across at once. Reversed, that
        order is the one ``_follow_the_revert`` walks.
        """
        for operation in plan.operations:
            if operation.kind is OperationKind.RENAME_FILE:
                self._store.relocate_file(
                    str(operation.target_path), str(operation.after_state["path"])
                )
                continue
            if operation.kind is not OperationKind.RENAME_FOLDER:
                continue
            moved = str(operation.after_state["path"])
            was = str(operation.target_path)
            if self._store.relocate_download(was, moved):
                self._logger.info(
                    "A downloaded album was renamed; its recorded path followed it.",
                    extra={"operation": "api.apply.download_moved", "folder": moved},
                )
            # And the album's own row, which is what the Library is restored
            # from. A stale path here would take the album off the shelf at
            # the next launch.
            if self._store.relocate_unit(was, moved):
                self._logger.info(
                    "An album was renamed; its recorded folder followed it.",
                    extra={"operation": "api.apply.album_moved", "folder": moved},
                )

    def _follow_the_album(self, state: _AlbumState, plan: ChangePlan) -> None:
        """Move the album the window is holding to where the apply just put it.

        Renaming the folder is what this application is for, and the copy of the
        album in memory keeps the name it was scanned under. Left that way,
        everything that reaches for the album by path would ask about a folder
        that no longer exists: `Open album` would answer that the folder is no
        longer on disk about the folder this application has just written, and
        `Send to bench` would refuse the same album for the same reason.

        The plan is the only truthful account of where things went — file
        renames and folder renames shift paths in order — so ``_settled``
        replays it rather than this guessing at the new name.

        The recorded row is followed separately, by ``_follow_the_rename``.
        ``library_present`` deliberately checks that one and never this one:
        a presence check reading a stale in-memory path would take the row away
        from every album this application had just organized.

        **The files move even when the folder does not.** The album's cover is
        drawn by reading the picture out of the first track, by path, so
        returning early on an unchanged folder name would leave every track
        holding the name it was scanned under: an album whose folder was
        already named correctly would lose its cover from the shelf the moment
        it was applied, until the next scan, with the picture itself untouched.
        """
        moved = _settled(state.unit.folder_path, plan.operations)
        files = tuple(
            replace(file, path=_settled(file.path, plan.operations))
            for file in state.unit.audio_files
        )
        companions = tuple(_settled(path, plan.operations) for path in state.unit.companion_files)
        if (
            moved == state.unit.folder_path
            and companions == state.unit.companion_files
            and all(
                new.path == old.path for new, old in zip(files, state.unit.audio_files, strict=True)
            )
        ):
            return
        state.unit = replace(
            state.unit, folder_path=moved, audio_files=files, companion_files=companions
        )

    def _follow_the_bytes(self, paths: Iterable[Path]) -> None:
        """Write down what the files weigh, now that this write has changed them.

        A renamed album is found by weighing the audio inside every folder this
        application has been shown and comparing the multiset against
        `audio_files.file_size_bytes`. If only a scan wrote that column, an
        apply that embeds a cover would leave the database describing the bytes
        the tracks had before it ran, and an album organized and then renamed
        outside the application could not be found again. The next launch
        repairs the sizes of every album still sitting where its row says
        (``_restore_unit`` re-records what it reads), which is every album
        except the one that moved.

        **A file that will not answer is skipped and said.** `stat` failing here
        means the disk will not describe a file this application has just
        written; the row keeps what it had, which is stale rather than invented.
        """
        weighed: dict[Path, tuple[int, datetime]] = {}
        refused: list[Path] = []
        for path in paths:
            try:
                stats = path.stat()
            except OSError:
                refused.append(path)
                continue
            weighed[path] = (stats.st_size, datetime.fromtimestamp(stats.st_mtime, UTC))
        if refused:
            self._logger.warning(
                "Files this apply wrote could not be weighed afterwards; their recorded "
                "size still describes what they held before it ran.",
                extra={
                    "operation": "api.apply.unweighable",
                    "files": len(refused),
                    "first": str(refused[0]),
                },
            )
        if not weighed:
            return
        self._store.record_audio_sizes(weighed)

    def _where_the_trail_leads(
        self, trail: Sequence[AppliedOperation], unit_id: int | None
    ) -> tuple[Path, Path]:
        """Name the folder the trail ends in, and the folder the album is in now.

        The two are the same for an album still sitting where it was organized,
        and they differ for one that has been filed away since, which is an
        ordinary thing to do with an organized album. Where the album *is* is
        asked of the disk and then of the search for a moved album, the same
        two questions the Library itself asks; when neither answers, the
        recorded folder is returned for both and the reversal is left to refuse
        honestly rather than to guess.
        """
        folders = [
            Path(str(entry.operation.after_state["path"]))
            for entry in trail
            if entry.operation.kind is OperationKind.RENAME_FOLDER
        ]
        if folders:
            was = folders[-1]
        else:
            # A cover-only plan renames nothing, so the album folder is what its
            # operations have in common. `commonpath` of a single path is that
            # path itself, and of several files in one folder it is the folder —
            # so the answer is the common path unless the common path is one of
            # the files, which is the trail of one operation.
            targets = [entry.operation.target_path for entry in trail]
            common = Path(os.path.commonpath([str(path) for path in targets]))
            was = common.parent if common in targets else common
        stored = self._store.unit_by_id(unit_id) if unit_id is not None else None
        if stored is None:
            return was, was
        if stored.folder_path.is_dir():
            return was, stored.folder_path
        moved = self._look_for_moved(stored)
        if moved is None:
            return was, was
        self._store.relocate_unit(str(stored.folder_path), str(moved))
        return was, moved

    def _follow_the_revert(
        self, undone: Sequence[AppliedOperation], state: "_AlbumState | None"
    ) -> None:
        """Put every recorded path back where a reversal has just put the files.

        The mirror of ``_follow_the_rename`` and ``_follow_the_album``.
        Reverting moves the folder back to the name it had; if the album's row
        went on naming the organized one, the next start would look for a
        folder that is no longer there and take the row off the Library.
        Nothing reads the stale value while the window stays open, so the
        defect would not show until then.

        A reversal undoes the most recent operation first, so the folder goes
        back before the files do — which is the order the registered paths need,
        because a file's two names were both recorded under the *old* folder.
        """
        for entry in undone:
            if entry.operation.kind is OperationKind.RENAME_FILE:
                self._store.relocate_file(
                    str(entry.operation.after_state["path"]),
                    str(entry.operation.before_state.get("path", entry.operation.target_path)),
                )
                continue
            if entry.operation.kind is not OperationKind.RENAME_FOLDER:
                continue
            was = str(entry.operation.after_state["path"])
            now = str(entry.operation.before_state.get("path", entry.operation.target_path))
            if was == now:
                continue
            self._store.relocate_download(was, now)
            if self._store.relocate_unit(was, now):
                self._logger.info(
                    "A reverted album's recorded folder went back with it.",
                    extra={"operation": "api.revert.album_moved", "folder": now},
                )
        if state is None:
            return
        moved = _settled_back(state.unit.folder_path, undone)
        if moved == state.unit.folder_path:
            return
        state.unit = replace(
            state.unit,
            folder_path=moved,
            audio_files=tuple(
                replace(file, path=_settled_back(file.path, undone))
                for file in state.unit.audio_files
            ),
            companion_files=tuple(
                _settled_back(path, undone) for path in state.unit.companion_files
            ),
        )

    def _certify(
        self,
        state: _AlbumState,
        plan: ChangePlan,
        plan_id: int | None,
        fingerprints: dict[Path, AudioProof | None],
    ) -> None:
        """Prove what an apply did: the audio is intact and every write took.

        A failed proof never rolls anything back on its own — the trail is
        intact and reversal is the user's deliberate gesture — but it is loud:
        the album is flagged and the certificate says exactly which file or
        write disagreed.
        """
        finals = _final_paths(plan)
        audio: dict[str, str] = {}
        audio_ok = True
        for original, before in fingerprints.items():
            final = finals.get(original, original)
            if before is None:
                audio[original.name] = "unverifiable"
                continue
            after = audio_proof(final)
            # Method as well as digest: one container can be proved two ways,
            # and two digests taken differently are not evidence of anything
            # when they disagree.
            if after is not None and (after.method, after.digest) == (before.method, before.digest):
                audio[original.name] = "intact"
            else:
                audio[original.name] = "CHANGED"
                audio_ok = False
        writes, writes_ok = self._verify_writes(plan, finals)
        state.audio_ok = audio_ok
        state.writes_ok = writes_ok
        if not audio_ok or not writes_ok:
            # With the album and the entries that disagreed, so that the log
            # alone says what failed and nobody has to open the database.
            self._logger.error(
                "An apply failed its own certificate.",
                extra={
                    "operation": "api.certify.failure",
                    "album": state.unit_id,
                    "folder": str(state.unit.folder_path),
                    "audio": sorted(name for name, said in audio.items() if said == "CHANGED"),
                    "writes": sorted(label for label, said in writes.items() if said != "verified")[
                        :8
                    ],
                },
            )
        if plan_id is not None:
            self._store.record_proof(
                plan_id, "audio", audio_ok, {"files": audio, "method": "per-container stream hash"}
            )
            self._store.record_proof(plan_id, "writes", writes_ok, {"writes": writes})

    def _verify_writes(
        self, plan: ChangePlan, finals: dict[Path, Path]
    ) -> tuple[dict[str, str], bool]:
        """Read back every write and compare it with what the plan intended.

        Each write is checked where its target ends up, not where it happened:
        a file renamed before the album folder moves lives at neither of the
        paths its own operation recorded, and only replaying the later renames
        finds it.
        """
        results: dict[str, str] = {}
        ok = True
        operations = plan.operations
        for index, operation in enumerate(operations):
            label = f"{operation.sequence}. {operation.kind}"
            try:
                verified = self._verify_operation(operation, operations[index + 1 :])
            except Exception as error:
                verified = f"unreadable: {error}"
            results[label] = verified
            if verified != "verified":
                ok = False
        return results, ok

    def _verify_operation(
        self, operation: ChangeOperation, later: Sequence[ChangeOperation]
    ) -> str:
        after = dict(operation.after_state)
        if operation.kind in (OperationKind.RENAME_FILE, OperationKind.RENAME_FOLDER):
            settled = _settled(Path(str(after["path"])), later)
            return "verified" if settled.exists() else "missing"
        if operation.kind is OperationKind.WRITE_TAGS:
            if self._pipeline.tag_store is None:
                return "unverifiable"
            settled = _settled(operation.target_path, later)
            written = self._pipeline.tag_store.read(settled)
            # Compared in the form writing to THIS file produces, through the
            # writer's own rule. The plan says `tracknumber "7"` and an ID3 file
            # reads back `"7/11"`, so comparing the pre-fold value would
            # certify every MP3 apply as `differs: tracknumber`.
            desired = {
                name: tuple(values)
                for name, values in as_the_container_writes(settled, dict(after["tags"])).items()
            }
            wrong = [
                name for name, values in desired.items() if tuple(written.get(name, ())) != values
            ]
            return "verified" if not wrong else f"differs: {', '.join(sorted(wrong))}"
        if operation.kind is OperationKind.WRITE_IMAGE:
            facts = self._artwork_store.describe(_settled(operation.target_path, later))
            return "verified" if facts and facts.digest == after["digest"] else "differs"
        # A Finder icon carries no digest to compare: it is drawn by the system
        # from the file's own picture, so what can be verified is that the file
        # now draws one. Reading `after["digest"]` on this operation would
        # raise KeyError, and the certificate would report `unreadable` about
        # an icon that had been written.
        if after.get("icon"):
            settled = _settled(operation.target_path, later)
            drawn = self._artwork_store.shows_its_cover_in_the_finder(settled)
            return "verified" if drawn else "missing"
        facts = self._artwork_store.describe_embedded(_settled(operation.target_path, later))
        return "verified" if facts and facts.digest == after["digest"] else "differs"

    def _persist_outcome(
        self, state: _AlbumState, method: str = "text_search"
    ) -> tuple[int | None, int | None]:
        outcome = state.outcome
        # A new identification is being recorded, so the plan now held is not the
        # one that was written. Stated at this one funnel because every place
        # that replaces an outcome arrives here. `_apply` sets it back to True
        # after this call, which is the one order that matters.
        #
        # **`applied` is deliberately left alone.** It carries the other meaning —
        # *this album has been applied* — which `plan_anyway` and `Scan this
        # album` read to decide whether the album is finished with. Clearing it
        # here would make planning an album again work exactly once.
        state.written = False
        if outcome.candidate is None:
            # **An organized album stays organized here as well**, which is the
            # rule the last line of this method states, on the exit that never
            # reaches it. A catalogue that does not answer has said nothing
            # about the files, and they are where the last apply left them.
            self._store.set_unit_state(
                state.unit_id, "organized" if state.organized or state.applied else "discovered"
            )
            return None, None
        # An album the audio found is recorded as found by the audio. Without
        # it, an identification the fingerprint made would come back from a
        # restart looking like any text search, and the window would have no
        # way to say which it was.
        if outcome.acoustic == "named" and method == "text_search":
            method = "acoustic"
        # An album named by its own files is recorded as named by them, or an
        # album applied from its tags would come back from a restart looking
        # like a text search against a catalogue that was never asked.
        if str(outcome.candidate.release.source) == str(MetadataSources.TAGS) and (
            method == "text_search"
        ):
            method = "existing_tags"
        # **As the catalogue published it, never as it was grown here.** This
        # row is the cache of a *source's* release, keyed by (source, release
        # id), and writing an added track into it would put a track the source
        # never published inside the record of that source's release. It would
        # also come back on the next read unmarked, so the growth would run
        # again and the file would appear twice: once as the track holding its
        # audio and once as a track that no file could hold. Added tracks live
        # in `manual_corrections` and are put back on every read, which is the
        # one place they can be without a second copy to disagree with.
        release_row = self._store.record_release(
            without_extra_tracks(
                outcome.candidate.release,
                claims(self._stored_extras(state.unit_id, outcome.candidate.release, state.unit)),
            )
        )
        identification_id = self._store.record_identification(
            state.unit_id,
            release_row,
            method=method,
            confidence=outcome.confidence,
            explanation=outcome.candidate.explanation,
        )
        plan_id = None
        if outcome.plan is not None and outcome.plan.operations:
            plan_id = self._store.record_plan(
                state.unit_id, identification_id, outcome.plan.operations
            )
        # The application's own verdicts about this identification — plus what
        # the audio answered, so that "asked, and it has never heard of this
        # record" outlives the window. No catalogue of the witness is kept here
        # and none is added.
        verification = dict(outcome.verification or {})
        if outcome.acoustic:
            verification["acoustic"] = outcome.acoustic
        if verification:
            self._store.record_verification(identification_id, verification)
        # **An organized album stays organized in the row as well.** Every path
        # that re-plans one album by hand ends in this line, so pressing `Plan
        # this album again` on an organized album and being told *nothing
        # would change* must not write `identified` over `organized`: the next
        # start reads that row, and would bring the album back as one waiting
        # for review. The screen would keep the word for the session and the
        # database would lose it for good.
        #
        # **A rejected album stays rejected in the row as well**, by the same
        # reasoning. A re-plan of the whole shelf arrives here for every album
        # on it, and writing `identified` over `skipped` would leave the session
        # saying `Rejected` and the next start bringing the album back
        # automatic, in reach of the batch. A gesture that takes the album up
        # again clears the mark before it arrives here.
        self._store.set_unit_state(
            state.unit_id,
            (
                "organized"
                if state.organized or state.applied
                else (
                    "skipped"
                    if state.rejected
                    else "identified" if outcome.decision is Decision.AUTOMATIC else "needs_review"
                )
            ),
        )
        return identification_id, plan_id

    def _origin_of(self, unit_id: int) -> str:
        """Whether this album arrived by download **in this session**.

        One function, because the word must mean one thing: `album_units.origin`
        is the album's whole life, and most albums on a shelf may have been
        downloaded at some point, while the chip counts only what came in
        during this session. Reading one for the shelf's light row and the
        other for the card would make the chips count two different questions.

        The chip exists to clear away what has just arrived, so the session is
        the fact it is about.
        """
        return "downloaded" if unit_id in self._downloaded else "scanned"

    def _rating_of(self, unit_id: int) -> int | None:
        """The rating this album was given, or ``None`` where it has none."""
        stored = self._store.unit_by_id(unit_id)
        return stored.rating if stored else None

    def rate_album(self, unit_id: int, rating: int | None = None) -> dict[str, object]:
        """Record a rating for one album, from 1 to 5.

        ``None`` withdraws the rating and is what pressing the star already
        chosen sends: five values and a way back out, which is the whole
        vocabulary. Nothing here writes to the disk, reads a tag or asks a
        source — the number is the user's alone, and it is checked only for
        being one of the five.
        """
        if rating is not None and rating not in (1, 2, 3, 4, 5):
            return {"ok": False, "error": "A rating is a whole number from 1 to 5."}
        if self._store.unit_by_id(unit_id) is None:
            return {"ok": False, "error": "Unknown album."}
        self._store.rate_unit(unit_id, rating)
        # The shelf is drawn from the rows read at startup, so the row behind
        # the dialog has to learn this too or the card would keep the old
        # number until the next restart.
        if (row := self._shelf.get(unit_id)) is not None:
            row["rating"] = rating
        self._logger.info(
            "An album was rated.",
            extra={"operation": "api.library.rate", "album": unit_id, "rating": rating},
        )
        return {"ok": True, "rating": rating}

    def _album_summary(self, state: _AlbumState) -> dict[str, object]:
        outcome = state.outcome
        release = _shown(outcome.candidate.release if outcome.candidate else None)
        paired, total, files_unpaired, tracks_unpaired = _match_counts(outcome)
        return {
            # How much of this album is settled, in one number: every file
            # holding a track and every track holding a file. It is the
            # question "is this only a rename?" answered before the dialog is
            # opened, so a shelf of complete albums can be approved at a
            # glance.
            "match_paired": paired,
            "match_total": total,
            "match_percent": round(100 * paired / total) if total else None,
            # The two halves of what is open, so the sentence behind the badge
            # can say *1 file without a track · 2 tracks without a file* instead
            # of a total that reads as a count of files.
            "match_files_unpaired": files_unpaired,
            "match_tracks_unpaired": tracks_unpaired,
            # **How much of this album's own names met the release's.** The
            # explanation paragraph already says it in words; the screen has to
            # be able to say it where it is read first, so the number comes out
            # as a number.
            "names_agreeing": (
                outcome.candidate.signals.titles_agreeing if outcome.candidate else None
            ),
            "names_compared": (
                outcome.candidate.signals.titles_compared if outcome.candidate else None
            ),
            "unit_id": state.unit_id,
            "folder": state.unit.folder_path.name,
            "tracks": state.unit.track_count,
            "decision": str(outcome.decision),
            "confidence": outcome.confidence,
            "applied": state.applied,
            "written": state.written,
            "planned_again": state.planned_again,
            # Finished, and not asked about since. Computed here so the window
            # and the guards read one answer.
            "asleep": _asleep(state),
            # What the unmatched rows offer, and what has already been
            # answered. `next_position` is filled into the field so the case
            # that renumbers nothing — a track past the catalogue's last —
            # needs no typing at all. Past the FINISHED list's last, standing
            # adoptions included: `next_free_position` strips them on purpose,
            # and a ceiling that ignored them would make two unlisted files
            # compete for one place.
            "next_position": (
                next_free_position(outcome.candidate.release)
                + len(self._extra_positions(state.unit_id, outcome.candidate.release))
                if outcome.candidate is not None
                else 1
            ),
            "extra_positions": self._extra_positions(
                state.unit_id, outcome.candidate.release if outcome.candidate else None
            ),
            # Whether the given numbering has moved a track the catalogue
            # numbered. The screen has to say this out loud: the folder then
            # carries numbers that disagree with the release named in its own
            # header, and a provenance still claimed would be false.
            "user_numbering": (
                self._planned_from(state.unit_id, state.unit, outcome.candidate)[2]
                if outcome.candidate is not None
                else False
            ),
            # A cover that was chosen and has not been written yet, with what
            # it would replace: a picture over a picture is the write that most
            # needs reading before it runs.
            "cover_choice": (
                {
                    # **A picture inside a file and the icon drawn on it are two
                    # writes of one kind**: both are `EMBED_IMAGE`, and the
                    # icon's says so in its `after_state`. Counted together
                    # they read as twice the album's tracks, and the icon — the
                    # one place the Finder shows — would go unnamed.
                    "files": sum(
                        1
                        for operation in state.cover_plan.operations
                        if operation.kind is OperationKind.EMBED_IMAGE
                        and not operation.after_state.get("icon")
                    ),
                    "icons": sum(
                        1
                        for operation in state.cover_plan.operations
                        if operation.kind is OperationKind.EMBED_IMAGE
                        and operation.after_state.get("icon")
                    ),
                    "replaces": sum(
                        1 for operation in state.cover_plan.operations if operation.before_state
                    ),
                    "width": next(
                        (
                            operation.after_state.get("width")
                            for operation in state.cover_plan.operations
                            if operation.kind is OperationKind.WRITE_IMAGE
                        ),
                        None,
                    ),
                    "height": next(
                        (
                            operation.after_state.get("height")
                            for operation in state.cover_plan.operations
                            if operation.kind is OperationKind.WRITE_IMAGE
                        ),
                        None,
                    ),
                    # **The picture itself, before it is written.** A size and
                    # a count are not enough: a site's logo and the album's
                    # cover differ in nothing a number here would show.
                    "preview": self._chosen_preview(state.cover_plan),
                }
                if state.cover_chosen_by_user
                and state.cover_plan is not None
                and not state.cover_applied
                else None
            ),
            "rejected": state.rejected,
            "held": state.held,
            # One fact, one word. Organized in this session and organized in an
            # earlier one are the same thing about the album; which session did
            # it is a fact about a run, and lives in `applied`.
            "organized": state.organized or state.applied,
            "applicable": bool(outcome.plan is not None and outcome.plan.is_applicable),
            "title": release.title if release else None,
            "artist": (written_credit(release.artists) if release else None),
            "year": (
                (release.original_released_on or release.released_on).year
                if release and (release.original_released_on or release.released_on)
                else None
            ),
            "url": _source_url(release),
            # Which catalogue that url leads to. The right-click menu is built
            # from this summary, so without it the menu has no source to name.
            "source": str(release.source) if release else None,
            "cover_pending": bool(state.cover_plan is not None and not state.cover_applied),
            "proof": _proof_tier(outcome),
            "witnesses": _witness_summary(outcome.verification),
            # What the witness is looking at, when it is not this record.
            # Computed here and named, never reassembled in the window from the
            # two folds that hold the numbers.
            "witness_knows_better": _witness_knows_better(outcome.verification),
            # Where each of the three catalogues stands, whether it won or not.
            # By name, not by length: a source that publishes every one of the
            # album's titles is saying something about the record. Always
            # computed: the header names the source in use and its witnesses as
            # links, and the detail of where each stands is one fold away, so
            # hiding them when nothing is in doubt would take away the links as
            # well.
            "positions": _source_positions(outcome, state.unit_id in self._asking),
            # What this album's own identification went without (source →
            # reason), for the dialog's quiet note — never a notice over the
            # shelf.
            "sources_missed": dict((outcome.verification or {}).get("missed") or {}),
            "audio_ok": state.audio_ok,
            "writes_ok": state.writes_ok,
            # Where this album came from, and whether it is still where it was
            # recorded. Both live on the row rather than being inferred by the
            # window, so the chips and the cards can never disagree about what
            # they are counting.
            "origin": self._origin_of(state.unit_id),
            # Read from the row every time rather than carried on the state:
            # rating it is a write that must show on the shelf behind the
            # dialog, and a second copy in memory would go stale the first time
            # the rating changed.
            "rating": self._rating_of(state.unit_id),
            # Whether this album has been looked up at all. An album whose
            # folder was merely opened is on screen waiting to be scanned, and
            # the card says so rather than looking like a failed identification.
            "looked_at": state.looked_at,
            # **And whether one ever was, which is a different question.**
            # `looked_at` is about the outcome on screen: arranging an album by
            # its own tags produces an outcome no catalogue answered, so it
            # goes false — and with one flag the window would say that no
            # catalogue has been asked about an album it had scanned and
            # identified. This one is about the album's history, read from the
            # row rather than from the outcome.
            #
            # **The album's history, not its standing row.** *There is an
            # identification on record* is something an arrangement writes for
            # itself the moment it is applied, so that reading would make an
            # album nobody ever looked up claim a catalogue had answered, and
            # one whose only look-ups were superseded claim none ever had. It
            # is asked of every identification the album ever had, and of the
            # release's source.
            "catalogue_asked": self._store.catalogue_answered_for(state.unit_id),
            # Whether this album is still where the application thinks it is.
            # **Asked of the disk every time rather than remembered**, because
            # a remembered answer is a second truth that drifts from the first:
            # following the album, relocating it by hand and organising it all
            # move the folder, and each of them would have to remember to clear
            # a flag. A `stat` per card costs little; a card wrong about its
            # folder goes on offering AUTO and accepting scans that do nothing.
            "folder_missing": not state.unit.folder_path.is_dir(),
            "folder_path": str(state.unit.folder_path),
            # Read back off the disk, as opposed to drawn from the database's
            # columns. Both rows carry the key so that neither has to be
            # recognised by what it is missing.
            "settled": True,
        }

    def _album_payload(self, state: _AlbumState) -> dict[str, object]:
        # Every gesture that answers with a dialog comes through here — opening
        # an album, re-planning it, adopting another release, correcting a name.
        # Asking the witness only in `album()` would mean that re-planning an
        # album produces a screen where the witness was never consulted. One
        # request per album acted on, never one per album on screen: nothing
        # that draws the grid reaches this.
        #
        # **Never while the dialog waits.** The request takes seconds, so every
        # gesture that answers with a dialog starts the question and hands the
        # screen what is already known; the answer arrives as its own event, on
        # the album it belongs to.
        self._ask_the_witness_later(state)
        outcome = state.outcome
        payload = self._album_summary(state)
        payload["reason"] = outcome.reason
        # What the match measured, kept apart from why it is blocked, so the
        # heading and the body do not list the same blockers twice.
        payload["explanation"] = outcome.candidate.explanation if outcome.candidate else ""
        # The same evidence, minus the facts this dialog states elsewhere.
        # Built here rather than trimmed in the window, because a sentence
        # reassembled on the other side of the bridge is a second rule for the
        # same thing. `explanation` stays whole for everything that reads it on
        # its own: a candidate row, a below-threshold reason, a search message.
        # **The lengths go too.** The proof line two rows down is the lengths,
        # so a sentence stating how many track lengths agreed above `lengths
        # match the release` would be the same fact in two wordings.
        payload["evidence_line"] = (
            explain_evidence(
                outcome.candidate.signals,
                said_elsewhere=frozenset({"titles", "track_count", "durations"}),
            )
            if outcome.candidate
            else ""
        )

        # Whether the footer may offer a way back from the last gesture. Read
        # from the map rather than from the album, because the album this draws
        # may be the photograph itself — the answer `Cancel` returns — and that
        # one has just stopped being undoable.
        payload["can_cancel"] = state.unit_id in self._undoable
        # **Whether leaving this album is worth a question.** Asking wherever
        # anything can be taken back would include a scan, which arms that, and
        # scanning an album again would then cost a second click on the way
        # out. A scan replaces a proposal with a fresher one and writes nothing;
        # leaving it standing is the ordinary outcome, and `Cancel` is still on
        # the row for as long as the session holds it.
        #
        # Answered here and named, never worked out in the window from the
        # gesture's label.
        #
        # **And only where there is something waiting.** A heading saying that
        # a plan is waiting over a fold reading `Nothing would change` is one
        # screen making two opposite claims: nothing is waiting and nothing is
        # there to keep, so leaving costs what staying costs.
        #
        # Asked of **every plan this album has**, rather than of the one the
        # dialog happens to be offering. Which of the two is on offer is a rule
        # the window already states, and restating it here in another language
        # would be a second place for it; the question here does not need it.
        # Nothing is waiting when nothing anywhere has an operation in it,
        # which is the same set written as its complement.
        pending = self._undoable.get(state.unit_id)
        # **Leaving a finished album that was planned again puts it back**:
        # closing keeps the album as it was. `Plan this album again` is only
        # offered on an album already organized or applied, and leaving one
        # standing with a proposal would draw it on the shelf as `Auto` — a
        # finished record reading as unfinished because it was looked into.
        # The way to keep the proposal is to apply it; closing takes it back
        # without a question.
        payload["leaving_takes_back"] = pending is not None and pending.gesture == "planned_again"
        payload["ask_before_leaving"] = (
            pending is not None
            and pending.gesture not in ("scanned_again", "planned_again")
            and any(
                plan is not None and plan.operations
                for plan in (outcome.plan, outcome.partial_plan)
            )
        )
        # The word the database holds, which outlives a re-plan and is not
        # `organized` above. The window closes `Reject` on it.
        payload["organized_on_record"] = self._organized_on_record(state)
        payload["source"] = str(outcome.candidate.release.source) if outcome.candidate else None
        payload["release_id"] = (
            outcome.candidate.release.source_release_id if outcome.candidate else None
        )
        payload["operations"] = (
            [_operation(operation) for operation in outcome.plan.operations]
            if outcome.plan is not None
            else []
        )
        payload["blockers"] = list(outcome.plan.blockers) if outcome.plan else []
        # What the plan has to say without refusing to run: today, that the
        # library folder already holds this album's name.
        payload["warnings"] = list(outcome.plan.warnings) if outcome.plan else []
        partial = outcome.partial_plan
        payload["partial_applicable"] = partial is not None
        payload["partial_warnings"] = list(partial.warnings) if partial else []
        payload["partial_matched"] = partial.matched_tracks if partial else 0
        payload["partial_operations"] = (
            [_operation(operation) for operation in partial.operations] if partial else []
        )
        payload["candidates"] = [_candidate(c) for c in state.alternatives]
        # The full facts of the release actually in use — format, country,
        # label, catalogue number. Every edition of one album shares a title
        # and a year, so without these the header reads identically before and
        # after "Use this", and adopting looks like it did nothing.
        payload["release"] = _candidate(outcome.candidate) if outcome.candidate else None
        # What each field opens filled with, which is what *would be written*:
        # the correction where one stands, the catalogue where none does. The
        # release itself stays as the source published it, since that is what a
        # correction is measured against, so the standing set is laid over it
        # here rather than kept in it.
        _, corrected_titles, corrected_credits = self._stored_corrections(state.unit_id)
        # How many titles of this album carry a correction, which is what arms
        # the album's own way out. Counted from the same set the fields are
        # filled from, so the number on the button and the words on the rows
        # can never describe two different albums.
        payload["title_corrections"] = len(corrected_titles)
        # The file's own spelling, on the one kind of line where the catalogue
        # is as likely to be wrong as the file. Nothing is decided here and
        # nothing is written: the row is marked and both spellings are shown.
        # Where a correction stands the same aside carries the way back out of
        # it instead.
        mine = self._user_titles(state, outcome)
        # And the wider question, asked only where there is evidence to ask it
        # with: a witness that agreed with every one of the files' titles turns
        # *did somebody slip a letter* into *does the catalogue write a
        # different word here*. Two titles that differ by one real word are
        # several edits apart, which no rule over letters may settle.
        backing = _witness_backing_user_titles(outcome.verification)
        payload["release_tracks"] = (
            [
                {
                    "position": track.position,
                    "title": corrected_titles.get(track.position, track.title),
                    # The credit as this track's file name writes it.
                    "artist": corrected_credits.get(track.position, _written_credit(track)),
                    **_user_spelling_offer(
                        mine.get(track.position),
                        track.title,
                        backing,
                        standing=corrected_titles.get(track.position),
                    ),
                }
                # In the case the plan writes, since an untouched field is what
                # gets written.
                for track in normalize_release(outcome.candidate.release).tracks
            ]
            if outcome.candidate
            else []
        )
        # Only a compilation puts a track's artist into its file name, so only a
        # compilation offers one to type. The window is told the fact rather
        # than deriving it: the rule that decides the name has to be the rule
        # that decides the field, or the field appears where the name has
        # nowhere to put what it holds.
        payload["is_various"] = (
            is_various(outcome.candidate.release) if outcome.candidate else False
        )
        payload["corrections"] = list(self._store.corrections(state.unit_id))
        # What the corrections form opens filled with. It is computed here
        # rather than in the window so that what is offered and what counts as
        # a correction cannot drift apart.
        payload["planned_folder"] = _planned_folder_name(state.unit, outcome.plan)
        # An arrangement's provenance, and an identification read against the
        # tags. The first says where an arranged name came from and which files
        # dissented; the second, on an album a catalogue answered, says in one
        # line whether that answer agrees with what the files themselves were
        # claiming. Computed only where it applies, so neither can appear on
        # the other's kind of album.
        provenance = outcome.provenance
        payload["tag_reading"] = (
            {
                "files": provenance.files,
                "artist": provenance.artist,
                "artist_agree": provenance.artist_agree,
                "album": provenance.album,
                "album_agree": provenance.album_agree,
                "year": provenance.year,
                "titled": provenance.titled,
                "numbered": provenance.numbered,
                "dissent": list(provenance.dissent),
            }
            if provenance is not None
            else None
        )
        payload["tags_say"] = (
            self._pipeline.workflow.compare_with_tags(state.unit, outcome.candidate.release)
            if (
                outcome.candidate is not None
                and str(outcome.candidate.release.source) != str(MetadataSources.TAGS)
                # After an apply the tags were rewritten from this very release,
                # so the line would always read "agrees" — true and empty.
                and not (state.applied or state.organized)
            )
            else {}
        )
        payload["verification"] = outcome.verification
        # What the audio answered when the names failed. The window says it
        # beside the identification, because "the service has never heard of
        # this record" is an ordinary answer for music the fingerprint
        # database does not cover, and an application that goes quiet there
        # reads as one that did not try.
        payload["acoustic"] = outcome.acoustic or self._what_the_audio_answered(state.unit)
        payload["unit_signature"] = state.unit.unit_signature
        payload["quality_overrides"] = dict(
            self._store.quality_overrides(state.unit.unit_signature)
        )
        # The pairings made by hand, and the ones waiting on a question. The
        # suspended rows carry the file's name when the file is still here, and
        # say so when it is not — a pairing can outlive the folder it was made
        # in, and "3 pairings from another release" with nothing to look at is
        # not something anyone can answer.
        pinned, suspended = self._stored_pairings(
            state.unit_id, outcome.candidate.release if outcome.candidate else None
        )
        here = {file.content_signature: file for file in state.unit.audio_files}
        payload["suspended_pairings"] = [
            {
                "track_position": row["track_position"],
                "file": (here[str(row["value"])].path.name if str(row["value"]) in here else None),
                # Which of the two the user made, because they are answers to
                # different questions: *this file is that track* against *this
                # file is a track the catalogue does not list*. Reusing them is
                # one gesture, but a line that called both a pairing would be
                # telling them something they did not do.
                "added": row["field"] == "extra_track",
            }
            for row in suspended
        ]
        payload["evidence"] = _evidence_rows(
            outcome,
            self._transcoded_for(state.unit),
            self._measured.get(state.unit.unit_signature, frozenset()),
            dict(pinned),
        )
        return payload

    def _what_the_audio_answered(self, unit: AlbumUnit) -> str:
        """Derive this album's acoustic state from the files, when the outcome forgot.

        The outcome knows only what *this* run learned, and an album on the shelf
        after a restart may have been restored from an identification recorded
        without the answer — so the dialog would say the audio had not been
        fingerprinted about files that carry a fingerprint and a lookup.

        Read from the files rather than from the identification because that is
        where the fact lives: `acoustic_prints` records what was asked and what
        came back, per audio, and it survives every re-identification. No
        request is spent — this is a store read.
        """
        answers = [answer for answer in self._answers_about(unit).values() if answer.asked]
        if not answers:
            return ""
        return "named" if any(answer.recording_id for answer in answers) else "unknown"

    def _answers_about(self, unit: AlbumUnit) -> dict[Path, AcousticAnswer]:
        """Return what the audio answered, per file, where the answer is provably about it.

        Keyed by the file rather than by its signature, because two files of one
        album can share a signature and an answer filed under the shape would be
        shown on both lines.

        No request is spent — this is a store read. Two things are asked,
        because a content signature is a declared shape and not a recording:
        the shape of the file, and the identity of its audio, which is cheap
        for a FLAC that declares its own digest and costs a read of the stream
        for one that does not.

        **The same two ways to earn an answer as a bitrate has:** the row names
        the audio it came from, or no other file this application has ever
        read shares the signature. Two different recordings of one length and
        format share a signature, so reading by shape alone would offer one
        file the other's catalogue page as its own identity. A row from before
        the audio had a name is claimed the moment that audio is fingerprinted
        again and the print comes back identical; until then it answers only
        where nothing contradicts it.
        """
        keys = {file.path: audio_key(file.path) for file in unit.audio_files}
        heard = self._store.acoustic_answers(
            (file.content_signature, keys[file.path]) for file in unit.audio_files
        )
        contested = self._store.ambiguous_signatures(
            (signature for (signature, _), answer in heard.items() if not answer.is_this_audio),
            _is_on_disk,
        )
        found: dict[Path, AcousticAnswer] = {}
        for file in unit.audio_files:
            answer = heard.get((file.content_signature, keys[file.path]))
            if answer is None:
                continue
            if answer.is_this_audio or file.content_signature not in contested:
                found[file.path] = answer
        return found

    def _stored_exclusions(self, root: Path) -> list[str]:
        stored = self._store.setting(f"exclusions.{root}")
        if not stored:
            return []
        try:
            names = json.loads(stored)
        except ValueError:
            return []
        return [str(name) for name in names] if isinstance(names, list) else []

    def _staged_cover(self, plan: ChangePlan | None) -> Path | None:
        if plan is None:
            return None
        for operation in plan.operations:
            if operation.kind is OperationKind.WRITE_IMAGE:
                source = Path(str(operation.after_state["source"]))
                if source.is_file():
                    return source
        return None

    def _embedded_cover(self, unit: AlbumUnit) -> str | None:
        """The picture this album already has, from wherever it actually keeps it.

        The embedded one first, because it is this application's own business —
        and then the folder's, which is where a great many albums keep the only
        cover they have. Reading only the first track would show an empty card
        for a folder whose files carry no picture and which holds a perfectly
        good `cover.jpg`.
        """
        if self._cover_cache is None or not unit.audio_files:
            return None
        # **The folder's picture first.** It is the album's cover in the only
        # sense anything outside this application uses: it is what the Finder
        # draws and what every player reads, and it is one picture for the
        # record rather than one per track. The first track may carry a
        # ripper's logo while the real cover sits in the folder beside it, or
        # no picture at all.
        from_folder = self._folder_cover(unit)
        if from_folder is not None:
            return from_folder
        first = unit.audio_files[0].path
        try:
            facts = self._artwork_store.describe_embedded(first)
            if facts is None:
                return None
            self._cover_cache.mkdir(parents=True, exist_ok=True)
            cached = self._cover_cache / f"{facts.digest}{extension_for(facts.media_type)}"
            if not cached.is_file():
                self._artwork_store.extract(first, cached)
            return self._picture(cached)
        except Exception:
            return None

    def _folder_cover(self, unit: AlbumUnit) -> str | None:
        """The image sitting in the album's own folder, drawn as it is."""
        if unit.is_loose_track:
            return None
        artwork = self._pipeline.artwork
        if self._cover_cache is None or artwork is None:
            return None
        folder = unit.folder_path
        try:
            # **One rule, one place**: `front_in_folder`, the largest picture
            # that does not say it is not the front. Taking the first image in
            # alphabetical order instead would pick `back.jpg` in every folder
            # that has one.
            found = artwork.front_in_folder(folder)
            if found is not None:
                facts, entry = found
                # Copied into the cache and served from there, exactly as an
                # embedded picture is. Serving it from the album's own folder
                # would hand the window a path outside the one directory the
                # image server answers for, and the fallback to a `data:` URI
                # is the slow path this cache exists to avoid.
                self._cover_cache.mkdir(parents=True, exist_ok=True)
                cached = self._cover_cache / f"{facts.digest}{extension_for(facts.media_type)}"
                if not cached.is_file():
                    cached.write_bytes(entry.read_bytes())
                return self._picture(cached)
        except Exception:
            return None
        return None

    def _chosen_preview(self, plan: ChangePlan) -> str | None:
        """Draw the picture a cover plan would write, before it is written.

        The staged copy is named by its digest, so the cache takes it under the
        same name and two choices of one picture are one file.
        """
        source = next(
            (
                operation.after_state.get("source")
                for operation in plan.operations
                if operation.kind is OperationKind.WRITE_IMAGE
            ),
            None,
        )
        if not source or self._cover_cache is None:
            return None
        staged = Path(str(source))
        try:
            self._cover_cache.mkdir(parents=True, exist_ok=True)
            cached = self._cover_cache / staged.name
            if not cached.is_file():
                cached.write_bytes(staged.read_bytes())
        except OSError:
            return None
        return self._picture(cached)

    def _picture(self, cached: Path) -> str | None:
        """How a drawn picture reaches the window: a URL when one can be made.

        Drawing a shelf of covers from `data:` URIs is several times slower
        than drawing it from URLs, and the difference grows on the redraw every
        refresh performs, because a `data:` URI is decoded again for each card
        that is rebuilt. Over that sits the bridge: one call per cover, each
        carrying the whole picture as base64, which by URL never crosses at
        all.

        The `data:` URI stays as the fallback for a window built without a
        server — the tests compose one that way, and a cover that draws slowly
        is better than a Library with no covers in it.
        """
        if self._images is None or self._cover_cache is None:
            return _data_uri(cached)
        return self._images(cached, self._cover_cache) or _data_uri(cached)

    def _load_settings(self) -> dict[str, object]:
        stored = {key: self._store.setting(f"ui.{key}") for key in _UI_SETTING_KEYS}
        settings: dict[str, object] = {}
        if stored["naming_style"]:
            settings["naming_style"] = stored["naming_style"]
        if stored["include_edition"]:
            settings["include_edition"] = stored["include_edition"] == "True"
        if stored["artwork_enabled"]:
            settings["artwork_enabled"] = stored["artwork_enabled"] == "True"
        if stored["threshold"]:
            settings["threshold"] = float(stored["threshold"])  # type: ignore[arg-type]
        # Unlike the rest, this one has a default that is not "off": a tree
        # that closes itself has to be unfolded again on every visit. Absent
        # means on, so it is written here rather than left out, and the window
        # never has to guess.
        settings["keep_trees_open"] = (
            stored["keep_trees_open"] == "True" if stored["keep_trees_open"] else True
        )
        if stored["ui_density"]:
            settings["ui_density"] = stored["ui_density"]
        # Always answered, so the Settings field is never blank and the ceiling
        # the pictures are held to is always a number on screen.
        settings["spectrogram_cache_mb"] = (
            int(float(stored["spectrogram_cache_mb"]))
            if stored["spectrogram_cache_mb"]
            else DEFAULT_CACHE_LIMIT_BYTES // MEGABYTE
        )
        settings["metadata_cache_mb"] = (
            int(float(stored["metadata_cache_mb"]))
            if stored["metadata_cache_mb"]
            else DEFAULT_METADATA_CACHE_BYTES // MEGABYTE
        )
        settings["artwork_cache_mb"] = (
            int(float(stored["artwork_cache_mb"]))
            if stored["artwork_cache_mb"]
            else DEFAULT_ARTWORK_CACHE_BYTES // MEGABYTE
        )
        settings["welcomed"] = stored["welcomed"] == "True" if stored["welcomed"] else False
        settings["backup_mb"] = (
            int(float(stored["backup_mb"]))
            if stored["backup_mb"]
            else DEFAULT_BACKUP_LIMIT_BYTES // MEGABYTE
        )
        settings["database_copies_mb"] = (
            int(float(stored["database_copies_mb"]))
            if stored["database_copies_mb"]
            else DEFAULT_COPY_LIMIT_BYTES // MEGABYTE
        )
        # A setting is two lists, what is saved and what is loaded, and this
        # function is the only thing that reads any of them at launch. A key
        # in `_UI_SETTING_KEYS` that is not read here is stored on every change
        # and never comes back: the shelf would open in the default order
        # however it was left.
        if stored["library_sort"]:
            settings["library_sort"] = stored["library_sort"]
        if stored["column_widths"]:
            settings["column_widths"] = stored["column_widths"]
        return settings

    def _emit(self, kind: str, payload: dict[str, object]) -> None:
        with self._events_lock:
            self._events.append({"type": kind, "payload": payload})


def _camelot_of(measured: StoredHarmonics) -> str | None:
    """Where this reading sits on the wheel, or `None` when no key was found."""
    if not measured.tonic or not measured.mode:
        return None
    return camelot_for(measured.tonic, measured.mode)


def _numbered_name(name: str) -> tuple[str | int, ...]:
    """A sort key that reads every run of digits in a name as one number.

    File names carry the album's order in their track numbers, and comparing
    them as text betrays that the moment the digits change width: `010` sorts
    between `01 ` and `02 `, because a space orders before a digit. Splitting
    on digit runs keeps the pieces strictly alternating — text at even
    positions, numbers at odd ones — so the tuples always compare like with
    like.
    """
    parts = re.split(r"(\d+)", name.casefold())
    return tuple(int(part) if index % 2 else part for index, part in enumerate(parts))


def _by_user(folders: list[dict[str, object]]) -> list[dict[str, object]]:
    """Gather folders under whoever is sending them, newest sender first.

    Who a download is coming from is the first thing about it on this network:
    one peer's queue moves as one, a peer that goes offline takes everything of
    its own with it, and every action worth offering — pause the lot, cancel the
    lot — is naturally that peer's. Clients of the network group this way.
    """
    users: dict[str, list[dict[str, object]]] = {}
    for folder in folders:
        users.setdefault(str(folder["source"]), []).append(folder)
    return [
        {
            "source": source,
            "folders": theirs,
            "files": sum(len(f["files"]) for f in theirs),  # type: ignore[arg-type]
            "size": sum(int(f["size"]) for f in theirs),  # type: ignore[call-overload]
            "moved": sum(int(f["moved"]) for f in theirs),  # type: ignore[call-overload]
            "rate": sum(float(f["rate"] or 0) for f in theirs),  # type: ignore[arg-type]
            # The same question one level up: everything of this peer's that has
            # still to arrive, so their row ends when they do.
            "still_coming": sum(int(f["still_coming"]) for f in theirs),  # type: ignore[call-overload]
            "done": all(bool(f["done"]) for f in theirs),
            # The order the folders already carry is the order they were asked
            # for, so the first of them dates the user.
            "at": str(theirs[0]["at"]),
        }
        for source, theirs in users.items()
    ]


_SLEEP_WATCH_SECONDS = 30.0
"""How often to ask whether anything is still moving.

Deliberately slow. This decides whether a machine may doze, which is not a
question that changes in a second, and the answer costs an HTTP call.
"""


def _landed_folder(root: Path, directory: object) -> Path | None:
    """Where a download landed under *root*, or nothing when the name is not a leaf.

    ``directory`` is the folder name a stranger on Soulseek published, kept
    verbatim on purpose: the files keep the names the source gave them.
    Turning it into a path with ``root / name.rsplit("\\", 1)[-1]`` is a way
    out of the download folder entirely, for two reasons: splitting on the
    Windows separator does nothing to a POSIX one, and
    ``Path("/downloads") / "/Users/someone/Music"`` **is**
    ``/Users/someone/Music`` — an absolute right-hand side replaces the left.

    The collector scans wherever it decides a download landed and marks
    everything it finds as *downloaded*; a peer folder named for the user's
    music library would have this application walk that library, claim it, and
    hand every album of it to `Clear downloaded`. The same value is also passed
    to the platform's ``open``.

    So exactly one component is taken, on either separator, which is why
    `@@abcde\\shared\\Zorblat Nine` still lands as `Zorblat Nine`. The result
    is then *proved* to be under the root before it is handed back, because
    taking a component is a rule about the string and the guarantee needed
    here is about the path.

    A name that is no component at all — empty, `.`, `..` — is refused outright,
    since there is no honest folder to point at and inventing one would be
    claiming to know where a stranger's files went.
    """
    name = str(directory or "")
    if not name:
        return root
    # Both separators, because the name may have been made on either system.
    leaf = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not leaf or set(leaf) == {"."}:
        return None
    landed = root / leaf
    try:
        if not landed.resolve().is_relative_to(root.resolve()):
            return None
    except OSError:
        return None
    return landed


def _download_arrival(record: dict[str, object], root: Path | None) -> str:
    """Say where one recorded download actually got to, rather than assuming.

    Writing `Downloaded` on every row without asking anything would leave a
    request whose files never arrived, from a peer that went away, claiming
    that it had.

    Four answers, and each is a different thing to do about it:

    - ``collected`` — it arrived and became an album. This is the ordinary one.
    - ``cleared`` — it has been taken off the list.
    - ``waiting`` — the folder is on this disk and the collect has not run yet,
      which lasts thirty seconds.
    - ``absent`` — nothing is there. Either still coming or never coming, and
      this deliberately does not guess which: the row carries the date it was
      asked for, which is what tells those apart.

    ``waiting`` is also the answer when the service cannot be asked where
    downloads land, because then nothing here can be said honestly.
    """
    if record.get("cleared_at"):
        return "cleared"
    if record.get("collected_at"):
        return "collected"
    if root is None:
        return "waiting"
    landed = _landed_folder(root, record.get("directory"))
    return "waiting" if landed is not None and landed.is_dir() else "absent"


def _download_finished(
    record: dict[str, object],
    transfers: tuple[object, ...],
    landed: Path,
    accepted: Container[str] = (),
) -> bool:
    """Say whether one recorded download has finished *arriving* — all of it.

    The service is asked first, because it is the one moving the files. When its
    list has been cleared it can no longer say — which is an ordinary state, and
    what is on disk is then weighed against what was asked for.

    Settled is not the same as arrived, and **arrived is not the same as whole**.
    Asking only whether at least one file completed would call one file of
    fourteen a finished album, and a folder collected that way may be missing
    tracks and hold others cut off mid-file. The library would then measure
    that audio, fingerprint it and identify the part against the whole release.

    A truncated file is the expensive half. `content_signature` is a digest of
    the sample count, so the signature of half a song is a valid signature of
    a recording that does not exist — and every measurement keyed to it is
    orphaned the moment the rest of the file arrives.

    The question is therefore asked twice over: did every file this folder was
    asked for stop at `completed`, and did each one bring all its bytes.

    ``accepted`` holds the folders that were looked at and taken anyway — a
    peer that vanished leaves a folder that will never be whole, and refusing
    it for ever is not this application's decision to make.
    """
    if str(record["directory"]) in accepted:
        return True
    mine = [
        transfer
        for transfer in transfers
        if transfer.source == record["source"] and transfer.directory == record["directory"]
    ]
    if mine:
        if not all(transfer.state.has_stopped for transfer in mine):
            return False
        return all(_arrived_whole(transfer) for transfer in mine)
    if not landed.is_dir():
        return False
    # The service has forgotten this transfer, so the only witness left is the
    # disk. Weighed in bytes rather than counted in files: what was asked for
    # includes whatever else the folder held — a cue sheet, a log, a scan — and
    # counting audio against a file count would refuse a folder that arrived
    # complete. Anything this application writes afterwards only adds.
    asked = int(record.get("size_bytes") or 0)
    if not asked:
        return any(
            entry.suffix.casefold() in AUDIO_EXTENSIONS
            for entry in landed.iterdir()
            if entry.is_file()
        )
    return _bytes_below(landed) >= asked


def _arrived_whole(transfer: object) -> bool:
    """Say whether one transfer both completed and brought every byte it promised."""
    if transfer.state.value != "completed":
        return False
    size = transfer.size_bytes
    moved = transfer.transferred_bytes
    # A service that does not say how big the file was cannot be contradicted;
    # `completed` is then all there is, and it is taken at its word.
    if not size or moved is None:
        return True
    return moved >= size


def _bytes_below(folder: Path) -> int:
    """Weigh everything in one folder, including what sits in subfolders of it."""
    return sum(entry.stat().st_size for entry in folder.rglob("*") if entry.is_file())


def _stopped_short(files: Sequence[Mapping[str, object]]) -> bool:
    """Say whether every transfer of a folder has stopped and the folder is short.

    Computed here, once, and carried in the payload. The window draws this and
    the Transfers menu reads it, which would otherwise be two places working
    out one condition in two languages.
    """
    if not files:
        return False
    if not all(TransferState(str(entry["state"])).has_stopped for entry in files):
        return False
    return any(
        str(entry["state"]) != "completed" or int(entry["moved"]) < int(entry["size"])
        for entry in files
    )


def _under_any(path: Path, roots: set[str]) -> bool:
    """Say whether a folder sits inside any of these."""
    return any(path == Path(root) or Path(root) in path.parents for root in roots if root)


def _terms(query: str) -> tuple[str, ...]:
    """Break a query into the words a folder name can be checked against."""
    return tuple(word for word in re.split(r"[^0-9a-z]+", query.casefold()) if word)


def _relevance(folder: object, terms: tuple[str, ...]) -> list[float]:
    """Score one result the way a person reads the list, most important first.

    The network answers in the order peers reply, which is no order at all:
    a search for an artist can put single tracks from compilations that merely
    mention the name above the artist's own albums. The order of the criteria
    is: the terms in the folder, then whether it looks like an album, then
    what the audio is, then a free slot, then speed.

    A list rather than a number, because these are ranks and not quantities: a
    thousand kilobytes a second must never outweigh the artist's name being in
    the folder.
    """
    name = " ".join(_terms(getattr(folder, "name", "") or ""))
    matched = sum(1 for term in terms if term in name) / len(terms) if terms else 0.0
    files = getattr(folder, "files", ())
    audio = [
        file
        for file in files
        if any(file.basename.casefold().endswith(kind) for kind in AUDIO_EXTENSIONS)
    ]
    return [
        matched,
        1.0 if len(audio) >= 3 else 0.0,
        _audio_rank(audio),
        (
            1.0
            if getattr(folder, "availability", None) and folder.availability.value == "available"
            else 0.0
        ),
        float(getattr(folder, "transfer_rate", 0) or 0),
    ]


def _audio_rank(files: list[object]) -> float:
    """Rank what a folder holds by what its audio is, lossless above lossy.

    Read from the attributes the sources publish, which is a claim rather than a
    measurement — what a file really is gets decided by measuring it, elsewhere
    and later. For choosing which of two copies to fetch, the claim is what
    there is.
    """
    if not files:
        return 0.0
    lossless = sum(
        1
        for file in files
        if file.bit_depth or file.basename.casefold().endswith((".flac", ".wav", ".aiff", ".aif"))
    )
    if lossless >= len(files) / 2:
        return 2.0
    rates = [file.bitrate_kbps for file in files if file.bitrate_kbps]
    return 1.0 + (sum(rates) / len(rates) / 1000) if rates else 0.5


def _candidate(candidate: MatchCandidate) -> dict[str, object]:
    """Describe one candidate with everything needed to judge it by eye.

    A score and a title are not enough: telling the CD edition from the vinyl
    master is the whole question a review answers, and that lives in the
    format, the country, the label and the two different years.
    """
    release = candidate.release
    signals = candidate.signals
    return {
        "source": str(release.source),
        "release_id": release.source_release_id,
        "title": release.title,
        "artist": written_credit(release.artists),
        "year": release.released_on.year if release.released_on else None,
        "master_year": (
            release.original_released_on.year if release.original_released_on else None
        ),
        "tracks": len(release.tracks),
        "track_titles": [track.title for track in release.tracks],
        "formats": list(release.formats),
        # What kind of record it is, which the dialog's head names beside the
        # label. Genres and not styles: Discogs' styles run to three or four
        # per release and describe a scene rather than a shelf.
        "genres": list(release.genres),
        "country": release.country,
        "labels": list(release.labels),
        "catalog_numbers": list(release.catalog_numbers),
        "barcode": release.barcode,
        "has_master": bool(release.release_group_id),
        "confidence": candidate.confidence,
        "explanation": candidate.explanation,
        "titles_agreeing": signals.titles_agreeing,
        "titles_compared": signals.titles_compared,
        "durations_agreeing": signals.durations_agreeing,
        "durations_compared": signals.durations_compared,
        "local_tracks": signals.local_track_count,
        "url": _source_url(release),
    }


_YEAR_IN_NAME = re.compile(r"\((\d{4})\)")
_EDITION_IN_NAME = re.compile(r"[-\u2013\u2014]?\s*\bEd\.?\s*(\d{4})\b", re.IGNORECASE)
_FORMAT_IN_NAME = re.compile(r"\[([^\]]+)\]\s*$")


def _album_reports(
    log: Sequence[Mapping[str, object]],
    sources: Mapping[str, tuple[str, str]],
    proof_flags: Mapping[int, Mapping[str, bool]] | None = None,
    witnesses: Mapping[str, Mapping[str, object]] | None = None,
) -> list[AlbumReport]:
    """Group an executed trail into one report row per album.

    A run touches a folder, its files, their tags and their pictures; read
    back file by file, a report runs to thousands of rows nobody can scan.
    Everything one album did belongs on one line.
    """
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for entry in log:
        grouped.setdefault(str(entry["folder_path"]), []).append(entry)
    return [
        _album_report(path, entries, sources, proof_flags or {}, witnesses or {})
        for path, entries in grouped.items()
    ]


def _album_report(
    folder_path: str,
    entries: Sequence[Mapping[str, object]],
    sources: Mapping[str, tuple[str, str]],
    proof_flags: Mapping[int, Mapping[str, bool]],
    witnesses: Mapping[str, Mapping[str, object]],
) -> AlbumReport:
    before_name, after_name = _folder_move(folder_path, entries)
    tracks = _track_runs(entries)
    source = sources.get(folder_path)
    url = _RELEASE_LINKS.get(MetadataSourceId(source[0])).format(id=source[1]) if source else ""
    return AlbumReport(
        album=after_name,
        folder=change(before_name, after_name),
        year=change(_captured(_YEAR_IN_NAME, before_name), _captured(_YEAR_IN_NAME, after_name)),
        edition=change(
            _captured(_EDITION_IN_NAME, before_name), _captured(_EDITION_IN_NAME, after_name)
        ),
        container_format=change(
            _captured(_FORMAT_IN_NAME, before_name), _captured(_FORMAT_IN_NAME, after_name)
        ),
        tracks=tracks,
        tags=_tag_runs(entries),
        artwork=_artwork_runs(entries),
        audio=_proof_runs(entries, proof_flags, "audio"),
        verified=_proof_runs(entries, proof_flags, "writes"),
        witnesses=_witness_runs(witnesses.get(folder_path)),
        source_label=f"{source[0]} {source[1]}" if source else "",
        source_url=url or "",
        state="reverted" if all(entry["reverted_at"] for entry in entries) else "applied",
    )


def _proof_runs(
    entries: Sequence[Mapping[str, object]],
    proof_flags: Mapping[int, Mapping[str, bool]],
    kind: str,
) -> tuple[Run, ...]:
    """Render one proof column: green certainty, red alarm, or honest absence."""
    verdicts = [
        proof_flags[int(str(entry["plan_id"]))][kind]
        for entry in entries
        if entry.get("plan_id") is not None
        and int(str(entry["plan_id"])) in proof_flags
        and kind in proof_flags[int(str(entry["plan_id"]))]
    ]
    if not verdicts:
        return (Run("—", Ink.MUTED),)
    if all(verdicts):
        return (Run("✓ intact" if kind == "audio" else "✓ verified", Ink.ADDED),)
    return (Run("✗ FAILED", Ink.REMOVED),)


def _user_spelling_offer(
    mine: str | None,
    catalogue: str,
    backing: tuple[str, int] | None,
    *,
    standing: str | None,
) -> dict[str, object]:
    """What this line offers of the file's own spelling, and on what evidence.

    One place rather than two, because the window draws one aside and the reason
    is the only thing that differs — two computations here would be two
    sentences that could disagree about the same row.

    **The witness is asked first**, since a rule over letters that also fires is
    the weaker reason for the same offer: every title agreeing is evidence,
    and one edit is a resemblance. An empty reason and an empty spelling always
    travel together, so the window has one thing to test.

    The count the witness read is deliberately not carried here: the witness
    panel of this same dialog already states it, and one number stated in two
    places is the pair that drifts.

    **A line that has been answered offers the way back instead.** The aside
    changes state rather than disappearing: if ``Keep yours`` wrote the
    spelling into the field and left no trace that anything had been decided,
    the one gesture on this screen that overrides the catalogue would be the
    one gesture with no undo.

    ``kept`` is filled **only where withdrawing would bring this very offer
    back** — where what stands is exactly the spelling this line would offer of
    its own accord. A typed title is not that: withdrawing it restores the
    catalogue and loses words no file carries, so one click would destroy
    something unrecoverable and the button that does it must not look like the
    reversible one. Those are the album's gesture, which asks first and says how
    many.
    """
    blank: dict[str, object] = {"yours": "", "yours_reason": "", "yours_witness": "", "kept": ""}
    offer = blank
    if mine:
        if backing and a_whole_word_apart(mine, catalogue):
            offer = {
                "yours": mine,
                "yours_reason": "witness",
                "yours_witness": backing[0],
                "kept": "",
            }
        elif typo := one_word_apart(mine, catalogue):
            offer = {**blank, "yours": typo, "yours_reason": "typo"}
    if standing is None:
        return offer
    return {**blank, "kept": standing} if offer["yours"] == standing else blank


def _witness_backing_user_titles(
    verification: Mapping[str, object] | None,
) -> tuple[str, int] | None:
    """The witness that compared this album's titles and agreed with every one.

    Returns its name and how many titles it read, or ``None``.

    **Only total agreement answers**, because only total agreement is about a
    line. What is kept of a witness is a count: all of them agreeing says any
    given title is among those it agreed with, while all but one names no line
    at all and would let a mark be put on the one row the witness actually
    disagreed about. The narrow reading is the only sound one.

    Written by asking what an entry *is* rather than by naming iTunes: any
    witness this application grows will carry the same field, and a lookup by
    key would leave the new one silently unable to back a title, drawing
    nothing.

    It says nothing about *what* the witness called anything. No character of
    its answer reaches this function, the payload, a name or a tag.
    """
    for entry in (verification or {}).values():
        if not isinstance(entry, Mapping) or not entry.get("witness"):
            continue
        compared, agreeing = entry.get("titles_compared"), entry.get("titles_agreeing")
        if not isinstance(compared, int) or not isinstance(agreeing, int):
            continue
        if compared > 0 and compared == agreeing:
            return str(entry["witness"]), compared
    return None


def _witness_runs(verification: Mapping[str, object] | None) -> tuple[Run, ...]:
    if not verification:
        return ()
    summary = _witness_summary(dict(verification))
    names = summary.get("witnesses") or []
    if not names:
        return (Run("none agreed", Ink.MUTED),)
    return (Run(f"✓ {', '.join(str(name) for name in names)}", Ink.ADDED),)


def _folder_move(folder_path: str, entries: Sequence[Mapping[str, object]]) -> tuple[str, str]:
    for entry in entries:
        if entry["kind"] == "rename_folder":
            before = dict(entry["before_state"]).get("path", folder_path)
            after = dict(entry["after_state"]).get("path", folder_path)
            return Path(str(before)).name, Path(str(after)).name
    return Path(folder_path).name, Path(folder_path).name


def _track_runs(entries: Sequence[Mapping[str, object]]) -> tuple[Run, ...]:
    runs: list[Run] = []
    for entry in entries:
        if entry["kind"] != "rename_file":
            continue
        before = Path(str(dict(entry["before_state"]).get("path", ""))).name
        after = Path(str(dict(entry["after_state"]).get("path", ""))).name
        if runs:
            runs.append(Run("\n"))
        runs.extend(change(before, after))
    return tuple(runs)


def _tag_runs(entries: Sequence[Mapping[str, object]]) -> tuple[Run, ...]:
    """Summarize the tag fields that moved, once per album rather than per file.

    Every track of an album gets the same fields written, so listing them per
    file would say the same thing once per track. What differs between tracks
    is the title and the number, and those are already visible in the file
    names.
    """
    fields: dict[str, tuple[str, str]] = {}
    for entry in entries:
        if entry["kind"] != "write_tags":
            continue
        old = dict(entry["before_state"]).get("tags") or {}
        new = dict(entry["after_state"]).get("tags") or {}
        for name in WRITTEN_FIELDS:
            if name in ("title", "tracknumber", "artist"):
                continue
            was, now = _tag_value(old.get(name)), _tag_value(new.get(name))
            if was != now:
                fields.setdefault(name, (was, now))
    runs: list[Run] = []
    for name, (was, now) in sorted(fields.items()):
        if runs:
            runs.append(Run("\n"))
        runs.append(Run(f"{name}: ", Ink.MUTED))
        runs.extend(change(was or "(none)", now or "(none)"))
    return tuple(runs)


def _artwork_runs(entries: Sequence[Mapping[str, object]]) -> tuple[Run, ...]:
    covers = sum(1 for entry in entries if entry["kind"] == "write_image")
    embeds = [entry for entry in entries if entry["kind"] == "embed_image"]
    # A repair is not an embedded cover: the picture that goes back in is the one
    # that was already there, and the History has to be able to say so — it is
    # the record read to decide whether a run is worth reverting.
    repaired = sum(1 for entry in embeds if _after_state(entry).get("repair"))
    drawn = sum(1 for entry in embeds if _after_state(entry).get("icon"))
    parts = []
    if covers:
        parts.append(f"{covers} in folder")
    if len(embeds) - repaired - drawn:
        parts.append(f"{len(embeds) - repaired - drawn} embedded")
    if repaired:
        parts.append(f"{repaired} relabelled")
    if drawn:
        parts.append(f"{drawn} drawn in the Finder")
    return (Run(", ".join(parts), Ink.ADDED),) if parts else ()


def _after_state(entry: Mapping[str, object]) -> Mapping[str, object]:
    """Return an operation record's after-state, whichever name it arrived under."""
    state = entry.get("after_state", entry.get("after"))
    return state if isinstance(state, Mapping) else {}


def _captured(pattern: re.Pattern[str], name: str) -> str:
    match = pattern.search(name)
    return match.group(1) if match else ""


def _tag_value(values: object) -> str:
    if not isinstance(values, list | tuple):
        return ""
    return ", ".join(str(value) for value in values)


def _settled(path: Path, later: Sequence[ChangeOperation]) -> Path:
    """Return where a path ends up once the operations after it have run."""
    current = path
    for operation in later:
        if operation.kind not in (OperationKind.RENAME_FILE, OperationKind.RENAME_FOLDER):
            continue
        before = Path(str(operation.before_state.get("path", operation.target_path)))
        after = Path(str(operation.after_state["path"]))
        if current == before:
            current = after
        elif before in current.parents:
            current = after / current.relative_to(before)
    return current


def _settled_back(path: Path, undone: Sequence[AppliedOperation]) -> Path:
    """Return where a path ends up once a reversal has undone these operations.

    ``undone`` arrives most-recent-first, which is the order a reversal runs in,
    so walking it forwards here walks the renames backwards.
    """
    current = path
    for entry in undone:
        operation = entry.operation
        if operation.kind not in (OperationKind.RENAME_FILE, OperationKind.RENAME_FOLDER):
            continue
        after = Path(str(operation.after_state["path"]))
        before = Path(str(operation.before_state.get("path", operation.target_path)))
        if current == after:
            current = before
        elif after in current.parents:
            current = before / current.relative_to(after)
    return current


def _audio_weighed_in(folder: Path) -> tuple[int, ...]:
    """What the audio sitting directly in one folder weighs, sorted.

    A size comes back with the directory listing, so this reads no audio at
    all. A folder that will not answer weighs nothing, which is the same
    answer as a folder with no audio in it: neither is offered as a candidate,
    and the album says it is misplaced rather than being adopted by a guess.
    """
    try:
        with os.scandir(folder) as listing:
            return tuple(
                sorted(
                    item.stat().st_size
                    for item in listing
                    if item.is_file() and Path(item.name).suffix.lower() in AUDIO_EXTENSIONS
                )
            )
    except OSError:
        return ()


def _audio_weighed_a_level_down(folder: Path) -> tuple[int, ...]:
    """The same question of a folder whose audio is in its children.

    A two-disc album is a folder holding `CD1` and `CD2` and no audio of its
    own, so weighing only what sits directly in it answers nothing and the
    album could never be recognized once it was renamed. One level, and only
    when the folder itself is empty of audio: the album that keeps its files
    directly in its folder costs nothing more.
    """
    weighed: list[int] = []
    try:
        with os.scandir(folder) as listing:
            children = [Path(item.path) for item in listing if item.is_dir()]
    except OSError:
        return ()
    for child in children:
        weighed.extend(_audio_weighed_in(child))
    return tuple(sorted(weighed))


def _audio_the_reversal_left(undone: Sequence[AppliedOperation]) -> tuple[Path, ...]:
    """Name every audio file this reversal touched, where the reversal left it.

    The trail is the only truthful account of where things went, in either
    direction, and it is the account that survives a restart — so this asks it
    rather than the copy of the album in memory, which a reverted plan may not
    have.
    """
    landed: dict[Path, None] = {}
    for entry in undone:
        target = entry.operation.target_path
        if target.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        landed.setdefault(_settled_back(target, undone), None)
    return tuple(landed)


def _final_paths(plan: ChangePlan) -> dict[Path, Path]:
    """Replay the plan's renames to learn where every path ends up.

    The proof taken before an apply must be checked at the path the file holds
    after it, and the plan itself is the only truthful account of that move —
    folder renames shift prefixes and file renames shift names, in order.
    """
    mapping: dict[Path, Path] = (
        {file.path: file.path for file in plan.unit.audio_files} if plan.unit else {}
    )
    for operation in plan.operations:
        if operation.kind not in (OperationKind.RENAME_FILE, OperationKind.RENAME_FOLDER):
            continue
        before = Path(str(operation.before_state.get("path", operation.target_path)))
        after = Path(str(operation.after_state["path"]))
        for original, current in mapping.items():
            if current == before:
                mapping[original] = after
            elif before in current.parents:
                mapping[original] = after / current.relative_to(before)
    return mapping


def _evidence_rows(
    outcome: IdentificationOutcome,
    marked: frozenset[str] = frozenset(),
    measured: frozenset[str] = frozenset(),
    by_hand: Mapping[str, int] | None = None,
) -> list[dict[str, object]]:
    """Lay the pairing out for human eyes: each file beside the track it is.

    A person trusts what they can see. Each row carries both durations and how
    far apart they are, so the review reads as evidence rather than as a score
    to take on faith.
    """
    alignment = outcome.alignment
    if alignment is None:
        return []
    rows: list[dict[str, object]] = []
    for assignment in alignment.assignments:
        file = assignment.file
        rows.append(
            {
                "file": file.path.name,
                "file_ms": file.properties.duration_ms if file.properties else None,
                "track": _written_title(assignment.track, outcome),
                "track_ms": assignment.track.duration_ms,
                "position": assignment.track_number,
                # The release position, which is how a correction is keyed —
                # the printed number is not, on a multi-disc release.
                "track_position": assignment.track.position,
                "disc": assignment.disc_number,
                "delta_ms": assignment.duration_delta_ms,
                # A pairing the *name* settled may still disagree with the
                # published length, and that gap is the one thing on the row
                # that says the file is not what its own name claims: a file
                # named as the instrumental pairs with the instrumental track
                # and runs longer because the audio in it is the vocal take.
                # Said here rather than left as a number among numbers. The
                # threshold is the matcher's own, so the screen cannot drift
                # from what the pairing was judged against.
                "length_disagrees": (
                    assignment.duration_delta_ms is not None
                    and assignment.duration_delta_ms > DURATION_TOLERANCE_MS
                ),
                "status": "paired",
                # Offered rather than settled: this file fitted more than one
                # track and nothing could prove which, so the row is marked and
                # nothing is written from it until it is confirmed. It is the
                # difference between a pairing the album is *waiting on* and
                # one it is sure of.
                "suggested": assignment.suggested,
                # What the audio was proved to be, carried to the row that
                # shows the name it would be given: the label lands on this
                # file, so the disagreement belongs here too.
                "signature": file.content_signature,
                "marked_fake": file.content_signature in marked,
                # What the audio measured, before any override. The two differ
                # exactly where an override stands, and that is what lets a
                # second click withdraw rather than contradict.
                "measured_fake": file.content_signature in measured,
                # Whether this pair was made by hand rather than by the
                # matcher. The row says so, and offers to take it back — a
                # pairing that can be made and cannot be seen cannot be undone.
                # A pairing by hand names one position; a second file of the
                # same audio, paired by the ladder elsewhere, is not by hand.
                "by_hand": (by_hand or {}).get(file.content_signature) == assignment.track.position,
                # What holds this pair together: `hand`, `name`, or `length`
                # alone. Rows held only by a length within tolerance can all be
                # wrong together — an album matched against a compilation none
                # of its tracks is on — so they must not draw like rows a name
                # confirmed.
                "paired_by": assignment.paired_by,
            }
        )
    # A failed pairing arrives as two halves: `no file here` at the track's
    # position, and the file itself at the bottom as `left alone`. Both are
    # true, and ten rows apart they read as though a file plainly in the
    # folder had been lost. So a track with no file carries the files nothing
    # claimed, nearest first.
    #
    # Offered, not asserted. The pairing failed precisely because nothing cleared
    # the bar, so a "likeliest" drawn as a conclusion would be a guess made
    # where the matcher had just refused to make one, and a floor low enough to
    # claim a weak resemblance would also claim pure noise.
    credit = album_credit(outcome.candidate.release) if outcome.candidate else ""
    # The shortlist is scored by the matcher's own comparison, so it has to be
    # told the same thing the matcher was told: on a release that publishes a
    # song and its instrumental, the decoration is the difference. Both
    # album-wide facts, not just the contested stems: a screen that scores a
    # title differently from the pairing it is describing states one condition
    # twice, which is how the two come apart.
    contested = (
        album_titles(outcome.candidate.release.tracks) if outcome.candidate else NO_ALBUM_TITLES
    )
    unclaimed = alignment.unmatched_files
    for track in alignment.unmatched_tracks:
        rows.append(
            {
                "file": None,
                "file_ms": None,
                "track": _written_title(track, outcome),
                "track_ms": track.duration_ms,
                "position": track.position,
                "track_position": track.position,
                "delta_ms": None,
                "status": "unmatched_track",
                "candidates": _unclaimed_for(track, unclaimed, credit, contested),
            }
        )
    for file in alignment.unmatched_files:
        rows.append(
            {
                "file": file.path.name,
                "file_ms": file.properties.duration_ms if file.properties else None,
                "track": None,
                "track_ms": None,
                "position": None,
                "delta_ms": None,
                "status": "unmatched_file",
                # Carried so the window can hand it back when this file is
                # paired by hand: the pairing is keyed by the audio, never by
                # the name on screen, which the very next apply rewrites.
                "signature": file.content_signature,
                # Whether this file is reachable from a track's own row. When it
                # is, the window need not say it twice; when no track is missing
                # at all, this row is the only place it can be said, because
                # there is genuinely no track for it to belong to.
                "offered_above": bool(alignment.unmatched_tracks),
            }
        )
    rows.sort(key=_evidence_order)
    return rows


def _unclaimed_for(
    track: TrackMetadata,
    files: Sequence[object],
    credit: str = "",
    contested: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """The files nothing claimed, ordered by how much each resembles this track.

    The order is the matcher's own reading — the same credit-aware comparison
    that decides a pairing — so the file at the top of the list is the file the
    matcher came closest to choosing, and the numbers beside it are why it did
    not. No floor and no verdict: this is a shortlist to look at, and on a
    release where nothing resembles anything it is simply a list of what is loose.
    """
    scored: list[dict[str, object]] = []
    for file in files:
        properties = getattr(file, "properties", None)
        file_ms = properties.duration_ms if properties else None
        title = file.path.stem  # type: ignore[attr-defined]
        scored.append(
            {
                "file": file.path.name,  # type: ignore[attr-defined]
                "signature": file.content_signature,  # type: ignore[attr-defined]
                "file_ms": file_ms,
                # Read off the file's *name*, not its tag: the tag is what the
                # matcher already tried and failed with, and the name is the
                # other thing a person compares by eye.
                "similarity": round(track_title_similarity(title, track, credit, contested), 3),
                "delta_ms": (
                    abs(file_ms - track.duration_ms)
                    if file_ms is not None and track.duration_ms is not None
                    else None
                ),
            }
        )
    scored.sort(key=lambda row: (-float(row["similarity"] or 0), str(row["file"])))
    return scored


def _evidence_order(row: Mapping[str, object]) -> tuple[int, tuple[int, ...], str]:
    """Read the evidence the way a person reads a tracklist: in release order.

    In the order the aligner paired them, with the release's unfiled tracks
    appended last, track 10 can sit above track 01 and neither list can be
    followed. A file the release has no track for has no position to sort by,
    so it closes the list, under its own name.
    """
    if row.get("track") is None:
        return (1, (), str(row.get("file") or "").casefold())
    numbers = tuple(int(part) for part in re.findall(r"\d+", str(row.get("position") or "")))
    disc = row.get("disc")
    if disc is not None:
        # A paired row knows its disc outright; a release-only row may carry it
        # inside its position ("2-5") or not at all ("5", "A4"), in which case
        # disc one is assumed — otherwise the two kinds cannot interleave and
        # a file paired to track 7 sorts around the unfiled tracks 1 to 6.
        key = (int(disc), *(numbers or (0,)))
    elif len(numbers) >= 2:
        key = numbers
    else:
        key = (1, *(numbers or (0,)))
    return (0, key, str(row.get("track") or "").casefold())


def _proof_tier(outcome: IdentificationOutcome) -> str:
    """Name what proved this identification, because "93%" names nothing.

    The tier is what the batch is grouped by and what the badge says — audio
    (durations from the files themselves), titles, or text alone.
    """
    if outcome.candidate is None:
        return "none"
    # An arrangement proves nothing and says so. Any other answer here would be
    # a file agreeing with itself and drawn as proof.
    if str(outcome.candidate.release.source) == str(MetadataSources.TAGS):
        return "none"
    signals = outcome.candidate.signals
    if signals.durations_compared > 0 and signals.durations_agreeing > 0:
        return "audio"
    if signals.titles_agreeing > 0:
        return "titles"
    return "text"


def _as_search_terms(hints: SearchHints) -> ReleaseMetadata | None:
    """Carry an album's own words to the witness when no catalogue named it.

    Not a release and never treated as one: nothing is identified by this, no
    value from it is written, and it exists only so the search this
    application would otherwise be unable to make can be made at all. The
    witness reads the artist and the title and answers with verdicts, as always.
    """
    if not hints.album:
        return None
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="",
        title=hints.album,
        artists=(ArtistMetadata(name=hints.artist),) if hints.artist else (),
    )


def _source_positions(
    outcome: IdentificationOutcome, asking: bool = False
) -> list[dict[str, object]]:
    """Say where every catalogue consulted stands, in the same words.

    Three lines, always: the one in use, the one that lost, and the witness.
    The number is about *names*: the source in use carries a link and the word
    `using this`, the loser carries its own agreement so a wrong choice is
    visible without opening a search, and iTunes carries the only number it is
    allowed to leave behind.

    The witness contributes counts and a page address and nothing else: its
    titles were compared inside the verifier and dropped there.
    """
    verification = outcome.verification or {}
    recorded = verification.get("sources")
    answers = {
        str(entry.get("source", "")): entry
        for entry in (recorded if isinstance(recorded, list) else [])
        if isinstance(entry, dict)
    }
    # The source in use answers for itself, from the candidate rather than from
    # the record. A restored album may carry no testimony, and the panel would
    # then say "not asked" about the very catalogue named one line above it.
    # The candidate is right there and knows all of it, so this cannot disagree
    # with the header.
    candidate = outcome.candidate
    if candidate is not None:
        answers.setdefault(
            str(candidate.release.source),
            {
                "source": str(candidate.release.source),
                "release_id": candidate.release.source_release_id,
                "titles_agreeing": candidate.signals.titles_agreeing,
                "titles_compared": candidate.signals.titles_compared,
                "tracks": len(candidate.release.tracks),
                "winner": True,
            },
        )
    positions: list[dict[str, object]] = []
    # An arrangement shows no source panel at all. The panel is identification
    # apparatus — where each catalogue stands about a claim — and an
    # arrangement makes no claim for them to stand about: drawing it would put
    # `not asked — the first answer settled it` under a header that had asked
    # nothing.
    if candidate is not None and str(candidate.release.source) == str(MetadataSources.TAGS):
        return positions
    for source in _RELEASE_LINKS:
        entry = answers.get(str(source))
        if entry is None:
            # Not asked, and that is a fact rather than a gap: the second
            # catalogue is consulted only when the first fails to settle the
            # album, which is deliberate — it is what keeps a clean library from
            # spending a request per album on a question already answered.
            positions.append({"source": str(source), "asked": False})
            continue
        release_id = str(entry.get("release_id", ""))
        positions.append(
            {
                "source": str(source),
                "asked": True,
                "in_use": bool(entry.get("winner")),
                "titles_agreeing": entry.get("titles_agreeing"),
                "titles_compared": entry.get("titles_compared"),
                "tracks": entry.get("tracks"),
                "url": (
                    _RELEASE_LINKS[source].format(id=release_id)
                    if source in _RELEASE_LINKS and release_id
                    else None
                ),
            }
        )
    itunes = verification.get("itunes")
    if isinstance(itunes, dict) and itunes.get("found"):
        page = itunes.get("url")
        positions.append(
            {
                "source": "itunes",
                "asked": True,
                "in_use": False,
                "witness": True,
                "titles_agreeing": itunes.get("titles_agreeing"),
                "titles_compared": itunes.get("titles_compared"),
                "tracks": itunes.get("track_count"),
                "url": page if isinstance(page, str) else None,
            }
        )
    else:
        # Three states, each with its own sentence: the answer has not come
        # back yet; it came back and the witness knows this record not at all;
        # or it could not be reached. Only the last is a fault, and only the
        # first is temporary.
        positions.append(
            {
                "source": "itunes",
                "asked": isinstance(itunes, dict),
                "witness": True,
                "in_use": False,
                "asking": asking,
                "reached": itunes.get("reached", True) if isinstance(itunes, dict) else None,
            }
        )
    return positions


def _witness_summary(verification: dict[str, object] | None) -> dict[str, object]:
    """Compress the witnesses' verdicts into what a badge can say."""
    if not verification:
        return {"count": 0}
    agreed = 0
    detail: list[str] = []
    cross = verification.get("cross_source")
    if isinstance(cross, dict) and cross.get("title_agrees"):
        agreed += 1
        detail.append(str(cross.get("witness", "cross-source")))
    itunes = verification.get("itunes")
    pages: dict[str, str] = {}
    if isinstance(itunes, dict) and (
        itunes.get("durations_agree") or itunes.get("track_count_agrees")
    ):
        agreed += 1
        detail.append("itunes")
        # Where the witness read what it read. A verdict that cannot be looked
        # up has to be taken on faith, so a witness that contributes to a
        # decision is attributed.
        page = itunes.get("url")
        if isinstance(page, str):
            pages["itunes"] = page
    return {"count": agreed, "witnesses": detail, "pages": pages}


def _witness_knows_better(verification: dict[str, object] | None) -> dict[str, object] | None:
    """Say when the witness recognised a record the one in use does not match.

    The case: one catalogue is unreachable, the other offers only editions of
    the *album*, and the witness finds the EP with every title and every
    duration agreeing — against one title for the candidate the run was
    comparing. Every number is already on the screen, in two different folds,
    and the sentence that decides is in neither.

    **It stays a reading and never becomes a value.** Nothing here is written,
    adopted or persisted: it reports that the witness is looking at something
    else and how much better it fits, so that it can be looked at. The link is
    the witness's own page, which this screen already draws.

    Only where the comparison is honest: both sides must have compared titles,
    and the witness must agree on more of them than the candidate did.
    """
    if not verification:
        return None
    itunes = verification.get("itunes")
    if not isinstance(itunes, dict) or not itunes.get("found"):
        return None
    sources = verification.get("sources")
    winner = next(
        (
            entry
            for entry in (sources if isinstance(sources, list) else [])
            if isinstance(entry, dict) and entry.get("winner")
        ),
        None,
    )
    if winner is None:
        return None
    witness_of = int(itunes.get("titles_compared") or 0)
    winner_of = int(winner.get("titles_compared") or 0)
    if not witness_of or not winner_of:
        return None
    witness_agreeing = int(itunes.get("titles_agreeing") or 0)
    winner_agreeing = int(winner.get("titles_agreeing") or 0)
    # Rates, not counts: the candidate is compared against its own track list and
    # the witness against its own, and those lists are different lengths — which
    # is the whole point when the record in use has ten tracks and the folder
    # has four.
    if witness_agreeing / witness_of <= winner_agreeing / winner_of:
        return None
    answer: dict[str, object] = {
        "witness": "itunes",
        "titles_agreeing": witness_agreeing,
        "titles_compared": witness_of,
        "in_use_agreeing": winner_agreeing,
        "in_use_compared": winner_of,
    }
    if itunes.get("durations_agree"):
        answer["durations_agreeing"] = int(itunes.get("durations_agreeing") or 0)
        answer["durations_compared"] = int(itunes.get("durations_compared") or 0)
    page = itunes.get("url")
    if isinstance(page, str):
        answer["url"] = page
    return answer


def _stages_a_folder_cover(plan: ChangePlan | None) -> bool:
    """Report whether an identification's plan already writes a folder cover."""
    if plan is None:
        return False
    return any(operation.kind is OperationKind.WRITE_IMAGE for operation in plan.operations)


_OLD_QUALITY_LABEL = re.compile(
    r"(?<=[\[,])(\s*)(?:ALAC|AIFF|FLAC|M4A|MP3|WAV)\s+Fake(?=[\],])",
    re.IGNORECASE,
)
"""This application's own transcode token, in the form it was once written.

Anchored on the brackets it lives in and on the codec it followed, because the
word alone is a word an album title can contain, and a sweep that matched it
would rename that album. The lookarounds keep `[FLAC, FLAC Fake]` working
as one list — the comma is a boundary on both sides.
"""


def _renamed_without_the_old_label(name: str, label: str = DEFAULT_TRANSCODED_LABEL) -> str | None:
    """Return ``name`` with the old token rewritten, or ``None`` when it has none."""
    rewritten = _OLD_QUALITY_LABEL.sub(lambda match: f"{match.group(1)}{label}", name)
    return rewritten if rewritten != name else None


def _old_labelled(root: Path) -> list[tuple[Path, str]]:
    """Return every path under ``root`` whose name still carries the old token.

    Deepest first, so a file inside a folder that is also being renamed is
    listed — and therefore renamed — before the folder moves under it.
    """
    found: list[tuple[Path, str]] = []
    for parent, folders, files in os.walk(root):
        here = Path(parent)
        for name in (*folders, *files):
            renamed = _renamed_without_the_old_label(name)
            if renamed is not None:
                found.append((here / name, renamed))
    found.sort(key=lambda entry: -len(entry[0].parts))
    return found


_FIELDS_ANSWERING_TO_A_RELEASE = ("track_file", "extra_track")
"""The corrections whose meaning depends on which release is identified.

Named once because every path that carries corrections across a release change
has to carry all of them. A path that moved pairings and not added tracks would
leave the second kind answering to a release nobody is looking at, read by
nothing and reported by nothing. A third such field added later belongs here,
not in another list.

`folder_name` and `track_title` are deliberately absent: a folder's name and a
song's spelling are words about the album itself, and they mean the same thing
whichever pressing is on screen.
"""


def _release_key(release: ReleaseMetadata | None) -> str | None:
    """Name the release a pairing was made against: the source and its id.

    Two pressings of one album are two releases, and a pairing that survived
    from one to the other would be answering about a tracklist nobody is looking
    at.
    """
    if release is None:
        return None
    return f"{release.source}:{release.source_release_id}"


def _shown(release: ReleaseMetadata | None) -> ReleaseMetadata | None:
    """A release's title and credit as the screens draw them, in the written case.

    One helper for every screen that names an album and none of its tracks,
    because a shelf reading `Band Of The City` over a folder named `Band of the
    City` is one fact in two forms, and the Library, the queue and Mixing
    all draw it. Its tracks stay as published: none of those reads them.
    """
    return normalize_album_names(release) if release is not None else None


def _written_title(track: TrackMetadata, outcome: IdentificationOutcome) -> str:
    """A paired track's title in the case its file will be named in.

    A title that is not the catalogue's for its position is the user's
    correction, laid over the release before it was paired, and is shown as
    they wrote it.
    """
    if outcome.candidate is None:
        return track.title
    release = outcome.candidate.release
    published = {entry.position: entry.title for entry in release.tracks}
    if published.get(track.position) != track.title:
        return track.title
    return normalize_track(track, release).title


def _written_credit(track: TrackMetadata) -> str:
    """The credit as this track's file name would write it.

    What a correction is compared against, so that resubmitting the form
    unchanged records nothing. The joining rule lives in
    `application/contracts`, the vocabulary both layers speak, because
    `library/` may not import `application/` and the planner needs the same
    rule.
    """
    if track.artist_credit and track.artist_credit.strip():
        return track.artist_credit
    return written_credit(track.artists)


def _source_url(release: object) -> str | None:
    """Build the public page of a release, so the user can verify the match."""
    if release is None:
        return None
    template = _RELEASE_LINKS.get(getattr(release, "source", None))
    identifier = getattr(release, "source_release_id", None)
    if template is None or not identifier:
        return None
    return template.format(id=identifier)


WITNESS_HOSTS = frozenset({"music.apple.com", "itunes.apple.com", "open.spotify.com"})
"""The pages a witness publishes, which this application may open for checking.

The gate exists because this is a verification gesture and not a browser, and a
witness page is exactly a verification gesture: a witness contributing to a
decision is attributed with a link, and a link that refuses to open is not
attribution. Matched on the host exactly — `music.apple.com.evil` must not pass
by ending up inside a substring test.
"""


HELP_PAGES = frozenset(
    {
        "https://www.discogs.com/settings/developers",
        "https://acoustid.org/new-application",
        "https://developer.spotify.com/dashboard",
    }
)
"""Where the welcome sends someone to create a key, matched exactly."""

CATALOGUE_HOSTS = frozenset(
    {"discogs.com", "www.discogs.com", "musicbrainz.org", "www.musicbrainz.org"}
)
"""The catalogues' own sites, matched on the host and nothing else."""

_RECORDING_PAGE = re.compile(r"^/recording/[0-9a-fA-F-]{36}/?$")
"""A MusicBrainz *recording* page, which the release patterns do not describe.

The Quality bench's `heard: named` chip links here — it shows which recording
the audio was identified as — so a check that asked only about releases and
masters would refuse the one link on that screen. Anchored end to end, because
this is permission and not parsing."""


def _is_catalogue_page(url: str) -> bool:
    """Report whether this is a catalogue's own release page, on https.

    ``_parse_release_link`` is a *parser*: it runs ``pattern.search`` over the
    string, unanchored, with no scheme and no host. Used as permission it would
    let ``https://elsewhere.example/discogs.com/release/1`` be handed to the
    system's ``open`` — and ``some-scheme://discogs.com/release/1`` too, which
    goes to whatever registered that scheme.

    It matters because the URL is not always this application's own: the iTunes
    witness's ``collectionViewUrl`` comes straight out of a remote answer and is
    rendered as a link with this application's framing behind it.

    Asked the way ``_is_witness_page`` asks, and asked first.
    ``_parse_release_link`` stays loose, because for a pasted link it feeds an
    API request rather than a browser.
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.hostname not in CATALOGUE_HOSTS:
        return False
    return (
        _parse_release_link(url) is not None
        or _parse_group_link(url) is not None
        or _RECORDING_PAGE.match(parsed.path) is not None
    )


def _is_witness_page(url: str) -> bool:
    """Report whether this is a witness's own page, on https and nothing else."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in WITNESS_HOSTS


def _a_folder_on_this_disk(value: object) -> Path | None:
    """Read a recorded string as a folder here, or refuse it.

    Every path this application receives from outside itself comes through
    here: a folder slskd says it downloads into, a landing this application
    wrote down, a name a stranger published. One place, so that there is one
    idea of what is suspicious.

    Two refusals, and without either one a Finder window opens on the directory
    this process happens to be running in:

    - **empty** — `str(Path(""))` is `"."`, so a value that has not been written
      down yet becomes *here* rather than nothing;
    - **relative** — slskd answers with the folder *it* writes to, which may be
      relative to wherever slskd is running, and this process is not that.

    And one question of fact: the folder has to be on this disk. A path inside a
    container is a truthful answer to a question about another machine.
    """
    recorded = str(value or "").strip()
    if not recorded:
        return None
    path = Path(recorded).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None
    return path


def _inside(path: Path, folder: Path) -> bool:
    """Whether a folder is that folder or lives under it, spelling and all.

    Compared through `resolve`, because the collect's landing is built from a
    stranger's folder name and the album's path is read off the disk — and on
    APFS the same folder answers to two spellings of an accent.
    """
    try:
        here, there = path.resolve(), folder.resolve()
    except OSError:
        return False
    return here == there or there in here.parents


def _opener_that_says_so(opener: PathOpener | None, logger: logging.Logger) -> PathOpener | None:
    """Wrap the platform's opener so every Finder window it opens is on record.

    Many gestures reach the Finder, and a window that opens at the wrong folder
    shows no error: something opened *successfully* with a path nobody could
    see, and nothing says which gesture did it.

    One wrapper rather than a log line per caller, because the next caller
    would be the one that forgets.
    """
    if opener is None:
        return None

    def opened(path: str) -> None:
        logger.info(
            "Opening a path outside the window.",
            extra={"operation": "api.open.path", "path": path},
        )
        opener(path)

    return opened


def _shelf_summary(row: Mapping[str, object], origin_of: Callable[[int], str]) -> dict[str, object]:
    """Describe one album from the database's columns alone.

    Every field here is either read from a column or is the same default the
    real `_AlbumState` carries, so a card drawn from this one and the same card
    drawn after the album is read back say the same things — with one exception,
    said out loud: **the pairing is not measured yet**, so `match_paired` and
    `match_total` are absent rather than guessed. The window draws that badge
    only for an album that is not organized, and an album that is not organized
    is one the restore reads back first.

    `looked_at` is whether a catalogue ever answered for this album, which is
    exactly whether the row found a release to join.
    """
    named = bool(row.get("title"))
    return {
        "unit_id": int(row["unit_id"]),
        "folder": Path(str(row["folder_path"])).name,
        "folder_path": str(row["folder_path"]),
        "tracks": row.get("tracks"),
        "title": row.get("title"),
        "artist": row.get("artist"),
        "year": row.get("year"),
        "source": row.get("source"),
        "confidence": row.get("confidence") or 0.0,
        "organized": row.get("state") == "organized",
        "folder_missing": not Path(str(row["folder_path"])).is_dir(),
        "decision": "review",
        "looked_at": named,
        # **And the other half of the pair, or this card changes its badge on
        # being read back.** The badge that says `not scanned` over an album
        # arranged from its own tags asks this before saying it, so a card
        # drawn from columns that did not carry it would answer `undefined` —
        # falsy, and therefore the sentence this field exists to stop.
        "catalogue_asked": bool(row.get("catalogue_asked")),
        "applied": False,
        "written": False,
        "planned_again": False,
        # A row read back from the database is an album no gesture of this
        # session has touched, so an organized one is asleep by definition.
        # Written out rather than left to the reader: this payload never
        # passes through `_asleep`, and a card that answered `undefined` here
        # would be a finished album offering both its doors wide open.
        "asleep": row.get("state") == "organized",
        "rejected": row.get("state") == "skipped",
        "held": False,
        "applicable": False,
        "match_paired": None,
        "match_total": None,
        "match_percent": None,
        "match_files_unpaired": None,
        "match_tracks_unpaired": None,
        # A row read back from the database carries no candidate to ask.
        "names_agreeing": None,
        "names_compared": None,
        "cover_pending": False,
        "url": None,
        "proof": None,
        "witnesses": (),
        "positions": {},
        "audio_ok": None,
        "writes_ok": None,
        # Asked, never read off the row: `album_units.origin` is the album's
        # whole life and the chip is about this session. Handed in rather than
        # reached for, because this function is module-level and the answer
        # belongs to the running window.
        "origin": origin_of(int(row["unit_id"])),
        # Read straight off the column, because it is the one fact on this row
        # that no source supplied and no restore has to recompute.
        "rating": row.get("rating"),
        # The one thing on this row that is about the *shelf* rather than the
        # album: it has not been read back off the disk yet. Nothing draws it
        # today; it is here so that a screen which wants to say so can, without
        # inferring it from a missing field.
        "settled": False,
    }


def _parse_release_link(url: str) -> tuple[MetadataSourceId, str] | None:
    """Read a Discogs or MusicBrainz release out of a pasted URL."""
    for source, pattern in _LINK_PATTERNS:
        match = pattern.search(url.strip())
        if match is not None:
            return source, match.group(1)
    return None


def _parse_group_link(url: str) -> tuple[MetadataSourceId, str] | None:
    """Read an album-level link — a Discogs master — out of a pasted URL.

    This is the link a browser hands you: searching Discogs and clicking the
    album lands on its master, not on any one pressing.
    """
    for source, pattern in _MASTER_PATTERNS:
        match = pattern.search(url.strip())
        if match is not None:
            return source, match.group(1)
    return None


def _operation(operation: ChangeOperation) -> dict[str, object]:
    return {
        "sequence": operation.sequence,
        "kind": str(operation.kind),
        "target": str(operation.target_path),
        "after": dict(operation.after_state),
        "before": dict(operation.before_state),
    }


def _not_looked_at(unit: AlbumUnit) -> IdentificationOutcome:
    """Describe an album that is on screen but has not been identified yet.

    It has no candidate and no plan, which is what keeps it out of every batch
    gesture until it is asked for — the same protection an album organized by
    an earlier run has, for the opposite reason.
    """
    return IdentificationOutcome(
        unit=unit,
        hints=SearchHints(),
        decision=Decision.REVIEW,
        reason="This album has not been looked up yet. Mark it and press Scan.",
    )


def _already_organized(unit: AlbumUnit) -> IdentificationOutcome:
    """Describe an album organized by an earlier run, which this scan leaves alone.

    It is not identified again, so it has no candidate and no plan — which is
    also what keeps it out of every batch gesture. The decision is
    ``REVIEW`` rather than a state of its own: the window groups it by the flag
    on the album, and nothing that acts on many albums may mistake it for an
    answer this scan produced.
    """
    return IdentificationOutcome(
        unit=unit,
        hints=SearchHints(),
        decision=Decision.REVIEW,
        reason=(
            "This album was organized by an earlier run, so this scan left it "
            "exactly as it is. Plan it again to look at it."
        ),
    )


def _a_catalogue_answered(outcome: IdentificationOutcome) -> bool:
    """Whether any source outside this machine has spoken about this album.

    This is what `looked_at` means, as opposed to *"there is something to
    show"*: an album dropped on the window comes back arranged from its own
    tags, which is a complete plan and not a look-up. Left at its default
    `True`, the shelf's card would say `not scanned` — it reads the database,
    where no identification exists — and the dialog for the same album would
    say the opposite.

    The album's own files are a source in every place downstream, on purpose,
    and they are the one source that is not a catalogue. That is the whole of
    the distinction: `tags` means nobody was asked.
    """
    candidate = outcome.candidate
    return candidate is not None and candidate.release.source != MetadataSources.TAGS


def _normalized(path: Path) -> str:
    """One spelling for one path, so two of them can be compared as text.

    macOS hands back a decomposed name from a directory listing and a composed
    one from a folder dragged onto the window; APFS opens either, and Python
    compares them as different strings. `_both_spellings` answers the same
    problem for the database, which must keep what the file system wrote — here
    nothing is stored, so one spelling is enough.
    """
    return unicodedata.normalize("NFC", str(path))


def _plan_still_points_at_the_disk(plan: ChangePlan) -> bool:
    """Whether everything this plan names is still where it says it is.

    A cover-only plan can outlive the folder it was made for: the album's own
    plan renames the folder and every file in it, and a plan made before that
    names paths nothing answers to any more. A plan made afterwards names the
    paths as they now are and is perfectly good, however long ago the album was
    applied — which is why this asks the disk instead of reading how the album
    got here.

    A picture being written is allowed not to exist yet; that is the write. Its
    folder has to.
    """
    for operation in plan.operations:
        target = operation.target_path
        if operation.kind is OperationKind.WRITE_IMAGE:
            if not target.parent.is_dir():
                return False
        elif not target.exists():
            return False
    return True


def _asleep(state: _AlbumState) -> bool:
    """Whether this album is finished and nobody has asked about it since.

    **The one place this question is answered.** It decides whether an
    organized album's names can be typed into: an album restored from a restart
    draws its table and nothing has planned those names against the files since
    they were written, so an edit would be made against a reading of unknown
    age.

    Choosing a cover does not ask it: a cover names nothing, so the fresh
    reading this question guards is not something a picture needs. The window
    reads the answer off the payload rather than working it out again, so the
    rule is not kept in two languages.
    """
    return state.organized and not state.planned_again


def _holding_the_applied_plan(state: _AlbumState) -> bool:
    """Whether the plan this album carries is the one already written to disk.

    ``applied`` alone is the wrong question for an album organized by an
    earlier run: the restore marks those applied as well (they are organized,
    and that is one fact), so every gesture that re-plans a single album by
    hand would refuse every organized album, and `Plan this album again` would
    answer *"Nothing would change"* with no names on screen and no way to
    change one.

    What must never be corrected is a plan whose renames have already run: its
    rows name files that no longer carry those names, and a correction would be
    typed against a screen describing the past. **A plan with no operations
    describes nothing**, so there is nothing stale to correct — an organized
    album at rest carries no plan at all, and one planned again and found
    already right carries an empty one. Both are exactly the case this lets
    through, and it is the same condition the window reads to decide whether to
    draw the names.

    **It asks ``written``, and it has to.** ``applied`` says the album was
    applied at some point; ``written`` says the plan being held is the one that
    ran, which is the question the sentence above asks. The two part on an
    organized album re-planned into artwork writes, and the window's half of
    this pair reads the same word.

    The window asks one thing more than this — an organized album it has not
    been asked to plan again draws its names and does not open them — and the
    two are **not** the same question. This one refuses a correction typed
    against a plan that has already run. That one declines to invite typing at
    all until the album is planned again. Every edit the window offers still
    passes here; the reverse is deliberately not true, and narrowing this to
    match would refuse corrections on paths that never show a sleeping album.
    """
    return state.written and bool(state.outcome.plan is not None and state.outcome.plan.operations)


def _organized_only_while_nothing_changes(state: _AlbumState) -> None:
    """Keep the album's own word for as long as its plan has nothing to write.

    One place for the rule. Planning an album again is asking for a fresh
    answer about it, so the run that organized it is no longer what the screen
    is about — **unless the answer is that nothing would change**. An album
    planned again and found already right is still an organized album; saying
    otherwise would turn it from `Organized` into `Auto` for having answered
    the question perfectly, and take `Plan this album again` off its own menu.

    A correction reaches here too: the moment an edited name gives the album
    something to write, it is an album with a plan waiting for `Approve &
    apply`, and the shelf says so.

    **And it is given back when the reason goes.** When a typed name is
    replaced by the catalogue's again, the plan changes nothing again, so the
    album is again one that needs nothing. Otherwise the word would be gone for
    the rest of the session over an edit that no longer exists.
    """
    if state.words_it_had is None and (state.organized or state.applied):
        state.words_it_had = (state.organized, state.applied)
    if state.outcome.plan is not None and state.outcome.plan.operations:
        state.organized = False
        state.applied = False
    elif state.words_it_had is not None:
        state.organized, state.applied = state.words_it_had


def _planned_folder_name(unit: AlbumUnit, plan: ChangePlan | None) -> str:
    """Return the folder name this album would carry if its plan ran now.

    The plan's own answer, not a second rendering of the naming policy: the two
    could disagree, and the one that matters is the one that would be written.
    An album whose folder is already right has no rename to read, and its
    current name is the answer.

    Only the album's own folder counts. A multi-disc plan renames `CD1` and
    `CD2` first, so reading the first folder rename in the plan would answer
    `CD1`, and the name this album is really claiming would never be registered
    against the run's other albums.
    """
    for operation in plan.operations if plan is not None else ():
        if operation.kind is OperationKind.RENAME_FOLDER and operation.target_path == (
            unit.folder_path
        ):
            return Path(str(operation.after_state["path"])).name
    return unit.folder_path.name


def _data_uri(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{media};base64,{base64.b64encode(data).decode('ascii')}"


def _held_album(copy: "HeldCopy") -> dict[str, object]:
    """One side of a pair, in what the window needs to name it and reach it."""
    return {
        "album": copy.album,
        "path": str(copy.album_path) if copy.album_path else "",
        "signature": copy.album_signature,
        "unit_id": copy.unit_id,
    }


def _pair_order(pair: dict[str, object]) -> tuple[int, str]:
    """Most shared tracks first, then by name, so the biggest question is on top."""
    tracks = pair["tracks"]
    first = pair["first"]
    assert isinstance(tracks, list) and isinstance(first, dict)
    return (-len(tracks), str(first.get("album", "")))


def _is_arrangement(outcome: IdentificationOutcome) -> bool:
    """Whether this album was named from its own tags rather than identified.

    One question with one answer, asked in one place, so that no caller reaches
    through the candidate for itself and none forgets to ask.
    """
    candidate = outcome.candidate
    return candidate is not None and str(candidate.release.source) == str(MetadataSources.TAGS)


def _followed(was: Path, now: Path) -> dict[str, str] | None:
    """Say that an album was found somewhere else, or nothing when it had not moved.

    Every gesture that re-anchors an album says so. A move recorded only in
    the log is a silent move: the screen would claim nothing happened.
    """
    if was == now:
        return None
    return {"was": str(was), "now": str(now), "album": now.name}


def _is_on_disk(path: str) -> bool:
    """Whether a registered path still names a file, for the guards that ask.

    A row naming a file that is gone is history, not a rival: counting it would
    make the library call one file two and withhold from a track the bitrate
    measured on that very track. Asked of the disk rather than settled by
    deleting those rows, because nothing is deleted and the answer corrects
    itself the moment a file comes back.

    The check is cheap, and the guards ask about one album's worth of paths at
    a time.

    **The limit:** a library on a volume that is not mounted answers `False`
    here, and the guard then withholds nothing where it might have. That is
    the loose direction, and it is why this asks about a *file* rather than
    trusting a row.
    """
    return Path(path).exists()


def _match_counts(outcome: IdentificationOutcome) -> tuple[int, int, int, int]:
    """Return how many tracks are settled, how many there are to settle, and what is open.

    Counted both ways on purpose: a file with no track and a track with no file
    are each a piece of this album still open, and an album is only whole when
    neither exists. The two open halves come back on their own as well, because
    the sum hides which it is: *10 of 13* reads as thirteen files over a folder
    holding twelve, when the thirteenth is the one file with no track added to
    twelve tracks.
    """
    alignment = outcome.alignment
    if alignment is None:
        return 0, 0, 0, 0
    paired = len(alignment.assignments)
    files_unpaired = len(alignment.unmatched_files)
    tracks_unpaired = len(alignment.unmatched_tracks)
    return paired, paired + files_unpaired + tracks_unpaired, files_unpaired, tracks_unpaired


def _what_stands_about(
    quality: StoredQuality | None, said: str | None
) -> tuple[bool, str | None, str]:
    """Return the conclusion, the encoding and the reason this track should show.

    **An override replaces the conclusion and never the numbers** — the same
    sentence `_with_user_word` is built on, and the same rule, applied one row
    lower. The album header above these rows yields to an override, so this
    table has to as well: otherwise a track marked `Not lossy` goes on reading
    `Was lossy`, and a word that failed to save cannot be told from a word
    that saved and was ignored.

    The fall, the ceiling, the wall and the bitrate are untouched, on screen
    beside the answer, because a disagreement that erases its own evidence
    cannot be audited or withdrawn — and `Yours` still shows which word is the
    override, so the two columns say different things rather than the same
    thing twice.

    Only a lossless container is re-judged. An honest MP3 is `lossy` because
    that is what it *is*, and no word about it makes it something else — the
    same boundary `_album_word` draws.

    **The two encodings are named deliberately, and writing this as its
    complement would be a defect.** Elsewhere a set is written as what it
    excludes, so that the member nobody has met yet falls inside it — and here
    that rule inverts, because `undecided` answers no question about a
    container at all. `judge` returns it before it has looked at one: a silent
    stream and a stream with no measurable band are both undecided, so a silent
    FLAC and a silent MP3 are indistinguishable here. `not in ("lossy",
    "overstated")` would therefore let an override re-judge an honest MP3,
    which is the boundary above.

    The cost of naming them is a lossless container that measured undecided —
    silent, or with no band below Nyquist — where an override is dropped in
    silence. The right fix is not a different list: it is asking the file what
    container it is, which this function cannot do, because `StoredQuality`
    carries the verdict and not the codec.
    """
    measured = bool(quality and stored_track_is_transcoded(quality))
    encoding = quality.encoding if quality else None
    reason = quality.reason if quality else ""
    if not said or quality is None or encoding not in ("lossless", "transcoded"):
        return measured, encoding, reason
    stands = said == "transcoded"
    spoken = "lossy" if stands else "not lossy"
    return (
        stands,
        "transcoded" if stands else "lossless",
        f"You said this track is {spoken}. What it measured is unchanged: {reason}",
    )


def _stored_is_borderline(quality: StoredQuality) -> bool:
    """Report whether one measured track missed conviction by a hair.

    Asked of the numbers rather than of the word stored beside them, for the
    same reason ``stored_track_is_transcoded`` is: the measurements outlive
    every rule written about them.
    """
    if quality.decay_db is None or quality.ceiling_db is None:
        return False
    if stored_track_is_transcoded(quality):
        return False
    return is_borderline(quality.decay_db, quality.ceiling_db)


def _deep_payload(album: DeepAlbum) -> dict[str, object]:
    """Render one deeply measured album, with the reading a spectrogram gives by eye.

    Every track carries its band-pass table and the step between neighbouring
    bands, because that is the shape the eye reads: after a wall, a transcode
    leaves a floor that stops falling, and music that has run out of treble
    keeps falling and falls faster near the top.
    """
    return {
        "id": album.album_id,
        "folder": album.folder,
        "was": str(album.before.encoding),
        "now": str(album.after.encoding),
        "verdict_moved": album.verdict_moved,
        "changed": len(album.changed),
        "tracks": [
            {
                "file": track.file_name,
                "encoding": track.measured.encoding,
                "was": track.was.encoding if track.was is not None else None,
                "differs": track.differs,
                "cutoff": track.measured.cutoff_hertz,
                "bitrate": track.measured.effective_bitrate_kbps,
                "decay_db": (
                    round(track.measured.decay_db, 1)
                    if track.measured.decay_db is not None
                    else None
                ),
                "ceiling_db": (
                    round(track.measured.ceiling_db, 1)
                    if track.measured.ceiling_db is not None
                    else None
                ),
                "bands": [
                    {
                        "low": band.low_hertz,
                        "high": band.high_hertz,
                        "db": round(band.rms_db, 1),
                        # The step to the next band, which is the whole point:
                        # a floor is flat across neighbours and falling music is
                        # not. Absent on the last band, which has no next.
                        "step": (
                            round(track.bands[index + 1].rms_db - band.rms_db, 1)
                            if index + 1 < len(track.bands)
                            else None
                        ),
                    }
                    for index, band in enumerate(track.bands)
                ],
            }
            for track in album.tracks
        ],
    }


def _bench_payload(finding: BenchFinding) -> dict[str, object]:
    """Render one bench album for the window, with what its verdict is worth.

    The confidence travels as a word *and* the numbers behind it, because a
    verdict nobody can audit is one nobody can disagree with, and disagreeing is
    the point. Nothing here is stored: every field was re-judged from the
    measurements on record when this was called.
    """
    verdict = finding.verdict
    confidence = finding.confidence
    return {
        "id": finding.album.album_id,
        "root": finding.album.root_id,
        "folder": finding.album.folder_path.name,
        "path": str(finding.album.folder_path),
        "signature": finding.album.unit_signature,
        "encoding": str(verdict.encoding),
        "reason": verdict.reason,
        # Which of the two proofs convicted, so the row can say so and the
        # reader is not sent to a picture that cannot show it.
        "proved_by": str(verdict.proved_by) if verdict.proved_by else None,
        "median_drop_db": round(verdict.median_drop_db, 1),
        "median_ceiling_db": round(verdict.median_ceiling_db, 1),
        # Only from a measurement provably taken from these files. A number that
        # may be another song's is not shown as this album's.
        "bitrate": verdict.effective_bitrate_kbps if not finding.shared else None,
        "tracks": finding.album.track_count,
        "analyzed": finding.analyzed,
        "suspect": finding.is_suspect,
        "borderline": verdict.is_borderline,
        "findings": sorted({str(name) for track in verdict.tracks for name in track.findings}),
        "dissenting": len(verdict.dissenting_tracks),
        "confidence": str(confidence.sureness),
        "confidence_reasons": list(confidence.reasons),
        "margin_db": (round(confidence.margin_db, 1) if confidence.margin_db is not None else None),
        "shared": finding.shared,
        # Shown, not merely obeyed: the measurement stays on screen beside the
        # override, and a row that silently reads the override looks identical
        # to one that measured it.
        "spoken": finding.spoken,
    }


def _quality_payload(finding: AlbumQuality, root: Path) -> dict[str, object]:
    """Render one album's measurement for the window, reason included.

    The reason travels with the verdict because a user who cannot see why an
    album was called a transcode cannot disagree with it — and disagreeing is
    the point of showing findings before offering to rename anything.
    """
    verdict = finding.verdict
    try:
        relative = str(finding.folder_path.relative_to(root))
    except ValueError:
        relative = finding.folder_path.name
    dissenting = verdict.dissenting_tracks
    return {
        "folder": relative,
        "path": str(finding.folder_path),
        "signature": finding.unit_signature,
        "encoding": str(verdict.encoding),
        "reason": verdict.reason,
        # The same fact the bench row carries, for the same reason: the report
        # after a scan is the other screen that states a verdict.
        "proved_by": str(verdict.proved_by) if verdict.proved_by else None,
        "median_drop_db": round(verdict.median_drop_db, 1),
        "bitrate": verdict.effective_bitrate_kbps,
        "tracks": finding.tracks,
        "analyzed": finding.analyzed,
        "suspect": finding.is_suspect,
        "borderline": verdict.is_borderline,
        "median_ceiling_db": round(verdict.median_ceiling_db, 1),
        "findings": sorted({str(name) for track in verdict.tracks for name in track.findings}),
        "dissenting": len(dissenting),
    }


def _attributes(file: object) -> str:
    """Spell what a file is, the way a Soulseek window spells it.

    Bit depth and sample rate for lossless, bitrate for lossy, and the length
    either way — the column read to tell two copies apart.
    """
    parts: list[str] = []
    depth = getattr(file, "bit_depth", None)
    rate = getattr(file, "sample_rate_hz", None)
    bitrate = getattr(file, "bitrate_kbps", None)
    seconds = getattr(file, "duration_seconds", None)
    if depth and rate:
        parts.append(f"{depth}/{rate / 1000:.1f}kHz")
    elif bitrate:
        parts.append(f"{bitrate}kbps")
    if seconds:
        parts.append(f"{seconds // 60}m{seconds % 60}s")
    return ", ".join(parts)
