"""What a quality analysis observed, before anything is concluded from it."""

import math
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path

from diglibrary.quality.framing import FrameGrid

FLOOR_FLAT_DB = 6.0
"""How close to the quietest band the ones above it must sit to be one floor.

An encoder's noise floor is flat across the whole dead region, and music that is
merely running out of treble keeps falling. Measured on real files: above their
cut, the tracks of a transcoded album spread only a few decibels across the
whole dead region, while a genuine master that steps in the same place spreads
about twice this line, because there is still music there.
"""

CLIFF_DB = 16.0
"""How far energy must fall on the way to the floor for the fall to be a cut.

**A lower line of 12.0 accuses more honest tracks and convicts no more
transcodes.** Priced against paired ground truth — sources each also encoded at
LAME 256 CBR and 320 CBR, beside real 320 kbps MP3s and genuine FLACs — this
line and the reprieve below it were moved together and every arm held, while
most of the honest tracks the lower line accused stopped being accused.

**This is not what finds the cut** — ``floor_from_hertz`` does, and the cut's
frequency is what convicts. This only separates *cut off* from *dark*: an old,
quiet master simply has nothing up there and reaches its floor without falling
off anything.

Among files whose audio ends at or below 16 kHz, dark masters fall a few
decibels — their floor is tape hiss and not an encoder — and lossy audio falls
this much or more. The line sits in the gap between the two, and the gap is
wide on purpose: the expensive mistake here is calling a genuine record a fake.

**The margin is not spent chasing a quiet track.** The fall is a difference, so
a quiet track falls less from the same cut and can be missed on the same floor
as the tracks beside it. Lowering the line to catch it would spend most of the
margin and buy nothing, because an album is judged by the majority of its
tracks and named as one thing.
"""

DECAY_LOW = 16_000
"""Where the fall towards Nyquist is measured from, in hertz."""

DECAY_HIGH = 19_000
"""Where it is measured to — past every common encoder's low-pass but one."""

SILENCE_FLOOR_DB = -95.0
"""Below this, a band holds no audio at all — it is numerical silence.

A 16-bit stream cannot represent anything quieter than about -96 dB, so a band
this quiet was not merely attenuated by an encoder: it was removed.

Not what finds a wall: an absolute line convicts a dark master that crosses it
by a fraction of a decibel, and misses transcodes whose wall lands above it. It
is what `TimeDomainStats.is_silent` asks of a whole stream.
"""

WALL_STEP_DB = 20.0
"""How far energy must fall between two probes for the drop to be a wall.

A wall is the frequency at which a straight, nearly total cut occurs, above
which practically nothing passes: a step, not a level — which is what an
absolute floor cannot see.

Calibrated against files marked honest by hand and a CBR ladder from 128 to
320 kbps: every rung of the ladder steps more than this, and every honest file
less. The cost, stated: LAME V0 steps less than this and passes, because a VBR
that cuts high and gently is indistinguishable here from a master that simply
runs out of treble — and convicting it would convict dark masters with it.
"""


class Encoding(StrEnum):
    """What an audio stream turned out to be, regardless of what it claims."""

    LOSSLESS = "lossless"
    """No sign of a lossy ancestor: the spectrum fades instead of stopping."""

    TRANSCODED = "transcoded"
    """A lossless container holding audio that was lossy before it got here."""

    LOSSY = "lossy"
    """Honestly lossy, and measured to be what it says it is."""

    OVERSTATED = "overstated"
    """Lossy, but coarser than it declares — a 128 kbps stream labelled 320."""

    UNDECIDED = "undecided"
    """Too little high-frequency content to judge either way."""


class Proof(StrEnum):
    """Which measurement convicted, so a screen can say which one did.

    A verdict that names no evidence cannot be argued with, and this
    application has two kinds of evidence that disagree about what a picture
    shows. A wall is visible in a spectrogram; a frame grid is not, and a file
    caught by its grid draws as a full, healthy spectrum. Saying only
    *transcoded* about both sends a reader to a picture that appears to
    contradict the verdict.

    ``None`` is not a member: nothing was proved, because nothing was
    convicted.
    """

    GRID = "grid"
    """The encoder's own 576-sample frame grid was found in the samples."""

    WALL = "wall"
    """The audio stops dead below the trusted ceiling, which is a filter."""

    SPECTRUM = "spectrum"
    """The two spectral signs, fall and emptiness, were both met."""


@dataclass(frozen=True, slots=True)
class BandEnergy:
    """The energy remaining above one frequency, in dBFS."""

    hertz: int
    rms_db: float

    @property
    def is_silent(self) -> bool:
        """Report whether this band holds nothing at all."""
        return self.rms_db <= SILENCE_FLOOR_DB


@dataclass(frozen=True, slots=True)
class PassBand:
    """The energy inside one band of frequencies, in dBFS.

    Different from ``BandEnergy``, which reports what is left *above* a
    frequency. That reading can only ever fall as the frequency rises, so it
    cannot show the one shape that separates a transcode from a dark master: an
    encoder's noise floor is FLAT, and music that is simply running out of
    treble keeps falling and falls faster near the top. In 1 kHz bands from 15
    to 22 kHz a transcode reads as a cliff followed by nearly identical levels,
    and a genuine master as a descent that accelerates.

    The cumulative bands cannot carry this reading, which is why it is a shape
    of its own.
    """

    low_hertz: int
    high_hertz: int
    rms_db: float


