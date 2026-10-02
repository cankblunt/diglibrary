"""Turning what is on disk into a search query, without trusting it as truth."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from diglibrary.application.contracts import MetadataQuery
from diglibrary.library.containers import NumberedTrack
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.library.naming import strip_trailing_kind
from diglibrary.library.tags import TagStore
from diglibrary.metadata.normalization import fold_accents, normalize_text

_BRACKET_SUFFIX = re.compile(r"\s*\[[^\]]*\]\s*$")
# Edition wording a catalogue does not carry in its release title. Left in the
# query, a search for "Night Train (Remastered)" returns nothing at all.
_EDITION_WORDS = (
    "remaster",
    "remasteriz",
    "remasteriza",
    "deluxe",
    "expanded",
    "anniversar",
    "aniversár",
    "edition",
    "edição",
    "versão",
    "bonus",
    "bônus",
    "ao vivo",
    "live",
    "mono",
    "stereo",
    "reissue",
)
# Full-width parentheses and dashes appear in real folder names, so they are
# matched deliberately rather than by accident.
_YEAR_SUFFIX = re.compile(
    "\\s*[\uff08(]\\s*"
    "(?:(?:EP|Single|Maxi-Single|Remix|Remixes|Compilation|Live|Mixtape)\\s*[-\u2013\u2014]\\s*)?"
    "(\\d{4})\\s*[)\uff09]\\s*$",
    re.IGNORECASE,
)
"""A trailing year, alone or behind a release designator.

``(2015)`` and ``(EP - 2015)`` both name the year and neither belongs in a
search: a catalogue stores "Attic Sessions", not "Attic Sessions (EP - 2015)".
Recognizing only the designators this project knows keeps a real parenthetical
title — "(The Paper Boats)" — out of the pattern."""
_TRACK_PREFIX = re.compile("^\\s*(?:\\d{1,2}\\s*-\\s*)?\\d{1,3}\\s*[.\\-\u2013]\\s*")
_BARE_NUMBER_PREFIX = re.compile("^\\s*(\\d{1,3})\\s+(?=\\S)")
"""A number with nothing but space after it, which only an album can interpret."""
_INDEX_MINIMUM_FILES = 3
_INDEX_MAJORITY = 0.8
"""How much of a folder must be numbered before the numbers are read as an index.

