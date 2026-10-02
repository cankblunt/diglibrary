"""Drawing a spectrogram of one audio file, and keeping the pictures on a leash.

A picture is not a measurement. Everything this project concludes about a file it
concludes from numbers, and no verdict is read off the *shape* of a picture. So
nothing here decides anything: it renders the file the user asked to look at,
and the reading beside it is the one the analyzer already made.

The picture is drawn without ffmpeg's own legend on purpose. `showspectrumpic`
places its axes inside margins whose size is an internal detail, while the bare
spectrum has an exact geometry: the frequency axis is linear from zero to the top
of the drawing across the full height. Measured with three
known tones through a 600x300 render — 5000, 10000 and 15000 Hz came back as
5015, 10029 and 15044 Hz, which is one pixel of rounding at 73 Hz per row. That
is what lets the window draw its own ruler and put a line exactly on the wall the
analyzer found; a margin nobody measured would put that line somewhere near it.
"""

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from diglibrary.quality.analysis import CommandRunner

WIDTH = 1280
HEIGHT = 480
"""How big one picture is, measured rather than chosen.

At this size a render of a four-minute FLAC costs well under a second and about
a megabyte. 900x300 saves half the disk and reads coarser exactly where it
matters, since a wall is a horizontal edge and height is the resolution that
shows it; ffmpeg's own default of 4096x2048 costs many times the disk and the
time for a picture no screen shows at once.
"""

DRAWN_TOP_HERTZ = 24_000
"""The highest frequency ever drawn, however high the stream's own limit is.

The height spans zero to the top, so a stream whose Nyquist limit is far above
anything music reaches spends most of the picture on black and squeezes the part
that decides everything into a strip. For a 96 kHz stream, whose limit is
48 kHz, more than half of the height sits above 22 kHz, and the four encoder
rungs the ruler marks — 16, 18, 20 and 22 kHz — are pushed down to between 54%
and 67% of the height, out of the top third they are meant to occupy. Capped
here they sit between 8% and 33%, and 44.1 and 48 kHz streams are untouched,
since their limits are 22.05 and 24 kHz already.

Not the lowest limit in the library, deliberately: cutting at 22.05 kHz would
crop the real top off every 48 kHz file to tidy the picture of a 96 kHz one.
"""

_DRAWING = ".drawing-"
"""What an in-flight render is called before it is named for what it holds.

The final name is not known until ffmpeg has said what sample rate it decoded,
so there has to be a first name — but it must be unique per render, and it must
be invisible to the ceiling, or an eviction can unlink a picture that is still
being written.
"""

DEFAULT_CACHE_LIMIT_BYTES = 500 * 1024 * 1024
"""The cache is discardable, so it gets a ceiling instead of growing forever.

A limit in megabytes, oldest discarded first. At about a megabyte a picture
this holds about five hundred of them, and deleting the whole folder loses
nothing but time.
"""


@dataclass(frozen=True, slots=True)
class Spectrogram:
    """Purpose: say where one rendered picture is and how to read its geometry.

    Responsibilities: carry the file, its pixel size, and the frequency the
    height spans up to, which is what turns a frequency into a row. Boundaries:
    it holds no verdict and no measurement — those come from the analyzer, and
    the window shows them beside the picture. Dependencies: none. Collaborators:
    ``SpectrogramRenderer`` and the window's API. Constraints: ``top_hertz`` is
    what was actually drawn — the stream's Nyquist limit, or ``DRAWN_TOP_HERTZ``
    where that limit is higher — and never an assumption, because a ruler drawn
    against the wrong limit mislabels every frequency on it. **It is not called
    ``nyquist``**: with a ceiling on the drawing it is not always one, and a
    name must not carry two meanings.
    """

    path: Path
    width: int
    height: int
    top_hertz: float
    from_cache: bool


