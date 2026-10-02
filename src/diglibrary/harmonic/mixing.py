"""Which recordings mix with which, by key and by speed together.

A suggestion requires a compatible Camelot key and a close tempo: both, and
neither on its own. Two tracks in compatible keys at 92 and 140 BPM do not mix,
and offering the pair because the wheel allows it would be advice that was only
half checked.

The speed rule is the pitch fader: ±6% is what a deck gives you without the
audio starting to sound like it has been moved. Half and double count as close,
because a 92 BPM track over a 184 BPM one is the same pulse and DJs mix them on
purpose.
"""

from dataclasses import dataclass

from diglibrary.harmonic.keys import camelot_neighbours

TEMPO_TOLERANCE = 0.06
"""What a pitch fader reaches before the pitch shift is audible."""


@dataclass(frozen=True, slots=True)
class MixCandidate:
    """One recording offered against another, and why it was offered."""

    audio_key: str
    # `same key`, `one step up`, `relative major`… said in words, because a
    # suggestion the screen cannot explain cannot be judged by whoever reads it.
    relation: str
    # How the speeds meet: `same speed`, `half time`, `double time`.
    tempo_relation: str
    tempo_difference: float


def key_relation(source: str, target: str) -> str | None:
    """Say how two Camelot codes are related, or ``None`` when they do not mix.

    Derived from the codes, never from a table of pairs, so no pair can be
    missing from a list.
    """
    if source == target:
        return "same key"
    if target not in camelot_neighbours(source):
        return None
    number, ring = int(source[:-1]), source[-1]
    if target[-1] != ring:
        return "relative major" if ring == "A" else "relative minor"
    return (
        "one step up"
        if target == f"{1 if number == 12 else number + 1}{ring}"
        else ("one step down")
    )


def tempo_relation(source: float, target: float) -> tuple[str, float] | None:
    """Say how two tempos meet, or ``None`` when they are too far apart.

    Written as the three ways a pulse can line up — as itself, at half, at
    double — and the first one that lands inside the fader's reach wins.
    """
    if source <= 0 or target <= 0:
        return None
    for name, scaled in (
        ("same speed", target),
        ("double time", target / 2.0),
        ("half time", target * 2.0),
    ):
        difference = abs(scaled - source) / source
        if difference <= TEMPO_TOLERANCE:
            return name, difference
    return None


def mixes_with(
    source_camelot: str,
    source_bpm: float | None,
    target_camelot: str,
    target_bpm: float | None,
) -> MixCandidate | None:
    """Return why these two mix, or ``None``.

    A track with no measured tempo is not silently excluded and not silently
    allowed: its key still has to match, and the pair is reported as `speed
    unknown` so the screen can say what it does not know rather than implying
    it checked.
    """
    relation = key_relation(source_camelot, target_camelot)
    if relation is None:
        return None
    if source_bpm is None or target_bpm is None:
        return MixCandidate(
            audio_key="", relation=relation, tempo_relation="speed unknown", tempo_difference=0.0
        )
    meeting = tempo_relation(source_bpm, target_bpm)
    if meeting is None:
        return None
    name, difference = meeting
    return MixCandidate(
        audio_key="", relation=relation, tempo_relation=name, tempo_difference=difference
    )
