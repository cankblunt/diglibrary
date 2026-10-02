"""Turning an identified release into the exact changes a folder needs."""

import os
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from diglibrary.application.contracts import ReleaseMetadata, written_credit
from diglibrary.library.artwork import (
    Artwork,
    ArtworkStore,
    ImageFacts,
    ImageKind,
    StagedImage,
    is_worth_embedding,
    is_worth_writing,
)
from diglibrary.library.casing import normalize_release, normalize_track
from diglibrary.library.matching import TrackAlignment
from diglibrary.library.models import AlbumUnit
from diglibrary.library.naming import (
    NamingPolicy,
    existing_designator,
    facts_from_properties,
    format_label,
    is_distinct_edition,
    odd_track_labels,
    replace_format_segment,
    sanitize_component,
)
from diglibrary.library.spelling import (
    NAMED_TAGS,
    preserve_existing_spelling,
    resolve_spelling,
)
from diglibrary.library.tags import TagStore, as_the_container_writes, desired_tags
from diglibrary.metadata.normalization import fold_accents


class OperationKind(StrEnum):
    """The kinds of change this project performs on a user's library."""

    WRITE_TAGS = "write_tags"
    RENAME_FILE = "rename_file"
    RENAME_FOLDER = "rename_folder"
    WRITE_IMAGE = "write_image"
    EMBED_IMAGE = "embed_image"


