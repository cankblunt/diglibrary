"""Tests for measuring what an audio file is, and for judging what was measured."""

import logging
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from diglibrary.quality import analysis as analysis_module
from diglibrary.quality.analysis import (
    DEEP_PROBE_FREQUENCIES,
    FfmpegQualityAnalyzer,
    SubprocessCommandRunner,
)
from diglibrary.quality.framing import FrameGrid, rate_could_carry_a_grid
from diglibrary.quality.models import (
    CLIFF_DB,
    BandEnergy,
    Encoding,
    Proof,
    SpectralProfile,
    TimeDomainStats,
    TrackAnalysis,
)
from diglibrary.quality.verdict import (
    CUTOFF_TRUST_HZ,
    REFERENCE_WALLS,
    AlbumVerdict,
    Finding,
    _cut_where_no_recording_ends,
    album_is_transcoded,
    is_borderline,
    judge,
    judge_album,
    track_is_transcoded,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "audio"


class ReplayRunner:
    """Return recorded ffmpeg reports, so no subprocess or media is needed.

    Keyed by the probe frequency in the command, because that is what makes one
    invocation differ from another; the report with no frequency is the
    ``astats`` pass.
    """

    def __init__(self, statistics: str, bands: dict[int, str]) -> None:
        self._statistics = statistics
        self._bands = bands
        self.invocations: list[tuple[str, ...]] = []

    def run(self, arguments):
        """Return the report recorded for this invocation."""
        self.invocations.append(tuple(arguments))
        joined = " ".join(arguments)
        for frequency, report in self._bands.items():
            if f"lt(f,{frequency})" in joined:
                return report
        return self._statistics


def _statistics_report(
    codec: str = "flac",
    rate: int = 44_100,
    peak: float = -1.2,
    rms: float = -14.0,
    dc: float = 0.0,
    depth: tuple[int, ...] = (16, 16, 16, 16),
    peaks: int = 4,
) -> str:
    return (
        f"  Duration: 00:03:20.00, bitrate: 900 kb/s\n"
        f"  Stream #0:0: Audio: {codec}, {rate} Hz, stereo, s16\n"
        f"[Parsed_astats_0 @ 0x1] DC offset: {dc}\n"
        f"[Parsed_astats_0 @ 0x1] Peak level dB: {peak}\n"
        f"[Parsed_astats_0 @ 0x1] RMS level dB: {rms}\n"
        f"[Parsed_astats_0 @ 0x1] Flat factor: 0.000000\n"
        f"[Parsed_astats_0 @ 0x1] Abs Peak count: {peaks}\n"
        f"[Parsed_astats_0 @ 0x1] Noise floor dB: -60.0\n"
        f"[Parsed_astats_0 @ 0x1] Bit depth: {'/'.join(map(str, depth))}\n"
        f"[Parsed_astats_0 @ 0x1] Dynamic range: 40.0\n"
    )


def _band_report(rms: float) -> str:
    return (
        f"[Parsed_astats_1 @ 0x1] Peak level dB: {rms}\n"
        f"[Parsed_astats_1 @ 0x1] RMS level dB: {rms}\n"
    )


def _analyzer(runner) -> FfmpegQualityAnalyzer:
    return FfmpegQualityAnalyzer(runner, logging.getLogger("test.quality"))


def test_a_gradual_spectrum_in_a_lossless_container_is_lossless(tmp_path: Path) -> None:
    """An untouched master loses its treble slowly, and nothing is concluded from noise."""
    runner = ReplayRunner(
        _statistics_report(),
        {
            13_000: _band_report(-46.0),
            14_000: _band_report(-49.5),
            15_000: _band_report(-52.8),
            16_000: _band_report(-56.3),
            17_500: _band_report(-60.2),
            19_000: _band_report(-63.9),
            20_000: _band_report(-67.0),
            21_500: _band_report(-70.8),
        },
    )

    analysis = _analyzer(runner).analyze(tmp_path / "track.flac")
    assert analysis is not None
    verdict = judge(analysis)

    assert verdict.encoding is Encoding.LOSSLESS
    assert verdict.is_honest
    assert Finding.TRANSCODED not in verdict.findings
    assert "fades gradually" in verdict.reason


def test_a_straight_cut_below_a_flac_is_a_transcode(tmp_path: Path) -> None:
    """A FLAC whose audio stops dead between two rungs was an MP3 first.

    The step is what says so, not the level. These readings are alive through
    15 kHz, gone from 16 on, and flat above it. Read from 16 kHz up only, the
    largest step here is 2.1 dB and there would be nothing to see, which is
    why the rungs begin below it.
    """
    runner = ReplayRunner(
        _statistics_report(),
        {
            13_000: _band_report(-65.4),
            14_000: _band_report(-68.6),
            15_000: _band_report(-73.8),
            16_000: _band_report(-100.0),
            17_500: _band_report(-101.6),
            19_000: _band_report(-103.4),
            20_000: _band_report(-105.2),
            21_500: _band_report(-111.0),
        },
    )

    analysis = _analyzer(runner).analyze(tmp_path / "fake.flac")
    assert analysis is not None
    verdict = judge(analysis)

    assert verdict.encoding is Encoding.TRANSCODED
    assert not verdict.is_honest
    assert verdict.cutoff_hertz == 16_000
    assert verdict.effective_bitrate_kbps == 128
    assert Finding.TRANSCODED in verdict.findings


def test_a_floor_in_the_converter_band_is_not_convicted(tmp_path: Path) -> None:
    """A record ending at 20 kHz is spared, because that is where honest ones end.

    A cliff towards Nyquist over a top band at -89.5 dB, and no silence
    anywhere. The floor begins at 20 kHz, which is the band ``CUTOFF_TRUST_HZ``
    was placed at 19 kHz to stay out of: an honest converter's anti-aliasing
    filter lands there, and so does a 320 kbps encoder, so a floor there cannot
    tell the two apart. The cost is that some 320 kbps transcodes pass, and the
    application does not claim to catch them.
    """
    runner = ReplayRunner(
        _statistics_report(),
        {
            13_000: _band_report(-35.0),
            14_000: _band_report(-38.0),
            15_000: _band_report(-41.0),
            16_000: _band_report(-43.7),
            17_500: _band_report(-47.0),
            19_000: _band_report(-62.9),
            20_000: _band_report(-80.0),
            21_500: _band_report(-89.5),
        },
    )

    analysis = _analyzer(runner).analyze(tmp_path / "fake.flac")
    assert analysis is not None
    assert analysis.spectral.floor_from_hertz == 20_000
    verdict = judge(analysis)

    assert verdict.encoding is Encoding.LOSSLESS


def test_the_deep_grid_reaches_the_same_verdict_as_the_ordinary_one(tmp_path: Path) -> None:
    """A finer ruler must not change what a file is.

    A floor is not a property of the audio alone: it is where the flat part
    *begins on the rungs that were asked*. The same master, filtered at 20 kHz,
    floors at 20 kHz on the ordinary rungs and at 21 kHz on the deep ones, so a
    reprieve written as a ceiling in hertz between the two absolves the file on
    one pass and convicts it on the other. The deep grid exists so a fresh
    reading can be held against a stored one, and a rule that disagrees with
    itself across the two breaks that.
    """
    same_audio = {
        13_000: _band_report(-35.0),
        14_000: _band_report(-38.0),
        15_000: _band_report(-41.0),
        16_000: _band_report(-43.7),
        16_500: _band_report(-44.5),
        17_500: _band_report(-47.0),
        18_000: _band_report(-52.0),
        18_500: _band_report(-57.0),
        19_000: _band_report(-62.9),
        19_500: _band_report(-71.0),
        20_000: _band_report(-80.0),
        20_500: _band_report(-86.0),
        21_000: _band_report(-88.5),
        21_500: _band_report(-89.5),
    }
    ordinary = _analyzer(ReplayRunner(_statistics_report(), same_audio)).analyze(
        tmp_path / "fake.flac"
    )
    deep = FfmpegQualityAnalyzer(
        ReplayRunner(_statistics_report(), same_audio),
        logging.getLogger("test.quality"),
        frequencies=DEEP_PROBE_FREQUENCIES,
        seconds=None,
    ).analyze(tmp_path / "fake.flac")
    assert ordinary is not None and deep is not None

    # The rulers genuinely disagree about the number — that is the hazard.
    assert ordinary.spectral.floor_from_hertz != deep.spectral.floor_from_hertz
    # And they must still agree about the file.
    assert judge(ordinary).encoding is judge(deep).encoding


def test_a_wall_below_the_trusted_line_is_still_a_transcode(tmp_path: Path) -> None:
    """The reprieve stops at the trusted line: below it, a wall still convicts.

    The same shape one rung lower. Nothing recorded stops at 17.5 kHz, so this
    is a filter whatever else is true of the file, and the floor rule convicts
    it outright.
    """
    runner = ReplayRunner(
        _statistics_report(),
        {
            13_000: _band_report(-35.0),
            14_000: _band_report(-38.0),
            15_000: _band_report(-41.0),
            16_000: _band_report(-43.7),
            17_500: _band_report(-62.9),
            19_000: _band_report(-95.0),
            20_000: _band_report(-101.0),
            21_500: _band_report(-104.0),
        },
    )

    analysis = _analyzer(runner).analyze(tmp_path / "fake.flac")
    assert analysis is not None
    verdict = judge(analysis)

    assert verdict.encoding is Encoding.TRANSCODED
    assert Finding.TRANSCODED in verdict.findings


def test_a_steep_but_alive_track_is_not_a_transcode(tmp_path: Path) -> None:
    """A steep fall over a live top band is not a transcode.

    A track cut through a vintage chain drops steeply between its highest
    bands — steepness to spare for a slope-only rule — while staying alive at
    -85 dB in the top one. Steepness alone would rename a genuine FLAC;
    steepness plus an empty top band is what a filter actually leaves.
    """
    runner = ReplayRunner(
        _statistics_report(),
        {
            13_000: _band_report(-38.0),
            14_000: _band_report(-41.0),
            15_000: _band_report(-44.0),
            16_000: _band_report(-46.7),
            17_500: _band_report(-50.0),
            19_000: _band_report(-54.9),
            20_000: _band_report(-68.1),
            21_500: _band_report(-85.1),
        },
    )

    analysis = _analyzer(runner).analyze(tmp_path / "vintage.flac")
    assert analysis is not None
    verdict = judge(analysis)

    assert verdict.encoding is Encoding.LOSSLESS
    assert Finding.TRANSCODED not in verdict.findings


def test_an_mp3_that_declares_more_than_it_holds_is_overstated(tmp_path: Path) -> None:
    """A 128 kbps stream relabelled 320 is caught by where its audio stops."""
    report = _statistics_report(codec="mp3").replace("bitrate: 900 kb/s", "bitrate: 320 kb/s")
    runner = ReplayRunner(
        report,
        {
            13_000: _band_report(-62.0),
            14_000: _band_report(-66.0),
            15_000: _band_report(-71.0),
            16_000: _band_report(-97.0),
            17_500: _band_report(-101.0),
            19_000: _band_report(-103.0),
            20_000: _band_report(-105.0),
            21_500: _band_report(-107.0),
        },
    )

    analysis = _analyzer(runner).analyze(tmp_path / "track.mp3")
    assert analysis is not None
    verdict = judge(analysis)

    assert verdict.encoding is Encoding.OVERSTATED
    assert verdict.effective_bitrate_kbps == 128
    assert Finding.OVERSTATED_BITRATE in verdict.findings


def test_bands_above_nyquist_are_never_measured(tmp_path: Path) -> None:
    """A 32 kHz stream must not look like a transcode for lacking 19 kHz content."""
    runner = ReplayRunner(
        _statistics_report(rate=32_000),
        {13_000: _band_report(-58.0), 14_000: _band_report(-60.0), 15_000: _band_report(-62.0)},
    )

    analysis = _analyzer(runner).analyze(tmp_path / "track.flac")

    assert analysis is not None
    # Its Nyquist limit is 16 kHz, so the rungs at and above it are never asked:
    # a band above a stream's own ceiling reads as silence for every file and
    # proves nothing. Three rungs remain, which is too few to hold a wall — a
    # wall needs a band above it.
    assert [band.hertz for band in analysis.spectral.bands] == [13_000, 14_000, 15_000]
    assert analysis.spectral.wall is None
    assert judge(analysis).encoding is Encoding.LOSSLESS


def test_a_quiet_sixteen_bit_master_is_not_called_inflated(tmp_path: Path) -> None:
    """An honest sixteen-bit master can read 15 of 16 bits.

    Quiet music does not swing through every bit, so reporting any shortfall as
    inflation would flag genuine albums.
    """
    runner = ReplayRunner(
        _statistics_report(depth=(15, 16, 16, 16)),
        {
            frequency: _band_report(-56.0 - index)
            for index, frequency in enumerate((16_000, 17_500, 19_000, 20_000, 21_500))
        },
    )

    analysis = _analyzer(runner).analyze(tmp_path / "quiet.flac")
    assert analysis is not None

    assert Finding.INFLATED_BIT_DEPTH not in judge(analysis).findings


def test_an_inflated_bit_depth_is_reported(tmp_path: Path) -> None:
    """A 24-bit container holding 16 bits of audio is the same lie as a fake FLAC."""
    runner = ReplayRunner(
        _statistics_report(depth=(16, 24, 24, 24)),
        {
            frequency: _band_report(-56.0 - index)
            for index, frequency in enumerate((16_000, 17_500, 19_000, 20_000, 21_500))
        },
    )

    analysis = _analyzer(runner).analyze(tmp_path / "track.flac")
    assert analysis is not None

    assert Finding.INFLATED_BIT_DEPTH in judge(analysis).findings


def test_clipping_and_offset_are_reported_alongside_the_encoding(tmp_path: Path) -> None:
    """A transcode that also clips is reported as both, not as whichever came first."""
    runner = ReplayRunner(
        _statistics_report(peak=-0.02, dc=0.05, peaks=9_000),
        # Alive below the wall and flat above it, because a wall is a step and
        # not a level: five dead bands with nothing under them are a dark
        # master, and would convict nothing.
        {
            13_000: _band_report(-60.0),
            14_000: _band_report(-64.0),
            15_000: _band_report(-69.0),
            **{f: _band_report(-99.0) for f in (16_000, 17_500, 19_000, 20_000, 21_500)},
        },
    )

    analysis = _analyzer(runner).analyze(tmp_path / "loud.flac")
    assert analysis is not None
    verdict = judge(analysis)

    assert verdict.encoding is Encoding.TRANSCODED
    assert Finding.CLIPPING in verdict.findings
    assert Finding.DC_OFFSET in verdict.findings


def test_a_verdict_always_explains_itself(tmp_path: Path) -> None:
    """Every decision carries its reason."""
    runner = ReplayRunner(
        _statistics_report(),
        {frequency: _band_report(-60.0) for frequency in (16_000, 17_500, 19_000, 20_000, 21_500)},
    )
    analysis = _analyzer(runner).analyze(tmp_path / "track.flac")
    assert analysis is not None

    assert judge(analysis).reason.strip() != ""


def test_an_unreadable_file_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """One damaged file must not abort the analysis of a whole library."""

    class FailingRunner:
        def run(self, arguments):
            raise OSError("ffmpeg is not installed")

    assert _analyzer(FailingRunner()).analyze(tmp_path / "track.flac") is None


def test_a_file_ffmpeg_will_not_release_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """A timeout is a SubprocessError, not an OSError, and is caught as well.

    Measuring a library is hours of decoding; one file ffmpeg hangs on must
    cost that file, not the survey.
    """

    class HangingRunner:
        def run(self, arguments):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=120.0)

    assert _analyzer(HangingRunner()).analyze(tmp_path / "track.flac") is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_real_file_is_measured_end_to_end() -> None:
    """The parsing must survive a real ffmpeg, not only a recorded report.

    The fixture is a synthesized tone, so its spectrum proves nothing about
    transcodes — what this asserts is that the command runs, the banner parses,
    and the statistics arrive.
    """
    analyzer = FfmpegQualityAnalyzer(SubprocessCommandRunner(), logging.getLogger("test.quality"))

    analysis = analyzer.analyze(FIXTURES / "tone.flac")

    assert analysis is not None
    assert analysis.declared_codec == "flac"
    assert analysis.sample_rate == 44_100
    assert analysis.time_domain.peak_db < 0.0
    assert judge(analysis).reason.strip() != ""


