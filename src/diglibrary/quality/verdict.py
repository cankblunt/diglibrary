"""Turning measurements into a verdict, with the reason always attached."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from statistics import median, median_low

from diglibrary.quality.framing import READINGS, FrameGrid, rate_could_carry_a_grid
from diglibrary.quality.models import CLIFF_DB, Encoding, Proof, SpectralProfile, TrackAnalysis

ALBUM_DECAY_DB = 15.0
"""The median fall from 16 to 19 kHz above which an album met a filter.

Slope alone is not enough: read as enough, it calls an album a transcode when
its energy simply descends, unbroken, to 22 kHz. Measured across albums of
known origin, genuine masters fall less than this and transcodes more, with a
gap of several decibels between the two. Fifteen sits in that gap.
"""

ALBUM_CEILING_DB = -88.0
"""The top-band level that corroborates a cliff — never a verdict by itself.

An empty top band alone convicts genuine masters whose content simply dies
before 22 kHz: they can read far below this level up there while falling the
whole way — a floor is flat, and that is the difference. The rule convicts
ONLY what is unmistakably lossy and lets the doubtful stay quiet, so this
level only counts when the cliff is there too.
"""

NEAR_DECAY_DB = 13.5
"""A fall this steep is close enough to the line to be worth a second look.

A track with a visible flat cut can fall a few tenths of a decibel short of
the threshold of 15.0 and pass, with its top band already inside the line. A
miss that small is not a verdict, but it is not nothing either.
"""

NEAR_CEILING_DB = -86.0
"""A top band this quiet is close enough to empty to be worth a second look.

Two decibels above the line that convicts. The other near miss: a real cliff
over a top band that has not quite gone dark.
"""

CUTOFF_TRUST_HZ = 19_000
"""A silent band convicts only at or below this frequency.

Silence above it is a shape honest audio produces: dark seventies masters are
near-empty above 21.5 kHz, and early digital converters cut at 20. Silence at
16, 17.5 or 19 kHz is the wall of a 128, 192 or 256 kbps encoder, and nothing
else makes it.

**And it is where the floor rule stops too, because it is the same line.** A
cap of 16 kHz covers the 128 kbps *class* and not the method: a cut at a
higher frequency convicts as well, each cut frequency with its own bitrate.
Two constants saying one thing drift apart, so there is one.

Set by measuring a real library rather than by argument. Nineteen is where
the evidence is and twenty is where it stops: the albums 19 kHz adds over a
lower ceiling are confirmed lossy by other means, and 20 kHz adds albums that
are not, because it lands on the frequency an honest CD master legitimately
ends at.
"""

CUTOFF_BITRATES: tuple[tuple[int, int | None], ...] = (
    (16_000, 128),
    (17_500, 192),
    # 256 and 320 both wall between 19 and 20 kHz, so this rung names neither.
    # A bucket that is right half the time must not write a number into
    # filenames. A verdict with no bitrate says less and claims nothing false.
    (19_000, None),
)
"""Where common encoders stop writing, and the bitrate that stops there.

Read against the **last rung that still held audio**, not the first that did
not. A wall lies between two probes and a high-pass leaks what sits just under
it, so the same encoder shows up at different pairs: one 128 kbps encode steps
between 15 and 16 kHz and another between 16 and 17.5. Against transcodes
whose rate is not in doubt, the lower edge names the rate more often than the
upper edge does.

**Where it cannot tell apart, it says nothing.** 256 and 320 both wall between
19 and 20 kHz, and a bucket that is right half the time would write a wrong
number into filenames — so that rung names no bitrate at all. 128 and 160 do share
the rung below, and it keeps naming 128: they are one bucket in what they mean,
where 256 against 320 is the difference between a transcode and a good one.
"""

REFERENCE_WALLS: tuple[tuple[int, int], ...] = (
    (16_000, 128),
    (18_000, 192),
    (20_000, 320),
)
"""Where the common encoders habitually cut, for reading a picture against.

**These are where an encoder cuts, not where this analyzer asks.** The values
are not taken from ``PROBE_FREQUENCIES``: a table built out of the instrument
instead of out of the thing it measures draws 192 at 17.5 kHz, a full
kilohertz low.

Measured with a LAME CBR ladder built from two source tracks and read on
500 Hz rungs:

        128     16.5 and 15.5 kHz     marked at 16.0
        192     18.5 and 18.0 kHz     marked at 18.0
        256     19.0 and 19.0 kHz     not marked
        320     19.5 and 20.0 kHz     marked at 20.0