@dataclass(frozen=True, slots=True)
class ChangeOperation:
    """Purpose: describe one reversible change, completely enough to undo it.

    Responsibilities: name what changes, where, from what, and to what.
    Boundaries: it performs nothing — it is a record the executor reads and the
    review interface displays. Dependencies: none. Collaborators:
    ``ChangePlan``, the executor, and ``change_operations`` in the database.
    Constraints: ``before_state`` here is a preview captured at planning time.
    The executor re-reads it immediately before writing, because that is the
    state a reversal must actually restore.
    """

    sequence: int
    kind: OperationKind
    target_path: Path
    after_state: Mapping[str, object]
    before_state: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChangePlan:
    """Purpose: hold everything one album needs, and why it may not be applied.

    Responsibilities: carry the ordered operations and any blocker that stops
    them. Boundaries: it changes nothing on disk and decides nothing about
    confidence — the threshold is applied by the caller. Dependencies:
    ``ChangeOperation``. Collaborators: the executor and the review interface.
    Constraints: an album already in its correct form yields no operations, so
    an empty plan is a normal, successful outcome rather than a failure. A
    cover-only plan carries no release, because it identifies nothing — it
    moves the user's own picture from inside their files to beside them.
    """

    unit: AlbumUnit
    release: ReleaseMetadata | None
    operations: tuple[ChangeOperation, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    matched_tracks: int = 0

    @property
    def is_empty(self) -> bool:
        """Return whether the album is already exactly as it should be."""
        return not self.operations

    @property
    def is_applicable(self) -> bool:
        """Return whether this plan may be executed at all."""
        return not self.blockers

    @property
    def is_partial(self) -> bool:
        """Return whether this plan deliberately leaves some files alone."""
        return bool(self.warnings)


class ChangePlanner:
    """Purpose: compute the smallest set of changes that makes an album correct.

    Responsibilities: render target names, compare them with what is on disk,
    and emit only the operations that actually change something, in the order
    that keeps every later path valid. Boundaries: it writes nothing, and it
    never decides whether confidence is sufficient. Dependencies: a naming
    policy, a tag store, and — only when cover art is planned — an artwork
    store, all injected. Collaborators: the executor and the
    review interface. Constraints: anything that could rename a file onto the
    wrong track becomes a blocker rather than a guess — an unusable alignment
    and a name collision both stop the plan instead of shaping it.
    """

    def __init__(
        self,
        policy: NamingPolicy,
        tag_store: TagStore,
        artwork_store: ArtworkStore | None = None,
    ) -> None:
        """Create a planner from an injected naming policy, tag store, and artwork store."""
        self._policy = policy
        self._tag_store = tag_store
        self._artwork_store = artwork_store

    def plan(
        self,
        unit: AlbumUnit,
        release: ReleaseMetadata,
        alignment: TrackAlignment,
        artwork: Artwork | None = None,
        folder_name: str | None = None,
        allow_partial: bool = False,
        taken_names: frozenset[str] = frozenset(),
        transcoded: frozenset[str] = frozenset(),
        measured_rates: Mapping[str, int] | None = None,
        write_tags: bool = True,
        user_titles: frozenset[int] = frozenset(),
    ) -> ChangePlan:
        """Return the operations that would bring ``unit`` in line with ``release``.

        ``user_titles`` are the positions whose title the user corrected: the
        catalogue's names are written in one case, and the user's are not.

        ``folder_name`` is the user's correction: when given, it replaces the
        name the policy would render, verbatim except for the characters no
        filesystem accepts.

        ``transcoded`` holds the content signatures the Quality Engine proved
        were lossy before they reached their container. It reaches the name
        and nothing else: a transcode is still organized, still tagged, and
        still reversible — it is only named for what it is.

        ``measured_rates`` is what those tracks' audio measured, by signature,
        and only where the measurement is provably that file's. It reaches no
        name: every lossy-origin track is `[Lossy]`, because most of them are
        convicted by a path that yields no bitrate, and a rule most of its
        files cannot follow is two rules. It is carried here because the plan
        carries it.

        ``allow_partial`` plans what was paired and leaves the rest alone. It
        is never set for unattended work, and it never relaxes ambiguity: a
        file that could go to more than one track still stops the plan, because
        a wrong rename is worse than no rename.

        ``write_tags`` is switched off by the one path where the release came
        from the files themselves. Writing it back would be the application
        returning to a file what that file just told it — and where the album's
        own tracks disagree, it would pick one value and overwrite the other
        without the difference ever being shown. That path renames, and makes
        the Finder icon from the picture each file already carries, which
        invents nothing.
        """
        if artwork is not None and not artwork.is_empty and self._artwork_store is None:
            raise ValueError("Cover art was fetched but no artwork store was injected.")
        blockers = list(_alignment_blockers(alignment))
        warnings: tuple[str, ...] = ()
        if blockers and allow_partial and alignment.is_partial:
            blockers, warnings = [], _partial_warnings(alignment)
        if blockers:
            return ChangePlan(unit=unit, release=release, blockers=tuple(blockers))

        # Settle every name against the spelling already on disk once, before
        # anything is rendered. Doing it here rather than per-value is what
        # keeps an album from being accented in its tags and flattened in its
        # folder name — and the *pairing* is settled with the release, because
        # the pairing is what both of those are rendered from.
        release, alignment = _named(release, alignment, user_titles)
        release, alignment = self._resolve_spelling(unit, release, alignment)

        # Only a track that differs from what the album mostly holds carries
        # its quality in its name. The album's own folder already says what it
        # holds, so repeating it on every file would be noise.
        odd = {
            file.path: mark
            for file, mark in zip(
                unit.audio_files,
                odd_track_labels(
                    [file.properties for file in unit.audio_files],
                    [file.content_signature in transcoded for file in unit.audio_files],
                    self._policy.transcoded_label,
                    [
                        (measured_rates or {}).get(file.content_signature)
                        for file in unit.audio_files
                    ],
                ),
                strict=False,
            )
            if mark is not None
        }
        disc_count = max({assignment.disc_number for assignment in alignment.assignments})
        width = max(2, len(str(max(a.track_number for a in alignment.assignments))))
        album_folder = unit.folder_path if not unit.is_loose_track else unit.folder_path.parent
        targets: dict[Path, Path] = {}
        operations: list[ChangeOperation] = []
        sequence = 0

        # A disc folder whose name is not canonical is renamed before anything
        # inside it moves, so a split never leaves an emptied folder behind.
        disc_folders = self._disc_folder_renames(alignment, album_folder, disc_count)
        for source_folder, destination_folder in disc_folders.items():
            if source_folder == destination_folder:
                continue
            sequence += 1
            operations.append(
                ChangeOperation(
                    sequence=sequence,
                    kind=OperationKind.RENAME_FOLDER,
                    target_path=source_folder,
                    after_state={"path": str(destination_folder)},
                    before_state={"path": str(source_folder)},
                )
            )

        pending_sources: dict[Path, Path] = {}
        # `settled`, not `assignments`. A pairing the matcher offered among
        # rivals it could not tell apart is a suggestion, and nothing is written
        # from a suggestion: the file keeps the name it arrived with until the
        # user says which track it is. Everything else in the album is planned,
        # so a doubt about two files does not hold back the rest.
        for assignment in alignment.settled:
            source = assignment.file.path
            # Tags are read from where the file is now, but written where it will
            # be once the disc folders above it have been renamed.
            pending = _moved(source, disc_folders)
            pending_sources[pending] = source
            tags = desired_tags(
                release,
                assignment,
                total_tracks=len(alignment.assignments),
                total_discs=disc_count,
                genre_spellings=self._policy.genre_spellings,
            )
            current = self._tag_store.read(source)
            # Not the genre: its one spelling is decided by `spell_genres`, and
            # the lower-case forms already on disk may have been written by this
            # application from a catalogue — preserving them would be the
            # application reading its own writing back as the user's.
            tags = preserve_existing_spelling(
                current, tags, settled=frozenset({"genre"}), cased=NAMED_TAGS
            )
            # Compared in the form writing to THIS file produces: an ID3 file
            # reads `tracknumber` back as `7/11`, and comparing it against the
            # pre-fold `7` makes every already-correct MP3 album plan a rewrite
            # of identical values, so it never reaches "already exactly as it
            # should be".
            if write_tags and _tags_differ(current, as_the_container_writes(source, tags)):
                sequence += 1
                operations.append(
                    ChangeOperation(
                        sequence=sequence,
                        kind=OperationKind.WRITE_TAGS,
                        target_path=pending,
                        after_state={"tags": {k: list(v) for k, v in tags.items()}},
                        before_state={"tags": {k: list(v) for k, v in current.items()}},
                    )
                )
            name = self._policy.track_name(
                release,
                title=_with_kept_decorations(assignment.track.title, source.stem),
                track_number=assignment.track_number,
                disc_number=assignment.disc_number,
                disc_count=disc_count,
                track_artist=_track_artist(assignment),
                extension=source.suffix,
                track_number_width=width,
                # The header's own numbers, for the templates that name them.
                # Nothing measured is passed from here: a key or a tempo reaches
                # a file name only where one has already been measured, and that
                # gesture lives on the Mixing screen rather than in an album's
                # plan.
                facts=facts_from_properties(assignment.file.properties),
            )
            # Between the file's current name and this one, the accents are the
            # richer side's and the case is the rule's, which the name was
            # rendered in — so a rename that changes only case is one worth
            # doing, and one that only composes an accent is not.
            name = resolve_spelling(source.name, name)
            if (mark := odd.get(source)) is not None:
                suffix = Path(name).suffix
                # Cleaned and re-measured, because the mark is appended *after*
                # `track_name` sanitized and trimmed: a label carrying a `/`
                # turns one name into two path components and the rename leaves
                # the album folder, and a name already at the 255-byte line
                # goes over it and raises `ENAMETOOLONG` partway through a run,
                # which is the half-applied album the rehearsal exists to make
                # impossible.
                name = sanitize_component(f"{Path(name).stem} [{mark}]", reserve=len(suffix))
                name = f"{name}{suffix}"
            enclosing = album_folder
            if disc_count > 1:
                enclosing = album_folder / self._policy.disc_folder_name(assignment.disc_number)
            targets[pending] = enclosing / name

        # Folded before counting, because two strings Python calls different can
        # be one file on the disk this library lives on. macOS is both
        # case-insensitive and normalization-insensitive: `04 Interlude` and
        # `09 INTERLUDE` are one name, and so is an accented letter typed as one
        # character and as a letter plus a combining accent — which is routine
        # when a catalogue's fields arrive through different ingest paths.
        #
        # Counting the raw strings lets both through, and the rehearsal cannot
        # catch it either: it asks whether each destination exists *before* any
        # rename has run, so neither does. The run would then begin and fail at
        # the second one, leaving some tracks renamed, some not, and tags half
        # written — the half-applied album this check exists to prevent.
        #
        # Refused even on a case-sensitive filesystem, where both could be
        # stored: a library is moved between disks, and a folder that only works
        # on one of them is a trap laid for later.
        folded = Counter(_one_name_on_disk(path) for path in targets.values())
        collisions = sorted(
            {str(path) for path in targets.values() if folded[_one_name_on_disk(path)] > 1}
        )
        if collisions:
            return ChangePlan(
                unit=unit,
                release=release,
                blockers=(
                    "Two tracks would be named identically on this filesystem: "
                    f"{', '.join(collisions)}.",
                ),
            )

        for source, destination in targets.items():
            if destination == source:
                continue
            sequence += 1
            operations.append(
                ChangeOperation(
                    sequence=sequence,
                    kind=OperationKind.RENAME_FILE,
                    target_path=source,
                    after_state={"path": str(destination)},
                    before_state={"path": str(source)},
                )
            )

        folder_operation = self._folder_operation(
            unit, release, sequence + 1, folder_name, taken_names, transcoded, measured_rates
        )
        if folder_operation is not None:
            sequence += 1
            operations.append(folder_operation)

        # Image operations come last, so every path they use is already final and
        # no rename can invalidate one.
        # The arrangement is asked first, because it is the narrower mode. It
        # carries artwork of its own, and the branch below would answer to that
        # by embedding pictures into the files — which is exactly the byte this
        # mode promises never to write. Order is the guard: `write_tags` being
        # off means the cover half and nothing else.
        if not write_tags and self._artwork_store is not None:
            # The arrangement's image gestures. Both fit the mode's own rule
            # because they invent nothing and reach nobody: the Finder icon is
            # made from the picture the file already carries, and the folder's
            # `cover.jpg` is that same picture written beside the album rather
            # than into it. Fetching stays out — the archive is addressed
            # through MusicBrainz and arranging asks nobody anything.
            final_folder = (
                Path(str(folder_operation.after_state["path"]))
                if folder_operation is not None
                else album_folder
            )
            track_paths = tuple(
                (pending_sources[pending], _relocated(target, album_folder, final_folder))
                for pending, target in targets.items()
            )
            covers = (
                self._cover_operations(
                    unit,
                    artwork,
                    alignment,
                    album_folder=album_folder,
                    final_folder=final_folder,
                    disc_folders=disc_folders,
                    disc_count=disc_count,
                    sequence=sequence,
                )
                if artwork is not None
                else []
            )
            operations.extend(covers)
            operations.extend(
                self._finder_icons(
                    self._artwork_store,
                    track_paths,
                    sequence + len(covers),
                    {},
                )
            )
        # Entered even when nothing was fetched, because a picture block that
        # lies about its own image is repaired from the file itself and needs no
        # candidate at all — the album identified on a catalogue the archive
        # cannot be addressed by is exactly the one that carries the lie.
        elif artwork is not None and self._artwork_store is not None:
            final_folder = (
                Path(str(folder_operation.after_state["path"]))
                if folder_operation is not None
                else album_folder
            )
            operations.extend(
                self._artwork_operations(
                    unit,
                    artwork,
                    alignment,
                    album_folder=album_folder,
                    final_folder=final_folder,
                    disc_folders=disc_folders,
                    disc_count=disc_count,
                    track_paths=tuple(
                        (pending_sources[pending], _relocated(target, album_folder, final_folder))
                        for pending, target in targets.items()
                    ),
                    sequence=sequence,
                )
            )

        return ChangePlan(
            unit=unit,
            release=release,
            operations=tuple(operations),
            warnings=warnings,
            matched_tracks=len(alignment.assignments),
        )

    def _folder_name(
        self,
        unit: AlbumUnit,
        release: ReleaseMetadata,
        container_format: str,
        taken_names: frozenset[str],
    ) -> str:
        """Choose the folder's name, and the edition it does or does not carry.

        In precedence order: no pressing year is written; an edition whose
        *content* differs earns one anyway; and a name already claimed — on
        disk, or by another album planned in the same run — takes the pressing
        year to tell the two apart. The switch overrides all of it for anyone
        who wants every pressing named.

        `(Ed. 1993)` already in the folder is not a reason to write it again.
        This application writes that exact form, so a marker read from the
        folder and preserved as the user's would be its own inscription coming
        back, and could never be taken off. A word the user really wrote still
        rides along — in its own shape, through `existing_designator` below,
        which this does not touch.
        """
        # A designator already written on the folder rides along, in its own
        # shape — "(EP - 2015)". None is invented where none was written. The
        # year written beside it rides along with it, because no catalogue can
        # say which pressing a folder was named for.
        kind, kind_year = existing_designator(unit.folder_path.name, release.title)
        if self._policy.include_edition:
            return self._policy.folder_name(
                release, container_format, kind=kind, kind_year=kind_year
            )
        pressing = self._policy.pressing_edition(release)
        # A pressing whose content differs from the plain album earns its name
        # even where the folder never carried one: a Deluxe with bonus tracks is
        # a different thing on the shelf, and the name should say so.
        earned = pressing if pressing and is_distinct_edition(release) else ""
        name = self._policy.folder_name(
            release, container_format, edition=earned, kind=kind, kind_year=kind_year
        )
        if folder_name_is_free(unit.folder_path.parent / name, unit.folder_path, taken_names):
            return name
        if not pressing or pressing == earned:
            return name
        return self._policy.folder_name(
            release, container_format, edition=pressing, kind=kind, kind_year=kind_year
        )

    def _resolve_spelling(
        self, unit: AlbumUnit, release: ReleaseMetadata, alignment: TrackAlignment
    ) -> tuple[ReleaseMetadata, TrackAlignment]:
        """Return the release and the pairing, every name settled against the disk.

        Only case and accent are ever settled here; any other difference is the
        catalogue's to decide. Reading fails silently because an unreadable tag
        is a reason to trust the catalogue, not to stop.

        The pairing is returned as well as the release, because the pairing is
        what gets rendered: `desired_tags` reads `assignment.track` and so does
        `track_name`, and nothing downstream reads `release.tracks`. A title
        resolved only into the release would reach neither. The tag would keep
        its accent anyway — `preserve_existing_spelling` restores it
        independently — and the file name, which has no such net, would lose
        it, leaving one file accented in its tag and flattened in its name.
        """
        local = self._album_tags(unit)
        artists = tuple(
            replace(artist, name=resolve_spelling(local.get("albumartist"), artist.name))
            for artist in release.artists
        )
        tracks = {track.position: track for track in release.tracks}
        assignments = []
        for assignment in alignment.assignments:
            title = self._track_title(assignment.file.path)
            resolved = resolve_spelling(title, assignment.track.title)
            if resolved != assignment.track.title:
                settled = replace(assignment.track, title=resolved)
                tracks[assignment.track.position] = settled
                assignment = replace(assignment, track=settled)
            assignments.append(assignment)
        return (
            replace(
                release,
                title=resolve_spelling(local.get("album"), release.title),
                artists=artists,
                tracks=tuple(tracks[position] for position in sorted(tracks)),
            ),
            replace(alignment, assignments=tuple(assignments)),
        )

    def _album_tags(self, unit: AlbumUnit) -> dict[str, str]:
        for file in unit.audio_files:
            try:
                tags = self._tag_store.read(file.path)
            except Exception:
                continue
            found = {
                name: values[0] for name in ("album", "albumartist") if (values := tags.get(name))
            }
            if found:
                return found
        return {}

    def _track_title(self, path: Path) -> str | None:
        try:
            values = self._tag_store.read(path).get("title", ())
        except Exception:
            return None
        return values[0] if values else None

    def cover_only_plan(self, unit: AlbumUnit, staged: StagedImage) -> ChangePlan:
        """Plan the one write that puts an album's own picture beside its files.

        Absence only. A folder containing any image file is left entirely
        alone — competing with an existing picture is the real plan's job. The
        plan carries no release because it identifies nothing.
        """
        if unit.is_loose_track:
            return ChangePlan(unit=unit, release=None)
        operations: list[ChangeOperation] = []
        sequence = 0
        folders = sorted({file.path.parent for file in unit.audio_files})
        for folder in folders:
            if _holds_an_image(folder):
                continue
            sequence += 1
            name = self._policy.image_name(staged.kind, staged.extension)
            operations.append(
                ChangeOperation(
                    sequence=sequence,
                    kind=OperationKind.WRITE_IMAGE,
                    target_path=folder / name,
                    after_state=_image_state(staged),
                    before_state={},
                )
            )
        return ChangePlan(unit=unit, release=None, operations=tuple(operations))

    def chosen_cover_plan(self, unit: AlbumUnit, staged: StagedImage) -> ChangePlan:
        """Plan the cover the user picked by hand, over whatever is there.

        `cover_only_plan` fills an absence and nothing else: a folder that
        already holds an image is left entirely alone, because competing with a
        picture is not something to do unasked. This is the asked case — the
        user pointed at the file — so it replaces, and it says what it replaced
        in `before_state` so the review shows what is at stake and the revert
        can put it back.

        It reaches all three places a cover lives, because a cover that is right
        in one of them and wrong in the other two is an album showing two
        pictures: the folder's `cover.jpg`, the picture inside each track, and
        the Finder icon that is the only one macOS draws for a FLAC.
        """
        store = self._artwork_store
        if store is None:
            raise ValueError("A cover was chosen but no artwork store was injected.")
        if unit.is_loose_track:
            return ChangePlan(unit=unit, release=None)
        operations: list[ChangeOperation] = []
        sequence = 0
        name = self._policy.image_name(staged.kind, staged.extension)
        for folder in sorted({file.path.parent for file in unit.audio_files}):
            present = folder / name
            sequence += 1
            operations.append(
                ChangeOperation(
                    sequence=sequence,
                    kind=OperationKind.WRITE_IMAGE,
                    target_path=present,
                    after_state=_image_state(staged),
                    before_state=_existing_state(present, store.describe(present)),
                )
            )
        track_paths = tuple((file.path, file.path) for file in unit.audio_files)
        for source, destination in track_paths:
            sequence += 1
            operations.append(
                ChangeOperation(
                    sequence=sequence,
                    kind=OperationKind.EMBED_IMAGE,
                    target_path=destination,
                    after_state=_image_state(staged),
                    before_state=_existing_state(source, store.describe_embedded(source)),
                )
            )
        icons = self._finder_icons(
            store,
            track_paths,
            sequence,
            {destination: staged.path for _, destination in track_paths},
            replacing=True,
        )
        return ChangePlan(unit=unit, release=None, operations=tuple(operations + icons))

    def _artwork_operations(
        self,
        unit: AlbumUnit,
        artwork: Artwork,
        alignment: TrackAlignment,
        album_folder: Path,
        final_folder: Path,
        disc_folders: dict[Path, Path],
        disc_count: int,
        track_paths: tuple[tuple[Path, Path], ...],
        sequence: int,
    ) -> list[ChangeOperation]:
        """Emit the image operations an album still needs, and only those.

        Every comparison reads from where the file is *now* and writes to where
        it will be once the renames above have run: a cover already in place at
        the same size or larger produces nothing, which is what keeps a second
        run of an organized album silent.
        """
        store = self._artwork_store
        assert store is not None
        operations = self._cover_operations(
            unit,
            artwork,
            alignment,
            album_folder=album_folder,
            final_folder=final_folder,
            disc_folders=disc_folders,
            disc_count=disc_count,
            sequence=sequence,
        )
        sequence += len(operations)
        embedded = artwork.embedded
        for source, destination in track_paths:
            existing = store.describe_embedded(source)
            if embedded is not None and is_worth_embedding(existing, embedded.facts):
                after = _image_state(embedded)
            elif existing is not None and store.misdeclares_embedded(source):
                # Repairing the label on a picture, which is not the same
                # question as replacing one, and must not be answered by
                # replacing it. `describe_embedded` reads the *bytes*, so a
                # block that says `0x0` over a good JPEG compares as perfectly
                # fine and would be left exactly as it is — a cover no player
                # draws.
                #
                # What goes back in is the file's own picture, byte for byte.
                # Sending the fetched candidate instead would overwrite a
                # larger cover with a smaller one, cancelling the refusal to
                # replace a picture with a lesser one. Written back through
                # `_flac_picture`, the same bytes arrive with the fields filled
                # in from the image itself.
                after = _repair_state(existing)
            else:
                continue
            sequence += 1
            operations.append(
                ChangeOperation(
                    sequence=sequence,
                    kind=OperationKind.EMBED_IMAGE,
                    target_path=destination,
                    after_state=after,
                    before_state=_existing_state(source, existing),
                )
            )
        # Which files this run is about to give a picture to, so that one
        # arriving with none is not made to wait a whole run for its icon.
        incoming = {
            operation.target_path: Path(str(operation.after_state["source"]))
            for operation in operations
            if operation.kind is OperationKind.EMBED_IMAGE and "source" in operation.after_state
        }
        # `sequence` has already advanced once per operation appended above;
        # adding the count again would number the icons from a hole.
        return operations + self._finder_icons(store, track_paths, sequence, incoming)

    def _cover_operations(
        self,
        unit: AlbumUnit,
        artwork: Artwork,
        alignment: TrackAlignment,
        album_folder: Path,
        final_folder: Path,
        disc_folders: dict[Path, Path],
        disc_count: int,
        sequence: int,
    ) -> list[ChangeOperation]:
        """The picture beside the album, and nothing that enters an audio file.

        Its own method because the arrangement needs exactly this half of the
        artwork and none of the rest. Writing `cover.jpg` next to an album is
        not writing to the album: no byte of the audio is touched, so it
        belongs to a mode whose whole promise is that it renames and does not
        rewrite. The embed and the repair, which do enter a file, stay with the
        caller that is allowed to make them.

        The comparison is the one every other cover write makes — a cover in
        place at the same size or larger produces nothing — because a second
        rule for the same question is a second place for it to be applied by
        half.
        """
        store = self._artwork_store
        assert store is not None
        operations: list[ChangeOperation] = []
        for read_folder, write_folder in self._image_folders(
            unit, alignment, album_folder, final_folder, disc_folders, disc_count
        ):
            for image in artwork.files:
                name = self._policy.image_name(image.kind, image.extension)
                present = read_folder / name
                existing = store.describe(present)
                if not is_worth_writing(existing, image.facts):
                    continue
                sequence += 1
                operations.append(
                    ChangeOperation(
                        sequence=sequence,
                        kind=OperationKind.WRITE_IMAGE,
                        target_path=write_folder / name,
                        after_state=_image_state(image),
                        before_state=_existing_state(present, existing),
                    )
                )
        return operations

    def _finder_icons(
        self,
        store: ArtworkStore,
        track_paths: Sequence[tuple[Path, Path]],
        sequence: int,
        incoming: Mapping[Path, Path],
        replacing: bool = False,
    ) -> list[ChangeOperation]:
        """Offer the small cover the Finder draws to the files that have none.

        A third place a cover lives, and the only one the Finder shows for a
        FLAC: a custom icon, made from the picture the file already carries.

        This only ever *gives*, never replaces: a file that already draws its
        own icon is left exactly as it is, whatever picture that icon shows.
        Nothing new enters the file either — the icon is made from its own
        bytes, which is why this can be planned for an album that fetched no
        artwork at all.

        Last in the plan, after the picture each file ends up with has been
        written, so the icon is drawn from what the file will actually hold —
        including a file that had no picture at all until this very run, which
        would otherwise have had to wait for the next one to be given its icon.
        """
        operations: list[ChangeOperation] = []
        for source, destination in track_paths:
            # An icon already there is left alone, unless the picture itself is
            # being replaced. Filling an absence is what this does for an
            # ordinary run. When a cover has been chosen by hand, every file is
            # getting a new picture, and an icon still drawing the old one
            # would leave the Finder showing the picture that was replaced.
            if store.shows_its_cover_in_the_finder(source) and not replacing:
                continue
            arriving = incoming.get(destination)
            drawable = store.can_show_its_cover_in_the_finder(source) or (
                arriving is not None and store.image_can_be_drawn(arriving)
            )
            if not drawable:
                continue
            sequence += 1
            operations.append(
                ChangeOperation(
                    sequence=sequence,
                    kind=OperationKind.EMBED_IMAGE,
                    target_path=destination,
                    after_state={"icon": True},
                    before_state={},
                )
            )
        return operations

    def _image_folders(
        self,
        unit: AlbumUnit,
        alignment: TrackAlignment,
        album_folder: Path,
        final_folder: Path,
        disc_folders: dict[Path, Path],
        disc_count: int,
    ) -> tuple[tuple[Path, Path], ...]:
        """Pair every folder that should hold a cover with where to read the current one.

        A loose track has no album folder of its own, so it receives the embedded
        picture and no file: dropping ``cover.jpg`` beside it would put an
        album's artwork in a folder that is not an album.
        """
        if unit.is_loose_track:
            return ()
        if disc_count <= 1:
            return ((album_folder, final_folder),)
        existing_folders = {destination: source for source, destination in disc_folders.items()}
        canonical = sorted(
            {
                album_folder / self._policy.disc_folder_name(assignment.disc_number)
                for assignment in alignment.assignments
            }
        )
        return tuple(
            (
                existing_folders.get(folder, folder),
                _relocated(folder, album_folder, final_folder),
            )
            for folder in canonical
        )

    def _disc_folder_renames(
        self, alignment: TrackAlignment, album_folder: Path, disc_count: int
    ) -> dict[Path, Path]:
        """Map each existing disc folder to the canonical name it should carry.

        Only folders that already hold tracks appear here. A split creates its
        disc folders as files move into them, so there is nothing to rename and,
        equally, nothing left empty afterwards.
        """
        if disc_count <= 1:
            return {}
        mapping: dict[Path, Path] = {}
        for assignment in alignment.assignments:
            enclosing = assignment.file.path.parent
            if enclosing == album_folder:
                continue
            mapping[enclosing] = album_folder / self._policy.disc_folder_name(
                assignment.disc_number
            )
        return mapping

    def _folder_operation(
        self,
        unit: AlbumUnit,
        release: ReleaseMetadata,
        sequence: int,
        folder_name: str | None = None,
        taken_names: frozenset[str] = frozenset(),
        transcoded: frozenset[str] = frozenset(),
        measured_rates: Mapping[str, int] | None = None,
    ) -> ChangeOperation | None:
        if unit.is_loose_track:
            # A loose track has no folder of its own to rename; it is identified
            # and tagged where it sits, and gathering it into an album folder is
            # a separate decision the user has not been asked for.
            return None
        measurable = [file for file in unit.audio_files if file.properties is not None]
        properties = tuple(file.properties for file in measurable)
        was_lossy = tuple(file.content_signature in transcoded for file in measurable)
        label = format_label(
            properties,
            was_lossy,
            self._policy.transcoded_label,
            [(measured_rates or {}).get(file.content_signature) for file in measurable],
        )
        if folder_name is not None:
            # The user's correction is taken verbatim, sanitized only for the
            # filesystem — spelling preservation would defeat the point of a
            # deliberate rename.
            #
            # Except the format segment, which is a measurement wearing
            # brackets. The corrected name is usually one this application
            # wrote, so taking every character of it as the user's word would
            # freeze this application's own words too: the folder would stop
            # being named from any verdict, and clearing the album's last
            # lossy track would leave the folder still claiming one.
            name = sanitize_component(
                replace_format_segment(folder_name, label, self._policy.transcoded_label)
            )
        else:
            name = resolve_spelling(
                unit.folder_path.name,
                self._folder_name(unit, release, label, taken_names),
            )
        destination = unit.folder_path.parent / name
        if destination == unit.folder_path:
            return None
        return ChangeOperation(
            sequence=sequence,
            kind=OperationKind.RENAME_FOLDER,
            target_path=unit.folder_path,
            after_state={"path": str(destination)},
            before_state={"path": str(unit.folder_path)},
        )


def folder_name_is_free(destination: Path, source: Path, taken_names: frozenset[str]) -> bool:
    """Report whether a folder name is available to this album.

    A name that differs from the folder's own only in case *exists* on a Mac,
    because the disk is case-insensitive, and what it finds is the album
    itself; refusing it would send `Songs Of The Sea` to a pressing year
    instead of to `Songs of the Sea`.
    """
    if destination.name in taken_names:
        return False
    return not destination.exists() or destination == source or _same_entry(destination, source)


def _same_entry(one: Path, other: Path) -> bool:
    try:
        return os.path.samefile(one, other)
    except OSError:
        return False


def _named(
    release: ReleaseMetadata, alignment: TrackAlignment, user_titles: frozenset[int]
) -> tuple[ReleaseMetadata, TrackAlignment]:
    """Return the release and its pairing with the catalogue's names in one case.

    The pairing is normalized with the release because it is what every name
    and tag is drawn from; normalizing the release alone reaches none of them.
    """
    named = normalize_release(release, user_titles)
    assignments = tuple(
        replace(
            assignment,
            track=normalize_track(
                assignment.track, release, title_from_user=assignment.track.position in user_titles
            ),
        )
        for assignment in alignment.assignments
    )
    return named, replace(alignment, assignments=assignments)


_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})