def test_spectral_profile_reports_the_steepest_drop() -> None:
    """The shape, not any single band, is what separates a filter from music."""
    profile = SpectralProfile(
        bands=(
            BandEnergy(16_000, -40.0),
            BandEnergy(17_500, -45.0),
            BandEnergy(19_000, -75.0),
        )
    )

    assert profile.steepest_drop_db == pytest.approx(30.0)
    assert profile.cutoff_hertz is None


def test_a_silent_stream_is_undecided_rather_than_lossless() -> None:
    """Nothing can be concluded from a file that carries no signal."""
    analysis = TrackAnalysis(
        path=Path("silent.flac"),
        declared_codec="flac",
        sample_rate=44_100,
        spectral=SpectralProfile(bands=(BandEnergy(16_000, -120.0),)),
        time_domain=TimeDomainStats(
            peak_db=-120.0,
            rms_db=-120.0,
            dc_offset=0.0,
            flat_factor=0.0,
            clipped_samples=0,
            noise_floor_db=-120.0,
        ),
    )

    verdict = judge(analysis)

    assert verdict.encoding is Encoding.UNDECIDED
    assert Finding.SILENT in verdict.findings


def _album_analyses(
    drops: tuple[float, ...],
    codec: str = "flac",
    ceiling_db: float | tuple[float, ...] = -60.0,
) -> list[TrackAnalysis]:
    """Build one analysis per track, with a chosen fall and a chosen ceiling."""
    ceilings = ceiling_db if isinstance(ceiling_db, tuple) else (ceiling_db,) * len(drops)
    return [
        TrackAnalysis(
            path=Path(f"{index:02d}.{codec}"),
            declared_codec=codec,
            sample_rate=44_100,
            spectral=SpectralProfile(
                bands=(
                    BandEnergy(16_000, -50.0),
                    BandEnergy(19_000, -50.0 - drop),
                    BandEnergy(21_500, ceilings[index]),
                )
            ),
            time_domain=TimeDomainStats(
                peak_db=-1.0,
                rms_db=-14.0,
                dc_offset=0.0,
                flat_factor=0.0,
                clipped_samples=0,
                noise_floor_db=-60.0,
            ),
        )
        for index, drop in enumerate(drops)
    ]


