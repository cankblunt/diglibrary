"""Walking a directory and grouping what is found into album units."""

import logging
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from diglibrary.library.audio import AUDIO_EXTENSIONS, AudioProbe
from diglibrary.library.audio_key import audio_key
from diglibrary.library.containers import names_the_folder
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.library.signature import content_signature, unit_signature

_AUDIO_BESIDE_A_DISC = (
    "This folder holds audio of its own beside a disc folder, so whether the loose "
    "files belong to that album or to another one is not something a scan can tell."
)
_DISC_WITHOUT_ALBUM = (
    "This is a disc of an album, and the folder that would name that album is not "
    "inside this scan."
)
_THE_SAME_ALBUM_MORE_THAN_ONCE = (
    "This folder holds {files} files that are only {tracks} tracks, each of them "
    "present {times} times, so which copy of the album to keep is not something a "
    "scan can tell."
)
"""Why a folder's shape sends it to review instead of being planned.

Neither merging nor splitting an ambiguous layout is neutral: merging can pull a
stray file into an album it does not belong to, and splitting leaves two halves
that both want one name. The application does neither and asks instead.
"""

DISC_FOLDER_PATTERN = re.compile(r"^(?:cd|disc|disk|disco)\s*[-_]?\s*(\d{1,2})$", re.IGNORECASE)
"""Folder names that denote a disc of a larger album, never an album of its own.

``Vol`` is deliberately absent: a volume is a separate release, not a disc.
"""


def the_same_album_more_than_once(audio_files: tuple[AudioFileFacts, ...]) -> str:
    """Report a folder that holds every one of its tracks more than once.

    A folder may hold one album twice — one set named `03 Song.flac` and one
    named `03. Artist - Song.flac`, the same audio to the sample, differing
    only by an embedded picture. Read as an album of twice the tracks it
    matches no release of that record, so the search falls through to some
    much larger release at a low score, and the aligner then has to give each
    file a *distinct* track: the second copy of every song lands on whatever
    leftover is nearest in length, and some of those pairs are firm enough to
    be written.

    **Asked of `audio_key`, not of `content_signature`.** The signature is a
    digest of five declared numbers and is blind to the bytes on purpose, so
    two different recordings of the same length in the same format wear one.
    Keying this on the signature would call albums holding such recordings
    duplicates. `audio_key` answers exactly: a FLAC's own md5 of its decoded
    samples where the encoder wrote one, a window past the tags otherwise, and
    the scanner already reads it for every file it lists.

    Written as *every* track repeating *the same* number of times, which is what
    a folder holding N copies looks like. One repeated track is a collision or a
    stray, and neither is this. A folder of a single track repeated is left out
    too: with nothing else to agree with it there is no album shape to see, and
    guessing there would cost more than the miss.
    """
    copies = Counter(file.audio_key or file.content_signature for file in audio_files)
    if len(copies) < 2:
        return ""
    how_many = set(copies.values())
    if len(how_many) != 1:
        return ""
    times = how_many.pop()
    if times < 2:
        return ""
    return _THE_SAME_ALBUM_MORE_THAN_ONCE.format(
        files=len(audio_files), tracks=len(copies), times=times
    )