def _holds_an_image(folder: Path) -> bool:
    """Report whether a folder already holds any image file at all."""
    try:
        return any(
            entry.suffix.lower() in _IMAGE_SUFFIXES and not entry.name.startswith(".")
            for entry in folder.iterdir()
            if entry.is_file()
        )
    except OSError:
        return True


def _what_did_not_pair(alignment: TrackAlignment) -> tuple[str, ...]:
    """What this alignment left over, said once and in one wording.

    A blocked plan and a partial plan describe the same facts about the same
    alignment, so both take their sentences from here: two wordings of one
    fact drift apart, and a screen that shows both repeats itself.

    One wording, and it is the short one: the names are the plan's own table,
    which draws every one of these on a row with a mark and a gesture.
    """
    said: list[str] = []
    if (doubt := _doubtful_pairings(alignment)) is not None:
        said.append(doubt)
    # The unmatched files are not counted here at all. Files that paired with
    # nothing and release tracks that paired with nothing are usually two views
    # of one failure, and only the tracks are stated. Nothing is lost by it,
    # because every unmatched file has a row of its own in the table below,
    # where it can be answered.
    if alignment.unmatched_tracks:
        said.append(f"{_tracks_that(len(alignment.unmatched_tracks))} no file.")
    return tuple(said)


