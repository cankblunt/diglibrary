"""Rendering the folder and file names an identified release should have."""

import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from diglibrary.application.contracts import ReleaseMetadata, written_credit
from diglibrary.library.artwork import ImageKind
from diglibrary.library.audio import AudioProperties
from diglibrary.library.genres import spell_genres

DEFAULT_FOLDER_TEMPLATE = (
    "%albumartist% - %album%{ ({%kind% - }%pressing%)}{ (Ed. %edition%)}{ (%year%)}{ [%format%]}"
)
"""Each parenthesis carries its own year, and only the years there are.

`(Remaster - 1994) (1979)` when the pressing and the album are different years,
`(EP - 2015)` when there is only one: a year that would be written twice is
written once.
"""
DEFAULT_TRACK_TEMPLATE = "{%disc%-}%track%. {%artist% - }%title%"
DEFAULT_DISC_FOLDER_TEMPLATE = "CD%disc%"
DEFAULT_VARIOUS_ARTISTS_LABEL = "VA"
DEFAULT_FRONT_IMAGE_NAME = "cover"
DEFAULT_BACK_IMAGE_NAME = "back"

_CATALOGUE_PLACEHOLDERS = frozenset({"label", "catalog", "country", "genre", "style"})
"""What the source published about the release, beyond its title and year.

Only fields that catalogues usually fill are offered, because a placeholder
that is usually empty promises something the library cannot keep. `barcode` is
deliberately absent for that reason: catalogues publish it for a minority of
releases, so it would mostly render nothing.

There is no `pressingyear`, and that is on purpose: `%edition%` already *is* the
pressing's year, and one value does not get a second name.

A release may publish several labels or catalogue numbers; the first is used.
Joining them would put a list of labels inside a folder name, which is not a
name.
"""

_AUDIO_PLACEHOLDERS = frozenset({"bitdepth", "samplerate", "bitrate", "duration"})
"""Facts read from the file itself rather than from any catalogue.

Always available — they come from the stream header the scan already read — and
they are what tells two rips of one album apart when the catalogue cannot.
"""

_MEASURED_PLACEHOLDERS = frozenset({"key", "keyname", "bpm"})
"""What was *measured* from the audio, which is a different kind of fact.

Every other placeholder here is transcribed from a catalogue or read from a
header. These three are the output of an algorithm over the recording, they
exist only for the files that have been measured, and nothing measures them on
the way to a rename: a name is written only where a measurement already sits.
Track names only — an album is not in a key.
"""

FOLDER_PLACEHOLDERS = (
    frozenset({"albumartist", "album", "year", "edition", "format", "kind", "pressing"})
    | _CATALOGUE_PLACEHOLDERS
)
TRACK_PLACEHOLDERS = (
    frozenset({"disc", "track", "artist", "title", "album", "albumartist", "year"})
    | _CATALOGUE_PLACEHOLDERS
    | _AUDIO_PLACEHOLDERS
    | _MEASURED_PLACEHOLDERS
)
DISC_FOLDER_PLACEHOLDERS = frozenset({"disc"})

_PLACEHOLDER = re.compile(r"%([a-z]+)%")
_OPTIONAL_GROUP = re.compile(r"\{([^{}]*)\}")
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in "123456789"}
    | {f"lpt{digit}" for digit in "123456789"}
)
_CODEC_LABELS = (
    ("alac", "ALAC"),
    ("mp4a", "M4A"),
    ("flac", "FLAC"),
    ("mp3", "MP3"),
    ("wave", "WAV"),
    ("wav", "WAV"),
    ("aiff", "AIFF"),
    ("aif", "AIFF"),
)
DEFAULT_TRANSCODED_LABEL = "Lossy"
"""What a format that turned out to be a transcode is called.

It *replaces* the codec rather than qualifying it — `[Lossy]`, not `[FLAC Fake]`
— and is written with one capital rather than in capitals. Nothing is lost by
dropping the codec: a transcode verdict is only ever reached inside a lossless container
(`verdict.py` reaches `TRANSCODED` only when `is_lossless_container`), so the
word never displaces a bitrate, and what the folder needs to say is that the
audio is lossy however it is packaged.

Configurable because it lands in the user's own folder names, in whatever
language those names are written: `[naming] transcoded_label` in `config.toml`.
"""

VARIABLE_BITRATE_LABEL = "VBR"
"""What a lossy codec is called when its bitrate differs from track to track."""

NOMINAL_BITRATES = frozenset({32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320})
"""The bitrates a constant-rate MP3 encoder can produce, in kbps.

This is what separates two rips of different quality from one variable-rate
rip. A CBR file reports its rate exactly, so 128 beside 320 is two qualities
and deserves to be named as two. A VBR file reports an average, so 167 beside
188 is one album breathing — a single such album can span many values, and
naming each would make an unreadable folder.
"""

_BITRATE_LABELS = frozenset({"MP3"})
"""Formats whose label carries the bitrate, because it is what tells two apart.

A lossless format is named by the format alone: every FLAC of an album is the
album. Two MP3 rips of the same album are not the same file, and the number is
the difference.
"""


class NamingError(ValueError):
    """Purpose: reject a naming template that cannot produce a usable name.

    Responsibilities: report an unknown placeholder or an empty result.
    Boundaries: it repairs nothing and never falls back to a default name.
    Dependencies: built-in exception behavior. Collaborators: ``NamingPolicy``
    and configuration loading. Constraints: a template is validated when it is
    configured, not when a file is about to be renamed — a write is the worst
    possible moment to discover a typo in a setting.
    """


