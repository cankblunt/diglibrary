"""The two mixing routes, executed rather than looked at.

The bridge guard in `tests/ui/test_web_assets.py` asserts that a method
exists; it does not assert that calling it returns. A route that reads a
field the album state does not have raises on the first call and the window
stops drawing. These tests call the routes over a scanned library.
"""

import logging
from pathlib import Path

import numpy as np
from tests.application.test_api import FakeSource, _api, _finish, _library

from diglibrary.harmonic.analyzer import HarmonicAnalyzer
from diglibrary.harmonic.keys import _CAMELOT


class _SilentDecoder:
    """A decoder that answers with audio whose key is a fact by construction."""

    def __init__(self, samples: np.ndarray) -> None:
        self._samples = samples

    def decode(self, path: Path) -> np.ndarray:
        return self._samples


def _a_major_chord(seconds: float = 8.0, rate: int = 11025) -> np.ndarray:
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    tones = []
    for hz in (220.0, 277.18, 329.63):  # A, C#, E — A major
        tones.append(sum(np.sin(2 * np.pi * hz * h * t) / h for h in (1, 2, 3)))
    return np.asarray(sum(tones), dtype=np.float32)


def _measured_api(tmp_path: Path):
    api = _api(tmp_path, FakeSource())
    api._harmonic_analyzer = HarmonicAnalyzer(
        _SilentDecoder(_a_major_chord()), logging.getLogger("test.harmonics")
    )
    return api


def test_the_mixing_screen_can_be_asked_before_anything_is_measured(tmp_path: Path) -> None:
    """The call the window makes every time the tab is opened.

    It reads every album on the shelf, so it must use only fields that
    `_AlbumState` has.
    """
    library = _library(tmp_path)
    api = _measured_api(tmp_path)
    assert api.scan(str(library))["ok"]
    _finish(api._job, "scan")

    answer = api.harmonics()

    assert answer["ok"] is True
    assert answer["tracks"] == [], "nothing has been measured, so nothing is claimed"


def test_measuring_one_album_puts_its_tracks_on_the_wheel(tmp_path: Path) -> None:
    """One album is measured on request, and the answers reach the screen's own route."""
    library = _library(tmp_path)
    api = _measured_api(tmp_path)
    assert api.scan(str(library))["ok"]
    _finish(api._job, "scan")
    unit_id = api.state()["albums"][0]["unit_id"]

    started = api.measure_harmonics([unit_id])
    assert started["ok"] is True
    _finish(api._harmonics_job, "harmonic measurement")

    tracks = api.harmonics()["tracks"]
    assert tracks, "the album was measured and the wheel would still be empty"
    for track in tracks:
        # Every row the screen draws, named here so a field renamed in the API
        # cannot silently stop reaching the wheel.
        assert set(track) == {
            "audio_key",
            "unit_id",
            "album",
            "artist",
            "track",
            "position",
            "key_name",
            "camelot",
            "key_margin",
            "runner_up_camelot",
            "bpm",
            "bpm_confidence",
            "bpm_alternative",
        }
        assert track["album"], "a track with no album name would draw a blank row"
        assert track["key_name"] == "A major"
        assert track["camelot"] == "11B"
        # The margin and the key it beat, which is what the column is drawn
        # from. A measured row carries both; only a row stored without them
        # carries neither, and the window says so.
        assert (
            track["key_margin"] is not None
        ), "a fresh measurement reached the screen dressed as an old one"
        assert track["runner_up_camelot"] in _CAMELOT.values()


def test_measuring_an_album_that_is_not_there_is_refused_and_not_raised(tmp_path: Path) -> None:
    api = _measured_api(tmp_path)
    answer = api.measure_harmonics([4242])
    assert answer["ok"] is False and answer["error"]


def test_without_ffmpeg_the_measurement_says_so_instead_of_measuring_nothing(
    tmp_path: Path,
) -> None:
    """Without ffmpeg the request is refused up front, and the refusal names ffmpeg.

    A job that runs and emits `measured: 0` is byte for byte what a real
    measurement emits when it read audio and got nothing back, so the window
    would report unreadable audio for files it never opened. No thread is
    started either.
    """
    library = _library(tmp_path)
    api = _api(tmp_path, FakeSource())
    api._harmonic_analyzer = None
    assert api.scan(str(library))["ok"]
    _finish(api._job, "scan")
    unit_id = api.state()["albums"][0]["unit_id"]

    answer = api.measure_harmonics([unit_id])

    assert answer["ok"] is False
    assert "ffmpeg" in str(answer["error"])
    assert api._harmonics_job is None
    assert api.harmonics()["tracks"] == []


def test_a_bare_id_from_a_cached_script_still_measures(tmp_path: Path) -> None:
    """The route accepts a bare id as well as a list of ids.

    WebKit caches this window's script across restarts, so a fresh Python can
    meet an `app.js` that still sends a bare id. Reading either shape means
    that pairing costs a stale-looking screen rather than a window that stops
    drawing.
    """
    library = _library(tmp_path)
    api = _measured_api(tmp_path)
    assert api.scan(str(library))["ok"]
    _finish(api._job, "scan")
    unit_id = api.state()["albums"][0]["unit_id"]

    assert api.measure_harmonics(unit_id)["ok"] is True  # type: ignore[arg-type]
    _finish(api._harmonics_job, "harmonic measurement")
    assert api.harmonics()["tracks"]


def test_measuring_nothing_is_refused_rather_than_started(tmp_path: Path) -> None:
    api = _measured_api(tmp_path)
    assert api.measure_harmonics([])["ok"] is False
