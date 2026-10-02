"""The match badge's numbers, counted both ways and said by half."""

from datetime import UTC, datetime
from pathlib import Path

from diglibrary.application.api import _match_counts
from diglibrary.application.contracts import TrackMetadata
from diglibrary.application.identification import Decision, IdentificationOutcome
from diglibrary.library.audio import AudioProperties
from diglibrary.library.hints import SearchHints
from diglibrary.library.matching import TrackAlignment, TrackAssignment
from diglibrary.library.models import AlbumUnit, AudioFileFacts


def _file(name: str, duration_ms: int) -> AudioFileFacts:
    return AudioFileFacts(
        path=Path("/music/Album") / name,
        content_signature=f"signature-{name}",
        file_size_bytes=1_000,
        modified_at=datetime(2001, 2, 3, tzinfo=UTC),
        properties=AudioProperties(
            codec="flac", duration_ms=duration_ms, sample_rate=44_100, channels=2
        ),
    )


def _outcome(alignment: TrackAlignment | None) -> IdentificationOutcome:
    unit = AlbumUnit(
        folder_path=Path("/music/Album"), unit_signature="unit", audio_files=(_file("01.flac", 1),)
    )
    return IdentificationOutcome(
        unit=unit, hints=SearchHints(), decision=Decision.REVIEW, alignment=alignment
    )


def test_the_total_is_the_sum_of_both_open_halves_and_each_half_is_said() -> None:
    """Ten paired, one file loose, two tracks loose: the badge reads `10 of 13`.

    Thirteen is neither the number of files nor the number of tracks, and the
    sum alone cannot say so. The two halves are returned beside it.
    """
    files = tuple(_file(f"{index:02d}.flac", 200_000) for index in range(1, 12))
    tracks = tuple(
        TrackMetadata(title=f"Track {index}", position=index, duration_ms=200_000)
        for index in range(1, 13)
    )
    paired = tuple(
        TrackAssignment(
            file=file,
            track=track,
            disc_number=1,
            track_number=track.position or 0,
            duration_delta_ms=0,
        )
        for file, track in zip(files[:10], tracks[:10], strict=False)
    )
    alignment = TrackAlignment(
        assignments=paired,
        method="duration",
        unmatched_files=files[10:],
        unmatched_tracks=tracks[10:],
    )

    assert _match_counts(_outcome(alignment)) == (10, 13, 1, 2)


def test_nothing_measured_is_four_zeros() -> None:
    assert _match_counts(_outcome(None)) == (0, 0, 0, 0)