@dataclass(frozen=True, slots=True)
class TrackFacts:
    """Purpose: carry what is true of one file's audio, for the names that ask.

    Responsibilities: hold the per-file values a track template may name, and
    render each as the string a file name should carry. Boundaries: it measures
    nothing, reads no file and consults no database — every field is handed to
    it by whoever already knows. Dependencies: none. Collaborators: the change
    planner, which fills the header facts, and the Mixing screen, which fills
    the measured ones. Constraints: **an absent value is empty, never invented
    and never approximated.** An empty value takes its optional group with it,
    which is how a template that names the key produces an ordinary name for a
    track nobody has measured.
    """

    bit_depth: int | None = None
    sample_rate: int | None = None
    bitrate: int | None = None
    duration_ms: int | None = None
    camelot: str | None = None
    key_name: str | None = None
    bpm: float | None = None

    def values(self) -> dict[str, str]:
        """Render each fact as the text a name would carry, or empty."""
        return {
            "bitdepth": str(self.bit_depth) if self.bit_depth else "",
            # In kHz, without the unit, so the template writes the word:
            # `{%samplerate%kHz}` or `{%samplerate% kHz}` is the template's
            # choice. `44.1` and `48`, never `44.10`.
            "samplerate": _khz(self.sample_rate),
            "bitrate": str(round(self.bitrate)) if self.bitrate else "",
            # Minutes and seconds are written with letters, `3m45s`, rather
            # than with a colon between them. The colon is the one character
            # this module exists to keep out of a name — macOS shows it as `/`
            # — and a duration silently turning into `3-45` would read as a
            # range.
            "duration": _length(self.duration_ms),
            "key": self.camelot or "",
            "keyname": self.key_name or "",
            # DJs read whole numbers; the tenth is noise in a file name.
            "bpm": f"{self.bpm:.0f}" if self.bpm else "",
        }


def facts_from_properties(properties: "AudioProperties | None") -> TrackFacts:
    """Read the header facts a name may ask for off one file's properties.

    Here rather than in the planner because two callers need exactly this and
    must get the same answer: the planner, naming a real file, and the composer's
    preview, showing what that name will look like. Two copies would let the
    preview promise something the rename does not do.

    A file whose stream could not be parsed has no properties, which is ordinary
    — it answers with every fact empty rather than with a guess.
    """
    if properties is None:
        return TrackFacts()
    return TrackFacts(
        bit_depth=properties.bit_depth,
        sample_rate=properties.sample_rate,
        bitrate=getattr(properties, "bitrate", None),
        duration_ms=properties.duration_ms,
    )


def _khz(sample_rate: int | None) -> str:
    if not sample_rate:
        return ""
    khz = sample_rate / 1000
    return f"{khz:.1f}".removesuffix(".0")


def _length(duration_ms: int | None) -> str:
    if not duration_ms or duration_ms <= 0:
        return ""
    seconds = round(duration_ms / 1000)
    return f"{seconds // 60}m{seconds % 60:02d}s"


@dataclass(frozen=True, slots=True)
class NamingStyle:
    """Purpose: name one ready-made arrangement of folder and track names.

    Responsibilities: pair the templates that belong together under a name the
    user can choose in one line. Boundaries: it renders nothing and validates
    nothing — it is the starting point a ``NamingPolicy`` is built from, and any
    template may still be overridden individually. Dependencies: none.
    Collaborators: ``NAMING_STYLES`` and configuration loading. Constraints: a
    style is a default, never a restriction; the free-form template remains the
    way to express an arrangement nobody anticipated.
    """

    folder_template: str
    track_template: str = DEFAULT_TRACK_TEMPLATE
    description: str = ""


NAMING_STYLES: Mapping[str, NamingStyle] = MappingProxyType(
    {
        "spotiflac": NamingStyle(
            DEFAULT_FOLDER_TEMPLATE,
            description="Artist - Album (Remaster - 1994) (1979) [FLAC]",
        ),
        "bracketed_year": NamingStyle(
            "%albumartist% - %album%{ - Ed. %edition%}{ [%year%]}",
            description="Artist - Album [1974]",
        ),
        "year_first": NamingStyle(
            "{(%year%) }%albumartist% - %album%{ [%format%]}",
            description="(1974) Artist - Album [FLAC]",
        ),
        # Two arrangements that never name the pressing, whatever the release
        # says. `include_edition` switches the edition off wherever a template
        # puts it; these two leave no place for it at all, which is a different
        # thing to want.
        #
        # They still carry the designator group, and that is not decoration:
        # `%kind%` holds a word the *user* wrote on the folder, read back by
        # `existing_designator`. A style that drops the group deletes that
        # word. Refusing to name the pressing and refusing to keep what the
        # user called the album are different refusals.
        "year_only": NamingStyle(
            "%albumartist% - %album%{ ({%kind% - }%pressing%)}{ (%year%)}",
            description="Artist - Album (1974)",
        ),
        "year_and_format": NamingStyle(
            "%albumartist% - %album%{ ({%kind% - }%pressing%)}{ (%year%)}{ [%format%]}",
            description="Artist - Album (1974) [FLAC]",
        ),
        "minimal": NamingStyle(
            "%albumartist% - %album%",
            description="Artist - Album",
        ),
    }
)
"""The arrangements this project ships, chosen by name in configuration."""

DEFAULT_NAMING_STYLE = "spotiflac"
"""The factory default: the arrangement that names the most about a release."""