The two tracks themselves spread ±0.5 kHz, which is why the tooltip says a
habit and not a law. For 192 the conservative end of its two readings is
taken.

**256 was measured and is not on the ruler.** It sits a kilohertz
under 320 and inside the spread of both its neighbours, so a fourth mark
crowds the one stretch of the picture where the judgement is made and adds no
reading the three do not already give.

Deliberately not `CUTOFF_BITRATES`: the two tables serve different purposes.
That one says what this application is willing to *assert* about a file, and
it reaches filenames, so it refuses the rung 256 and 320 share. This one is a
ruler shown on a spectrogram for the reader's eye, and the reader is the one
comparing.

Nothing is concluded from these. They are labels at a height, never a line
across the picture, never coloured like the measured wall, and every one
carries what it is not.
"""

CLIPPING_PEAK_DB = -0.1
"""A peak this close to full scale means samples were pinned to the maximum."""

MINIMUM_CLIPPED_SAMPLES = 100
"""Below this, samples at full scale are a loud mix rather than clipping."""

MAXIMUM_DC_OFFSET = 0.01
"""A baseline further from zero than this wastes headroom and thumps on cue."""

INFLATED_DEPTH_CONTAINER = 24
"""Only a container this deep can be lying about its bit depth."""

INFLATED_DEPTH_CEILING = 16
"""Samples occupying this few bits inside a deep container were upscaled.

