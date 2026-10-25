"""The encoder's low-pass, read frame by frame (`quality.edges`) and the rule on it."""

from dataclasses import replace
from pathlib import Path

import numpy as np

from diglibrary.application.quality import stored_track_is_transcoded
from diglibrary.database.library_store import StoredQuality
from diglibrary.quality.edges import (
    EDGE_CEILING_HZ,
    EDGE_FRAME,
    FilterEdge,
    FilterEdgeProbe,
    measure_edges,
)
from diglibrary.quality.models import (
    BandEnergy,
    Encoding,
    Proof,
    SpectralProfile,
    TimeDomainStats,
    TrackAnalysis,
)
from diglibrary.quality.verdict import (
    CUTOFF_TRUST_HZ,
    EDGE_SHARE_TRUST,
    convicted_by_filter_edge,
    judge,
    judge_album,
)

RATE = 44_100


def _noise(seconds: float, seed: int = 7) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(int(seconds * RATE)).astype(np.float32) * 0.1


def _brick_wall(signal: np.ndarray, hertz: float) -> np.ndarray:
    """Remove everything above ``hertz``, frame by frame, as an encoder's low-pass does."""
    out = np.zeros_like(signal)
    for start in range(0, len(signal) - EDGE_FRAME + 1, EDGE_FRAME):
        spectrum = np.fft.rfft(signal[start : start + EDGE_FRAME])
        spectrum[np.fft.rfftfreq(EDGE_FRAME, 1.0 / RATE) > hertz] = 0
        out[start : start + EDGE_FRAME] = np.fft.irfft(spectrum, EDGE_FRAME)
    return out


def _gentle(signal: np.ndarray) -> np.ndarray:
    """A treble that runs out the way music does: falling, never stopping."""
    spectrum = np.fft.rfft(signal)
    frequencies = np.fft.rfftfreq(len(signal), 1.0 / RATE)
    spectrum *= 10 ** (-np.clip(frequencies - 8_000, 0, None) / 1_000 * 3 / 20)
    return np.fft.irfft(spectrum, len(signal)).astype(np.float32)


def test_audio_cut_at_one_frequency_stops_there_in_almost_every_frame() -> None:
    edge = measure_edges(_brick_wall(_noise(20), 16_000), RATE)

    assert edge is not None and edge.hertz is not None
    assert abs(edge.hertz - 16_000) <= 150
    assert edge.share > 0.9


def test_audio_that_runs_out_of_treble_has_no_place_it_stops() -> None:
    edge = measure_edges(_gentle(_noise(20)), RATE)

    assert edge is not None
    assert edge.share < EDGE_SHARE_TRUST


def _held_chord(seconds: float) -> np.ndarray:
    """A synthesizer holding one note: harmonics as fixed lines over a quiet hiss.

    A resonant filter makes its highest harmonic, at 11.2 kHz, the loudest, and
    above it there is only hiss — the shape that was convicted.
    """
    t = np.arange(int(seconds * RATE)) / RATE
    tone = sum(np.sin(2 * np.pi * 1_400.0 * k * t) * k / 8 for k in range(1, 9))
    hiss = np.random.default_rng(5).standard_normal(len(t)) * 1e-5
    return (0.05 * tone + hiss).astype(np.float32)


def test_a_held_note_is_not_a_wall() -> None:
    """Right above a strong harmonic the level drops by tens of decibels, every frame.

    Read between two narrow windows that is an edge at a fixed frequency, which
    is what this module counts — and a real library has held synth chords and
    drones that were convicted that way. One line among the bins of a wide
    window barely moves its level.
    """
    edge = measure_edges(_held_chord(20), RATE)

    assert edge is not None
    assert not convicted_by_filter_edge(edge)


def test_a_cut_above_the_trusted_line_is_not_looked_for() -> None:
    """An honest converter's anti-aliasing filter is an edge exactly like an encoder's."""
    edge = measure_edges(_brick_wall(_noise(20), 20_000), RATE)

    assert edge is not None
    assert edge.share < EDGE_SHARE_TRUST


def test_a_wall_hidden_under_dither_rising_to_nyquist_is_still_found() -> None:
    """The case that blinds the spectral probes: every one of them reads the dither."""
    walled = _brick_wall(_noise(20), 16_000)
    rng = np.random.default_rng(3)
    hiss = rng.standard_normal(len(walled)).astype(np.float32) * 1e-4
    spectrum = np.fft.rfft(hiss)
    frequencies = np.fft.rfftfreq(len(hiss), 1.0 / RATE)
    spectrum *= (frequencies / (RATE / 2)) ** 6 * 30
    shaped = walled + np.fft.irfft(spectrum, len(hiss)).astype(np.float32)

    edge = measure_edges(shaped, RATE)

    assert edge is not None and edge.hertz is not None
    assert abs(edge.hertz - 16_000) <= 150
    assert convicted_by_filter_edge(edge)


def test_a_wall_that_comes_and_goes_is_counted_where_it_is() -> None:
    """Half the time cut at 16 kHz, half the time open: the average hides it, the count does not."""
    open_part = _gentle(_noise(10, seed=1))
    cut_part = _brick_wall(_noise(10, seed=2), 16_000)

    edge = measure_edges(np.concatenate([open_part, cut_part]), RATE)

    assert edge is not None and edge.hertz is not None
    assert abs(edge.hertz - 16_000) <= 150
    assert 0.3 < edge.share < 0.7
    assert convicted_by_filter_edge(edge)


def test_too_little_audio_is_not_a_reading() -> None:
    assert measure_edges(_noise(0.5), RATE) is None