@dataclass(frozen=True, slots=True)
class NamingPolicy:
    """Purpose: turn an identified release into the names its files should carry.

    Responsibilities: hold the templates, validate them, and render folder and
    track names. Boundaries: it touches no filesystem, decides nothing about
    what a release is, and never rewrites a value — it arranges what the source
    published. Dependencies: canonical metadata only. Collaborators:
    the change planner. Constraints: a segment in braces disappears whole when
    any placeholder inside it is empty, so an unknown year leaves no stray
    parentheses behind.
    """

    folder_template: str = DEFAULT_FOLDER_TEMPLATE
    track_template: str = DEFAULT_TRACK_TEMPLATE
    disc_folder_template: str = DEFAULT_DISC_FOLDER_TEMPLATE
    various_artists_label: str = DEFAULT_VARIOUS_ARTISTS_LABEL
    front_image_name: str = DEFAULT_FRONT_IMAGE_NAME
    back_image_name: str = DEFAULT_BACK_IMAGE_NAME
    include_edition: bool = False
    transcoded_label: str = DEFAULT_TRANSCODED_LABEL
    genre_spellings: tuple[str, ...] = ()
    """Spellings added to `genres.GENRE_SPELLINGS`, for the tags, `%genre%` and `%style%`."""

    @classmethod
    def from_style(cls, style: str, **overrides: object) -> "NamingPolicy":
        """Build a policy from a shipped style, with any field overridden.

        The style supplies the templates; an override wins over it. Wanting one
        of the shipped arrangements with a different disc folder name should not
        require copying a template.
        """
        if style not in NAMING_STYLES:
            known = ", ".join(sorted(NAMING_STYLES))
            raise NamingError(f"Unknown naming style: {style}. Available: {known}.")
        chosen = NAMING_STYLES[style]
        fields: dict[str, object] = {
            "folder_template": chosen.folder_template,
            "track_template": chosen.track_template,
        }
        fields.update({name: value for name, value in overrides.items() if value is not None})
        return cls(**fields)  # type: ignore[arg-type]

    def __post_init__(self) -> None:
        """Validate every template while they are still configuration, not a rename."""
        _validate(self.folder_template, FOLDER_PLACEHOLDERS, "folder")
        _validate(self.track_template, TRACK_PLACEHOLDERS, "track")
        _validate(self.disc_folder_template, DISC_FOLDER_PLACEHOLDERS, "disc folder")
        for name, kind in ((self.front_image_name, "front"), (self.back_image_name, "back")):
            if not name.strip() or Path(name).name != name:
                raise NamingError(f"The {kind} image name must be a plain file name.")

    def disc_folder_name(self, disc_number: int) -> str:
        """Render the folder name for one disc of a multi-disc album."""
        return sanitize_component(_render(self.disc_folder_template, {"disc": str(disc_number)}))

    def image_name(self, kind: ImageKind, extension: str) -> str:
        """Render the file name one cover image should carry."""
        base = self.front_image_name if kind is ImageKind.FRONT else self.back_image_name
        return sanitize_component(base, reserve=len(extension.encode("utf-8"))) + extension.lower()

    def folder_name(
        self,
        release: ReleaseMetadata,
        container_format: str,
        edition: str | None = None,
        kind: str | None = None,
        kind_year: str | None = None,
    ) -> str:
        """Render the folder name for one identified release.

        ``edition`` is the marker to place beside the album title, decided by
        the planner rather than here: a marker the folder already carried, or
        one added to break a collision. ``None`` falls back to the switch,
        which names the pressing whenever its year differs.

        ``kind_year`` is the year that belongs with the designator — the one
        written beside it on the folder, when there is one. A designator always
        carries a year, falling back to the album's own; the release year
        beside it then disappears, because two parentheses holding the same
        year say nothing twice.
        """
        values = self.folder_values(release, container_format, edition, kind, kind_year)
        return sanitize_component(_render(self.folder_template, values))

    def folder_values(
        self,
        release: ReleaseMetadata,
        container_format: str,
        edition: str | None = None,
        kind: str | None = None,
        kind_year: str | None = None,
    ) -> dict[str, str]:
        """Return what each piece of a folder name would hold for this release.

        Public because the composer has to answer a question the rendered name
        cannot: *which chosen piece does this album have nothing to put in?* A
        name with the year missing looks exactly like a name that was never
        asked for a year, and the composer shows the difference. Reading it
        from here rather than re-deriving it is what keeps the answer and the
        name from ever disagreeing.
        """
        values = self._release_values(release, container_format)
        if edition is not None:
            values["edition"] = edition
        if kind is not None:
            values["kind"] = kind
        if values["kind"]:
            values["pressing"] = kind_year or values["edition"] or values["year"]
            # The designator's parenthesis now carries the pressing, so the
            # edition segment would repeat it.
            values["edition"] = ""
        if values["pressing"] == values["year"] or values["edition"] == values["year"]:
            values["year"] = ""
        return values

    def pressing_edition(self, release: ReleaseMetadata) -> str:
        """Return the pressing year to use as an edition, when there is one to use."""
        original = release.original_released_on or release.released_on
        pressing = release.released_on
        if pressing is None or original is None or pressing.year == original.year:
            return ""
        return str(pressing.year)

    def track_name(
        self,
        release: ReleaseMetadata,
        title: str,
        track_number: int,
        disc_number: int,
        disc_count: int,
        track_artist: str | None,
        extension: str,
        track_number_width: int = 2,
        facts: "TrackFacts | None" = None,
    ) -> str:
        """Render one track's file name, including its extension.

        ``facts`` carries what is true of this particular file — the header's
        own numbers, and a key or tempo where one has been measured. Absent, the
        placeholders that name them render empty and take their optional groups
        with them, which is how one template serves a measured track and an
        unmeasured one without saying anything untrue about either.
        """
        values = self.track_values(
            release,
            title,
            track_number,
            disc_number,
            disc_count,
            track_artist,
            track_number_width,
            facts,
        )
        return (
            sanitize_component(
                _render(self.track_template, values),
                reserve=len(extension.encode("utf-8")),
            )
            + extension.lower()
        )

    def track_values(
        self,
        release: ReleaseMetadata,
        title: str,
        track_number: int,
        disc_number: int,
        disc_count: int,
        track_artist: str | None,
        track_number_width: int = 2,
        facts: "TrackFacts | None" = None,
    ) -> dict[str, str]:
        """Return what each piece of a track name would hold for this file.

        The companion of ``folder_values``, and public for the same reason: the
        composer says which chosen piece this particular track has nothing to
        fill, which is the whole difference between a key that was not asked for
        and a key nobody has measured.
        """
        values = self._release_values(release, "")
        values.update(
            {
                "title": title,
                "track": f"{track_number:0{track_number_width}d}",
                "disc": str(disc_number) if disc_count > 1 else "",
                "artist": track_artist if is_various(release) and track_artist else "",
            }
        )
        if facts is not None:
            values.update(facts.values())
        return values

    def _release_values(self, release: ReleaseMetadata, container_format: str) -> dict[str, str]:
        original = release.original_released_on or release.released_on
        pressing = release.released_on
        # The album's own year is the year; the pressing's year is named beside
        # it, and only when the two actually differ — and only when the policy
        # asks for the pressing to be named at all.
        names_the_pressing = (
            self.include_edition
            and pressing is not None
            and original is not None
            and pressing.year != original.year
        )
        edition = str(pressing.year) if names_the_pressing and pressing is not None else ""
        return {
            "albumartist": self._album_artist(release),
            "album": release.title,
            "year": str(original.year) if original else "",
            "edition": edition,
            "kind": "",
            "pressing": "",
            "format": container_format,
            "disc": "",
            "track": "",
            "artist": "",
            "title": "",
            # Transcribed, never chosen between: the first of each, because a
            # name holds one. Nothing here is derived, defaulted or guessed —
            # what the source did not publish stays empty and takes its group.
            "label": _without_disambiguator(_first(release.labels)),
            "catalog": _first(release.catalog_numbers),
            "country": release.country or "",
            "genre": _first(spell_genres(release, release.genres, self.genre_spellings)),
            "style": _first(spell_genres(release, release.styles, self.genre_spellings)),
            # Per file, so they are empty in a folder name and filled by
            # `track_name` from the facts it is handed.
            "bitdepth": "",
            "samplerate": "",
            "bitrate": "",
            "duration": "",
            "key": "",
            "keyname": "",
            "bpm": "",
        }

    def _album_artist(self, release: ReleaseMetadata) -> str:
        if is_various(release):
            return self.various_artists_label
        return written_credit(release.artists) or "Unknown Artist"