Not any shortfall: a genuine 16-bit master reads 15 or 16 of 16, because quiet
music simply does not swing through every bit. Reporting that as inflated
would flag every honest album.
"""


class Finding(StrEnum):
    """One thing worth telling the user about a file."""

    TRANSCODED = "transcoded"
    OVERSTATED_BITRATE = "overstated_bitrate"
    INFLATED_BIT_DEPTH = "inflated_bit_depth"
    CLIPPING = "clipping"
    DC_OFFSET = "dc_offset"
    SILENT = "silent"


@dataclass(frozen=True, slots=True)
class TrackVerdict:
    """Purpose: state what a file is, and why, in a form the user can check.

    Responsibilities: carry the encoding, the bitrate the audio actually looks
    like, the frequency where it stops, and every finding worth reporting.
    Boundaries: it renames nothing and writes nothing — a verdict is an opinion
    the user approves before anything happens to the file. Dependencies:
    ``Encoding`` and ``Finding``. Collaborators: the album-level verdict, the
    quality report, and the naming label. Constraints: ``reason`` is always
    populated, because a verdict the user cannot audit is a verdict that will
    eventually be wrong without anyone noticing.
    """

    encoding: Encoding
    reason: str
    findings: tuple[Finding, ...] = ()
    effective_bitrate_kbps: int | None = None
    cutoff_hertz: int | None = None
    proved_by: Proof | None = None
    """Which measurement convicted, so the screen can say which one did.

    ``None`` wherever nothing was convicted, and on a verdict rebuilt by
    something that did not record it — an absent answer must never read as
    *the spectrum decided*, which is the one answer that sends a reader to a
    picture.
    """

    @property
    def is_honest(self) -> bool:
        """Report whether the file is what its container claims it is."""
        return self.encoding in {Encoding.LOSSLESS, Encoding.LOSSY}


def judge(analysis: TrackAnalysis) -> TrackVerdict:
    """Return what this file is, from what was measured of it.

    The spectrum decides the encoding and the waveform adds what is wrong with
    it, so a transcode that also clips is reported as both rather than as
    whichever was noticed first.
    """
    findings = list(_health_findings(analysis))
    if analysis.time_domain.is_silent:
        return TrackVerdict(
            encoding=Encoding.UNDECIDED,
            reason="The stream carries no audible signal, so nothing can be judged from it.",
            findings=tuple(findings),
        )
    spectrum = analysis.spectral
    if not spectrum.bands:
        return TrackVerdict(
            encoding=Encoding.UNDECIDED,
            reason="No frequency band could be measured below this stream's Nyquist limit.",
            findings=tuple(findings),
        )
    cutoff = spectrum.cutoff_hertz
    # Named from the rung below the wall, which is the one the audio was still
    # coming out of. Read from the rung above, a 128 kbps file whose step falls
    # between 16 and 17.5 kHz would be written into a filename as 192.
    bitrate = _bitrate_for(spectrum.wall_low_hertz)

    # A wall found by its step convicts on its own, wherever it falls.
    # `track_is_transcoded` trusts a cutoff only at or below `CUTOFF_TRUST_HZ`,
    # which is right for a silent band — silence above 19 kHz is a shape honest
    # audio produces. A 20 dB step is not: it is a straight cut, and a 320 kbps
    # encoder makes one between 19 and 20 kHz, which the frequency guard alone
    # would let pass.
    walled = spectrum.wall is not None
    # **And the third way, which is the one that answers where the audio
    # ends.** A wall is a step and a step shrinks with the music that was under
    # it, so the same cut convicts a bright track and acquits a quiet one: the
    # tracks of one album cut at the same 15 kHz can step anywhere from a few
    # decibels to over twenty. Asking instead where the floor begins is a
    # question the music's own loudness cannot answer wrongly.
    cut_low = _cut_where_no_recording_ends(spectrum)
    if cut_low is not None and cutoff is None:
        # The frequency the screen should name, when this is what found it. A
        # bitrate is deliberately not derived from it: `_bitrate_for` reads the
        # rung below a *wall*, and a verdict with no bitrate says less and
        # claims nothing false.
        cutoff = cut_low

    if analysis.is_lossless_container:
        # **The encoder's own frame grid, asked before the spectrum.** It
        # answers a question the spectrum cannot: a 320 kbps encode with no
        # low-pass reaches 21.5 kHz and fades like a master, so every rule that
        # reads where the audio *ends* passes it, at any threshold. This reads
        # where the audio was framed instead, and a file that never met an
        # encoder was never framed.
        #
        # Asked only of a container that promises to have lost nothing: an MP3
        # fires here by definition, and convicting it would relabel an honest
        # lossy file as a transcode.
        if convicted_by_frame_grid(analysis.frame_grid):
            findings.insert(0, Finding.TRANSCODED)
            return TrackVerdict(
                encoding=Encoding.TRANSCODED,
                reason=_frame_grid_reason(analysis),
                findings=tuple(findings),
                cutoff_hertz=cutoff,
                proved_by=Proof.GRID,
            )
        if (
            not walled
            and cut_low is None
            and not track_is_transcoded(
                cutoff,
                spectrum.decay_db,
                spectrum.ceiling_db,
                spectrum.floor_from_hertz,
            )
        ):
            return TrackVerdict(
                encoding=Encoding.LOSSLESS,
                reason=(
                    "Energy fades gradually and the top band is still alive: "
                    f"a fall of {spectrum.decay_db:.1f} dB towards Nyquist, with "
                    f"{spectrum.ceiling_db:.1f} dB remaining above 21.5 kHz."
                ),
                findings=tuple(findings),
            )
        findings.insert(0, Finding.TRANSCODED)
        return TrackVerdict(
            encoding=Encoding.TRANSCODED,
            reason=_wall_reason(analysis, cutoff, bitrate),
            findings=tuple(findings),
            effective_bitrate_kbps=bitrate,
            cutoff_hertz=cutoff,
            # Which of the two spectral doors this came through, because only
            # one of them is visible in a picture. A wall is a cliff anybody
            # can see in a spectrogram; the fall-and-emptiness pair is a shape
            # that has to be read off numbers.
            proved_by=Proof.WALL if walled or cut_low is not None else Proof.SPECTRUM,
        )

    declared = analysis.declared_bitrate
    if bitrate is not None and declared is not None and bitrate < declared * 0.75:
        findings.insert(0, Finding.OVERSTATED_BITRATE)
        return TrackVerdict(
            encoding=Encoding.OVERSTATED,
            reason=(
                f"The file declares {declared} kbps, but its audio stops at "
                f"{cutoff} Hz, which is where a {bitrate} kbps encoder habitually stops."
            ),
            findings=tuple(findings),
            effective_bitrate_kbps=bitrate,
            cutoff_hertz=cutoff,
        )
    return TrackVerdict(
        encoding=Encoding.LOSSY,
        reason=(
            f"A lossy stream whose audio stops at {cutoff} Hz, "
            "consistent with the bitrate it declares."
            if cutoff is not None
            else "A lossy stream whose audio reaches every band measured."
        ),
        findings=tuple(findings),
        effective_bitrate_kbps=bitrate or declared,
        cutoff_hertz=cutoff,
    )


@dataclass(frozen=True, slots=True)
class AlbumVerdict:
    """Purpose: state what a whole album is, which is the only reliable unit.

    Responsibilities: carry the album's encoding, why it was reached, and the
    tracks that disagree with it. Boundaries: it names no folder and writes
    nothing. Dependencies: ``TrackVerdict``. Collaborators: the quality report
    and the format label that a rename proposes. Constraints: the encoding
    comes from the *median* of the album's tracks, because a single track is
    not evidence: a genuine master and a transcode each have tracks on the
    wrong side of any single-track line, while their album medians stay far
    apart.
    """

    encoding: Encoding
    reason: str
    tracks: tuple[TrackVerdict, ...]
    median_drop_db: float
    effective_bitrate_kbps: int | None = None
    median_ceiling_db: float = 0.0
    proved_by: Proof | None = None
    """Which measurement convicted this album, for the screen to say so.

    The album is the unit that gets renamed and the unit the shelf draws, so
    it is the one that has to answer *why* — and the two proofs it can carry
    disagree about what a spectrogram will show.
    """

    @property
    def is_borderline(self) -> bool:
        """Report whether an album called honest came close to being called a fake.

        Only ever asked of a lossless album: it is the accusation of hiding a
        lossy origin that this narrowly missed, and no other verdict carries
        that accusation. A default ceiling of zero reads as a full top band, so
        a verdict rebuilt without one is never marked on a guess.
        """
        return self.encoding is Encoding.LOSSLESS and is_borderline(
            self.median_drop_db, self.median_ceiling_db
        )

    @property
    def dissenting_tracks(self) -> tuple[TrackVerdict, ...]:
        """Return the tracks whose own verdict differs from the album's.

        These are what deserve a mark of their own: the 128 kbps track inside
        an otherwise honest album, or the one transcode in a real rip.
        """
        return tuple(track for track in self.tracks if track.encoding is not self.encoding)


def _cut_where_no_recording_ends(spectrum: SpectralProfile) -> int | None:
    """Return where the audio ends, when ending there is a filter's doing.

    Two questions, and only the first one convicts. **Where does the audio end**
    — at or below ``CUTOFF_TRUST_HZ``, nothing recorded stops there. **Did
    it fall off something to get there** — because an old, quiet master reaches
    its floor without a cliff, and calling one of those a fake is the expensive
    mistake this whole module is arranged around.

    ``None`` at or above `CUTOFF_TRUST_HZ`, where silence is a shape honest audio
    produces and this rule has nothing to add.

    **At, and not merely above.** The trusted frequency is also a rung of
    `PROBE_FREQUENCIES`, so a floor that truly begins higher has nowhere else
    to land on the ordinary grid, and `track_is_transcoded` spares it there.
    This function runs first and feeds it the cutoff, so leaving the line out
    here would convict the very files that reprieve exists to spare.
    """
    began = spectrum.floor_from_hertz
    if began is None or began >= CUTOFF_TRUST_HZ:
        return None
    return began if spectrum.cliff_db >= CLIFF_DB else None


PEAK_Z_TRUST = 8.0
"""How far the winning alignment must stand above the rest to count.