def test_an_album_is_judged_by_its_median_not_its_worst_track() -> None:
    """One bright track drags any maximum across the line.

    The worst track here falls 10.4 dB — above the single-track suspicion —
    while the album's median stays where a master belongs.
    """
    analyses = _album_analyses((2.9, 4.1, 5.0, 6.8, 7.2, 8.9, 10.4))

    verdict = judge_album(analyses)

    assert verdict.encoding is Encoding.LOSSLESS
    assert verdict.median_drop_db == pytest.approx(6.8)


def test_an_album_that_falls_off_a_cliff_is_a_transcode() -> None:
    """An album most of whose files wall is a transcode, and says what proved it.

    It is condemned by a majority of its files walling at or below the trusted
    ceiling — three of five — so the sentence names the wall and not a median
    fall, which is a number that did not decide it. A screen never names a
    cause that was not measured.
    """
    analyses = _album_analyses((16.0, 19.2, 21.0, 24.0, 34.7), ceiling_db=-90.0)

    verdict = judge_album(analyses)

    assert verdict.encoding is Encoding.TRANSCODED
    assert verdict.proved_by is Proof.WALL
    assert "3 of this album's 5 files stop dead" in verdict.reason


def test_an_album_that_descends_unbroken_is_not_a_transcode() -> None:
    """A steep descent that never breaks is not a transcode.

    The album falls 12.4 dB from 16 to 19 kHz — steeper than several genuine
    masters — but its energy keeps going, unbroken, to the top band. Slope
    alone would convict it. Slope plus what is left above it does not.
    """
    analyses = _album_analyses((9.0, 11.0, 12.4, 13.0, 14.0), ceiling_db=-85.1)

    verdict = judge_album(analyses)

    assert verdict.encoding is Encoding.LOSSLESS


def test_an_album_walled_below_twenty_kilohertz_is_a_transcode() -> None:
    """A 128-class fake needs no slope arithmetic: its audio stops dead at 16 kHz.

    The band reads -98 dB there, and a majority of walled tracks convicts the
    album outright. The rungs below the wall matter: what convicts is the step
    into silence, not the silence — five dead bands with nothing underneath
    them are a dark master, which is never convicted.
    """
    analyses = [
        TrackAnalysis(
            path=Path(f"{index:02d}.flac"),
            declared_codec="flac",
            sample_rate=44_100,
            spectral=SpectralProfile(
                bands=(
                    BandEnergy(13_000, -61.0),
                    BandEnergy(14_000, -64.5),
                    BandEnergy(15_000, -69.2),
                    BandEnergy(16_000, -98.4),
                    BandEnergy(19_000, -103.8),
                    BandEnergy(21_500, -111.5),
                )
            ),
            time_domain=TimeDomainStats(
                peak_db=-1.0,
                rms_db=-14.0,
                dc_offset=0.0,
                flat_factor=0.0,
                clipped_samples=0,
                noise_floor_db=-60.0,
            ),
        )
        for index in range(3)
    ]

    verdict = judge_album(analyses)

    assert verdict.encoding is Encoding.TRANSCODED


def _walled_track(path: str, codec: str) -> TrackAnalysis:
    """One track that stops dead at 16 kHz, in whichever container is asked for."""
    return TrackAnalysis(
        path=Path(path),
        declared_codec=codec,
        sample_rate=44_100,
        spectral=SpectralProfile(
            bands=(
                BandEnergy(13_000, -61.0),
                BandEnergy(14_000, -64.5),
                BandEnergy(15_000, -69.2),
                BandEnergy(16_000, -98.4),
                BandEnergy(19_000, -103.8),
                BandEnergy(21_500, -111.5),
            )
        ),
        time_domain=TimeDomainStats(
            peak_db=-1.0,
            rms_db=-14.0,
            dc_offset=0.0,
            flat_factor=0.0,
            clipped_samples=0,
            noise_floor_db=-60.0,
        ),
        declared_bitrate=128 if codec == "mp3" else None,
    )