def _first(values: Sequence[str]) -> str:
    """Return the first non-empty value a release published, or nothing."""
    return next((value.strip() for value in values if value and value.strip()), "")


_DISCOGS_DISAMBIGUATOR = re.compile(r"\s\(\d+\)$")
"""Discogs' own row number, appended when two entities share a name.

`Northlight (3)`, `Harbour Records (2)`. It is not part of the label's name: it
is how one catalogue tells its own duplicate entries apart, and a folder called
`… (Northlight (3))` says nothing to the person reading it.

Removing it is transcribing more accurately, not editing — which is why it is
done **only where a name is composed**. The tag keeps what the source
published, and so does every screen. It is applied to labels only.
"""


def _without_disambiguator(name: str) -> str:
    """Drop the catalogue's own row number from a name being written to disk."""
    return _DISCOGS_DISAMBIGUATOR.sub("", name).strip()


_VARIOUS_CREDITS = frozenset({"various", "various artists"})
"""How a catalogue writes *nobody in particular* in the release's own credit.

Deliberately not widened to the abbreviations and translations that `hints` and
`matching` accept in a folder name: those are what people write, and a
catalogue's release credit does not read any of them. A compilation credited
to a person by name is recognised by its tracklist instead.
"""


def is_various(release: ReleaseMetadata) -> bool:
    """Report whether the *release* is a compilation.

    The judgement belongs to the source, not to a count of distinct artists
    across the local files: the release is the authority, and a collaboration
    album is not a compilation however many artists it credits.

    Public and module-level because three layers ask the same question and must
    get the same answer: this is what puts an artist into a track's file name,
    what writes `compilation` into its tags, and what makes the window offer a
    credit to type at all. Two copies of the rule would let the field appear
    where the name has nowhere to put it.

    **Two ways the source can say it, because the credit alone is one too few.**
    A catalogue credits a compilation to its curator as readily as to
    `Various`. Read by the credit alone, such a release is not a compilation,
    and its file names lose the track credit that is the only thing telling
    those tracks apart. The second way is the release's own tracklist, read
    below.
    """
    if any(artist.name.strip().casefold() in _VARIOUS_CREDITS for artist in release.artists):
        return True
    return _every_track_is_somebody_else(release)


def _every_track_is_somebody_else(release: ReleaseMetadata) -> bool:
    """Whether this release's own tracklist credits people the release does not.

    Still the source's judgement and not the files': what is read here is the
    tracklist the catalogue published. Discogs publishes a track credit
    precisely when it differs from the release's, and MusicBrainz repeats the
    album's artist on every track of an ordinary album, so on both a
    compilation is the record whose tracks are by other people.

    Two weaker readings are refused. *The source declares a compilation* alone
    brings single-artist best-ofs with it, and *the tracklist names more than
    one artist* alone brings albums whose guests are credited per track.

    **Every credited track, not most of them**, so a curator who also contributes
    a track keeps the album out of this. It is the conservative direction on
    purpose: a compilation this misses keeps the name it would have had without
    this rule, while a wrong yes writes a credit into the file names of an
    album that has one artist. The known miss is a DJ mix whose DJs each
    contribute a track of their own.
    """
    credited = {artist.name.strip().casefold() for artist in release.artists if artist.name.strip()}
    per_track = [
        {artist.name.strip().casefold() for artist in track.artists if artist.name.strip()}
        for track in release.tracks
    ]
    with_a_credit = [names for names in per_track if names]
    if not with_a_credit or any(names & credited for names in with_a_credit):
        return False
    # More than one, or a single-artist album whose catalogue happens to spell
    # the credit differently on the tracks — `The Glass Herons` against
    # `Glass Herons` — would become a compilation of one person.
    return len(set().union(*with_a_credit)) > 1