def _tracks_that(count: int) -> str:
    """`1 release track has` / `3 release tracks have`, agreeing with the count.

    A release with one more track than the folder has files would otherwise
    read `1 release tracks have no file`. The wording is in one place so that
    every caller gets the singular right.
    """
    return f"{count} release track has" if count == 1 else f"{count} release tracks have"


def _partial_warnings(alignment: TrackAlignment) -> tuple[str, ...]:
    """Say what a partial plan is deliberately leaving behind.

    Everything it leaves behind, including the doubt. An ambiguous alignment
    can still be partial — what is certain is applied and what is doubtful is
    left exactly as it was — so the doubt is stated here too. Otherwise the
    window would have to fetch it from the blocked plan and show both lists at
    once, which states the same alignment twice.
    """
    return _what_did_not_pair(alignment)


def _named_briefly(names: Sequence[str], limit: int = 3) -> str:
    """Name a few and count the rest.

    A refusal that enumerates every file is a wall rather than a sentence: a
    folder of dozens of files matched against a two-track single would print
    every one of their names. The count is what carries the scale; the names
    are there so the eye has somewhere to start.
    """
    ordered = sorted(names)
    shown = ", ".join(ordered[:limit])
    rest = len(ordered) - limit
    return f"{shown}, and {rest} more" if rest > 0 else shown