@dataclass(frozen=True, slots=True)
class SpectralProfile:
    """Purpose: describe how an audio stream's energy dies out towards Nyquist.

    Responsibilities: carry one energy reading per probed frequency and expose
    the shape they form. Boundaries: it draws no conclusion about the encoding —
    that is the verdict's job, because the same shape means different things at
    different sample rates. Dependencies: none. Collaborators: ``TrackAnalysis``
    and the verdict rules. Constraints: bands are ordered by frequency and only
    include frequencies below the stream's own Nyquist limit, since a band above
    it would read as silence for every file and prove nothing.
    """

    bands: tuple[BandEnergy, ...]

    @property
    def wall(self) -> tuple[int, int] | None:
        """Return the two probes a straight cut falls between, if there is one.

        A wall is a *step*, not a level. Taking the lowest band under an
        absolute -95 dBFS is wrong in both directions: a dark master can cross
        that line by a fraction of a decibel while falling only a few decibels
        across the range, and a transcode can wall a few decibels above the
        line, where the rule never sees it.

        **The last pair is never a wall.** Every album ends by running out of
        treble, so the final descent to Nyquist is a shape all music has, and
        a clean master can step steeply there. A wall has audio below it,
        nothing above it, and room left to see that nothing is above it.
        """
        if len(self.bands) < 3:
            return None
        steps = [
            (
                self.bands[index].rms_db - self.bands[index + 1].rms_db,
                self.bands[index].hertz,
                self.bands[index + 1].hertz,
            )
            # Stopping two short: the pair that ends at the topmost band has no
            # band above it to corroborate the silence.
            for index in range(len(self.bands) - 2)
        ]
        drop, low, high = max(steps)
        return (low, high) if drop >= WALL_STEP_DB else None

    @property
    def band_density(self) -> tuple[tuple[int, int, float], ...]:
        """Return the energy *inside* each gap between probes, per hertz of it.

        The bands this profile holds are cumulative — energy above a frequency —
        and that reading can only ever fall as the frequency rises, so it cannot
        show the one shape that separates a transcode from a dark master: a
        floor is FLAT and music keeps falling. ``PassBand`` shows it and
        reaches only the bench, so the verdict reads it from here.

        It does not need a second measurement. Both readings come from the same
        brick-wall ``firequalizer``, so the energy in a gap is the difference of
        the two cumulative readings that bound it, in linear power. Checked
        against the direct band-pass reading, the two agree to within a few
        tenths of a decibel.

        Per hertz because the probes are not evenly spaced — 16 to 17.5 kHz is
        half again as wide as 19 to 20 — and a wider band holds more energy for
        no reason anybody is asking about. A density is comparable; a total is
        not.
        """
        levels: list[tuple[int, int, float]] = []
        for lower, upper in zip(self.bands, self.bands[1:], strict=False):
            width = upper.hertz - lower.hertz
            if width <= 0:
                continue
            inside = _linear(lower.rms_db) - _linear(upper.rms_db)
            levels.append((lower.hertz, upper.hertz, _decibels(inside / width)))
        return tuple(levels)

    @property
    def floor_from_hertz(self) -> int | None:
        """Return the frequency from which everything above is one flat floor.

        Where the audio *ends*, which is the essential question: the cut. It
        is deliberately not a step: a step is a difference, so it shrinks with
        the music that was under it, and a step rule calls the quiet tracks of
        an album clean while convicting the brightest one, although all of them
        were cut at the same frequency.

        ``None`` when the floor never begins — an untouched stream is still
        falling at the top rung, and there is no frequency above which it holds
        still.
        """
        densities = self.band_density
        if len(densities) < 3:
            return None
        floor = min(level for _, _, level in densities[-2:])
        for index, (low, _, _) in enumerate(densities):
            above = [level for _, _, level in densities[index:]]
            if max(above) - floor <= FLOOR_FLAT_DB:
                return low
        return None

    @property
    def cliff_db(self) -> float:
        """Return the steepest fall on the way down to the floor.

        Read only below ``floor_from_hertz``, because a fall *inside* the floor
        is the floor's own gentle slope and says nothing: a dead region drifts
        a decibel or two across three kilohertz, against a descent into it ten
        times as large.
        """
        began = self.floor_from_hertz
        if began is None:
            return 0.0
        densities = self.band_density
        falls = [
            one - other for (_, _, one), (low, _, other) in pairwise(densities) if low <= began
        ]
        return max(falls) if falls else 0.0

    @property
    def cutoff_hertz(self) -> int | None:
        """Return the frequency above which the stream holds no audio.

        The upper edge of the wall: audio was still there at the probe below it
        and gone at this one. ``None`` means no straight cut was found, which is
        the ordinary answer for an untouched stream.
        """
        wall = self.wall
        return wall[1] if wall else None

    @property
    def wall_low_hertz(self) -> int | None:
        """Return the last probe that still held audio below the wall.

        The pair is reported rather than a single number because the cut lies
        between two rungs and a high-pass leaks what sits just under it: two
        128 kbps encodes can step between 15 and 16 kHz and between 16 and
        17.5 kHz.
        """
        wall = self.wall
        return wall[0] if wall else None

    @property
    def decay_db(self) -> float:
        """Return how far energy falls across the region a lossy filter cuts.

        Measured between ``DECAY_LOW`` and ``DECAY_HIGH``: an encoder's
        low-pass turns this into a cliff, while music descends it: genuine
        masters fall less than the album threshold here and transcodes more.
        """
        return self._level_at(DECAY_LOW) - self._level_at(DECAY_HIGH)

    @property
    def ceiling_db(self) -> float:
        """Return the energy left in the highest band measured.

        This is the question a slope cannot answer: after the fall, is anything
        still there? A transcode leaves the encoder's noise floor; a master
        leaves quiet, real music.
        """
        return self.bands[-1].rms_db if self.bands else SILENCE_FLOOR_DB

    def _level_at(self, hertz: int) -> float:
        for band in self.bands:
            if band.hertz >= hertz:
                return band.rms_db
        return self.bands[-1].rms_db if self.bands else SILENCE_FLOOR_DB

    @property
    def steepest_drop_db(self) -> float:
        """Return the largest fall in energy between two neighbouring bands.

        A lossless stream loses its highest frequencies gradually, as the music
        itself runs out of them. An encoder's low-pass filter is a wall, and a
        wall shows up here as a drop no natural spectrum produces.
        """
        if len(self.bands) < 2:
            return 0.0
        return max(
            earlier.rms_db - later.rms_db
            for earlier, later in zip(self.bands, self.bands[1:], strict=False)
        )


