"""How much a verdict is worth, said in a word and backed by its numbers.

A verdict the user cannot audit is one they cannot disagree with, and
disagreeing is the point. The screen already shows what was measured;
this answers the question that sits above it — *how sure is this?* — from three
facts that are all failures of evidence rather than of the rule:

- how much of the album could be measured at all;
- how far it sits from the verdict flipping;
- and whether the measurement it is reading was even taken from these files.

Nothing here is stored. It is derived on every read, for the same reason a
stored verdict is never trusted across a rule change: the moment a threshold
moves, a confidence written under the old one is a number nobody measured.
"""

from dataclasses import dataclass
from enum import StrEnum

from diglibrary.quality.models import Encoding
from diglibrary.quality.verdict import ALBUM_CEILING_DB, ALBUM_DECAY_DB, AlbumVerdict

NARROW_MARGIN_DB = 3.0
"""Closer than this to the line, and the verdict is one recalibration from moving.

The rule is calibrated on a limited set of albums and is recalibrated as
evidence grows. An album sitting inside this band is one whose answer that
recalibration would most likely change.
"""

THIN_COVERAGE = 0.5
"""Below this much of an album measured, the median is about the half that was.

A verdict that came from two of fourteen tracks has to say so, which is why
the survey carries `analyzed` beside `tracks`.
"""

DISSENT_SHARE = 1 / 3
"""Above this share of tracks disagreeing, the album is not of one mind.

An album is judged by its median, and a median is a poor summary of a folder
whose tracks came from several places: a folder can hold a large minority of
transcodes among honest files.
"""


class Sureness(StrEnum):
    """How much weight a verdict carries, before anyone acts on it."""

    STRONG = "strong"
    """Everything was measured, from these files, and nothing is near a line."""

    FAIR = "fair"
    """Sound, with something worth reading: a near miss, dissent, or a gap."""

    WEAK = "weak"
    """Do not act on this without looking: the evidence is thin or borrowed."""


@dataclass(frozen=True, slots=True)
class Confidence:
    """Purpose: say how much an album's verdict is worth, and why, in checkable terms.

    Responsibilities: carry the word, the numbers it came from, and the reasons
    that lowered it. Boundaries: it never changes a verdict and never suppresses
    one — an album with weak confidence keeps whatever it measured, because
    hiding a doubtful verdict and stating a doubtful verdict are different
    things and only the second is honest. Dependencies: ``AlbumVerdict``.
    Collaborators: the bench and the window. Constraints: derived on every read
    and never stored, so it cannot outlive the thresholds it was measured
    against.
    """

    sureness: Sureness
    analyzed: int
    tracks: int
    margin_db: float | None
    dissenting: int
    shared: int
    reasons: tuple[str, ...]

    @property
    def coverage(self) -> float:
        """Return the share of the album that could be measured at all."""
        return self.analyzed / self.tracks if self.tracks else 0.0