Measured on the paired benchmark under `benchmark/`: the honest arms —
originals and masters filtered at 20 kHz, never near an encoder — peak well
below this line, and every LAME arm from 128 to 320, including the
no-low-pass 320, peaks above it. The line sits in the gap between the two
populations rather than against either edge of it, because a real library is
wider than a benchmark: honest files there peak higher than the benchmark's,
without reaching this line and without the corroboration below.
"""


AGREEING_TRUST = 4
"""How many of the six readings must name one alignment for it to be a grid.

Between the two populations: no honest file measured reached three, and
nearly every laundered transcode reached six, the rest four or five. One 320 in
the paired benchmark reached only three and goes free; a line at three would
take it in and sit against the honest edge, with nothing between it and a file
that reaches two. So the line stays at four.
"""


def convicted_by_frame_grid(grid: FrameGrid | None) -> bool:
    """Report whether this reading is an encoder's grid rather than an accident.

    **Both halves, always.** A tall peak says one alignment scored far above
    the rest; readings agreeing says the same alignment won under draws that
    could have disagreed — two channel views and three thresholds. Each half
    alone is weaker than the pair: honest audio does produce tall peaks
    occasionally, and a single reading agreeing with itself is not agreement at
    all.

    **The bar is a majority of the six, not all six.** Unanimity lets one
    noisy reading answer for the other five, and the reading that goes noisy is
    always the same one — the finest threshold, which is what it is for.
    Priced against paired ground truth, lossless sources each also encoded at
    LAME 128 and 192 CBR and converted back into FLAC:

    | arm | readings on one alignment |
    | --- | --- |
    | the sources, untouched | 1 or 2, **none above 2** |
    | the sources, filtered at 20 kHz | 1 or 2, **none above 2** |
    | LAME 128 to 320 -> FLAC | nearly every file at **6**, the rest at 4 or 5 |

    Honest audio does not get three readings onto one alignment, and a
    laundered transcode nearly always gets all six. Four is what reaches the
    files whose second view carries the trace, where four of six land on one
    alignment. It is not the whole of them: one 320 in the benchmark reached
    three, and is not convicted.

    ``None`` is *not measured* and convicts nothing — an absent measurement
    must never read as a clean one, and must never read as a guilty one either.
    """
    if grid is None:
        return False
    return grid.peak_z >= PEAK_Z_TRUST and grid.agreeing_readings >= AGREEING_TRUST


def _frame_grid_reason(analysis: TrackAnalysis) -> str:
    """Say what was found, in words that name what the file used to be.

    The point is not the number: it is that a person reading this understands
    their FLAC was made from a compressed file, and that converting it back
    copied the loss rather than undoing it.
    """
    grid = analysis.frame_grid
    assert grid is not None
    container = analysis.declared_codec.upper()
    # **What was measured, and not what the rule used to ask for.** The rule
    # convicts on four readings of six, so a sentence saying *all six* would
    # name an agreement that was not there on exactly the files the lower bar
    # was set to reach.
    agreeing = (
        "all six readings agree"
        if grid.agreeing_readings >= READINGS
        else f"{_COUNTED[grid.agreeing_readings]} of the six readings agree"
    )
    return (
        f"This file was an MP3 before it was a {container}. The audio still carries "
        "the MP3 encoder's own frame grid — one alignment out of 576 stands "
        f"{grid.peak_z:.0f} deviations above the rest, and {agreeing} on it. "
        "Converting it to a lossless format copied the loss; it did not undo it."
    )


_COUNTED = ("no", "one", "two", "three", "four", "five")
"""How many of the six readings agreed, as a word, for every count short of six."""


def track_is_transcoded(
    cutoff_hertz: int | None,
    decay_db: float,
    ceiling_db: float,
    floor_hertz: int | None = None,
) -> bool:
    """Decide whether one track, alone, is unmistakably a transcode.

    A silent band at or below ``CUTOFF_TRUST_HZ`` is a filter's signature
    outright — nothing else silences 16 to 19 kHz. Above it, silence is a
    shape honest audio produces, so short of a trusted wall the fall and the
    emptiness must BOTH be present. Either alone convicts genuine masters: one
    that falls steeply while its top band is still alive, and one whose top
    band is nearly empty while it keeps falling the whole way.

    **Both paths stop at the same frequency, which is the point of having one
    constant.** A fall-and-emptiness path that never asks where the audio ends
    convicts in the very band ``CUTOFF_TRUST_HZ`` exists to spare: a record
    whose floor begins at 20 kHz — an honest converter's anti-aliasing filter —
    reads as a steep fall into an empty top, because that is what a filter at
    20 kHz produces whoever built it. Measured on the benchmark's masters
    filtered at 20 kHz and never encoded: the wall path convicts none and the
    unbounded pair convicts a substantial share of them.

    **One line, not two, because a floor is read against the ruler that found
    it.** An upper bound on the reprieve does not work: the same honest master
    filtered at 20 kHz floors at 20 kHz on the eight rungs of
    ``PROBE_FREQUENCIES`` and at 21 kHz on the fourteen of
    ``DEEP_PROBE_FREQUENCIES``, because narrower bands find the flat part
    further up. Any ceiling written in hertz would absolve the album on the
    ordinary pass and convict it on the deep one — the same file, two verdicts.
    So the trusted line does the whole job: above it this pair of signs cannot
    tell a converter from an encoder, and it declines to guess.

    **The reprieve includes the line itself.** `19_000` is not only the trusted
    frequency, it is a *rung* of `PROBE_FREQUENCIES`, so a floor that truly
    begins above it has nowhere else to land on the ordinary grid: a file that
    floors at exactly 19 kHz there usually floors at 19.5, 20, 20.5 or 21 kHz
    on the finer ruler. Reading the coarse grid's last rung as *at or above*
    is what makes the two rulers agree about the same file.

    It reaches no known transcode: on paired ground truth, LAME 256 and LAME
    320 floor at 20 kHz — both already above the line, caught by the wall path
    or not at all.

    ``floor_hertz`` of ``None`` means the measurement was stored without a
    floor, and such a row is judged on the two signs alone rather than absolved
    on a number nobody took — the reprieve reaches that album when it is
    measured again.
    """
    if cutoff_hertz is not None and cutoff_hertz <= CUTOFF_TRUST_HZ:
        return True
    if floor_hertz is not None and floor_hertz >= CUTOFF_TRUST_HZ:
        return False
    return decay_db >= ALBUM_DECAY_DB and ceiling_db <= ALBUM_CEILING_DB


def album_is_transcoded(
    median_decay_db: float,
    median_ceiling_db: float,
    median_floor_hertz: int | None = None,
) -> bool:
    """Decide whether a lossless-containered album was lossy before it arrived.

    Both signs together, never either alone: only the unmistakable is
    convicted. Decay alone renames a genuine album that merely runs out of
    treble fast; ceiling alone renames genuine masters whose top band is dark
    by nature. A 128-class fake needs neither: its wall sits at 16 kHz, inside
    the range a silent band convicts on its own.

    And it stops at ``CUTOFF_TRUST_HZ``, like the track rule above it: an album
    whose typical track still holds audio past that line was cut where honest
    audio is also cut, and this pair of signs cannot tell the two apart.
    ``None`` judges on the two signs alone, for rows stored without a floor.

    The single home of this rule: a verdict rebuilt from the database and one
    measured from the audio must ask the same question to agree.
    """
    if median_floor_hertz is not None and median_floor_hertz >= CUTOFF_TRUST_HZ:
        return False
    return median_decay_db >= ALBUM_DECAY_DB and median_ceiling_db <= ALBUM_CEILING_DB


def is_borderline(decay_db: float, ceiling_db: float) -> bool:
    """Report whether this missed conviction by a hair, either way.

    One sign fully met and the other within a margin of its line. It is a mark
    of suspicion and nothing else: nothing is renamed, no label changes, and
    the album keeps whatever honest verdict it earned. The transcode rule is
    deliberately narrow so that what it convicts is unmistakable — the cost of
    that narrowness is the file it lets pass, and this is where that cost
    becomes visible instead of silent.

    Symmetric on purpose. One near miss is a top band already inside the line
    over a fall just short of it; the other is the mirror image, a real cliff
    over a top band not yet dark. Naming only one of them would leave the other
    silent.
    """
    if album_is_transcoded(decay_db, ceiling_db):
        return False
    cliff_with_a_lit_top = decay_db >= ALBUM_DECAY_DB and ceiling_db <= NEAR_CEILING_DB
    dark_top_with_a_soft_cliff = decay_db >= NEAR_DECAY_DB and ceiling_db <= ALBUM_CEILING_DB
    return cliff_with_a_lit_top or dark_top_with_a_soft_cliff


def judge_album(analyses: Sequence[TrackAnalysis]) -> AlbumVerdict:
    """Return what an album is, from every track that could be measured.

    The album decides, not the loudest track: the median of the per-track wall
    falls is weighed with the median top-band level (``album_is_transcoded``),
    and a lossless container that fails either was lossy before it arrived.
    """
    tracks = tuple(judge(analysis) for analysis in analyses)
    drops = [
        analysis.spectral.decay_db
        for analysis in analyses
        if analysis.spectral.bands and not analysis.time_domain.is_silent
    ]
    if not drops:
        return AlbumVerdict(
            encoding=Encoding.UNDECIDED,
            reason="No track in this album could be measured.",
            tracks=tracks,
            median_drop_db=0.0,
        )
    median_drop = median(drops)
    # The transcode question is asked of the tracks that could be transcodes,
    # and of no others. A wall below 19 kHz is not evidence against an MP3 —
    # it is what an MP3 is — so a folder holding ten honest MP3s and one FLAC
    # bonus track must not answer the question with the MP3s: ten walls
    # against eleven tracks would convict the album, and the untouched FLAC
    # would be written `[Lossy]` on disk along with it.
    inside_lossless = tuple(
        (analysis, track)
        for analysis, track in zip(analyses, tracks, strict=True)
        if analysis.is_lossless_container
    )
    bitrate = _median_bitrate(tracks)

    if not inside_lossless:
        encoding = Encoding.OVERSTATED if _majority(tracks, Encoding.OVERSTATED) else Encoding.LOSSY
        return AlbumVerdict(
            encoding=encoding,
            reason=(
                f"A lossy album whose tracks fall {median_drop:.1f} dB at the median, "
                f"consistent with {bitrate} kbps."
                if encoding is Encoding.LOSSY
                else (
                    "Most tracks declare more than they hold; "
                    f"the audio looks like {bitrate} kbps."
                )
            ),
            tracks=tracks,
            median_drop_db=median_drop,
            effective_bitrate_kbps=bitrate,
        )

    # Every number below is read from the lossless containers alone, for the
    # reason `inside_lossless` exists: the honestly-lossy tracks of a mixed
    # folder answer a question that was never about them.
    lossless_drops = [
        analysis.spectral.decay_db
        for analysis, _ in inside_lossless
        if analysis.spectral.bands and not analysis.time_domain.is_silent
    ]
    ceilings = [
        analysis.spectral.ceiling_db
        for analysis, _ in inside_lossless
        if analysis.spectral.bands and not analysis.time_domain.is_silent
    ]
    median_drop = median(lossless_drops) if lossless_drops else median_drop
    median_ceiling = median(ceilings) if ceilings else 0.0
    # Where the typical track of this album stops holding audio, so the
    # fall-and-emptiness rule below stops at the same line the wall count
    # does. Measured tracks only — a floor nobody found is not a floor at 0.
    floors = [
        analysis.spectral.floor_from_hertz
        for analysis, _ in inside_lossless
        if analysis.spectral.bands
        and not analysis.time_domain.is_silent
        and analysis.spectral.floor_from_hertz is not None
    ]
    # `median_low`, not `median`: a floor is one of the rungs the analyzer asked
    # at, and the ordinary median of an even split invents a frequency between
    # two of them. An album half at 19 kHz and half at 20 would be judged
    # on 19.5 kHz — a reading no track has and no probe took — and absolved on
    # it, because 19.5 clears the trusted line that 19 does not. The low median
    # is a rung something actually measured, and it errs towards convicting.
    median_floor = median_low(floors) if floors else None
    bitrate = _median_bitrate([track for _, track in inside_lossless])
    walls = sum(
        1
        for _, track in inside_lossless
        if track.cutoff_hertz is not None and track.cutoff_hertz <= CUTOFF_TRUST_HZ
    )
    # **And the tracks the frame grid caught, counted the same way the walls
    # are.** Every number above it is a spectral median, and a file caught by
    # its framing reads *lossless* spectrally — that is the point of the
    # measurement. Without this line an album of ten laundered transcodes
    # comes back `lossless` while each of its ten tracks says `transcoded`:
    # the tracks right, the album wrong, and the album is what the screen
    # shows and the folder is named for.
    framed = sum(
        1 for analysis, _ in inside_lossless if convicted_by_frame_grid(analysis.frame_grid)
    )
    if (
        walls * 2 > len(inside_lossless)
        or framed * 2 > len(inside_lossless)
        or album_is_transcoded(median_drop, median_ceiling, median_floor)
    ):
        proof = album_proof(framed, walls, len(inside_lossless))
        return AlbumVerdict(
            encoding=Encoding.TRANSCODED,
            reason=album_transcode_reason(
                proof, framed, walls, len(inside_lossless), median_drop, len(drops)
            ),
            tracks=tracks,
            median_drop_db=median_drop,
            effective_bitrate_kbps=bitrate,
            median_ceiling_db=median_ceiling,
            proved_by=proof,
        )
    return AlbumVerdict(
        encoding=Encoding.LOSSLESS,
        reason=(
            f"Across {len(drops)} tracks the energy fades gradually, "
            f"falling {median_drop:.1f} dB at the median."
        ),
        tracks=tracks,
        median_drop_db=median_drop,
        median_ceiling_db=median_ceiling,
    )


def album_proof(framed: int, walls: int, of: int) -> Proof:
    """Say which measurement convicted this album, asked strongest first.

    Grid before wall before spectrum, and deliberately not in the order the
    ``if`` above happens to evaluate: one file can carry both a wall and a
    grid, and the grid is the finding a screen has to warn about. A reader
    sent to a spectrogram for a grid conviction sees a spectrum that looks
    healthy and concludes the application is wrong.
    """
    if framed * 2 > of:
        return Proof.GRID
    if walls * 2 > of:
        return Proof.WALL
    return Proof.SPECTRUM


def album_transcode_reason(
    proof: Proof, framed: int, walls: int, of: int, median_drop: float, drops: int
) -> str:
    """Say what convicted this album, and never a reading that decided nothing.

    An album convicted by a majority of *walls* must not be handed the median
    sentence, which describes a number that did not decide it: that would name
    a cause nobody measured. So a wall has a sentence of its own.

    One function, read by the fresh path and by the replayed one, because the
    two agreeing is the contract ``verdict_from_stored`` exists to keep — and
    two sentences would be two places for it to drift.
    """
    if proof is Proof.GRID:
        return _album_frame_grid_reason(framed, of)
    if proof is Proof.WALL:
        return (
            f"{walls} of this album's {of} files stop dead at or below "
            f"{CUTOFF_TRUST_HZ // 1000} kHz. No instrument and no microphone ends "
            "that abruptly; a lossy encoder's low-pass filter does, and that is "
            "what is left of one here."
        )
    return (
        f"Across {drops} tracks the energy falls {median_drop:.1f} dB at the "
        "median between neighbouring bands, which is a filter rather than music "
        "running out of treble."
    )


def _album_frame_grid_reason(caught: int, of: int) -> str:
    """Say what the album is, in the words a person can act on.

    The same sentence the track carries, at the scale that matters: the album
    is the unit that gets renamed, and a reason about medians would describe a
    spectrum that is not what convicted anything here.
    """
    return (
        f"{caught} of this album's {of} files still carry the MP3 encoder's own frame "
        "grid, so they were MP3s before they were made lossless. Converting them "
        "copied the loss; it did not undo it."
    )


def _majority(tracks: Sequence[TrackVerdict], encoding: Encoding) -> bool:
    """Report whether more than half the tracks reached this verdict alone."""
    if not tracks:
        return False
    return sum(1 for track in tracks if track.encoding is encoding) * 2 > len(tracks)


def _median_bitrate(tracks: Sequence[TrackVerdict]) -> int | None:
    """Return the bitrate the album's tracks look like, ignoring the unmeasured."""
    rates = [track.effective_bitrate_kbps for track in tracks if track.effective_bitrate_kbps]
    return round(median(rates)) if rates else None


