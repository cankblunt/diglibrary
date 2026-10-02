"""Tests for how much a verdict is worth, before anyone acts on it."""

import pytest

from diglibrary.quality.confidence import Sureness, judge_confidence
from diglibrary.quality.models import Encoding
from diglibrary.quality.verdict import AlbumVerdict, TrackVerdict


def test_a_whole_album_measured_far_from_any_line_is_strong() -> None:
    """The ordinary case has to read as ordinary, or the word means nothing."""
    confidence = judge_confidence(_album(decay=6.1, ceiling=-80.1), tracks=12, analyzed=12)

    assert confidence.sureness is Sureness.STRONG
    assert confidence.reasons == ()
    assert confidence.coverage == 1.0


def test_a_measurement_shared_with_another_recording_is_weak() -> None:
    """This is where the collision surfaces, and it outranks the other reasons.

    The other two say the evidence about this album is thin. This one says the
    evidence may be about a different album: two recordings of one length in one
    format share a signature, and one can read the other's verdict.
    """
    confidence = judge_confidence(
        _album(decay=6.1, ceiling=-80.1), tracks=12, analyzed=12, shared=3
    )

    assert confidence.sureness is Sureness.WEAK
    assert confidence.shared == 3
    assert "more than one" in confidence.reasons[0]
    # And it names the way out: measuring the file on its own.
    assert "in depth" in confidence.reasons[0]


def test_a_borrowed_measurement_inside_a_lossy_container_only_doubts_the_bitrate() -> None:
    """An MP3 is an MP3 because its container says so, and nothing borrowed changes that.

    Without this split the word is useless: most albums of a real library read
    weak, and most of those are MP3 albums whose verdict no measurement could
    move. With it, weak is left to the lossless albums where the question is
    real.
    """
    verdict = _album(decay=22.0, ceiling=-117.0, encoding=Encoding.LOSSY)

    confidence = judge_confidence(verdict, tracks=12, analyzed=12, shared=9)

    assert confidence.sureness is Sureness.FAIR
    assert "bitrate" in confidence.reasons[0]


def test_a_verdict_from_two_of_fourteen_tracks_says_so() -> None:
    """A median of a fifth of an album is a statement about that fifth."""
    confidence = judge_confidence(_album(decay=6.1, ceiling=-80.1), tracks=14, analyzed=2)

    assert confidence.sureness is Sureness.WEAK
    assert "2 of 14 tracks" in confidence.reasons[0]


def test_an_album_nothing_could_be_measured_of_claims_nothing() -> None:
    """Unmeasured is not the same as fine, and must never read as fine."""
    confidence = judge_confidence(_album(decay=0.0, ceiling=0.0), tracks=9, analyzed=0)

    assert confidence.sureness is Sureness.WEAK
    assert confidence.margin_db is None


def test_an_album_a_hair_from_the_line_is_only_fair() -> None:
    """The line is calibrated on a small sample and may be recalibrated.

    An album this close is one that recalibration would most likely move, so the
    screen says so before anyone acts on it.
    """
    confidence = judge_confidence(_album(decay=13.5, ceiling=-89.0), tracks=10, analyzed=10)

    assert confidence.sureness is Sureness.FAIR
    assert confidence.margin_db is not None
    assert confidence.margin_db < 3.0


def test_a_convicted_album_is_only_as_safe_as_its_weaker_sign() -> None:
    """Both signs must be present to convict, so the nearer one is the margin."""
    confidence = judge_confidence(_album(decay=30.0, ceiling=-88.4), tracks=10, analyzed=10)

    assert confidence.margin_db == pytest.approx(0.4)
    assert confidence.sureness is Sureness.FAIR


def test_an_acquitted_album_is_as_safe_as_the_sign_that_saved_it() -> None:
    """Either sign failing is an acquittal, so the margin is the one furthest short."""
    confidence = judge_confidence(_album(decay=6.1, ceiling=-95.0), tracks=10, analyzed=10)

    # The ceiling is well inside the convicting range; the fall is what saved it,
    # by 8.9 dB, and that is the distance to report.
    assert confidence.margin_db == pytest.approx(8.9)
    assert confidence.sureness is Sureness.STRONG


def test_a_wall_reports_no_margin_because_decibels_did_not_decide() -> None:
    """Nothing but an encoder silences 16 to 19 kHz, so there is no line to be near."""
    verdict = _album(decay=3.7, ceiling=-105.7, encoding=Encoding.TRANSCODED)

    confidence = judge_confidence(verdict, tracks=10, analyzed=10)

    assert confidence.margin_db is None
    assert confidence.sureness is Sureness.STRONG


def test_an_album_that_is_not_of_one_mind_says_how_many_disagree() -> None:
    """A median summarises a folder poorly when its files came from several places."""
    tracks = tuple(
        TrackVerdict(encoding=encoding, reason="")
        for encoding in ([Encoding.TRANSCODED] * 8 + [Encoding.LOSSLESS] * 10)
    )
    verdict = _album(decay=6.1, ceiling=-80.1, tracks=tracks)

    confidence = judge_confidence(verdict, tracks=18, analyzed=18)

    assert confidence.sureness is Sureness.FAIR
    assert confidence.dissenting == 8
    assert "8 of 18" in confidence.reasons[0]


def _album(
    decay: float,
    ceiling: float,
    encoding: Encoding = Encoding.LOSSLESS,
    tracks: tuple[TrackVerdict, ...] = (),
) -> AlbumVerdict:
    return AlbumVerdict(
        encoding=encoding,
        reason="measured",
        tracks=tracks or (TrackVerdict(encoding=encoding, reason=""),),
        median_drop_db=decay,
        median_ceiling_db=ceiling,
    )