def _doubtful_pairings(alignment: TrackAlignment) -> str | None:
    """Name the files that fit more than one track, or ``None`` when none do.

    Said as it happens: the tracks need not share a length, they only need to
    sit close enough together that a file fits both — and nothing else settled
    which.

    A single doubtful file is named, because on a long album "a file fits more
    than one release track" leaves the reader to find which one.

    One function because two callers need this sentence and there must be one of
    it: a blocked plan says it as a refusal, and a partial plan says it as what
    is being left alone. Two copies would drift.
    """
    if not alignment.is_ambiguous:
        return None
    doubtful = alignment.suggested
    if not doubtful:
        return "A file fits more than one release track and its title does not say which."
    if len(doubtful) == 1:
        name = doubtful[0].file.path.name
        return f"{name} fits more than one release track and its title does not say which."
    return (
        f"{len(doubtful)} files fit more than one release track and their titles "
        "do not say which."
    )


def _alignment_blockers(alignment: TrackAlignment) -> tuple[str, ...]:
    """Why a plan cannot run in full — counted, and never enumerated.

    The names are the table. Listing the files and tracks here would repeat,
    directly above it, a table that draws every one of them on a row of its own
    with a mark and a gesture, and put paragraphs of highlighted text before
    the plan.

    A count says the shape of the problem, which is what a person reads before
    deciding whether to look at all; the rows say which ones, where they can be
    answered.
    """
    if alignment.is_usable:
        return ()
    return _what_did_not_pair(alignment) or (
        "The files could not be matched to the release tracks.",
    )