_DISTINCT_EDITION_WORDS = (
    "deluxe",
    "expanded",
    "anniversar",
    "aniversár",
    "bonus",
    "bônus",
    "special edition",
    "edição especial",
    "legacy edition",
)
"""Wording that names an edition with different content, not just new mastering.

A remaster with the same tracklist is deliberately absent: most of a real
library is remastered reissues, and naming every one of them is noise. Content
that differs — extra tracks, expanded programme — is what a name has to
distinguish.
"""


def is_distinct_edition(release: ReleaseMetadata) -> bool:
    """Report whether this pressing's content differs from the plain album.

    Three signals, all free of extra requests: bonus-marked tracks in the
    release's own tracklist, an edition word among the Discogs format
    descriptions, or one in the release title itself.
    """
    haystacks = [release.title.casefold()]
    haystacks.extend(value.casefold() for value in release.formats)
    if any(word in haystack for word in _DISTINCT_EDITION_WORDS for haystack in haystacks):
        return True
    return any(
        "bonus" in track.title.casefold() or "bônus" in track.title.casefold()
        for track in release.tracks
    )


RELEASE_KINDS = (
    "EP",
    "Single",
    "Maxi-Single",
    "Remix",
    "Remixes",
    "Compilation",
    "Live",
    "Mixtape",
)
"""The release designators this project recognizes in a name it did not write."""

_KINDS_PATTERN = "|".join(RELEASE_KINDS)

EXISTING_KIND = re.compile(
    r"\(\s*(?P<word>[^()]+?)\s*[-\u2013\u2014]\s*(?P<year>\d{4})\s*\)",
)
"""The `(word - year)` convention: the designator rides inside the year parenthesis.

Any word is accepted here, not only the ones this project knows. This project
never *invents* a designator, so whatever sits in that slot was written by the
user: `(Remastered - 1994)`, `(En Vivo - 2009)`, `(Northlight Records - 2015)`.
A closed list would quietly delete every word it does not hold.
"""

_REMASTER_WITH_YEAR = re.compile(
    r"[({\[]\s*(?:"
    r"(?P<year_first>\d{4})\s+(?P<before>Remaster\w*)"
    r"|(?P<after>Remaster\w*)\s*(?P<year_last>\d{4})?"
    r")\s*[)}\]]",
    re.IGNORECASE,
)
"""A remaster note written the other ways people write it.

`{1994 Remaster}`, `(2013 Remaster)`, `(Remastered)`, `(Remasterizado)` — four
shapes, all meaning the pressing. They are read here and written back in the
`(word - year)` form. Only remaster words are read this loosely: outside that
form there is nothing around a word saying it was meant as a designator, and
`Live at the Harbour` is a title.
"""

_TRAILING_KIND = re.compile(
    r"(?:"
    r"\s*[(\[]\s*(?P<bracketed>" + _KINDS_PATTERN + r")\s*[)\]]"
    r"|\s*[-\u2013\u2014]\s*(?P<dashed>" + _KINDS_PATTERN + r")"
    r"|\s+(?P<bare>" + _KINDS_PATTERN + r")"
    r")\s*$",
    re.IGNORECASE,
)
"""The same designator written the other common ways, at the end of the name.

``Album EP``, ``Album - EP``, ``Album (EP)``, ``Album [EP]`` — all of them mean
what ``(EP - 2015)`` means, and a library assembled over years contains all of
them. Recognized only as the *last* token, because "Live at the Harbour" is a
title and "Album Live" is a designator.
"""


def existing_kind(folder_name: str, release_title: str = "") -> str:
    """Return the release designator a folder name carries, in canonical casing.

    ``release_title`` is the guard, and it is the whole reason this is safe:
    when the catalogue's own title contains the word, the word is *title* and
    is left entirely alone. A catalogue may hold both "Night Shift" and "Night
    Shift (Remixes)" as separate release groups, so rewriting a "(Remixes)" on
    the folder into a designator would destroy the album's actual name.

    Inside the `(word - year)` convention the word may be anything; written any
    other way it must be one this project recognizes, because there the word
    has nothing around it saying it was meant as a designator at all.
    """
    return existing_designator(folder_name, release_title)[0]


def existing_designator(folder_name: str, release_title: str = "") -> tuple[str, str]:
    """Return the designator a folder name carries and the year written beside it.

    The year is the one on the folder, not the catalogue's: `{1994 Remaster}`
    says which pressing this is, and no source can tell that from the release
    alone. It is empty when the designator was written without one.
    """
    match = EXISTING_KIND.search(folder_name)
    if match is not None:
        return _accepted(match.group("word"), match.group("year"), release_title)
    match = _REMASTER_WITH_YEAR.search(folder_name)
    if match is not None:
        word = match.group("before") or match.group("after")
        year = match.group("year_first") or match.group("year_last") or ""
        return _accepted(word, year, release_title)
    match = _TRAILING_KIND.search(_without_suffixes(folder_name))
    if match is None:
        return "", ""
    return _accepted(next(value for value in match.groups() if value), "", release_title)