def test_the_lossy_half_of_a_mixed_folder_does_not_convict_the_lossless_half() -> None:
    """A wall below 19 kHz is not evidence against an MP3 — it is what an MP3
    is — so the MP3s of a mixed folder must not answer a question that was
    never about them.

    Ten honest 128 kbps MP3s and one untouched FLAC: counting the walls over
    every track makes ten against eleven convict the album, and `[Lossy]` is
    then written onto the folder and onto the FLAC nothing had touched.
    """
    honest_flac = TrackAnalysis(
        path=Path("11 Bonus.flac"),
        declared_codec="flac",
        sample_rate=44_100,
        spectral=SpectralProfile(
            bands=(
                BandEnergy(13_000, -38.0),
                BandEnergy(14_000, -40.0),
                BandEnergy(15_000, -42.0),
                BandEnergy(16_000, -44.0),
                BandEnergy(19_000, -47.0),
                BandEnergy(21_500, -50.0),
            )
        ),
        time_domain=TimeDomainStats(
            peak_db=-1.0,
            rms_db=-14.0,
            dc_offset=0.0,
            flat_factor=0.0,
            clipped_samples=0,
            noise_floor_db=-60.0,
        ),
    )
    mixed = [_walled_track(f"{index:02d}.mp3", "mp3") for index in range(10)] + [honest_flac]

    verdict = judge_album(mixed)

    assert verdict.encoding is not Encoding.TRANSCODED


def test_a_walled_lossless_track_beside_honest_mp3s_is_still_convicted() -> None:
    """The complement of the test above, so the rule cannot be "never convict a
    mixed folder": the question is asked of the lossless containers, and when
    every one of them is walled the answer is still yes."""
    mixed = [_walled_track(f"{index:02d}.mp3", "mp3") for index in range(10)] + [
        _walled_track("11.flac", "flac")
    ]

    assert judge_album(mixed).encoding is Encoding.TRANSCODED


def test_a_dark_master_with_an_empty_top_band_is_not_convicted() -> None:
    """Only the unmistakable is convicted.

    Genuine 1970s masters can sit at -96 to -104 dB above 21.5 kHz and keep
    falling the whole way. An empty top band alone is a shape honest audio
    produces, so alone it convicts nothing.
    """
    analyses = _album_analyses((5.0, 6.0, 7.0), ceiling_db=-104.0)

    verdict = judge_album(analyses)

    assert verdict.encoding is Encoding.LOSSLESS


def test_the_album_verdict_names_the_tracks_that_disagree() -> None:
    """The one transcode inside an honest album is what deserves its own mark.

    The dissenter must be unmistakable — cliff AND empty top band. A steep
    track with a live ceiling does not count.
    """
    analyses = _album_analyses((3.0, 4.0, 5.0, 40.0), ceiling_db=(-60.0, -60.0, -60.0, -92.0))

    verdict = judge_album(analyses)

    assert verdict.encoding is Encoding.LOSSLESS
    assert len(verdict.dissenting_tracks) == 1
    assert verdict.dissenting_tracks[0].encoding is Encoding.TRANSCODED


def test_an_album_with_nothing_measurable_is_undecided() -> None:
    """An album of unreadable files is reported, never guessed at."""
    assert judge_album([]).encoding is Encoding.UNDECIDED


def test_ffmpeg_is_found_where_it_lives_when_path_is_empty(monkeypatch) -> None:
    """An app launched from the Dock inherits no shell PATH.

    Homebrew's ffmpeg is invisible to `shutil.which` there, even though every
    terminal on the machine finds it, so the quality survey would disable
    itself in the window while passing every test that runs from a shell.
    """
    monkeypatch.setattr(analysis_module.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        analysis_module, "KNOWN_FFMPEG_PATHS", ("/nowhere/ffmpeg", "/opt/homebrew/bin/ffmpeg")
    )
    monkeypatch.setattr(
        analysis_module.os, "access", lambda path, _: path == "/opt/homebrew/bin/ffmpeg"
    )

    assert analysis_module.find_ffmpeg() == "/opt/homebrew/bin/ffmpeg"


def test_an_absent_ffmpeg_is_reported_rather_than_guessed(monkeypatch) -> None:
    """No ffmpeg means no measurement, said out loud — never a clean bill of health."""
    monkeypatch.setattr(analysis_module.shutil, "which", lambda _: None)
    monkeypatch.setattr(analysis_module.os, "access", lambda *_: False)

    assert analysis_module.find_ffmpeg() is None


# --- borderline: the mark for what missed conviction by a hair ----------------


@pytest.mark.parametrize(
    ("decay_db", "ceiling_db", "expected", "case"),
    [
        (14.6, -89.0, True, "top band dark, fall 0.4 dB short"),
        (16.0, -87.0, True, "a real cliff over a top band not yet dark"),
        (16.0, -89.0, False, "both signs met is a conviction, not a question"),
        (5.3, -104.0, False, "dark up top and no cliff at all"),
        (10.5, -80.0, False, "simply runs out of treble"),
        (13.4, -89.0, False, "just outside the margin is outside it"),
    ],
)
def test_what_counts_as_borderline(
    decay_db: float, ceiling_db: float, expected: bool, case: str
) -> None:
    """One sign fully met and the other within reach — symmetric, both ways."""
    assert is_borderline(decay_db, ceiling_db) is expected, case


def test_a_borderline_album_keeps_the_verdict_it_earned() -> None:
    """The mark never renames anything: the album is still lossless."""
    verdict = judge_album(_album_analyses((14.6, 14.6, 14.7), ceiling_db=-89.0))

    assert verdict.encoding is Encoding.LOSSLESS, "a question is not a conviction"
    assert verdict.is_borderline


def test_a_convicted_album_is_never_also_borderline() -> None:
    """Two marks on one row would contradict each other."""
    verdict = judge_album(_album_analyses((20.0, 21.0, 22.0), ceiling_db=-92.0))

    assert verdict.encoding is Encoding.TRANSCODED
    assert not verdict.is_borderline


def test_a_comfortable_album_is_left_alone() -> None:
    """Noise here would be worse than silence: most of the library must stay quiet."""
    verdict = judge_album(_album_analyses((6.0, 7.0, 8.0), ceiling_db=-60.0))

    assert not verdict.is_borderline


def test_a_lossy_album_is_not_asked_the_question() -> None:
    """An MP3 is not accused of hiding a lossy origin; it declares one."""
    verdict = judge_album(_album_analyses((14.6, 14.6, 14.7), codec="mp3", ceiling_db=-89.0))

    assert verdict.encoding is Encoding.LOSSY
    assert not verdict.is_borderline


def test_a_verdict_rebuilt_without_a_ceiling_is_never_marked() -> None:
    """Rows taken before the two-sign rule carry no ceiling, so they carry no guess."""
    verdict = AlbumVerdict(
        encoding=Encoding.LOSSLESS, reason="replayed", tracks=(), median_drop_db=16.0
    )

    assert not verdict.is_borderline


# --- a report that will not decode is still a report --------------------------


def test_a_latin1_byte_in_the_report_does_not_raise() -> None:
    """ffmpeg echoes the file's tags, and a tag need not be UTF-8.

    Decoding strictly raises `UnicodeDecodeError` on the first such file and
    ends the survey there. The report is read for numbers, so a replaced byte
    costs nothing.
    """
    runner = SubprocessCommandRunner("/bin/sh")

    report = runner.run(["-c", "printf 'title: Fa\\xe7ade\\nRMS level dB: -14.0\\n' >&2"])

    assert "RMS level dB: -14.0" in report


def test_a_report_that_will_not_decode_costs_only_its_own_file() -> None:
    """Whatever the runner raises, the file is unanalyzed and the survey lives."""

    class Doomed:
        def run(self, arguments):
            raise UnicodeDecodeError("utf-8", b"\xe7", 0, 1, "invalid continuation byte")

    analyzer = FfmpegQualityAnalyzer(Doomed(), logging.getLogger("test.quality.decode"))

    assert analyzer.analyze(Path("whatever.flac")) is None