def _tags_differ(current: Mapping[str, tuple[str, ...]], desired: Mapping[str, object]) -> bool:
    """Report whether writing would change anything the project is responsible for.

    Only the fields this project writes are compared, so a comment or a rating
    left by another tool never makes an album look out of date.
    """
    return any(
        tuple(current.get(name, ())) != tuple(values)  # type: ignore[arg-type]
        for name, values in desired.items()
    )


_PARENTHETICAL = re.compile(r"\(([^()]+)\)")
"""One bracketed aside in a name: `(feat. Some Artist)`, `(Somebody remix)`."""


def _with_kept_decorations(title: str, local_stem: str) -> str:
    """Keep the asides the file's own name carries and the catalogue leaves out.

    A file named `01 - Title (feat. Some Artist).flac` would be renamed to
    `01. Title.flac` when the catalogue publishes the bare title, and the
    feature credit, the remix and the version are exactly what must not be
    lost. What the catalogue already says is not repeated, and the asides are
    appended in the order the file had them.

    Only parentheses, and only whole ones. A name is not a place to invent, so
    this copies what is already written rather than composing anything.

    Compared without punctuation, word by word. The file name is often this
    application's own writing, and writing it turns the title's
    `(feat: Some Artist)` into `(feat - Some Artist)`, because a colon is not
    stored in a name; read back with its punctuation, that aside would match
    nothing and be appended to the title that already carries it. Punctuation
    is exactly what the writing changes, so it is what the comparison sets
    aside. Words stay whole, so a `(Mix)` in a title is not taken for part of
    `Remixed`.
    """
    published = f" {_words_only(title)} "
    kept = [
        f"({aside.strip()})"
        for aside in _PARENTHETICAL.findall(local_stem)
        if _words_only(aside) and f" {_words_only(aside)} " not in published
    ]
    return " ".join([title, *kept]) if kept else title