@dataclass(frozen=True, slots=True)
class TimeDomainStats:
    """Purpose: describe the waveform's own health, independent of its spectrum.

    Responsibilities: carry the measurements that reveal damage rather than
    provenance — clipping, an offset baseline, a stream that is silent or was
    cut short, and how many bits the samples actually use. Boundaries: it holds
    no judgement and no threshold. Dependencies: none. Collaborators:
    ``TrackAnalysis``. Constraints: ``effective_bit_depth`` is what the samples
    occupy, while ``container_bit_depth`` is what the file declares; the two
    disagreeing is the same kind of lie as a transcoded FLAC.
    """

    peak_db: float
    rms_db: float
    dc_offset: float
    flat_factor: float
    clipped_samples: int
    noise_floor_db: float
    effective_bit_depth: int | None = None
    container_bit_depth: int | None = None
    dynamic_range_db: float | None = None

    @property
    def is_silent(self) -> bool:
        """Report whether the stream carries no audible signal at all."""
        return self.peak_db <= SILENCE_FLOOR_DB


@dataclass(frozen=True, slots=True)
class TrackAnalysis:
    """Purpose: hold everything one pass over a file measured, and nothing more.

    Responsibilities: pair the file with what it declares and what it turned
    out to contain. Boundaries: observation only — no verdict, no threshold, no
    comparison against another file. Dependencies: ``SpectralProfile`` and
    ``TimeDomainStats``. Collaborators: the analyzer that fills it and the
    verdict rules that read it. Constraints: ``declared_bitrate`` is what the
    container claims, kept beside the measurements precisely so the two can be
    confronted.
    """

    path: Path
    declared_codec: str
    sample_rate: int
    spectral: SpectralProfile
    time_domain: TimeDomainStats
    declared_bitrate: int | None = None
    # Where the audio was cut into frames, when that was measured. `None` is
    # *not measured* — a file the probe could not read, a stream too short, or
    # a pass that did not run it — and never *measured and clean*, which is why
    # the rule asks for a reading rather than for a number.
    frame_grid: FrameGrid | None = None

    @property
    def is_lossless_container(self) -> bool:
        """Report whether the file promises to have lost nothing.

        **PCM is asked as a family, not as a list of its members.** A set
        spelling out `pcm_s16le` and `pcm_s24le` leaves out every AIFF file,
        which reports `pcm_s16be` or `pcm_s24be`: those files would be judged
        as though their container promised nothing, so no transcode verdict
        could reach them. The question is what PCM *is* — uncompressed
        samples, whatever their width, endianness or signedness — so that a
        flavour ffmpeg has not shipped yet still gets the right answer.
        """
        return self.declared_codec.startswith("pcm_") or self.declared_codec in _LOSSLESS_CODECS


_LOSSLESS_CODECS = frozenset({"flac", "alac", "wav", "aiff", "ape", "wavpack", "tta", "shorten"})


def _linear(decibels: float) -> float:
    """Power, from a level in dB. Sums and differences are meaningless in dB."""
    return 10 ** (decibels / 10)


def _decibels(power: float) -> float:
    """A level, from power. Nothing at all reads as the silence floor."""
    return 10 * math.log10(power) if power > 0 else SILENCE_FLOOR_DB