def test_a_dark_track_that_never_steps_is_not_a_wall() -> None:
    """A dark track that descends without a step has no wall.

    Its 19 kHz band reads -95.3 dBFS, so a rule with an absolute floor at -95.0
    convicts it by three tenths of a decibel, consulting neither the fall nor
    the ceiling. From 16 kHz up it descends 1.5, 2.1, 2.1 and then 7.9 dB,
    which accelerates at the top the way a master running out of treble does.
    It is dark, which is a thing music is allowed to be.
    """
    profile = SpectralProfile(
        bands=(
            BandEnergy(13_000, -90.1),
            BandEnergy(14_000, -90.8),
            BandEnergy(15_000, -91.7),
            BandEnergy(16_000, -91.7),
            BandEnergy(17_500, -93.2),
            BandEnergy(19_000, -95.3),
            BandEnergy(20_000, -97.4),
            BandEnergy(21_500, -105.3),
        )
    )

    assert profile.wall is None
    assert profile.cutoff_hertz is None
    assert profile.wall_low_hertz is None


def test_the_last_pair_is_never_a_wall() -> None:
    """A step into the topmost band is never a wall, however large.

    Every album ends by running out of treble, so the final descent to Nyquist
    is a shape all music has. A wall needs a band above it — audio below,
    nothing above, and room left to see that nothing is above.
    """
    profile = SpectralProfile(
        bands=(
            BandEnergy(16_000, -44.0),
            BandEnergy(17_500, -48.9),
            BandEnergy(19_000, -53.9),
            BandEnergy(20_000, -57.9),
            # A 32 dB step, and the last one there is.
            BandEnergy(21_500, -90.0),
        )
    )

    assert profile.wall is None


def test_a_straight_cut_is_a_wall_wherever_it_falls() -> None:
    """Transcodes an absolute floor never sees.

    Their walls land at -86.7 and -93.9 dBFS — above a floor of -95.0 — so a
    rule that waits for that level reports them at 21.5 kHz, which is nowhere
    near where either stopped. The step finds both.
    """
    at_128 = SpectralProfile(
        bands=(
            BandEnergy(13_000, -40.0),
            BandEnergy(14_000, -42.3),
            BandEnergy(15_000, -46.1),
            BandEnergy(16_000, -52.1),
            BandEnergy(17_500, -86.7),
            BandEnergy(19_000, -88.5),
            BandEnergy(20_000, -90.2),
            BandEnergy(21_500, -96.1),
        )
    )
    at_256 = SpectralProfile(
        bands=(
            BandEnergy(13_000, -38.0),
            BandEnergy(14_000, -40.2),
            BandEnergy(15_000, -42.3),
            BandEnergy(16_000, -44.3),
            BandEnergy(17_500, -50.4),
            BandEnergy(19_000, -62.3),
            BandEnergy(20_000, -93.9),
            BandEnergy(21_500, -99.7),
        )
    )

    assert at_128.wall == (16_000, 17_500)
    assert at_128.cutoff_hertz == 17_500
    assert at_128.wall_low_hertz == 16_000
    assert at_256.wall == (19_000, 20_000)
    assert at_256.cutoff_hertz == 20_000


def test_the_deep_rungs_stay_a_superset_of_the_ordinary_ones() -> None:
    """A deep measurement and an ordinary one must be comparable.

    The verdict reads the same rungs from both, so a fresh result can be held up
    against a stored one. Adding a rung to the ordinary set alone breaks that
    silently, so it is enforced here.
    """
    from diglibrary.quality.analysis import DEEP_PROBE_FREQUENCIES, PROBE_FREQUENCIES

    missing = sorted(set(PROBE_FREQUENCIES) - set(DEEP_PROBE_FREQUENCIES))
    assert missing == [], f"the deep pass no longer probes: {missing}"


def test_the_bitrate_is_named_from_the_rung_the_audio_came_out_of() -> None:
    """Transcodes whose rate is known, against the table that names them.

    A wall lies *between* two rungs, so which of the two names the bitrate is a
    real choice, and it reaches filenames. The lower rung — the last one that
    still held audio — is the one that names it. The upper rung would write 192
    into the name of a 128 kbps file.

    Where two bitrates share a rung it names neither: 256 and 320 both wall
    between 19 and 20 kHz, so that rung answers nothing. 128 and 160 keep
    sharing theirs and it keeps saying 128 — they are one bucket in what they
    mean, where 256 against 320 is the difference between a transcode and a
    good one.
    """
    from diglibrary.quality.verdict import _bitrate_for

    named = {
        "a 128 that walls a rung early": (15_000, 128),
        "a 128": (16_000, 128),
        "a 192": (17_500, 192),
    }
    for case, (wall_low, expected) in named.items():
        assert _bitrate_for(wall_low) == expected, case

    # A bucket, not a measurement. This one holds 128 and 160, and says 128.
    assert _bitrate_for(16_000) == 128, "a 160 reads as 128"
    # And this one holds 256 and 320, so it says nothing rather than pick.
    assert _bitrate_for(19_000) is None, "256 and 320 wall between the same rungs"

    # As does a stream with no wall at all.
    assert _bitrate_for(None) is None


def test_a_wall_above_the_trusted_frequency_still_convicts() -> None:
    """A 320 kbps encoder cuts between 19 and 20 kHz, and must not walk for it.

    `CUTOFF_TRUST_HZ` is 19 kHz, which is right when a cutoff means a silent
    band: silence above 19 kHz is a shape honest audio produces. A 20 dB step
    is not that shape. So a wall found by its step convicts wherever it falls,
    and the frequency guard goes on protecting readings that carry no step.
    """
    at_320 = TrackAnalysis(
        path=Path("320.flac"),
        declared_codec="flac",
        sample_rate=44_100,
        spectral=SpectralProfile(
            bands=(
                BandEnergy(16_000, -44.3),
                BandEnergy(17_500, -50.4),
                BandEnergy(19_000, -62.3),
                BandEnergy(20_000, -93.9),
                BandEnergy(21_500, -99.7),
            )
        ),
        time_domain=TimeDomainStats(
            peak_db=-1.0,
            rms_db=-14.0,
            dc_offset=0.0,
            flat_factor=0.0,
            clipped_samples=0,
            noise_floor_db=-60.0,
        ),
    )

    assert at_320.spectral.wall == (19_000, 20_000)
    assert at_320.spectral.cutoff_hertz == 20_000
    # Above the frequency the old shortcut would trust, and convicted anyway.
    assert at_320.spectral.cutoff_hertz > CUTOFF_TRUST_HZ
    assert judge(at_320).encoding is Encoding.TRANSCODED


def _cut_track(path: str, codec: str, levels: tuple[tuple[int, float], ...]) -> TrackAnalysis:
    """One track whose cumulative spectrum is exactly these readings."""
    return TrackAnalysis(
        path=Path(path),
        declared_codec=codec,
        sample_rate=44_100,
        spectral=SpectralProfile(bands=tuple(BandEnergy(h, v) for h, v in levels)),
        time_domain=TimeDomainStats(
            peak_db=-1.0,
            rms_db=-14.0,
            dc_offset=0.0,
            flat_factor=0.0,
            clipped_samples=0,
            noise_floor_db=-60.0,
        ),
        declared_bitrate=None,
    )


# A cut at 15 kHz: everything above it is one flat floor.
_CUT_AT_15K = (
    (13_000, -63.0),
    (14_000, -68.0),
    (15_000, -83.0),
    (16_000, -84.0),
    (17_500, -85.0),
    (19_000, -86.0),
    (20_000, -88.0),
    (21_500, -93.0),
)