def _words_only(text: str) -> str:
    """A text's words, without accent, case or anything between them but one space."""
    folded = fold_accents(text).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in folded).split())


def _one_name_on_disk(path: Path) -> str:
    """Return the form of a path that decides whether two of them are one file.

    Composed first, then case-folded. NFC because the two spellings of an
    accented character are the same character and only one of them is what a
    Mac stores; `casefold` rather than `lower` because it is the aggressive one
    — it is what turns `ß` into `ss` — and this is a question about collision
    rather than about display.
    """
    return unicodedata.normalize("NFC", str(path)).casefold()


def _moved(path: Path, disc_folders: dict[Path, Path]) -> Path:
    """Return where a file will be once its disc folder has been renamed."""
    destination = disc_folders.get(path.parent)
    return destination / path.name if destination is not None else path


def _relocated(path: Path, album_folder: Path, final_folder: Path) -> Path:
    """Return where a path inside the album ends up once the album folder is renamed."""
    if final_folder == album_folder:
        return path
    return final_folder / path.relative_to(album_folder)


def _image_state(image: StagedImage) -> dict[str, object]:
    """Describe a staged image as the JSON a change operation can carry."""
    return {
        "source": str(image.path),
        "kind": str(image.kind),
        "digest": image.facts.digest,
        "media_type": image.facts.media_type,
        "width": image.facts.width,
        "height": image.facts.height,
    }


