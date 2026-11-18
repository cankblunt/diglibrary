"""The top of a stream that declares more than a CD (`quality.topband`), and the rule on it."""

from pathlib import Path

import numpy as np
import pytest

from diglibrary.application.quality import stored_top_of_band_is_empty
from diglibrary.database.library_store import StoredQuality
from diglibrary.quality.models import DECLARES_MORE_THAN_A_CD_HERTZ, NOTHING_ABOVE_THE_REFERENCE_DB
from diglibrary.quality.topband import (
    TOP_BAND_FRAME,
    TopBandProbe,
    measure_top_band,
)

RATE = 96_000
SECONDS = 8.0


def _dense(cut_hertz: float, amplitude: float, seed: int) -> np.ndarray:
    """Sound filling every bin below one frequency, as real music does.

    Dense on purpose. What is read here is the *middle* of a band, so a handful
    of pure tones over silence would leave that middle sitting on the silence
    and the reference would describe nothing.
    """
    samples = int(SECONDS * RATE)
    spectrum = np.fft.rfft(np.random.default_rng(seed).standard_normal(samples))
    spectrum[np.fft.rfftfreq(samples, 1.0 / RATE) > cut_hertz] = 0
    made = np.fft.irfft(spectrum, samples)
    return made / np.max(np.abs(made)) * amplitude


def _recorded_at_this_rate(floor_db: float = -60.0) -> np.ndarray:
    """Music, over a noise floor that runs the whole way to Nyquist.

    What a converter working at this rate leaves behind: its own floor, flat
    across tens of kilohertz, far under the music and never absent. That floor
    is the whole difference, so the test states it rather than implying it.
    """
    samples = int(SECONDS * RATE)
    floor = np.random.default_rng(11).standard_normal(samples) * 10 ** (floor_db / 20) * 0.5
    return (_dense(16_000, 0.5, seed=3) + floor).astype(np.float32)


def _lifted_to_this_rate(signal: np.ndarray, source_rate: int) -> np.ndarray:
    """The same audio as it comes back from a lower rate: empty above that Nyquist.

    Written in the frequency domain rather than by resampling, so the test
    states the one property it is about and leans on no resampler's filter.
    """
    spectrum = np.fft.rfft(signal.astype(np.float64))
    spectrum[np.fft.rfftfreq(len(signal), 1.0 / RATE) > source_rate / 2] = 0
    return np.fft.irfft(spectrum, len(signal)).astype(np.float32)


def test_a_stream_recorded_at_its_own_rate_is_full_at_the_top() -> None:
    band = measure_top_band(_recorded_at_this_rate(), RATE)

    assert band is not None
    assert band.headroom_db > NOTHING_ABOVE_THE_REFERENCE_DB
    assert band.wall_hertz is None, "a band that is full has no frequency above which it is empty"


@pytest.mark.parametrize("source_rate", [44_100, 48_000])
def test_a_stream_lifted_from_a_lower_rate_is_empty_at_the_top(source_rate: int) -> None:
    band = measure_top_band(_lifted_to_this_rate(_recorded_at_this_rate(), source_rate), RATE)

    assert band is not None
    assert band.headroom_db <= NOTHING_ABOVE_THE_REFERENCE_DB
    assert band.wall_hertz is not None
    assert band.wall_hertz >= source_rate / 2


def test_the_reading_is_a_proportion_and_not_a_level() -> None:
    """A quiet pressing and a loud one are the same file to this reading.

    An absolute floor would describe loudness, which is the mistake the wall
    rule records having made once.
    """
    loud = _recorded_at_this_rate()
    quiet = (loud * 0.25).astype(np.float32)

    one = measure_top_band(loud, RATE)
    other = measure_top_band(quiet, RATE)
    assert one is not None and other is not None
    assert abs(one.headroom_db - other.headroom_db) < 1.0


def test_audio_too_short_to_read_answers_nothing_rather_than_empty() -> None:
    assert measure_top_band(np.zeros(TOP_BAND_FRAME, dtype=np.float32), RATE) is None


class _Reader:
    """A reader that counts how often it was asked to decode."""

    def __init__(self, pcm: np.ndarray | None) -> None:
        self.pcm = pcm
        self.reads = 0

    def read(self, path: Path, seconds: float, skip: float) -> np.ndarray | None:
        self.reads += 1
        return None if self.pcm is None else np.stack([self.pcm, self.pcm], axis=1)


def test_a_stream_at_a_cd_rate_is_never_decoded_for_this_question() -> None:
    """The rate is asked before the audio, because the decode is the whole cost."""
    reader = _Reader(_recorded_at_this_rate())
    probe = TopBandProbe(reader, seconds=SECONDS)

    assert probe.measure(Path("a.flac"), DECLARES_MORE_THAN_A_CD_HERTZ) is None
    assert reader.reads == 0

    assert probe.measure(Path("a.flac"), RATE) is not None
    assert reader.reads == 1


def test_a_file_that_cannot_be_decoded_answers_nothing() -> None:
    assert TopBandProbe(_Reader(None), seconds=SECONDS).measure(Path("a.flac"), RATE) is None


def _row(**fields: object) -> StoredQuality:
    return StoredQuality(
        encoding="lossless",
        effective_bitrate_kbps=None,
        cutoff_hertz=None,
        steepest_drop_db=None,
        findings=(),
        reason="",
        **fields,
    )


def test_a_row_from_before_this_was_measured_proves_nothing() -> None:
    """An absent reading is not an empty band: nothing about the file is known."""
    assert stored_top_of_band_is_empty(_row()) is False


def test_a_row_with_a_reading_and_no_wall_proves_nothing() -> None:
    """The wall is written only where the top was already found empty."""
    assert stored_top_of_band_is_empty(_row(top_band_headroom_db=-120.0)) is False


def test_a_row_whose_top_is_full_says_nothing_however_loud_the_file_is() -> None:
    assert (
        stored_top_of_band_is_empty(_row(top_band_headroom_db=-60.0, top_band_wall_hertz=30_000))
        is False
    )


def test_a_row_that_measured_an_empty_top_and_found_its_wall_says_so() -> None:
    assert (
        stored_top_of_band_is_empty(_row(top_band_headroom_db=-120.0, top_band_wall_hertz=30_000))
        is True
    )