# A record that is merely dark: it keeps falling the whole way, which is what a
# floor never does.
_MERELY_DARK = (
    (13_000, -56.0),
    (14_000, -60.0),
    (15_000, -64.0),
    (16_000, -68.0),
    (17_500, -72.0),
    (19_000, -76.0),
    (20_000, -79.0),
    (21_500, -84.0),
)


def test_a_cut_at_15_kilohertz_convicts_even_though_no_step_is_large_enough() -> None:
    """A cut is found by where the floor begins, not by the size of a step.

    Tracks sharing one cut at 15 kHz do not share a step, because a step
    shrinks with the music that was under it: a bright track falls far and a
    quiet one little, off the same cliff. Asking where the floor *begins* is a
    question the music's own loudness cannot answer wrongly.
    """
    analysis = _cut_track("/music/quiet.flac", "flac", _CUT_AT_15K)

    assert analysis.spectral.wall is None, "no single step here is large enough to be a wall"
    assert analysis.spectral.floor_from_hertz == 15_000
    assert judge(analysis).encoding is Encoding.TRANSCODED


def test_a_record_that_is_merely_dark_is_not_a_record_that_was_cut() -> None:
    """The expensive mistake: convicting a master for being dark.

    A genuine master falls a few decibels on the way to its floor, and a cut
    file falls far more. A floor that sits high is tape hiss and not an
    encoder.
    """
    analysis = _cut_track("/music/dark.flac", "flac", _MERELY_DARK)

    assert analysis.spectral.cliff_db < 12.0, "nothing was fallen off here"
    assert judge(analysis).encoding is Encoding.LOSSLESS


def test_the_floor_rule_says_where_the_audio_ended() -> None:
    """The frequency reaches the verdict, because the screen has to name it.

    Not a bitrate, though: `_bitrate_for` reads the rung below a *wall*, and a
    verdict with no bitrate says less and claims nothing false.
    """
    verdict = judge(_cut_track("/music/quiet.flac", "flac", _CUT_AT_15K))

    assert verdict.cutoff_hertz == 15_000
    assert verdict.effective_bitrate_kbps is None


def test_a_high_cut_is_left_to_the_rules_that_already_own_it() -> None:
    """Above `CUTOFF_TRUST_HZ` this rule says nothing, whatever it can see.

    A floor beginning at 19 or 20 kHz is a shape honest audio produces — early
    digital converters cut at 20 — and both `CUTOFF_TRUST_HZ` and the fall
    already decide that question with evidence of their own. Two rules answering
    one question in different words is how this window ends up disagreeing with
    itself.

    Asked of the rule itself rather than of `judge`, deliberately: this spectrum
    falls 20 dB into a dark ceiling, which is what the older rule convicts on,
    and asserting the whole verdict here would be asserting that rule's answer
    while claiming to test this one.
    """
    # A floor that begins at 20 kHz, reached by a cliff this rule would convict
    # anywhere lower — and by no single step the wall rule would call a wall, so
    # what answers here is the frequency and nothing else.
    high = (
        (13_000, -56.0),
        (14_000, -60.0),
        (15_000, -64.0),
        (16_000, -68.0),
        (17_500, -72.0),
        (19_000, -76.0),
        (20_000, -91.0),
        (21_500, -92.0),
    )
    spectrum = _cut_track("/music/high.flac", "flac", high).spectral

    assert spectrum.wall is None, "no step here is a wall"
    assert spectrum.cliff_db >= 12.0, "and the cliff alone would have convicted"
    assert spectrum.floor_from_hertz == 20_000
    assert _cut_where_no_recording_ends(spectrum) is None, "too high for this rule to answer"


def test_an_honest_mp3_is_not_re_accused_by_the_floor_rule() -> None:
    """A cut at 15 kHz is what a 128 kbps MP3 *is*, not a charge against it.

    Asking the transcode question of a lossy container would call most honest
    MP3 albums fakes.
    """
    assert judge(_cut_track("/music/honest.mp3", "mp3", _CUT_AT_15K)).encoding is not (
        Encoding.TRANSCODED
    )


def test_the_encoder_ruler_is_measured_and_not_borrowed_from_the_probes() -> None:
    """The reference lines are where encoders cut, not where this analyzer asks.

    `PROBE_FREQUENCIES` is where this analyzer *asks* rather than where an
    encoder *cuts*, and the two differ: a 192 kbps encode cuts at about 18 kHz,
    which is not a probe rung.

    Guarded by the shape of the mistake rather than by the numbers: a ruler that
    happens to land only on this instrument's own rungs is a ruler that was
    copied from it.
    """
    rungs = set(analysis_module.PROBE_FREQUENCIES)
    drawn = {hertz for hertz, _ in REFERENCE_WALLS}

    assert dict(REFERENCE_WALLS)[18_000] == 192, "192 kbps cuts at 18 kHz, not at a probe rung"
    assert drawn - rungs, (
        "every encoder reference sits on a probe frequency again, which puts "
        "the 192 mark a kilohertz low"
    )


def test_the_encoder_ruler_marks_three_rates_and_256_is_not_one_of_them() -> None:
    """Three marks share the top of the picture, and a fourth crowds it.

    256 sits a kilohertz under 320 and inside the spread both its neighbours
    show, so it adds a label to the one stretch where the judgement is made and
    no reading the other three do not already give.
    """
    assert [bitrate for _, bitrate in REFERENCE_WALLS] == [128, 192, 320]


def test_a_floor_that_begins_on_the_trusted_line_is_spared(tmp_path: Path) -> None:
    """The reprieve includes the line, because the line is also a rung.

    `CUTOFF_TRUST_HZ` is 19 kHz and 19 kHz is a rung of `PROBE_FREQUENCIES`, so
    a floor that really begins above it has nowhere else to land on the ordinary
    grid. Reading the last rung as *below* the line accuses files the deep grid
    places at 19.5 kHz or higher — the same file, two answers, which is exactly
    what one constant exists to prevent.
    """
    assert not track_is_transcoded(
        None, 30.0, -100.0, CUTOFF_TRUST_HZ
    ), "a floor beginning on the trusted line is spared, however steep the fall"
    assert track_is_transcoded(
        None, 30.0, -100.0, CUTOFF_TRUST_HZ - 1_000
    ), "and one that begins below it is still judged by fall and emptiness"
    assert not album_is_transcoded(30.0, -100.0, CUTOFF_TRUST_HZ), "the album rule agrees"

    # `_cut_where_no_recording_ends` runs first and hands `track_is_transcoded`
    # its cutoff, so sparing the line in one and not in the other convicts
    # exactly the files the reprieve spares. Both stop at the same frequency.
    on_the_line = SpectralProfile(
        bands=(
            BandEnergy(hertz=13_000, rms_db=-20.0),
            BandEnergy(hertz=14_000, rms_db=-24.0),
            BandEnergy(hertz=15_000, rms_db=-28.0),
            BandEnergy(hertz=16_000, rms_db=-32.0),
            BandEnergy(hertz=17_500, rms_db=-36.0),
            BandEnergy(hertz=19_000, rms_db=-80.0),
            BandEnergy(hertz=20_000, rms_db=-81.0),
            BandEnergy(hertz=21_500, rms_db=-82.0),
        )
    )
    assert on_the_line.floor_from_hertz == CUTOFF_TRUST_HZ
    assert on_the_line.cliff_db > CLIFF_DB, "the fall is steep enough to have counted"
    assert (
        _cut_where_no_recording_ends(on_the_line) is None
    ), "the cut rule stops at the same frequency the reprieve does"