def _accepted(found: str, year: str | None, release_title: str) -> tuple[str, str]:
    """Settle a designator's casing, and drop it when the catalogue owns the word."""
    canonical = _canonical_kind(found)
    if canonical and _names_it(release_title, canonical):
        return "", ""
    return canonical, year or ""


def strip_trailing_kind(text: str) -> str:
    """Return the text without a trailing designator, for searching.

    A catalogue stores "Harbour Sessions", not "Harbour Sessions EP". This runs
    only on a rung of the query ladder that the previous rung already failed,
    so a title that genuinely ends in one of these words costs one extra
    request and nothing else.
    """
    return _TRAILING_KIND.sub("", text).strip()


def _canonical_kind(found: str) -> str:
    for canonical in RELEASE_KINDS:
        if canonical.casefold() == found.casefold():
            return canonical
    return found


def _names_it(release_title: str, kind: str) -> bool:
    """Report whether the catalogue's own title already carries this word."""
    if not release_title:
        return False
    return re.search(rf"\b{re.escape(kind)}\b", release_title, re.IGNORECASE) is not None


def _without_suffixes(folder_name: str) -> str:
    """Drop the trailing [FORMAT] and (YEAR) so the designator becomes the last token."""
    text = re.sub(r"\s*\[[^\]]*\]\s*$", "", folder_name)
    return re.sub(r"\s*[(\uff08]\s*\d{4}\s*[)\uff09]\s*$", "", text).strip()


def track_format_label(
    item: AudioProperties,
    transcoded: bool = False,
    transcoded_word: str = DEFAULT_TRANSCODED_LABEL,
    measured_rate: int | None = None,
) -> str:
    """Return what one file is, in the words a folder name uses.

    ``transcoded`` comes from measuring the audio, never from the container:
    it is the difference between a FLAC and a FLAC that used to be an MP3,
    which nothing in the file itself admits to. When that is the answer, the
    word stands alone and the codec is not named.

    **The word stands alone, and the rate is not written beside it.** Some
    transcodes are detected by a path that yields no bitrate by design, so a
    name carrying the rate could be written for only part of the files the
    rule applies to: `Lossy 128 kbps` beside a bare `Lossy` would be two rules.
    The word is the form that is always available.

    ``measured_rate`` is still accepted and deliberately unused, exactly as
    `format_label` accepts and ignores `measured_rates`: the callers hold it
    because the plan carries it, and taking it away here would move the
    decision to whoever calls.

    The accepted cost: a lossless folder whose tracks came from encoders of
    different rates names them all alike. The Quality screen still tells them
    apart, per track.
    """
    label = _codec_label(item.codec)
    if transcoded:
        return transcoded_word
    if label in _BITRATE_LABELS and item.bitrate:
        return f"{label} {round(item.bitrate / 1000)}"
    return label


def odd_track_labels(
    properties: Sequence[AudioProperties | None],
    transcoded: Sequence[bool],
    transcoded_word: str = DEFAULT_TRANSCODED_LABEL,
    measured_rates: Sequence[int | None] = (),
) -> tuple[str | None, ...]:
    """Return the suffix each track deserves, or ``None`` where it deserves none.

    Only a track that differs from what the album mostly holds is marked: an
    album that agrees with itself already says so on its folder, and repeating
    it on every file would be noise. When the odd track shares the album's
    codec, the number is what distinguishes it (`128kbps`); when it does not,
    the format is (`FLAC`, `Lossy`).

    ``measured_rates`` is what the *audio* of a lossy-origin track turned out to
    be, and **it does not reach a name**: the mark is `[Lossy]`, never a
    measured rate. The argument is still taken, and taken deliberately: the
    callers hold it because the plan carries it.

    The number that does still reach a track's mark is the file's *declared*
    bitrate, below, and it is a different fact about a different situation —
    the odd track that shares the album's codec.

    The folder is deliberately not affected: it keeps `[Lossy]` and `[FLAC,
    Lossy]`, because a folder names the qualities inside it and a bitrate that
    varies from track to track is not one of them.

    A rate arrives here only when the measurement it came from is provably this
    file's. Where it is not, the word stands — and that is the whole guard,
    because a name is written onto the user's disk.
    """
    labels = [
        (
            None
            if item is None
            else track_format_label(
                item,
                transcoded[index] if index < len(transcoded) else False,
                transcoded_word,
                measured_rates[index] if index < len(measured_rates) else None,
            )
        )
        for index, item in enumerate(properties)
    ]
    present = [label for label in labels if label is not None]
    if len(set(present)) < 2:
        return tuple(None for _ in labels)
    dominant = Counter(present).most_common(1)[0][0]
    dominant_codec = dominant.split(" ")[0]
    # **A measured rate does not reach a track name.** Part of the lossy-origin
    # tracks have no rate to write, because the detection that found them
    # yields none, so numbering the others would mark one situation in two
    # ways. The word is what every one of them can say.
    marks: list[str | None] = []
    for index, label in enumerate(labels):
        item = properties[index]
        if label is None or label == dominant:
            marks.append(None)
        elif (
            label.split(" ")[0] == dominant_codec
            and dominant_codec in _BITRATE_LABELS
            and item is not None
            and item.bitrate
        ):
            marks.append(f"{round(item.bitrate / 1000)}kbps")
        else:
            marks.append(label)
    return tuple(marks)