def test_silence_is_measured_and_has_no_edge() -> None:
    edge = measure_edges(np.zeros(RATE * 5, dtype=np.float32), RATE)

    assert edge == FilterEdge(share=0.0, hertz=None)


def test_the_search_stops_at_the_line_the_verdict_trusts() -> None:
    """Two constants saying one thing drift apart, so this holds them together."""
    assert EDGE_CEILING_HZ == CUTOFF_TRUST_HZ


def test_the_rule_needs_the_share_and_a_trusted_frequency() -> None:
    assert convicted_by_filter_edge(FilterEdge(share=EDGE_SHARE_TRUST, hertz=16_000))
    assert not convicted_by_filter_edge(FilterEdge(share=EDGE_SHARE_TRUST - 0.01, hertz=16_000))
    assert not convicted_by_filter_edge(FilterEdge(share=0.9, hertz=CUTOFF_TRUST_HZ + 50))
    assert not convicted_by_filter_edge(FilterEdge(share=0.0, hertz=None))
    assert not convicted_by_filter_edge(None), "unmeasured is not clean, and not guilty"


class _Reader:
    def __init__(self, pcm: np.ndarray | None) -> None:
        self.pcm = pcm
        self.asked: list[tuple[float, float]] = []

    def read(self, path: Path, seconds: float, skip: float) -> np.ndarray | None:
        self.asked.append((seconds, skip))
        return self.pcm


def test_the_probe_reads_the_stretch_it_was_given_and_mixes_both_channels() -> None:
    mono = _brick_wall(_noise(20), 16_000)
    reader = _Reader(np.stack([mono, mono], axis=1))

    edge = FilterEdgeProbe(reader, seconds=60).measure(Path("x.flac"), RATE)

    assert reader.asked == [(60, 0.0)]
    assert edge is not None and convicted_by_filter_edge(edge)


def test_an_unreadable_file_is_not_a_clean_one() -> None:
    assert FilterEdgeProbe(_Reader(None), seconds=60).measure(Path("x.flac"), RATE) is None


def _lossless_looking(codec: str = "flac") -> TrackAnalysis:
    """Numbers the spectral rules call lossless: a fall that never stops, a live top band."""
    levels = (-46.0, -49.5, -52.8, -56.3, -60.2, -63.9, -67.0, -70.8)
    rungs = (13_000, 14_000, 15_000, 16_000, 17_500, 19_000, 20_000, 21_500)
    return TrackAnalysis(
        path=Path("track." + codec),
        declared_codec=codec,
        sample_rate=RATE,
        spectral=SpectralProfile(
            bands=tuple(
                BandEnergy(hertz, level) for hertz, level in zip(rungs, levels, strict=True)
            )
        ),
        time_domain=TimeDomainStats(
            peak_db=-1.0,
            rms_db=-18.0,
            dc_offset=0.0,
            flat_factor=0.0,
            clipped_samples=0,
            noise_floor_db=-90.0,
        ),
    )


def test_an_edge_convicts_a_flac_the_spectral_probes_would_pass() -> None:
    analysis = _lossless_looking()
    assert judge(analysis).encoding is Encoding.LOSSLESS, "the probes alone say lossless"

    caught = judge(replace(analysis, filter_edge=FilterEdge(share=0.62, hertz=16_050)))

    assert caught.encoding is Encoding.TRANSCODED
    assert caught.proved_by is Proof.WALL, "an edge is a wall, and a picture shows it"
    assert caught.cutoff_hertz == 16_050
    assert caught.effective_bitrate_kbps is None, "no rung was read, so no rate is claimed"
    assert "16050 Hz" in caught.reason and "62%" in caught.reason


def test_an_edge_says_nothing_about_a_file_that_declares_itself_lossy() -> None:
    caught = judge(
        replace(_lossless_looking("mp3"), filter_edge=FilterEdge(share=0.9, hertz=16_000))
    )

    assert caught.encoding is Encoding.LOSSY


def test_an_album_whose_tracks_the_edge_caught_is_a_transcoded_album() -> None:
    """The album is what the folder is named for, so it must count these tracks."""
    caught = replace(_lossless_looking(), filter_edge=FilterEdge(share=0.5, hertz=16_000))
    clean = replace(_lossless_looking(), filter_edge=FilterEdge(share=0.01, hertz=None))

    assert judge_album([caught, caught, clean]).encoding is Encoding.TRANSCODED
    assert judge_album([caught, clean, clean]).encoding is Encoding.LOSSLESS


def _row(**changes: object) -> StoredQuality:
    base = StoredQuality(
        encoding="transcoded",
        effective_bitrate_kbps=None,
        cutoff_hertz=16_050,
        steepest_drop_db=4.1,
        findings=("transcoded",),
        reason="stops dead at 16050 Hz",
        decay_db=5.2,
        ceiling_db=-70.8,
        frame_grid_z=3.4,
        frame_grid_agreeing=1,
        edge_share=0.62,
        edge_hertz=16_050,
    )
    return replace(base, **changes)


def test_an_edge_conviction_survives_the_re_judgement_of_its_row() -> None:
    assert stored_track_is_transcoded(_row())
    assert not stored_track_is_transcoded(
        _row(edge_share=0.02, cutoff_hertz=None, encoding="lossless")
    )


def test_a_row_from_before_the_edge_is_measured_again() -> None:
    assert _row().answers_the_rules_in_force
    assert not _row(edge_share=None, edge_hertz=None).answers_the_rules_in_force
    assert _row(edge_share=0.0, edge_hertz=None).answers_the_rules_in_force, "measured, no edge"
    assert _row(encoding="lossy", edge_share=None).answers_the_rules_in_force
