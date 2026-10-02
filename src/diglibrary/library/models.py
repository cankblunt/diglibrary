"""Immutable descriptions of what a scan found on disk."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from diglibrary.library.audio import AudioProperties


@dataclass(frozen=True, slots=True)
class AudioFileFacts:
    """Purpose: describe one audio file exactly as found, before any judgement.

    Responsibilities: pair a path with the facts needed to recognize the file
    again and to feed identification. Boundaries: it holds no tag value, no
    release, and no proposed change — it is observation, not interpretation.
    Dependencies: ``AudioProperties``. Collaborators: ``AlbumUnit``, the
    scanner, and the future matcher. Constraints: ``properties`` is ``None``
    when the stream could not be parsed, which is a fact worth recording rather
    than an error worth raising, and ``audio_key`` is ``None`` when the audio
    cannot be named — the content signature describes a declared shape, and two
    different recordings can wear one.
    """

    path: Path
    content_signature: str
    file_size_bytes: int
    modified_at: datetime
    properties: AudioProperties | None = None
    disc_hint: int | None = None
    audio_key: str | None = None


@dataclass(frozen=True, slots=True)
class AlbumUnit:
    """Purpose: group the files a scan believes belong to one album.

    Responsibilities: carry the folder, its audio files, the non-audio files
    that travel with them, and a signature for the group. Boundaries: it is
    what the user *has*, never what the release *is* — no title, artist, or
    year appears here, because those are decided by identification against a
    trusted source. Dependencies: ``AudioFileFacts``. Collaborators:
    the scanner, the matcher, and the change planner. Constraints: companion
    files are recorded so they move with the folder and are never discarded.
    """

    folder_path: Path
    unit_signature: str
    audio_files: tuple[AudioFileFacts, ...]
    companion_files: tuple[Path, ...] = ()
    is_loose_track: bool = False
    disc_count: int = 1
    ambiguous_layout: str = ""
    """Why this folder's shape leaves it unclear which album it is, if it does.

    Empty for every ordinary album. When set, the text says what the scanner
    saw, and the album is identified but never planned: an ambiguous layout
    goes to review rather than being merged, split, or renamed on a guess.
    """

    @property
    def track_durations_ms(self) -> tuple[int, ...] | None:
        """Return every track duration, or ``None`` if any file could not be parsed.

        Durations are the matcher's primary evidence, so a partial list is
        withheld rather than scored — an incomplete sequence would look like a
        poor match against the correct release.
        """
        durations = [
            file.properties.duration_ms for file in self.audio_files if file.properties is not None
        ]
        if len(durations) != len(self.audio_files):
            return None
        return tuple(durations)

    @property
    def track_count(self) -> int:
        """Return how many audio files this unit holds."""
        return len(self.audio_files)

    @property
    def total_duration_ms(self) -> int | None:
        """Return the summed duration, or ``None`` if any file could not be parsed."""
        durations = [
            file.properties.duration_ms for file in self.audio_files if file.properties is not None
        ]
        if len(durations) != len(self.audio_files):
            return None
        return sum(durations)