def format_label(
    properties: Iterable[AudioProperties],
    transcoded: Sequence[bool] | None = None,
    transcoded_word: str = DEFAULT_TRANSCODED_LABEL,
    measured_rates: Sequence[int | None] = (),
) -> str:
    """Return the format tag for an album's files, as it appears in its folder name.

    ``measured_rates`` is accepted and **not used**: the callers hold it because
    the plan carries it, and taking it away here would only move the decision
    to whoever calls. The track labels do not use it either — a folder says
    `Lossy` and so does the odd track, which is what makes this the same rule
    on both sides rather than two.

    Every distinct quality present is named, separated by commas, so a folder
    holding three kinds says so: ``FLAC, FLAC Fake, MP3 128``. An album whose
    files agree reads as one label, which is the common case.

    A lossy codec whose bitrate varies from track to track is ``VBR`` rather
    than a list of numbers. Variable-rate MP3 albums are common, and one may
    report a different average on every track — naming each would produce a
    folder name no one can read, and the album is a single variable-bitrate
    rip, not several qualities.
    """
    items = list(properties)
    flags = list(transcoded) if transcoded is not None else [False] * len(items)
    rates: dict[str, list[int]] = {}
    labels: list[str] = []
    for index, item in enumerate(items):
        is_transcoded = flags[index] if index < len(flags) else False
        # **The measured rate is deliberately not passed here.** A folder names
        # the qualities inside it, and a bitrate that varies from track to
        # track is not one of them. Passing it would make one situation wear
        # two names, `[FLAC, Lossy]` and `[FLAC, Lossy 192 kbps]`, the
        # difference being only whether the audio had been measured yet — and
        # a folder name that changes because a measurement later succeeded is
        # a folder that renames itself for a reason nobody asked for.
        label = track_format_label(item, is_transcoded, transcoded_word, None)
        labels.append(label)
        if not is_transcoded and _codec_label(item.codec) in _BITRATE_LABELS and item.bitrate:
            rates.setdefault(_codec_label(item.codec), []).append(item.bitrate)

    variable = {
        codec: f"{codec} {VARIABLE_BITRATE_LABEL}"
        for codec, measured in rates.items()
        if len(distinct := {round(rate / 1000) for rate in measured}) > 1
        and not distinct <= NOMINAL_BITRATES
    }
    settled = {variable.get(label.split(" ")[0], label) for label in labels}
    return ", ".join(sorted(settled))


_BRACKETED = re.compile(r"\[([^\[\]]*)\]")
"""One bracketed group of a folder name, which is where a format label sits."""


def _is_a_format_segment(content: str, transcoded_word: str) -> bool:
    """Say whether a bracketed group is this application's own format label.

    Read against the vocabulary ``format_label`` writes with, never a list
    copied here: a codec this project learns tomorrow is recognised the same
    day. Every comma-separated item must open with a word from it, so
    ``FLAC, Lossy`` and ``MP3 320`` are format labels and a note such as
    ``Missing 3 tracks``, written in brackets by the user, is not.
    """
    items = [part.strip() for part in content.split(",")]
    if not items or not all(items):
        return False
    known = {label for _, label in _CODEC_LABELS} | {transcoded_word}
    return all(any(item == word or item.startswith(f"{word} ") for word in known) for item in items)


def replace_format_segment(
    name: str, label: str, transcoded_word: str = DEFAULT_TRANSCODED_LABEL
) -> str:
    """Put the current format label into a name the user wrote, leaving the rest alone.

    A folder name the user corrects is taken verbatim — and the name being
    corrected is one this application wrote, so the edit freezes *this
    application's own words* along with the user's. A folder edited for any
    other reason would stop being named from the verdict: when its last lossy
    track is later cleared, the per-track mark goes, since that name is still
    computed, and the folder keeps a word about audio that no longer exists.

    **Before preserving anything read off the disk, ask whether this
    application could have put it there.** The format segment is a measurement
    wearing brackets, so it is always the current one; everything else in the
    name is the user's and is not touched.

    A name with no format segment is returned exactly as written. Nothing is
    appended: the brackets may have been taken off on purpose, and adding them
    back would be this function inventing a name nobody asked for.
    """
    found = [
        match
        for match in _BRACKETED.finditer(name)
        if _is_a_format_segment(match[1], transcoded_word)
    ]
    if not found:
        return name
    # The last one, because the template puts the format at the end and a name
    # holding two would have the earlier one inside the album's own title.
    last = found[-1]
    return f"{name[: last.start(1)]}{label}{name[last.end(1) :]}"


_COLON_BEFORE_SPACE = re.compile(r":\s")
"""A colon used as punctuation, which is where a dash reads as the source meant."""