def test_a_wall_below_the_line_still_convicts_whatever_the_floor_says(tmp_path: Path) -> None:
    """The reprieve is not a way out for a proven cut.

    Nothing silences 16 kHz but a filter, so a trusted wall convicts before the
    floor is consulted at all. This is the guard on the reprieve above.
    """
    assert track_is_transcoded(
        16_000, 3.0, -60.0, 20_000
    ), "a wall inside the trusted band convicts even with a floor above the line"


def test_a_fall_shallower_than_the_cliff_is_darkness_and_not_a_cut() -> None:
    """`CLIFF_DB` separates *cut off* from merely *dark*.

    A file whose floor begins at 16 kHz after a fall short of the line is
    called dark, and one that falls further is a fake — the two spectra below
    differ in nothing else. The line sits where it convicts fewer honest files,
    which is the direction to choose whenever it moves.
    """

    def spectrum(floor_db: float) -> SpectralProfile:
        return SpectralProfile(
            bands=(
                BandEnergy(hertz=13_000, rms_db=-20.0),
                BandEnergy(hertz=14_000, rms_db=-24.0),
                BandEnergy(hertz=15_000, rms_db=-28.0),
                BandEnergy(hertz=16_000, rms_db=floor_db),
                BandEnergy(hertz=17_500, rms_db=floor_db - 1.0),
                BandEnergy(hertz=19_000, rms_db=floor_db - 2.0),
                BandEnergy(hertz=20_000, rms_db=floor_db - 3.0),
                BandEnergy(hertz=21_500, rms_db=floor_db - 4.0),
            )
        )

    dark, cut = spectrum(-36.0), spectrum(-40.0)

    assert dark.floor_from_hertz == cut.floor_from_hertz == 16_000
    assert 12.0 < dark.cliff_db < CLIFF_DB, "the shallower one falls between 12 dB and the line"
    assert cut.cliff_db > CLIFF_DB

    assert (
        _cut_where_no_recording_ends(dark) is None
    ), "a fall short of the line is darkness, and darkness accuses nobody"
    assert (
        _cut_where_no_recording_ends(cut) == 16_000
    ), "and a real cut at the same frequency is still a cut"


def test_a_frame_grid_convicts_a_flac_the_spectrum_alone_would_pass(tmp_path: Path) -> None:
    """The whole reason this measurement exists.

    These readings are a spectrum that fades gradually and lives to 21.5 kHz —
    the exact shape the rule calls lossless, and the shape a 320 kbps encode
    with no low-pass has. What convicts is the other measurement: one alignment
    of 576 standing far above the rest, agreed on by all six readings.
    """
    runner = ReplayRunner(
        _statistics_report(),
        {
            13_000: _band_report(-46.0),
            14_000: _band_report(-49.5),
            15_000: _band_report(-52.8),
            16_000: _band_report(-56.3),
            17_500: _band_report(-60.2),
            19_000: _band_report(-63.9),
            20_000: _band_report(-67.0),
            21_500: _band_report(-70.8),
        },
    )
    analysis = _analyzer(runner).analyze(tmp_path / "track.flac")
    assert analysis is not None
    assert judge(analysis).encoding is Encoding.LOSSLESS, "the spectrum alone says lossless"

    caught = judge(replace(analysis, frame_grid=FrameGrid(28.6, 319, agreeing_readings=6)))

    assert caught.encoding is Encoding.TRANSCODED
    assert Finding.TRANSCODED in caught.findings
    assert "MP3" in caught.reason, "the sentence has to name what the file was"
    assert "all six readings agree" in caught.reason


def test_a_conviction_on_four_readings_does_not_say_all_six_agreed(tmp_path: Path) -> None:
    """The sentence states the agreement that was measured, and no more than that.

    Four readings of six convict. A sentence written when all six were required
    would go on saying *all six* about exactly the files the lower bar reaches,
    which is a screen naming something it did not measure.
    """
    runner = ReplayRunner(
        _statistics_report(),
        {
            13_000: _band_report(-46.0),
            14_000: _band_report(-49.5),
            15_000: _band_report(-52.8),
            16_000: _band_report(-56.3),
            17_500: _band_report(-60.2),
            19_000: _band_report(-63.9),
            20_000: _band_report(-67.0),
            21_500: _band_report(-70.8),
        },
    )
    analysis = _analyzer(runner).analyze(tmp_path / "track.flac")
    assert analysis is not None

    caught = judge(replace(analysis, frame_grid=FrameGrid(12.4, 319, agreeing_readings=4)))

    assert caught.encoding is Encoding.TRANSCODED
    assert "four of the six readings agree" in caught.reason
    assert "all six" not in caught.reason


def test_the_frame_grid_convicts_only_on_both_halves_of_the_rule(tmp_path: Path) -> None:
    """z ≥ 8 **and** the readings agreeing.

    Each half alone is weaker: honest audio can peak high on one reading, and a
    single reading agreeing with itself is one draw rather than six. A file
    that fails either half is left exactly as the spectrum found it.
    """
    runner = ReplayRunner(
        _statistics_report(),
        {
            13_000: _band_report(-46.0),
            14_000: _band_report(-49.5),
            15_000: _band_report(-52.8),
            16_000: _band_report(-56.3),
            17_500: _band_report(-60.2),
            19_000: _band_report(-63.9),
            20_000: _band_report(-67.0),
            21_500: _band_report(-70.8),
        },
    )
    analysis = _analyzer(runner).analyze(tmp_path / "track.flac")
    assert analysis is not None

    loud_but_alone = judge(replace(analysis, frame_grid=FrameGrid(202.3, 319, 1)))
    agreed_but_faint = judge(replace(analysis, frame_grid=FrameGrid(7.9, 319, 6)))
    unmeasured = judge(replace(analysis, frame_grid=None))

    assert loud_but_alone.encoding is Encoding.LOSSLESS, "a peak nobody corroborated"
    assert agreed_but_faint.encoding is Encoding.LOSSLESS, "agreement on a peak that is noise"
    assert unmeasured.encoding is Encoding.LOSSLESS, "unmeasured is not clean, and not guilty"


def test_a_frame_grid_says_nothing_about_a_file_that_declares_itself_lossy(
    tmp_path: Path,
) -> None:
    """An MP3 in an MP3 is not lying, and its grid is not evidence of anything.

    Every MP3 would fire — of course it would, it *is* an MP3. Convicting on
    that would relabel honest lossy files as transcodes.
    """
    runner = ReplayRunner(
        _statistics_report(codec="mp3"),
        {
            13_000: _band_report(-46.0),
            14_000: _band_report(-49.5),
            15_000: _band_report(-52.8),
            16_000: _band_report(-56.3),
            17_500: _band_report(-95.0),
            19_000: _band_report(-96.0),
            20_000: _band_report(-96.4),
            21_500: _band_report(-96.6),
        },
    )
    analysis = _analyzer(runner).analyze(tmp_path / "track.mp3")
    assert analysis is not None

    verdict = judge(replace(analysis, frame_grid=FrameGrid(120.0, 15, agreeing_readings=6)))

    assert verdict.encoding is not Encoding.TRANSCODED
    assert "MP3 before" not in verdict.reason