class LibraryScanner:
    """Purpose: report what audio exists under a root, grouped into album units.

    Responsibilities: walk the tree, read stream properties, compute content and
    unit signatures, join disc subfolders into one album, and record the
    non-audio files that belong to each folder. Boundaries: it reads nothing but
    the filesystem — no tag, no network, no database — and it never renames,
    writes, or deletes anything. Dependencies: an injected ``AudioProbe``.
    Collaborators: the matcher and the change planner. Constraints: a folder
    that directly contains audio is one unit only when that audio is most of
    what the folder holds; a folder that mostly holds other albums is
    a container, and its own files become one unit per file. The scan root is
    judged by that same rule and nothing else: a downloads folder
    shelves albums and is therefore not one, while the folder of a single album
    is an album even when it is the folder you pointed at.
    """

    def __init__(
        self,
        probe: AudioProbe,
        logger: logging.Logger,
        audio_extensions: Iterable[str] = AUDIO_EXTENSIONS,
    ) -> None:
        """Create a scanner for one set of recognized audio extensions."""
        self._probe = probe
        self._logger = logger
        self._audio_extensions = frozenset(extension.lower() for extension in audio_extensions)

    def scan(self, root: Path, excluded: Iterable[Path] = ()) -> tuple[AlbumUnit, ...]:
        """Return every album unit under ``root``, in a deterministic order.

        ``excluded`` prunes whole subtrees during the walk: an
        excluded folder is never read, never identified, and never planned —
        exclusion is absence, not a filter applied afterwards.
        """
        if not root.is_dir():
            raise NotADirectoryError(f"Scan root is not a directory: {root}")
        resolved_root = root.resolve()
        excluded_set = {path.resolve() for path in excluded}
        audio_by_folder: dict[Path, list[Path]] = {}
        companions_by_folder: dict[Path, list[Path]] = {}

        walk = os.walk(resolved_root, onerror=self._walk_error)
        for directory, subdirectories, filenames in walk:
            folder = Path(directory)
            subdirectories[:] = sorted(
                name
                for name in subdirectories
                if not name.startswith(".") and (folder / name) not in excluded_set
            )
            audio_paths, companion_paths = self._partition(folder, filenames)
            companions_by_folder[folder] = companion_paths
            if audio_paths:
                audio_by_folder[folder] = audio_paths

        folder_units, container_paths = self._folder_units(
            audio_by_folder, companions_by_folder, resolved_root
        )
        units = self._loose_units(sorted(container_paths))
        units.extend(folder_units)
        self._logger.info(
            "Library scan completed.",
            extra={"operation": "library.scan", "album_units": len(units)},
        )
        return tuple(sorted(units, key=lambda unit: str(unit.folder_path)))

    def _folder_units(
        self,
        audio_by_folder: dict[Path, list[Path]],
        companions_by_folder: dict[Path, list[Path]],
        resolved_root: Path,
    ) -> tuple[list[AlbumUnit], list[Path]]:
        """Return the album units, and the files of folders that are containers.

        A container's own files are handed back rather than turned into a unit,
        so that the caller identifies them as loose tracks — the folder keeps
        its name because it is not an album.
        """
        discs_by_album: dict[Path, list[Path]] = defaultdict(list)
        single_folders: list[Path] = []
        ambiguous: dict[Path, str] = {}
        for folder in audio_by_folder:
            parent = folder.parent
            if DISC_FOLDER_PATTERN.fullmatch(folder.name) is None:
                single_folders.append(folder)
                continue
            if folder == resolved_root:
                # The album this disc belongs to is above the scan root, and the
                # scan may not reach outside what it was pointed at.
                ambiguous[folder] = _DISC_WITHOUT_ALBUM
                single_folders.append(folder)
            elif parent in audio_by_folder:
                # Audio beside a disc folder: is the loose audio part of this
                # album, or is it another album? Merging silently is the one
                # thing this scanner will not do, and splitting leaves two
                # halves that both want one name, which is a collision at apply
                # time. So neither: both halves go to review.
                ambiguous[folder] = _AUDIO_BESIDE_A_DISC
                ambiguous[parent] = _AUDIO_BESIDE_A_DISC
                single_folders.append(folder)
            else:
                discs_by_album[parent].append(folder)

        below = _audio_below(audio_by_folder)
        units: list[AlbumUnit] = []
        container_paths: list[Path] = []
        for folder in single_folders:
            own_paths = audio_by_folder[folder]
            if folder not in ambiguous and not self._is_an_album(
                folder, len(own_paths), below.get(folder, 0)
            ):
                container_paths.extend(own_paths)
                continue
            unit = self._album_unit(
                folder,
                own_paths,
                companions_by_folder.get(folder, []),
                ambiguous.get(folder, ""),
            )
            if unit is not None:
                units.append(unit)
        for album_folder, discs in discs_by_album.items():
            own = sum(len(audio_by_folder[disc]) for disc in discs)
            foreign = below.get(album_folder, 0) - own
            if not self._is_an_album(album_folder, own, foreign):
                # The folder around these discs holds other albums, so it is not
                # their album — and a folder named `CD2` is a disc of something
                # this scan cannot name. Each disc stands alone, in review, and
                # the container keeps its name.
                for disc in sorted(discs):
                    orphan = self._album_unit(
                        disc,
                        audio_by_folder[disc],
                        companions_by_folder.get(disc, []),
                        _DISC_WITHOUT_ALBUM,
                    )
                    if orphan is not None:
                        units.append(orphan)
                continue
            unit = self._multi_disc_unit(
                album_folder, sorted(discs), audio_by_folder, companions_by_folder
            )
            if unit is not None:
                units.append(unit)
        return units, container_paths

    def _is_an_album(self, folder: Path, own: int, foreign: int) -> bool:
        """Report whether ``folder`` is an album, and say so when it is not."""
        if names_the_folder(own, foreign):
            return True
        self._logger.info(
            "Folder holds mostly other albums; treated as a container, not an album.",
            extra={
                "operation": "library.scan.container",
                "own_tracks": own,
                "foreign_tracks": foreign,
            },
        )
        return False

    def _partition(self, folder: Path, filenames: list[str]) -> tuple[list[Path], list[Path]]:
        audio_paths: list[Path] = []
        companion_paths: list[Path] = []
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = folder / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
            except OSError as error:
                self._unreadable_file(path, error)
                continue
            if path.suffix.lower() in self._audio_extensions:
                audio_paths.append(path)
            else:
                companion_paths.append(path)
        return audio_paths, companion_paths

    def _album_unit(
        self,
        folder: Path,
        audio_paths: list[Path],
        companion_paths: list[Path],
        ambiguous_layout: str = "",
    ) -> AlbumUnit | None:
        audio_files = tuple(facts for path in audio_paths if (facts := self._facts(path)))
        if not audio_files:
            return None
        ambiguous_layout = ambiguous_layout or the_same_album_more_than_once(audio_files)
        if ambiguous_layout:
            self._logger.info(
                "Folder shape leaves it unclear which album this is; sent to review.",
                extra={"operation": "library.scan.ambiguous", "reason": ambiguous_layout},
            )
        return AlbumUnit(
            folder_path=folder,
            unit_signature=unit_signature(file.content_signature for file in audio_files),
            audio_files=audio_files,
            companion_files=tuple(companion_paths),
            ambiguous_layout=ambiguous_layout,
        )

    def _multi_disc_unit(
        self,
        album_folder: Path,
        disc_folders: list[Path],
        audio_by_folder: dict[Path, list[Path]],
        companions_by_folder: dict[Path, list[Path]],
    ) -> AlbumUnit | None:
        """Join disc subfolders into the one album they belong to.

        The disc number comes from the folder name, so it is a hint like every
        other name — it orders the files for later numbering, and identification
        still decides what the release actually is.
        """
        audio_files: list[AudioFileFacts] = []
        companion_files = list(companions_by_folder.get(album_folder, []))
        for disc_folder in disc_folders:
            match = DISC_FOLDER_PATTERN.fullmatch(disc_folder.name)
            disc_number = int(match.group(1)) if match else None
            audio_files.extend(
                facts
                for path in audio_by_folder[disc_folder]
                if (facts := self._facts(path, disc_hint=disc_number))
            )
            companion_files.extend(companions_by_folder.get(disc_folder, []))
        if not audio_files:
            return None
        return AlbumUnit(
            folder_path=album_folder,
            unit_signature=unit_signature(file.content_signature for file in audio_files),
            audio_files=tuple(audio_files),
            companion_files=tuple(companion_files),
            disc_count=len(disc_folders),
            ambiguous_layout=the_same_album_more_than_once(tuple(audio_files)),
        )

    def _loose_units(self, audio_paths: list[Path]) -> list[AlbumUnit]:
        units: list[AlbumUnit] = []
        for path in audio_paths:
            facts = self._facts(path)
            if facts is None:
                continue
            units.append(
                AlbumUnit(
                    folder_path=path,
                    unit_signature=unit_signature([facts.content_signature]),
                    audio_files=(facts,),
                    is_loose_track=True,
                )
            )
        return units

    def _unreadable_file(self, path: Path, error: OSError) -> None:
        """Say which file was skipped, so a missing track is never a silent one."""
        self._logger.warning(
            "An audio file could not be read and was skipped.",
            extra={
                "operation": "library.scan.unreadable_file",
                "file": str(path),
                "error": str(error),
            },
        )

    def _walk_error(self, error: OSError) -> None:
        """Say that a folder could not be read, instead of letting it disappear.

        ``os.walk`` swallows these by default, so an unreadable folder would
        simply be absent from the scan with nothing said — and a large library
        is measured by counts nobody can verify by hand.
        """
        self._logger.warning(
            "A folder could not be read and was skipped.",
            extra={
                "operation": "library.scan.unreadable_folder",
                "folder": str(getattr(error, "filename", "")),
                "error": str(error),
            },
        )

    def _facts(self, path: Path, disc_hint: int | None = None) -> AudioFileFacts | None:
        """Describe one file, or ``None`` when the disk will not give it up.

        A file that vanishes or refuses to be read mid-scan must not end the
        whole scan. It is skipped, and named in the log so the loss is visible
        rather than assumed.
        """
        try:
            stats = path.stat()
        except OSError as error:
            self._unreadable_file(path, error)
            return None
        properties = self._probe.read(path)
        if properties is None:
            self._logger.warning(
                "Audio stream could not be read; falling back to file size.",
                extra={"operation": "library.scan.unreadable"},
            )
        return AudioFileFacts(
            path=path,
            content_signature=content_signature(properties, stats.st_size),
            file_size_bytes=stats.st_size,
            modified_at=datetime.fromtimestamp(stats.st_mtime, UTC),
            properties=properties,
            disc_hint=disc_hint,
            # What the signature above cannot say: which recording this is.
            # Taken here because the file is already being read and the key is
            # cheap: a header read for a FLAC that declares the md5 of its own
            # samples, one seek and one window otherwise. A file that will not
            # answer gives `None`, which is an ordinary answer and not a
            # failure.
            audio_key=audio_key(path),
        )


def _audio_below(audio_by_folder: dict[Path, list[Path]]) -> dict[Path, int]:
    """Count, for every folder, the audio that sits in folders beneath it.

    Built by walking each folder's ancestors once rather than by comparing every
    folder against every other, because a real library holds thousands of them.
    Only folders the scan actually read are counted, so an excluded subtree
    is absent here exactly as it is absent everywhere else.
    """
    below: dict[Path, int] = defaultdict(int)
    for folder, paths in audio_by_folder.items():
        for ancestor in folder.parents:
            below[ancestor] += len(paths)
    return below
