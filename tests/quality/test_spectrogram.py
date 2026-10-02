"""Drawing a spectrogram, caching it, and keeping the folder under its ceiling.

Offline and deterministic: the runner is injected, so no ffmpeg and no audio file
are involved. What the fake runner does is what ffmpeg does — write the file named
in the arguments and report the stream on standard error — and the tests read the
arguments it was given, which is where a wrong filter or a lost `legend=0` would
show up.
"""

import logging
import os
from collections.abc import Sequence
from pathlib import Path

from diglibrary.quality.spectrogram import (
    DRAWN_TOP_HERTZ,
    HEIGHT,
    WIDTH,
    SpectrogramRenderer,
)

REPORT = """
ffmpeg version 8.1.2 Copyright (c) 2000-2019 the FFmpeg developers
  Duration: 00:04:12.03, start: 0.000000, bitrate: 658 kb/s
  Stream #0:0: Audio: flac, 44100 Hz, stereo, s16
"""


class FakeFfmpeg:
    """Write the picture ffmpeg would write, and count every invocation.

    Counted rather than merely recorded: proving a *second* look draws nothing
    needs a number, because a call that happened and returned a cached-looking
    answer is invisible to any assertion about the answer.
    """

    def __init__(
        self, report: str = REPORT, payload: bytes = b"PNG-ish", fail: bool = False
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._report = report
        self._payload = payload
        self._fail = fail

    def run(self, arguments: Sequence[str]) -> str:
        """Write the destination file and return the report, as ffmpeg does."""
        self.calls.append(tuple(arguments))
        if self._fail:
            raise OSError("ffmpeg is not installed")
        Path(arguments[-1]).write_bytes(self._payload)
        return self._report


def _renderer(tmp_path: Path, runner: FakeFfmpeg) -> SpectrogramRenderer:
    return SpectrogramRenderer(runner, tmp_path / "spectrograms", logging.getLogger("test.spec"))


def test_a_picture_is_drawn_once_and_served_from_disk_after(tmp_path: Path) -> None:
    """The second look must cost nothing, and only a call count can say so."""
    runner = FakeFfmpeg()
    renderer = _renderer(tmp_path, runner)

    first = renderer.render(tmp_path / "track.flac", "sig-a")
    second = renderer.render(tmp_path / "track.flac", "sig-a")

    assert first is not None and second is not None
    assert first.path == second.path and first.path.is_file()
    assert first.from_cache is False and second.from_cache is True
    assert len(runner.calls) == 1, "a cached picture asks ffmpeg nothing at all"
    # The scale travels in the name, which is what makes that possible: asking
    # the audio again for its sample rate would be a subprocess per look.
    assert first.path.name == f"sig-a-{WIDTH}x{HEIGHT}-top22050hz.png"
    assert first.top_hertz == 22050.0
    assert (first.width, first.height) == (WIDTH, HEIGHT)


def test_the_picture_is_the_bare_spectrum(tmp_path: Path) -> None:
    """`legend=0` is what makes the geometry knowable, so it is asserted.

    With ffmpeg's own legend the spectrum sits inside margins whose size is an
    internal detail, and the window's ruler — and the line it draws on the wall —
    would be placed against a number nobody measured.
    """
    runner = FakeFfmpeg()

    _renderer(tmp_path, runner).render(tmp_path / "track.flac", "sig-a")

    arguments = runner.calls[0]
    assert f"showspectrumpic=s={WIDTH}x{HEIGHT}:legend=0" in arguments
    assert arguments[-1].endswith(".png")


def test_a_picture_with_no_scale_is_not_kept(tmp_path: Path) -> None:
    """A picture whose frequencies are unknown is worse than no picture.

    Every label on the ruler would be a guess, and a guessed ruler is read as a
    measurement. So the drawing is discarded and the window is told there is none.
    """
    runner = FakeFfmpeg(report="ffmpeg version 8.1.2\n  Duration: 00:04:12.03\n")
    renderer = _renderer(tmp_path, runner)

    assert renderer.render(tmp_path / "track.flac", "sig-a") is None
    assert list((tmp_path / "spectrograms").glob("*.png")) == []


def test_ffmpeg_missing_is_reported_as_no_picture(tmp_path: Path) -> None:
    """``None``, never an exception: one unreadable file must not take a screen down."""
    renderer = _renderer(tmp_path, FakeFfmpeg(fail=True))

    assert renderer.render(tmp_path / "track.flac", "sig-a") is None


def test_the_oldest_pictures_go_when_the_cache_is_over_its_ceiling(tmp_path: Path) -> None:
    """The ceiling is a setting, and what goes is what was looked at longest ago.

    Modification time decides, and a cache hit touches the file — so revisiting an
    album keeps its pictures and the ones nobody came back to are the ones that
    make room.
    """
    folder = tmp_path / "spectrograms"
    folder.mkdir(parents=True)
    for index, name in enumerate(("old", "middle", "new")):
        picture = folder / f"{name}-{WIDTH}x{HEIGHT}-44100hz.png"
        picture.write_bytes(b"x" * 1000)
        os.utime(picture, (1_000_000 + index * 100, 1_000_000 + index * 100))
    renderer = SpectrogramRenderer(FakeFfmpeg(), folder, logging.getLogger("test.spec"))
    assert renderer.held_bytes() == 3000

    held = renderer.enforce_limit(2000)

    assert held == 2000
    assert sorted(entry.name.split("-")[0] for entry in folder.glob("*.png")) == ["middle", "new"]


def test_a_look_keeps_a_picture_from_being_the_next_to_go(tmp_path: Path) -> None:
    """A cache hit touches the file, which is the difference between oldest and least-looked-at."""
    runner = FakeFfmpeg()
    renderer = _renderer(tmp_path, runner)
    renderer.render(tmp_path / "a.flac", "sig-a")
    folder = tmp_path / "spectrograms"
    older = next(folder.glob("sig-a-*.png"))
    os.utime(older, (1_000_000, 1_000_000))
    renderer.render(tmp_path / "b.flac", "sig-b")
    newer = next(folder.glob("sig-b-*.png"))
    assert older.stat().st_mtime < newer.stat().st_mtime

    renderer.render(tmp_path / "a.flac", "sig-a")

    assert older.stat().st_mtime > newer.stat().st_mtime, "looking at it made it the newer one"


def test_clearing_removes_the_pictures_and_reports_what_came_back(tmp_path: Path) -> None:
    """Only pictures, only this folder: the rule that makes `Clear downloaded` safe."""
    folder = tmp_path / "spectrograms"
    folder.mkdir(parents=True)
    (folder / f"one-{WIDTH}x{HEIGHT}-44100hz.png").write_bytes(b"x" * 700)
    (folder / f"two-{WIDTH}x{HEIGHT}-44100hz.png").write_bytes(b"x" * 300)
    keeper = folder / "notes.txt"
    keeper.write_text("not a picture")
    renderer = SpectrogramRenderer(FakeFfmpeg(), folder, logging.getLogger("test.spec"))

    freed = renderer.clear()

    assert freed == 1000
    assert list(folder.glob("*.png")) == []
    assert keeper.is_file(), "the folder is not emptied, only its pictures are"


HIGH_RATE_REPORT = """
ffmpeg version 8.1.2 Copyright (c) 2000-2019 the FFmpeg developers
  Duration: 00:04:06.85, start: 0.000000, bitrate: 2076 kb/s
  Stream #0:0: Audio: flac, 96000 Hz, stereo, s32 (24 bit)
"""


def test_a_stream_above_the_ceiling_is_drawn_only_up_to_it(tmp_path: Path) -> None:
    """A 96 kHz file's picture stops at the ceiling, and says so in its geometry.

    Drawn to its own 48 kHz limit, more than half of the height is above 22 kHz
    and holds nothing.
    """
    runner = FakeFfmpeg(report=HIGH_RATE_REPORT)
    renderer = _renderer(tmp_path, runner)

    drawn = renderer.render(tmp_path / "track.flac", "sig-hi", sample_rate=96_000)

    assert drawn is not None
    assert drawn.top_hertz == float(DRAWN_TOP_HERTZ)
    filter_argument = runner.calls[0][runner.calls[0].index("-lavfi") + 1]
    assert f"stop={DRAWN_TOP_HERTZ}" in filter_argument


def test_a_stream_under_the_ceiling_is_drawn_with_no_stop_at_all(tmp_path: Path) -> None:
    """No `stop` at all below the ceiling: the ordinary render must not move.

    Asserted on the argument rather than on the geometry, because `stop=22050`
    would produce the same `top_hertz` while changing the command every 44.1 kHz
    file in the library is drawn with.
    """
    runner = FakeFfmpeg()
    renderer = _renderer(tmp_path, runner)

    drawn = renderer.render(tmp_path / "track.flac", "sig-lo", sample_rate=44_100)

    assert drawn is not None and drawn.top_hertz == 22050.0
    assert "stop=" not in runner.calls[0][runner.calls[0].index("-lavfi") + 1]


def test_a_picture_drawn_before_the_ceiling_existed_is_not_served(tmp_path: Path) -> None:
    """It was named for the rate; the ruler now reads the name as the top drawn.

    Served, it would put 22 kHz at 54% of a picture that stops at 24 kHz — every
    label wrong, on the screen a verdict is overruled from. So the old name must
    simply not match, and the picture must be drawn again.
    """
    runner = FakeFfmpeg(report=HIGH_RATE_REPORT)
    folder = tmp_path / "spectrograms"
    folder.mkdir(parents=True)
    stale = folder / f"sig-hi-{WIDTH}x{HEIGHT}-96000hz.png"
    stale.write_bytes(b"PNG-ish")

    drawn = _renderer(tmp_path, runner).render(
        tmp_path / "track.flac", "sig-hi", sample_rate=96_000
    )

    assert drawn is not None and drawn.path != stale
    assert drawn.from_cache is False, "the old name must never be served under the new ruler"
    assert drawn.top_hertz == float(DRAWN_TOP_HERTZ)