def test_every_pcm_flavour_is_a_lossless_container_not_the_four_that_were_listed(
    tmp_path: Path,
) -> None:
    """Big-endian PCM is as lossless a container as little-endian PCM.

    A set written by enumeration — `pcm_s16le` and `pcm_s24le` — leaves out
    AIFF, which reports `pcm_s16be` or `pcm_s24be`. Those files would be judged
    as if their container promised nothing, so no transcode verdict would reach
    them and none would be asked for a frame grid. A set spelled out one member
    at a time answers wrongly for the member nobody thought of.

    Written by what PCM *is* instead: uncompressed samples, whatever the width,
    the endianness or the signedness. A codec ffmpeg has not shipped yet still
    gets the right answer.
    """
    for codec in ("pcm_s16be", "pcm_s24be", "pcm_s16le", "pcm_s24le", "pcm_f32le", "pcm_u8"):
        runner = ReplayRunner(
            _statistics_report(codec=codec),
            {
                13_000: _band_report(-46.0),
                14_000: _band_report(-49.5),
                15_000: _band_report(-52.8),
                16_000: _band_report(-95.0),
                17_500: _band_report(-96.0),
                19_000: _band_report(-96.2),
                20_000: _band_report(-96.3),
                21_500: _band_report(-96.4),
            },
        )
        analysis = _analyzer(runner).analyze(tmp_path / f"track-{codec}.wav")
        assert analysis is not None
        assert analysis.is_lossless_container, f"{codec} holds uncompressed samples"
        assert (
            judge(analysis).encoding is Encoding.TRANSCODED
        ), f"a wall at 16 kHz inside {codec} is the same lie it is inside FLAC"

    lossy = ReplayRunner(_statistics_report(codec="aac"), {13_000: _band_report(-46.0)})
    heard = _analyzer(lossy).analyze(tmp_path / "track.m4a")
    assert heard is not None
    assert not heard.is_lossless_container, "a lossy codec promises nothing to break"


def _grid_caught_album(count: int, grid: FrameGrid | None) -> list[TrackAnalysis]:
    """`count` tracks whose spectrum reads lossless, carrying this grid reading."""
    bands = tuple(
        BandEnergy(hertz=hertz, rms_db=level)
        for hertz, level in (
            (13_000, -46.0),
            (14_000, -49.5),
            (15_000, -52.8),
            (16_000, -56.3),
            (17_500, -60.2),
            (19_000, -63.9),
            (20_000, -67.0),
            (21_500, -70.8),
        )
    )
    waveform = TimeDomainStats(
        peak_db=-1.2,
        rms_db=-14.0,
        dc_offset=0.0,
        flat_factor=0.0,
        clipped_samples=0,
        noise_floor_db=-60.0,
        effective_bit_depth=16,
        container_bit_depth=16,
    )
    return [
        TrackAnalysis(
            path=Path(f"{index:02d}.flac"),
            declared_codec="flac",
            sample_rate=44_100,
            spectral=SpectralProfile(bands=bands),
            time_domain=waveform,
            frame_grid=grid,
        )
        for index in range(count)
    ]


def test_an_album_whose_tracks_the_grid_caught_is_a_transcoded_album() -> None:
    """The album is the unit that is shown and named.

    Every track here is convicted by its frame grid, and every track's spectrum
    reads lossless — which is the whole point of the measurement. The spectral
    medians alone would answer `lossless` about an album of ten transcodes, so
    the album asks the tracks it has just judged: the album is what the screen
    shows and the folder is named for, and it must not contradict them.
    """
    caught = judge_album(_grid_caught_album(10, FrameGrid(30.0, 319, agreeing_readings=6)))

    assert caught.encoding is Encoding.TRANSCODED
    assert all(track.encoding is Encoding.TRANSCODED for track in caught.tracks)


def test_a_few_caught_tracks_do_not_convict_the_album_that_holds_them() -> None:
    """The majority decides, exactly as it does for walls.

    A compilation with three laundered tracks among ten is not a transcoded
    album, and calling it one would write `[Lossy]` onto seven files nobody
    measured as anything of the kind. Those three still say what they are, on
    their own rows.
    """
    analyses = _grid_caught_album(3, FrameGrid(30.0, 319, agreeing_readings=6))
    analyses += _grid_caught_album(7, None)

    mixed = judge_album(analyses)

    assert mixed.encoding is Encoding.LOSSLESS, "three of ten is not the album"
    assert sum(1 for track in mixed.tracks if track.encoding is Encoding.TRANSCODED) == 3


def test_both_views_are_always_swept_because_the_trace_may_live_in_the_mid() -> None:
    """Skipping the second view would hide the view that matters.

    While unanimity is the question, a first view whose three readings already
    scatter cannot become six that agree, and the second sweep is arithmetic
    nobody needs. The question is *how many of the six*, and four of six is a
    conviction — which a scattered first view can still reach, on a mid signal
    that is exactly where a joint-stereo encoder quantizes.
    """
    import numpy as np

    from diglibrary.quality import framing

    calls: list[int] = []
    real_sweep = framing.sweep

    def counting_sweep(signal, alphas=framing.ALPHAS):
        calls.append(1)
        return real_sweep(signal, alphas)

    framing.sweep = counting_sweep
    try:
        # Noise: no encoder ever touched it, so the readings scatter.
        rng = np.random.default_rng(299)
        samples = rng.standard_normal((512 + framing.GRANULE * 80, 2)).astype(np.float32) * 0.2
        reading = framing.measure(samples)
    finally:
        framing.sweep = real_sweep

    assert reading is not None
    assert not reading.readings_agree, "noise has no grid to agree on"
    assert reading.agreeing_readings < 4, "noise must not reach the conviction bar"
    assert len(calls) == 2, "both views are swept; the count is what is asked now"


def test_a_rate_no_encoder_writes_at_cannot_carry_a_grid() -> None:
    """The sweep looks for a grid an encoder framed; resampling erases it.

    A 128 kbps encode decoded and written at 44,100 Hz reads a strong grid with
    every reading agreeing; the same audio written at 96,000 Hz reads what an
    honest FLAC reads. So the answer at an unlisted rate is not evidence either
    way.
    """
    assert rate_could_carry_a_grid(44_100)
    assert rate_could_carry_a_grid(48_000)
    assert not rate_could_carry_a_grid(96_000)
    assert not rate_could_carry_a_grid(88_200)
    assert not rate_could_carry_a_grid(None), "an unread rate is not a rate that could carry one"


def test_the_wall_sentence_never_names_the_encoder_as_the_cause() -> None:
    """A rung is where an encoder *habitually* cuts, over a ±0.5 kHz spread.

    `REFERENCE_WALLS` says so in its own docstring, so the sentence built from
    it must not say `the signature of a 128 kbps encoder` — that states as
    established the one thing the wall alone cannot establish.
    """
    verdict = judge(replace(_walled_track("a.flac", "flac"), declared_bitrate=None))

    assert verdict.encoding is Encoding.TRANSCODED
    assert "signature of a" not in verdict.reason
    assert "habitually cuts" in verdict.reason
    assert "16000 Hz" in verdict.reason, "the measured fact still leads the sentence"

    # The sibling sentence: an overstated file goes out of another branch with
    # another string, which must not state the same ±0.5 kHz habit as a fact
    # either (`where a 128 kbps encoder stops`).
    overstated = judge(replace(_walled_track("b.mp3", "mp3"), declared_bitrate=320))

    assert overstated.encoding is Encoding.OVERSTATED
    assert "habitually stops" in overstated.reason


def test_a_wall_at_a_resampled_rate_says_the_grid_could_not_be_read() -> None:
    """The corroborating reading is unavailable there, and silence reads as clean.

    A 96 kHz file that walls at 16 kHz is convicted with a sentence about where
    an encoder habitually cuts, while the one measurement that could second
    that has no chance of finding anything there. The sentence says so.
    """
    resampled = judge(replace(_walled_track("a.flac", "flac"), sample_rate=96_000))
    ordinary = judge(_walled_track("b.flac", "flac"))

    assert "cannot be read at 96000 Hz" in resampled.reason
    assert "resampling erases the grid" in resampled.reason
    assert "cannot be read" not in ordinary.reason, "44,100 Hz says nothing about a grid"