class SpectrogramRenderer:
    """Purpose: render a file's spectrogram once and serve it from disk after that.

    Responsibilities: run ffmpeg's ``showspectrumpic``, name the result by the
    audio's content signature, and keep the folder under a byte ceiling.
    Boundaries: it measures nothing and decides nothing about the audio; it never
    touches the user's library, only its own cache folder. Dependencies: an
    injected ``CommandRunner``, so every test is offline and deterministic.
    Collaborators: the window's API. Constraints: identity is the audio, so
    the same recording in two folders shares one picture. Keying by content is
    the safe direction, because a picture of the same bytes is the same
    picture.
    """

    def __init__(
        self,
        runner: CommandRunner,
        cache_directory: Path,
        logger: logging.Logger,
        width: int = WIDTH,
        height: int = HEIGHT,
    ) -> None:
        """Create a renderer writing into one cache folder."""
        self._runner = runner
        self._cache_directory = cache_directory
        self._logger = logger
        self._width = width
        self._height = height

    def render(
        self,
        path: Path,
        signature: str,
        limit_bytes: int = DEFAULT_CACHE_LIMIT_BYTES,
        sample_rate: int | None = None,
    ) -> Spectrogram | None:
        """Return the picture of this file, drawing it only if it is not cached.

        ``None`` when it cannot be drawn, never an exception: ffmpeg missing, a
        file that will not decode, a full disk. The same promise ``analyze``
        makes, for the same reason — one unreadable file must not take a screen
        down with it.

        ``sample_rate`` is what caps the drawing, and it is a parameter because
        the rate ffmpeg reports arrives *after* the render: deciding the ceiling
        from it would mean drawing the picture and then drawing it again. The
        caller has it in hand from the scan, so it costs nothing there — and a
        caller that does not pass it gets the whole stream drawn.
        """
        stop = DRAWN_TOP_HERTZ if sample_rate and sample_rate / 2 > DRAWN_TOP_HERTZ else None
        held = self._cached(signature)
        if held is not None:
            # Touched so the ceiling discards what has not been looked at
            # rather than what was looked at first.
            held.path.touch()
            return held
        self._cache_directory.mkdir(parents=True, exist_ok=True)
        # One name per render, not one for the renderer. Every call from the
        # window arrives on its own thread, so with one shared name two tracks
        # clicked in quick succession would draw into the same file: the second
        # finishes first, and the first then renames *the second's picture*
        # into its own signature's place. Nothing would detect it — the cache
        # would serve that wrong picture from then on, with this track's
        # measured cutoff drawn over another track's spectrum.
        handle, made = tempfile.mkstemp(dir=self._cache_directory, prefix=_DRAWING, suffix=".png")
        os.close(handle)
        drawn = Path(made)
        try:
            report = self._runner.run(self._arguments(path, drawn, stop))
        except Exception as error:
            drawn.unlink(missing_ok=True)
            self._logger.warning(
                "A spectrogram could not be drawn; the track is reported without one.",
                extra={
                    "operation": "quality.spectrogram.failed",
                    "file": str(path),
                    "error": str(error),
                },
            )
            return None
        if not drawn.is_file() or drawn.stat().st_size == 0:
            drawn.unlink(missing_ok=True)
            self._logger.warning(
                "ffmpeg drew no spectrogram for this file.",
                extra={"operation": "quality.spectrogram.empty", "file": str(path)},
            )
            return None
        rate = _sample_rate_from(report)
        if rate is None:
            # The picture exists and its geometry is unknown, which is worse than
            # no picture: every label on the ruler would be a guess.
            drawn.unlink(missing_ok=True)
            self._logger.warning(
                "The stream's sample rate was not reported, so the picture has no scale.",
                extra={"operation": "quality.spectrogram.no_scale", "file": str(path)},
            )
            return None
        # **The scale goes into the name, and the scale is the top drawn**, so a
        # cache hit is a folder listing and not a second ffmpeg: asking the file
        # again for its sample rate would be a subprocess per look, a cost
        # hidden inside the cache.
        # The word `top` is in the name for a second reason: pictures drawn by
        # builds without the ceiling are named for the *rate*, so they do not
        # match, and each is redrawn instead of being served under a ruler
        # built for a different geometry. Nothing is deleted — the ceiling
        # collects them in its own time.
        top = float(stop) if stop else rate / 2
        final = (
            self._cache_directory / f"{signature}-{self._width}x{self._height}-top{top:.0f}hz.png"
        )
        drawn.replace(final)
        self.enforce_limit(limit_bytes)
        return Spectrogram(final, self._width, self._height, top, from_cache=False)

    def _pictures(self) -> list[Path]:
        """Every finished picture in the cache, and never one still being drawn.

        The ceiling, the size report and `Clear` all sweep this folder, and a
        render in flight is a `.png` in it. Evicting one would delete a file
        ffmpeg is still writing into — so what is in flight is not a picture
        here until it has been named for what it holds.
        """
        if not self._cache_directory.is_dir():
            return []
        return [
            entry
            for entry in self._cache_directory.glob("*.png")
            if entry.is_file() and not entry.name.startswith(_DRAWING)
        ]

    def _cached(self, signature: str) -> Spectrogram | None:
        """Return the picture already held for this audio, scale and all."""
        if not self._cache_directory.is_dir():
            return None
        prefix = f"{signature}-{self._width}x{self._height}-top"
        for entry in self._cache_directory.glob(f"{prefix}*hz.png"):
            if not entry.is_file() or entry.stat().st_size == 0:
                continue
            try:
                top = int(entry.name[len(prefix) : -len("hz.png")])
            except ValueError:
                continue
            if top > 0:
                return Spectrogram(entry, self._width, self._height, float(top), from_cache=True)
        return None

    def enforce_limit(self, limit_bytes: int) -> int:
        """Discard the oldest pictures until the folder is under *limit_bytes*.

        Returns the bytes held afterwards. Oldest by modification time, and a
        cache hit touches the file, so what goes is what has not been looked at
        for longest rather than what happened to be drawn first.
        """
        pictures = sorted(
            self._pictures(),
            key=lambda entry: entry.stat().st_mtime,
        )
        held = sum(entry.stat().st_size for entry in pictures)
        for entry in pictures:
            if held <= limit_bytes:
                break
            size = entry.stat().st_size
            entry.unlink(missing_ok=True)
            held -= size
            self._logger.info(
                "A cached spectrogram was discarded to stay under the cache limit.",
                extra={"operation": "quality.spectrogram.evicted", "file": entry.name},
            )
        return held

    def held_bytes(self) -> int:
        """Return what the cache folder currently holds, so the window can say it."""
        if not self._cache_directory.is_dir():
            return 0
        return sum(entry.stat().st_size for entry in self._pictures())

    def clear(self) -> int:
        """Delete every cached picture and return how many bytes went.

        Only ever this folder, and only ever pictures this renderer named. The
        user's library is never a target here.
        """
        gone = 0
        for entry in self._pictures():
            gone += entry.stat().st_size
            entry.unlink(missing_ok=True)
        return gone

    def _arguments(self, path: Path, destination: Path, stop: int | None) -> tuple[str, ...]:
        # `legend=0` is what makes the geometry knowable: the picture is the
        # spectrum and nothing else, so a frequency is a row and the window can
        # draw a ruler that is right rather than close. `stop` is left off
        # entirely below the ceiling rather than set to Nyquist, so the ordinary
        # 44.1 kHz render does not depend on the ceiling at all.
        ceiling = f":stop={stop}" if stop else ""
        return (
            "-nostdin",
            "-y",
            "-i",
            str(path),
            "-lavfi",
            f"showspectrumpic=s={self._width}x{self._height}:legend=0{ceiling}",
            str(destination),
        )


_SAMPLE_RATE = re.compile(r"Stream #\d+:\d+.*?: Audio: [\w-]+.*?(\d+) Hz", re.DOTALL)


def _sample_rate_from(report: str) -> int | None:
    """Return the sample rate ffmpeg says it decoded, or None if it did not say."""
    match = _SAMPLE_RATE.search(report)
    if match is None:
        return None
    rate = int(match.group(1))
    return rate if rate > 0 else None