def _wall_reason(analysis: TrackAnalysis, cutoff: int | None, bitrate: int | None) -> str:
    """Say why a lossless container is not holding lossless audio."""
    codec = analysis.declared_codec.upper()
    unread = _the_grid_could_not_be_read(analysis)
    if cutoff is not None:
        # **A habit, never a signature.** `REFERENCE_WALLS` records where an
        # encoder *habitually* cuts, over a ±0.5 kHz spread, so the sentence
        # built from it must not name the encoder as the established cause.
        # The one reading that could corroborate that is the frame grid, and on
        # a resampled file it cannot be taken at all.
        origin = f", where a {bitrate} kbps encoder habitually cuts" if bitrate else ""
        return (
            f"This {codec} holds no audio above {cutoff} Hz{origin}, "
            f"so it was lossy before it was made lossless.{unread}"
        )
    return (
        f"This {codec}'s energy falls {analysis.spectral.decay_db:.1f} dB towards Nyquist "
        f"and leaves only {analysis.spectral.ceiling_db:.1f} dB above 21.5 kHz, "
        f"which is a filter rather than a fading instrument.{unread}"
    )


def _the_grid_could_not_be_read(analysis: TrackAnalysis) -> str:
    """Say that the corroborating reading was unavailable, when it was.

    Said in the sentence rather than left to the absence of a number, because
    an absence reads as a clean reading: a low `z` on a 96 kHz file would be
    taken for a second opinion that agreed.
    """
    if rate_could_carry_a_grid(analysis.sample_rate):
        return ""
    return (
        " The encoder's frame grid, which is what would corroborate this, cannot be read "
        f"at {analysis.sample_rate} Hz: audio only arrives at that rate by resampling, "
        "and resampling erases the grid."
    )