Not every file: a bonus track or a hidden one often arrives unnumbered beside
eleven that are numbered, and that folder is still numbered.
"""
_CATALOGUE_PREFIX = re.compile("^\\s*(\\d{1,4})\\s*[-\u2013\u2014]\\s*(?=\\S)")
"""A number leading a folder name, which is an index when a credit follows it."""
_INDEXED_FOLDER = re.compile("^\\s*(\\d{1,4})\\s*[-\u2013\u2014._]\\s*(?=\\S)")
"""The same number in a collection that numbers its folders, separator and all."""
_PLAUSIBLE_YEARS = range(1900, 2100)
_LEADING_YEAR = re.compile("^\\s*[\uff08(]\\s*(\\d{4})\\s*[)\uff09]\\s*")
"""A year written before the name, which no catalogue stores in a title."""
_SEPARATORS = (" - ", " \u2013 ", " \u2014 ")
# Catalogues credit a compilation to "Various"; a library folder says VA.
_VARIOUS_LABELS = frozenset({"va", "v.a.", "v/a", "various", "various artists"})
_VARIOUS_CANONICAL = "Various"


@dataclass(frozen=True, slots=True)
class SearchHints:
    """Purpose: carry what the disk suggests an album might be.

    Responsibilities: hold the artist, album, year, and free text derived from
    names and existing tags. Boundaries: none of this is authoritative — it is
    a hint and not the truth, and it exists only to find candidates that
    are then judged on their durations. Dependencies: none. Collaborators: the
    identification workflow and ``AlbumMatcher``. Constraints: a hint may be
    wrong, so every field is optional and an empty hint is a valid answer.
    """

    artist: str | None = None
    album: str | None = None
    year: int | None = None
    text: str = ""

    def to_query(self) -> MetadataQuery:
        """Return the metadata query these hints imply.

        "Various" is a library convention, not a name a catalogue indexes, so a
        compilation searches by title alone — sent as an artist, it makes the
        search for a compilation come back with nothing.
        """
        return MetadataQuery(artist=self._searchable_artist, album=self.album)

    @property
    def _searchable_artist(self) -> str | None:
        return None if self.artist == _VARIOUS_CANONICAL else self.artist

    @property
    def is_usable(self) -> bool:
        """Return whether there is enough here to search for anything."""
        return bool(self.album or self.artist or self.text)


def query_ladder(
    hints: SearchHints, track_titles: tuple[str, ...] = ()
) -> tuple[MetadataQuery, ...]:
    """Return the queries worth trying for these hints, most specific first.

    Only the first query runs for a healthy album; the rest exist because real
    titles arrive contaminated, and each pattern below defeats a search:

    - a label or subtitle riding the album tag ("Green Tide - Radio Lantern")
    - a presenter folded into the title ("Studio Aster Presents: The Harbour
      Connection", where the presenter is the credited artist)
    - a trailing parenthetical no catalogue stores ("25 Years of Harbour
      Beats (Aster Presents)")
    - a conjunction spelled the other way: the sleeve says "Ebb and Flow" and
      the catalogue says "Ebb & Flow"
    - an accent the catalogue does not index ("Zoé" credited as "Zoe")
    - a title no catalogue holds at all, where the only searchable text left is
      what the tracks are called — which is how a person finds a record whose
      folder was named by whoever uploaded it

    A later query is only sent when the previous one returned nothing, so the
    ladder costs nothing when the first answer exists. Wrong guesses are cheap
    by design: every result is still judged on its text and its durations, so a
    too-loose query can only add candidates that lose.
    """
    artist = hints._searchable_artist
    album = hints.album
    queries: list[MetadataQuery] = [hints.to_query()]
    if not album:
        queries.extend(_track_title_queries(artist, track_titles))
        return _unique(queries)
    if artist:
        queries.append(MetadataQuery(album=album))
    plain = normalize_text(re.sub(r"\s*\([^)]*\)\s*$", "", album))
    if plain and plain != album:
        queries.append(MetadataQuery(artist=artist, album=plain))
    # "Album EP" is not a title any catalogue stores. This rung only
    # runs when the ones above already found nothing, so a title that really
    # ends in one of these words costs one request and nothing else.
    undecorated = normalize_text(strip_trailing_kind(plain or album))
    if undecorated and undecorated != album and undecorated != plain:
        queries.append(MetadataQuery(artist=artist, album=undecorated))
    left, right = _split_title(plain or album)
    if left and right:
        queries.append(MetadataQuery(artist=artist, album=left))
        queries.append(MetadataQuery(artist=artist, album=right))
        # "X Presents: Y" credits X as the artist of an album called Y.
        queries.append(MetadataQuery(artist=left, album=right))
    for variant in _conjunction_variants(plain or album):
        queries.append(MetadataQuery(artist=artist, album=variant))
        if artist:
            queries.append(MetadataQuery(album=variant))
    folded_artist = _folded(artist)
    folded_album = _folded(album)
    if folded_artist != artist or folded_album != album:
        queries.append(MetadataQuery(artist=folded_artist, album=folded_album))
    free_text = " ".join(part for part in (artist, album) if part)
    if free_text:
        queries.append(MetadataQuery(text=free_text))
        folded_text = fold_accents(free_text)
        if folded_text != free_text:
            queries.append(MetadataQuery(text=folded_text))
    queries.extend(_track_title_queries(artist, track_titles))
    return _unique(queries)


_CONJUNCTION = re.compile(r"\s+(?:&|\+|and|e|y)\s+", re.IGNORECASE)
_CONJUNCTION_FORMS = ("&", "and", "e")


def _conjunction_variants(title: str) -> tuple[str, ...]:
    """Return the same title with its conjunction spelled every common way.

    A catalogue stores one spelling and a folder carries another, and neither
    side is wrong. Nothing is generated for a title without a conjunction, so
    this rung of the ladder simply does not exist for most albums.
    """
    if _CONJUNCTION.search(title) is None:
        return ()
    variants = []
    for form in _CONJUNCTION_FORMS:
        variant = normalize_text(_CONJUNCTION.sub(f" {form} ", title))
        if variant and variant != title and variant not in variants:
            variants.append(variant)
    return tuple(variants)


def _track_title_queries(artist: str | None, track_titles: tuple[str, ...]) -> list[MetadataQuery]:
    """Return the last resort: the artist and the most distinctive track name.

    The longest title is chosen rather than the first, because a first track is
    routinely called "Intro" while a long one is nearly unique. Two forms are
    tried, accented and folded, because this rung only runs when everything
    above it already found nothing.
    """
    usable = [title for title in track_titles if title and len(title) >= 4]
    if not usable:
        return []
    distinctive = max(usable, key=len)
    text = " ".join(part for part in (artist, distinctive) if part)
    queries = [MetadataQuery(text=text)]
    folded = fold_accents(text)
    if folded != text:
        queries.append(MetadataQuery(text=folded))
    return queries


def _folded(value: str | None) -> str | None:
    return fold_accents(value) if value else value


def _unique(queries: list[MetadataQuery]) -> tuple[MetadataQuery, ...]:
    """Drop repeats while keeping order, and cap how far the ladder may descend.

    The cap is a budget, not a rule about correctness: every rung costs a
    request against a rate-limited service, and an album that survived a dozen
    distinct queries is one a person should look at.
    """
    unique: list[MetadataQuery] = []
    for query in queries:
        if query not in unique:
            unique.append(query)
    return tuple(unique[:12])


def _split_title(title: str) -> tuple[str | None, str | None]:
    """Split one contaminated title into its two plausible halves."""
    for separator in (*_SEPARATORS, ": "):
        if separator in title:
            left, _, right = title.partition(separator)
            return normalize_text(left), normalize_text(right)
    return None, None


def derive_hints(unit: AlbumUnit, tag_store: TagStore | None = None) -> SearchHints:
    """Read search hints from an album unit's existing tags, then its folder name.

    Tags are preferred when present because they were written deliberately at
    some point; the folder name is the fallback. Both are only starting points.

    A folder that sits in a numbered collection is told so, because the question
    its own name cannot answer — whether ``020-`` is an index or the start of a
    credit — its neighbours answer between them (``folders_carry_an_index``).
    """
    if tag_store is not None:
        tagged = _hints_from_tags(unit, tag_store)
        if tagged is not None:
            return tagged
    return hints_from_folder_name(_unit_name(unit), _in_a_numbered_collection(unit))


def _in_a_numbered_collection(unit: AlbumUnit) -> bool:
    """Return whether this album's neighbours are numbered folders.

    Every album under one parent gets the same answer, so the listing is done
    once per folder rather than once per album: asking afresh reads the same
    thousands of names again for every album of a scan. The parent's
    modification time is part of the key, because that is
    what changes when a folder inside it is added or renamed, and organizing an
    album renames exactly that.
    """
    parent = unit.folder_path.parent
    try:
        return _numbered_collection_at(str(parent), parent.stat().st_mtime_ns)
    except OSError:
        return False


@lru_cache(maxsize=256)
def _numbered_collection_at(parent: str, modified_nanoseconds: int) -> bool:
    """Return whether the folders directly inside ``parent`` are numbered."""
    return folders_carry_an_index(
        [entry.name for entry in Path(parent).iterdir() if entry.is_dir()]
    )


def hints_from_folder_name(name: str, numbered_collection: bool = False) -> SearchHints:
    """Parse ``Artist - Album (Year) [FORMAT]`` and the looser forms around it.

    Trailing brackets, repeated years, and edition wording are removed, because a
    catalogue stores the release title without them and a query carrying them
    matches nothing: "Night Train (Remastered)" and "Harbour (1974) (1974)"
    are not titles any catalogue holds.

    ``numbered_collection`` says the caller established that these folders are
    numbered, which is the only way to read the tight form
    ``020-Some Artist-Some Album``: the index runs straight into what follows,
    and what follows is sometimes a credit and sometimes the album itself.
    Either way the number goes, and whatever is left is parsed — or, when no
    credit can be read out of it, searched as text, which is the ladder's last
    rung.
    """
    cleaned = normalize_text(name) or ""
    year: int | None = None
    while True:
        stripped = _BRACKET_SUFFIX.sub("", cleaned)
        match = _YEAR_SUFFIX.search(stripped)
        if match is not None:
            year = year or int(match.group(1))
            stripped = stripped[: match.start()]
        stripped = normalize_text(_strip_edition_suffix(stripped)) or ""
        if stripped == cleaned or not stripped:
            break
        cleaned = stripped
    cleaned, opening_year = _without_leading_year(cleaned)
    cleaned, leading_number = _without_catalogue_number(cleaned, numbered_collection)
    leading_year = opening_year or leading_number
    artist, album = _split_artist_album(cleaned)
    return SearchHints(artist=artist, album=album, year=year or leading_year, text=cleaned)


def _without_leading_year(text: str) -> tuple[str, int | None]:
    """Take a year written in front of the name, where a catalogue never puts one.

    ``(1974) Harbour`` is a common way to name a folder. A year pattern that
    only looks at the end of a name lets the parenthesis travel into the album
    title, where it matches nothing.
    """
    match = _LEADING_YEAR.match(text)
    if match is None:
        return text, None
    year = int(match.group(1))
    if year not in _PLAUSIBLE_YEARS:
        return text, None
    remainder = normalize_text(text[match.end() :]) or ""
    return (remainder, year) if remainder else (text, None)


def _without_catalogue_number(
    text: str, numbered_collection: bool = False
) -> tuple[str, int | None]:
    """Drop a number leading a folder name, and keep it when it is a year.

    A collection numbers its folders — ``003 - Some Artist - Some Album`` —
    and the artist read off that name would be ``003``, with the whole credit
    landing in the album. Measured on a real library, this is the commonest
    reason a folder-built query differs from the tag-built one.

    Without the neighbours' testimony the number only goes when a credit follows
    it, which is what keeps a band called ``404`` and the album
    ``1200 Lamps - Night Shift`` intact: a name with a single separator is
    ``Artist - Album`` and its first half is the artist. A leading four-digit
    year is the exception, because no catalogue credits one.

    In a numbered collection the guard is unnecessary and wrong: there the tight
    ``011-Some Album`` and ``020-Some Artist-Some Album`` are both album
    folders, the number is an index in both, and no rule reading one name could
    tell them from a band whose name is a number.
    """
    pattern = _INDEXED_FOLDER if numbered_collection else _CATALOGUE_PREFIX
    match = pattern.match(text)
    if match is None:
        return text, None
    remainder = text[match.end() :]
    number = int(match.group(1))
    is_year = len(match.group(1)) == 4 and number in _PLAUSIBLE_YEARS
    credit_follows = any(separator in remainder for separator in _SEPARATORS)
    if not numbered_collection and not is_year and not credit_follows:
        return text, None
    return remainder, number if is_year else None


def _strip_edition_suffix(text: str) -> str:
    """Remove one trailing parenthetical that names an edition rather than a release."""
    if not text.rstrip().endswith(")"):
        return text
    opening = text.rfind("(")
    if opening == -1:
        return text
    inside = text[opening + 1 :].rstrip().rstrip(")").casefold()
    if any(word in inside for word in _EDITION_WORDS):
        return text[:opening]
    return text


def track_title_hint(file_name: str, drop_leading_number: bool = False) -> str:
    """Return a track's title as the file name suggests it, without its numbering.

    ``drop_leading_number`` removes a number that has no separator after it —
    ``02 The Paper Boats``. Alone, such a number is unreadable: it is
    an index in that file and part of the title in ``99 Paper Boats``. So it is
    never decided here. The caller decides for the whole album, where the
    evidence exists (``names_carry_an_index``), and this function is told.
    """
    stem = file_name.rsplit(".", maxsplit=1)[0]
    stripped = _TRACK_PREFIX.sub("", stem)
    if drop_leading_number:
        stripped = _BARE_NUMBER_PREFIX.sub("", stripped)
    return normalize_text(stripped) or stem


def names_carry_an_index(file_names: Sequence[str]) -> bool:
    """Return whether these file names are numbered, rather than merely starting with a digit.

    In a real library a large share of files begin with a number and a space,
    which ``_TRACK_PREFIX`` leaves in place because it requires a separator.
    Carried into a comparison the number is dead weight, and stripping it file
    by file would take the ``99`` out of ``99 Paper Boats``.

    An album says which it is by agreeing with itself: a numbering runs across
    the folder and never repeats a number, while a title that happens to open
    with a digit stands alone among neighbours that do not.
    """
    stems = [name.rsplit(".", maxsplit=1)[0] for name in file_names]
    return _numbered_by_agreement(
        [_TRACK_PREFIX.sub("", stem) for stem in stems], _BARE_NUMBER_PREFIX
    )


def folders_carry_an_index(folder_names: Sequence[str]) -> bool:
    """Return whether these sibling folders are a numbered collection.

    The same evidence as ``names_carry_an_index``, one shelf up. It is what
    decides the tight form: ``020-Some Artist-Some Album`` beside twenty
    numbered neighbours is indexed, and a band called ``404`` standing among
    albums that carry no number is not.
    """
    return _numbered_by_agreement(folder_names, _INDEXED_FOLDER)


def _numbered_by_agreement(names: Sequence[str], pattern: re.Pattern[str]) -> bool:
    """Return whether most of these names open with a number, and none repeats one."""
    numbers = [int(match.group(1)) for name in names if (match := pattern.match(name)) is not None]
    return (
        len(names) >= _INDEX_MINIMUM_FILES
        and len(numbers) >= len(names) * _INDEX_MAJORITY
        and len(set(numbers)) == len(numbers)
    )


def tag_titles_distinguish(titles: Sequence[str | None]) -> bool:
    """Whether these title tags tell an album's own tracks apart at all.

    **Written by the complement, and asked of the album rather than of a file.**
    A value every track carries distinguishes none of them, so it cannot be any
    of their names — the judgement ``_shared_title_tail`` makes about a
    shared *ending*, asked here about the whole string. Nothing here knows what
    a bad title looks like: a site address, a label, a rip crew's banner and
    any other repeated value are all the same shape, which is one value
    standing where several names should be.

    With an album whose every `TIT2` reads one site address, a release chosen
    by hand pairs none of its files when the tag is read, while the file names
    agree with every one of its tracks; read from the names instead, the same
    release pairs them all.

    Two or more titles are needed to answer it: one file's title agrees with
    itself, which says nothing about anything.
    """
    present = [title.casefold() for title in titles if title and title.strip()]
    if len(present) < 2 or len(present) != len(titles):
        return True
    return len(set(present)) > 1


def _tag_title(file: AudioFileFacts, tag_store: TagStore | None) -> str | None:
    """Return one file's title tag, or ``None`` when it has none or cannot be read."""
    if tag_store is None:
        return None
    try:
        values = tag_store.read(file.path).get("title", ())
    except Exception:
        return None
    return normalize_text(values[0]) if values else None


def local_track_titles(unit: AlbumUnit, tag_store: TagStore | None = None) -> tuple[str, ...]:
    """Return each file's title as the disk suggests it, tag first, name second.

    These are hints like every other: they never become a written value, but
    they are per-track evidence — enough to break a duration tie or to
    recognize a release whose catalogue entry publishes no lengths.

    The album decides once whether its names are numbered, and every file is
    read the same way, because that is the only scale at which the question can
    be answered (``names_carry_an_index``). It decides the same way whether its
    tags name anything at all: a title every file repeats is not per-track
    evidence, and the names are read in its place
    (``tag_titles_distinguish``).
    """
    numbered = names_carry_an_index([file.path.name for file in unit.audio_files])
    tagged = [_tag_title(file, tag_store) for file in unit.audio_files]
    if not tag_titles_distinguish(tagged):
        tagged = [None] * len(tagged)
    return tuple(
        title or track_title_hint(file.path.name, drop_leading_number=numbered)
        for file, title in zip(unit.audio_files, tagged, strict=True)
    )


def clean_album_title(title: str) -> str:
    """Strip the decoration a catalogue does not store from an album title.

    Applied to whatever the title came from: a tag is as likely as a folder
    name to read "Night Train (Remastered)".
    """
    cleaned = normalize_text(title) or ""
    while True:
        stripped = _BRACKET_SUFFIX.sub("", cleaned)
        match = _YEAR_SUFFIX.search(stripped)
        if match is not None:
            stripped = stripped[: match.start()]
        stripped = normalize_text(_strip_edition_suffix(stripped)) or ""
        if stripped == cleaned or not stripped:
            return cleaned
        cleaned = stripped


def _hints_from_tags(unit: AlbumUnit, tag_store: TagStore) -> SearchHints | None:
    for file in unit.audio_files:
        try:
            tags = tag_store.read(file.path)
        except Exception:
            continue
        album = _first(tags, "album")
        artist = _first(tags, "albumartist") or _first(tags, "artist")
        if album or artist:
            date = _first(tags, "date") or ""
            year = int(date[:4]) if date[:4].isdigit() else None
            artist = _canonical_artist(artist)
            album = clean_album_title(album) if album else album
            return SearchHints(
                artist=artist,
                album=album,
                year=year,
                text=" ".join(part for part in (artist, album) if part),
            )
    return None


def _first(tags: dict[str, tuple[str, ...]], name: str) -> str | None:
    values = tags.get(name, ())
    return normalize_text(values[0]) if values else None


def _split_artist_album(text: str) -> tuple[str | None, str | None]:
    for separator in _SEPARATORS:
        if separator in text:
            artist, _, album = text.partition(separator)
            return _canonical_artist(normalize_text(artist)), normalize_text(album)
    return None, normalize_text(text)


def _canonical_artist(name: str | None) -> str | None:
    """Return the artist as a catalogue would credit it."""
    if name is not None and name.strip().casefold() in _VARIOUS_LABELS:
        return _VARIOUS_CANONICAL
    return name


def _unit_name(unit: AlbumUnit) -> str:
    if unit.is_loose_track:
        return unit.folder_path.stem
    return unit.folder_path.name


def files_in_track_order(
    unit: AlbumUnit, tag_store: TagStore | None = None
) -> tuple[AudioFileFacts, ...]:
    """Return this album's files in the order the album puts them in.

    A scan builds ``audio_files`` from the folder listing sorted by name, which
    is the album's order only when the names carry the numbers. An album whose
    names do not would otherwise be listed out of order. The place a track
    holds is a fact the file already states, so it is asked rather than assumed.

    **The tag is asked, and nothing is parsed out of the name.** Measured on a
    real library, no album has a name that knows what its tag does not — so a
    name reader would be a second source with nothing to add and its own way of
    being wrong.

    A file that states no place keeps the folder's order, after the files that
    do: this application never invents where a track belongs. An
    album where *nothing* is tagged therefore comes back exactly as it was —
    name order, which is what those albums have always played in.

    The disc comes from the scanner first, as it does in ``numbered_tracks``: a
    file out of a `CD2` folder is on disc two whatever its tags say.
    """
    if tag_store is None:
        return unit.audio_files
    placed: list[tuple[int, int, int, AudioFileFacts]] = []
    unplaced: list[AudioFileFacts] = []
    for position, file in enumerate(unit.audio_files):
        try:
            tags = tag_store.read(file.path)
        except Exception:
            unplaced.append(file)
            continue
        raw = (tags.get("tracknumber") or ("",))[0].split("/")[0].strip()
        if not raw.isdigit():
            unplaced.append(file)
            continue
        tagged = (tags.get("discnumber") or ("",))[0].split("/")[0].strip()
        disc = (
            file.disc_hint if file.disc_hint is not None else int(tagged) if tagged.isdigit() else 1
        )
        # The scan position breaks a tie, so two files claiming one number keep
        # the order the folder gave them instead of swapping between reads.
        placed.append((disc, int(raw), position, file))
    placed.sort()
    return tuple(entry[3] for entry in placed) + tuple(unplaced)


def numbered_tracks(
    unit: AlbumUnit, tag_store: TagStore | None = None
) -> tuple[NumberedTrack, ...]:
    """Return each file's own disc, track number and title, for the files that have one.

    Only what the tags actually say: a file whose track number is
    absent or not a number contributes nothing, which is what lets the caller
    tell "the numbers do not close" from "the numbers say two albums".

    **The disc is the scanner's answer first.** A file that came out of a `CD2`
    folder is on disc two whatever its tags say, and some two-disc albums carry
    no disc tag at all. Reading only the tag calls each of them two different
    albums and advises splitting an album the scanner has just joined. What
    the scanner measured off the folder outranks what the tag omits.

    The number may be written `3` or `3/12`; both are the same claim.
    """
    if tag_store is None:
        return ()
    # Read once per file and kept, because the titles have to be judged as a set
    # before any one of them is used — and asking the disk twice for the same
    # file is a cost this function is called once per identification to pay.
    read: list[tuple[AudioFileFacts, dict[str, tuple[str, ...]]]] = []
    for file in unit.audio_files:
        try:
            read.append((file, tag_store.read(file.path)))
        except Exception:
            continue
    # The titles are what tells a folder holding one album twice from a folder
    # holding two different albums (``_one_song``), so a title every file
    # repeats would answer *the same song* about every pair of them — and the
    # advice reverses. Asked of the album, exactly as in ``local_track_titles``.
    distinguishing = tag_titles_distinguish(
        [normalize_text(values["title"][0]) if values.get("title") else None for _, values in read]
    )
    found: list[NumberedTrack] = []
    for file, tags in read:
        raw = (tags.get("tracknumber") or ("",))[0].split("/")[0].strip()
        if not raw.isdigit():
            continue
        tagged = (tags.get("discnumber") or ("",))[0].split("/")[0].strip()
        disc = str(file.disc_hint) if file.disc_hint is not None else tagged or "1"
        title = (tags.get("title") or ("",))[0].strip() if distinguishing else ""
        found.append(
            NumberedTrack(
                disc=disc,
                number=int(raw),
                title=title or track_title_hint(file.path.name),
                bitrate=file.properties.bitrate if file.properties else None,
            )
        )
    return tuple(found)