def judge_confidence(
    verdict: AlbumVerdict, tracks: int, analyzed: int, shared: int = 0
) -> Confidence:
    """Weigh one album's verdict against the evidence that produced it.

    ``shared`` is how many of the album's files are reading a measurement that
    more than one recording answers to. It is the heaviest of the
    three, because the other two describe evidence that is thin about *this*
    album while this one describes evidence that may be about another one.
    """
    reasons: list[str] = []
    sureness = Sureness.STRONG
    margin = _margin_db(verdict)
    dissenting = len(verdict.dissenting_tracks)

    if shared:
        # What a borrowed measurement costs depends on what it is being asked.
        # Inside a lossless container it decides the whole question: a FLAC
        # can read as a transcode because it is holding another recording's
        # wall. Inside a lossy one the verdict is not in doubt at all: an MP3
        # is an MP3 because its container says so, and only the bitrate is
        # borrowed.
        #
        # Without this split most albums of a real library read weak, the
        # majority of them MP3 albums whose verdict nothing could change. A
        # word almost everything carries is not a warning.
        #
        # The pair is named rather than complemented: `undecided` belongs to
        # neither side, because it is returned before a container has been
        # looked at.
        if verdict.encoding in {Encoding.LOSSLESS, Encoding.TRANSCODED}:
            reasons.append(
                f"{shared} of these files read a measurement that more than one "
                "recording answers to — analyze this album in depth to settle it"
            )
            sureness = Sureness.WEAK
        else:
            reasons.append(
                f"{shared} of these files read a measurement that more than one "
                "recording answers to, so the bitrate shown may be another song's"
            )
            sureness = Sureness.FAIR
    if analyzed == 0:
        # **"Could be" names a cause nobody measured.** A row exists only
        # where audio was decoded, and this bench decodes nothing it was not
        # asked to, so an album with no rows was almost always never *asked*
        # rather than tried and failed — and the two are indistinguishable
        # from this number. On a real shelf this is not an edge case.
        reasons.append("nothing in this album has been measured yet")
        return Confidence(
            sureness=Sureness.WEAK,
            analyzed=0,
            tracks=tracks,
            margin_db=None,
            dissenting=dissenting,
            shared=shared,
            reasons=tuple(reasons),
        )
    if tracks and analyzed < tracks:
        share = analyzed / tracks
        # Stated as what was measured, for the same reason: tracks without
        # a row were usually never asked about, not tried and failed.
        reasons.append(f"{analyzed} of {tracks} tracks have been measured")
        sureness = _at_most(sureness, Sureness.WEAK if share < THIN_COVERAGE else Sureness.FAIR)
    if verdict.is_borderline:
        reasons.append("this one nearly went the other way")
        sureness = _at_most(sureness, Sureness.FAIR)
    if margin is not None and margin < NARROW_MARGIN_DB:
        reasons.append(f"{margin:.1f} dB from the verdict changing")
        sureness = _at_most(sureness, Sureness.FAIR)
    if dissenting and analyzed and dissenting / analyzed > DISSENT_SHARE:
        reasons.append(f"{dissenting} of {analyzed} measured tracks disagree with the album")
        sureness = _at_most(sureness, Sureness.FAIR)

    return Confidence(
        sureness=sureness,
        analyzed=analyzed,
        tracks=tracks,
        margin_db=margin,
        dissenting=dissenting,
        shared=shared,
        reasons=tuple(reasons),
    )


def _at_most(current: Sureness, ceiling: Sureness) -> Sureness:
    """Return the weaker of two, since every reason can only lower the word."""
    order = (Sureness.WEAK, Sureness.FAIR, Sureness.STRONG)
    return min(current, ceiling, key=order.index)


def _margin_db(verdict: AlbumVerdict) -> float | None:
    """Return how many decibels this album is from its verdict flipping.

    Two signs decide a transcode and both must be present, so the
    distance to the line is not the same question in the two directions. A
    convicted album is as safe as its *weaker* sign; an acquitted one is as safe
    as the sign that saved it, which is the one furthest from being met.

    ``None`` where decibels are not what decided: a silent band at or below
    19 kHz convicts outright, and nothing but an encoder silences that range, so
    there is no margin to report and none is invented.
    """
    # Named, not complemented: `undecided` answers nothing about a container.
    if verdict.encoding not in {Encoding.LOSSLESS, Encoding.TRANSCODED}:
        return None
    decay = verdict.median_drop_db
    ceiling = verdict.median_ceiling_db
    convicted = decay >= ALBUM_DECAY_DB and ceiling <= ALBUM_CEILING_DB
    if verdict.encoding is Encoding.TRANSCODED and not convicted:
        # Sentenced by something other than these two decibels: a wall the
        # majority of its tracks carry, or the encoder's frame grid found in
        # them. Neither has a margin in dB, and inventing one would describe
        # a spectrum that decided nothing here.
        return None
    if convicted:
        return min(decay - ALBUM_DECAY_DB, ALBUM_CEILING_DB - ceiling)
    return max(ALBUM_DECAY_DB - decay, ceiling - ALBUM_CEILING_DB)