def _bitrate_for(wall_low_hertz: int | None) -> int | None:
    """Return the bitrate whose encoder stops just above this rung, if one does.

    ``None`` for a stream with no wall, and equally for a wall on a rung that
    two bitrates share — the table says which.
    """
    if wall_low_hertz is None:
        return None
    for frequency, bitrate in CUTOFF_BITRATES:
        if wall_low_hertz <= frequency:
            return bitrate
    return None


def _health_findings(analysis: TrackAnalysis) -> tuple[Finding, ...]:
    """Return what is wrong with the waveform, apart from where it came from."""
    stats = analysis.time_domain
    findings: list[Finding] = []
    if stats.is_silent:
        findings.append(Finding.SILENT)
    if stats.peak_db >= CLIPPING_PEAK_DB and stats.clipped_samples >= MINIMUM_CLIPPED_SAMPLES:
        findings.append(Finding.CLIPPING)
    if abs(stats.dc_offset) > MAXIMUM_DC_OFFSET:
        findings.append(Finding.DC_OFFSET)
    if (
        stats.effective_bit_depth is not None
        and stats.container_bit_depth is not None
        and stats.container_bit_depth >= INFLATED_DEPTH_CONTAINER
        and stats.effective_bit_depth <= INFLATED_DEPTH_CEILING
    ):
        findings.append(Finding.INFLATED_BIT_DEPTH)
    return tuple(findings)