def _repair_state(facts: ImageFacts) -> dict[str, object]:
    """Describe a repair: this file's own picture, relabelled and not replaced.

    No ``source``, because there is no staged image to take bytes from — the
    bytes are already in the file. The executor reads ``repair`` and sends back
    what it just backed up, so what the operation promises and what it does are
    the same picture.
    """
    return {
        "repair": True,
        "kind": str(ImageKind.FRONT),
        "digest": facts.digest,
        "media_type": facts.media_type,
        "width": facts.width,
        "height": facts.height,
    }


def _existing_state(path: Path, facts: ImageFacts | None) -> dict[str, object]:
    """Describe the image being replaced, so a review can show what is at stake."""
    if facts is None:
        return {}
    return {
        "path": str(path),
        "digest": facts.digest,
        "media_type": facts.media_type,
        "width": facts.width,
        "height": facts.height,
    }


def _track_artist(assignment: object) -> str | None:
    """The credit as it should be written into this track's file name.

    A credit the user typed wins whole and unaltered, commas and all: joining
    split artists back together would be this application rewriting what was
    typed. Otherwise the source's artists are joined the way that source joins
    them, which is what a source that publishes several of them means.
    """
    track = getattr(assignment, "track", None)
    credit = getattr(track, "artist_credit", None)
    if isinstance(credit, str) and credit.strip():
        return credit
    artists = getattr(track, "artists", ())
    return written_credit(artists) if artists else None