def sanitize_component(name: str, reserve: int = 0) -> str:
    """Make one path component storable, using the rules of the running system.

    The rules are native rather than portable: on POSIX only the separator and
    NUL are impossible, so titles keep the punctuation their source published.
    The single-function shape is deliberate — a portable mode later is a change
    here and nowhere else.

    The colon is the exception, and it is not a portability concession. macOS
    stores it and then shows it as ``/`` in the Finder, so a title with a
    subtitle, ``Deep Cuts 5: The Roots of the Groove``, reads on screen as a
    path separator, the one character this function exists to keep out of a
    name. It is replaced everywhere rather than behind a platform test, because
    it is impossible on Windows too and a name should not change meaning when
    the library moves.
    """
    cleaned = _INVISIBLE.sub("", name).replace("\x00", "").replace("/", "-")
    # A colon separating a title from its subtitle becomes a spaced dash; one
    # inside a number has no space after it and must not grow one.
    cleaned = _COLON_BEFORE_SPACE.sub(" - ", cleaned).replace(":", "-").strip()
    if os.name == "nt":
        cleaned = re.sub(r'[\\*?"<>|]', "-", cleaned).rstrip(". ")
        if cleaned.split(".")[0].casefold() in _WINDOWS_RESERVED:
            cleaned = f"_{cleaned}"
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise NamingError("A name rendered to an empty path component.")
    # `.` and `..` survive every rule above — they hold no separator and no NUL —
    # and they are not names at all: the filesystem reads them as *this folder*
    # and *the one above it*. A folder named `..` is the album planned into its
    # own parent's parent, which is the one thing a rename must never be able to
    # do. Reachable by hand: `planner.py` sanitizes the folder name typed into
    # the plan editor and takes it verbatim otherwise. Refused rather than
    # quietly turned into something else, for the same reason the empty
    # component is: there is no honest name to substitute, and inventing one
    # would hide that something impossible was asked for.
    #
    # Only the all-dots forms. A trailing dot is legal on POSIX and is trimmed
    # just above on Windows, so `Mr.` keeps its full stop where the filesystem
    # keeps it.
    if set(cleaned) == {"."}:
        raise NamingError(f"A name rendered to {cleaned!r}, which is a path step and not a name.")
    # A leading dot is deliberately left alone, and the cost is accepted. It
    # makes the album invisible — `scanner._partition` and
    # `containers.count_audio_below` both skip dot-entries — so a release titled
    # `.Foo` becomes a hidden folder that vanishes on the next scan. Stripping
    # the dot is worse: a title may open with an ellipsis, and this project
    # does not delete what its source published to work around its own
    # scanner. Not reading dot-entries is the simpler rule, and the case is
    # rare.
    return _within_the_filesystem_s_limit(cleaned, reserve)


_INVISIBLE = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f"
    "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]"
)
"""Characters that reach a filename and then are not in it, as far as anyone can see.

Two different failures, one rule. **The overrides** — U+202E and its family —
reverse how the rest of the name renders, which is the classic way a filename
shows one extension and carries another; a title or a folder name published by
a catalogue or written into a tag by a stranger is all it takes, in an
application whose entire job is renaming files. **The zero-width ones** make
two names that differ by nothing anyone can see, which walks straight past the
collision check in `planner.py` — `_one_name_on_disk` folds case and accent and
has no reason to expect a character with no width — and lands two files whose
names cannot be told apart.

Dropped rather than replaced: none of them is a letter of anybody's title, so
removing one takes no word away. `\\t`, `\\n` and `\\r` are left to the
whitespace rule below, which folds them into the single space it folds every
other run of whitespace into."""

NAME_MAX_BYTES = 255
"""What one path component may hold on APFS, HFS+ and every ext filesystem.

Bytes, not characters, and that is the whole trap: an accented letter spends
two bytes in UTF-8, so a long title in a language that uses accents crosses the
line well before 255 characters.
"""


def _within_the_filesystem_s_limit(cleaned: str, reserve: int) -> str:
    """Cut a name down to what the filesystem can hold, on a character boundary.

    Without it the failure is the expensive kind rather than the loud kind.
    `_refuse_a_plan_that_cannot_finish` rehearses a plan against the disk and
    asks whether every destination is free — it never asks whether a name is
    *storable*. A name too long would pass the rehearsal and raise
    `ENAMETOOLONG` in the middle of the run: some tracks renamed, some not,
    tags half written, the folder left under its old name. That half-applied
    album is exactly the state the rehearsal exists to make impossible.

    Cut rather than refused, because unlike `..` there *is* an honest shorter
    name, and a catalogue title that runs long is an ordinary fact about
    classical and live records rather than a mistake anyone made. The tail is
    what goes: a title is identified by how it starts.

    ``reserve`` is what the caller will append afterwards — the extension, which
    is added outside this function and would otherwise push the result back over
    the line.
    """
    room = NAME_MAX_BYTES - reserve
    if len(cleaned.encode("utf-8")) <= room:
        return cleaned
    # Encode, cut, and decode back ignoring the partial character the cut may
    # have left. Doing it this way rather than by counting characters is the
    # point: the limit is bytes and the string is not.
    trimmed = cleaned.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip()
    if not trimmed:
        raise NamingError("A name rendered to nothing that would fit a file name.")
    return trimmed


def _codec_label(codec: str) -> str:
    lowered = codec.casefold()
    for prefix, label in _CODEC_LABELS:
        if lowered.startswith(prefix):
            return label
    return codec.upper()


def _validate(template: str, allowed: frozenset[str], kind: str) -> None:
    if not template.strip():
        raise NamingError(f"The {kind} template is empty.")
    if template.count("{") != template.count("}"):
        raise NamingError(f"The {kind} template has an unbalanced optional group.")
    unknown = sorted(set(_PLACEHOLDER.findall(template)) - allowed)
    if unknown:
        known = ", ".join(sorted(allowed))
        raise NamingError(
            f"The {kind} template uses unknown placeholders: {', '.join(unknown)}. "
            f"Available: {known}."
        )
    if not _PLACEHOLDER.search(template):
        raise NamingError(f"The {kind} template contains no placeholder.")


def _render(template: str, values: Mapping[str, str]) -> str:
    def optional(match: re.Match[str]) -> str:
        segment = match.group(1)
        if any(not values.get(name, "").strip() for name in _PLACEHOLDER.findall(segment)):
            return ""
        return segment

    # Groups nest: "{ ({%kind% - }%year%)}" drops the kind alone when there is
    # none, and the whole parenthesis when there is no year either. The regex
    # only ever matches an innermost group, so resolving repeats until stable.
    resolved = template
    while True:
        reduced = _OPTIONAL_GROUP.sub(optional, resolved)
        if reduced == resolved:
            break
        resolved = reduced
    return _PLACEHOLDER.sub(lambda match: values.get(match.group(1), ""), resolved)
