"""Record the install for real, and render it as a picture that plays.

The part of an install that can be shown without a camera is the terminal:
the steps run for real and drawn as a GIF. A recording whose tool is not in the
repository cannot be remade, and goes on showing an old version under a page
that names a new one.

What is recorded is the route the front pages put first:
`pipx install diglibrary`, run against the index, followed by the word that
opens the window.

**Nothing here is typed by hand.** The install runs, its output is read line by
line with the clock running, and the picture plays back at the speed it
actually took. Three things are presentation rather than recording, and they are
named here because that is the whole difference between a demonstration and a
fabrication:

- the prompt reads `~`, which is where a person will be standing, while the run
  happens with `PIPX_HOME` and `PIPX_BIN_DIR` pointed at a throwaway folder;
- pipx's note that the throwaway `bin` is not on `PATH` is dropped: it is about
  that folder rather than about this install, it carries its path inside it, and
  the reader of the front pages has run `pipx ensurepath` and will never see it;
- the two emoji in pipx's `done!` line are drawn as a pixel sparkle, because the
  font below is five pixels wide and has no emoji in it.

`diglibrary`, the last line, is **not run**: it opens a window, and a window is
not a thing a terminal recording can show. It prints nothing, so the last frame
shows nothing.

The font and the encoder are here because there is no image library in this
project and there is not going to be one. `icon.py` already draws the
application's mark with arithmetic alone; this draws text with a 5x7 bitmap
font, and writes the GIF a frame at a time, each frame carrying only the
rectangle that changed.

    .venv/bin/python tools/record_install.py              # rehearsal, writes nothing here
    .venv/bin/python tools/record_install.py --apply      # writes docs/images/
    .venv/bin/python tools/record_install.py --font-proof # look at every glyph

It never asks anything. The rehearsal is the default, `--apply` is the only way
to write into `docs/`, and a recording that leaks a path is refused rather than
questioned.
"""

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PICTURE = PROJECT / "docs" / "images" / "install.gif"
TRANSCRIPT = PROJECT / "docs" / "images" / "install.txt"

# The two lines the front pages put first. `INSTALL` is run; `OPEN` is typed and
# left unanswered, because it opens a window.
INSTALL = "pipx install diglibrary"
OPEN = "diglibrary"
PROMPT = "~ % "

# Anything matching these in the recorded output means the throwaway folder, or
# this machine, reached the picture. Either one is refused: a published page
# is immutable, and a path names a person or a machine.
LEAKS = (re.compile(r"/Users/"), re.compile(r"/private/"), re.compile(r"/var/folders/"))


# --------------------------------------------------------------------------
# The recording
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Printed:
    """One line the install printed, and how long after it started."""

    at: float
    text: str


def record() -> tuple[list[Printed], float]:
    """Run the install in a throwaway pipx home and read what it prints, timed.

    Returns the lines that survive the scrub, and how long the whole thing took.
    The throwaway home is the point: this must be the install a person who has
    never had pipx sees, including the shared libraries it builds the first time.

    Nothing is said here about which interpreter pipx picks. The route the front
    pages give is `brew install python ffmpeg pipx`, so the interpreter is
    whichever one that brought, and naming the floor here would record an install
    nobody following those pages performs.
    """
    if shutil.which("pipx") is None:
        raise SystemExit("pipx is not on this PATH, so there is nothing to record.")

    with tempfile.TemporaryDirectory(prefix="diglibrary-recording-") as throwaway:
        home = Path(throwaway)
        environment = dict(os.environ)
        environment["PIPX_HOME"] = str(home / "home")
        environment["PIPX_BIN_DIR"] = str(home / "bin")

        started = time.monotonic()
        process = subprocess.Popen(
            ["pipx", "install", "diglibrary"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=environment,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        printed = [
            Printed(at=time.monotonic() - started, text=line.rstrip("\n"))
            for line in process.stdout
        ]
        code = process.wait()
        took = time.monotonic() - started

    if code != 0:
        raise SystemExit(f"the install failed ({code}), and a failed run is not a recording.")
    return _scrub(printed), took


def _scrub(printed: list[Printed]) -> list[Printed]:
    """Drop the note about the throwaway `bin`, and refuse anything else that leaks.

    The note is a block: a line that opens with the warning sign, then its
    continuations, which are the wrapped remainder and are indented. Written as
    *the block that opens with it*, so a rewording of the sentence inside does
    not walk out of the rule.
    """
    kept: list[Printed] = []
    inside_note = False
    for line in printed:
        if line.text.startswith("⚠️"):
            inside_note = True
            continue
        if inside_note:
            if line.text.startswith((" ", "\t")) and line.text.strip():
                continue
            inside_note = False
        if not line.text.strip() and not kept:
            continue
        kept.append(line)

    leaked = [line.text for line in kept if any(leak.search(line.text) for leak in LEAKS)]
    if leaked:
        raise SystemExit(
            "the recording names a path on this machine, so nothing was written:\n  "
            + "\n  ".join(leaked)
        )
    return kept


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------

BACKGROUND = 0
DIM = 1
NORMAL = 2
BRIGHT = 3
ACCENT = 4
AMBER = 5
CHROME = 6
RED_DOT = 7
AMBER_DOT = 8
GREEN_DOT = 9
EDGE = 10

PALETTE = (
    (0x16, 0x1A, 0x20),  # background
    (0x5C, 0x68, 0x78),  # dim — the prompt, the window's title
    (0xC6, 0xCE, 0xD8),  # normal — what the install prints
    (0xFF, 0xFF, 0xFF),  # bright — what is typed, and the cursor
    (0x62, 0xD3, 0x95),  # accent — the `%`, and `done!`
    (0xE3, 0xB3, 0x4E),  # amber — the line that opens with WARNING
    (0x1F, 0x24, 0x2C),  # the title bar
    (0xE0, 0x6C, 0x60),  # the three dots
    (0xE2, 0xB3, 0x54),
    (0x62, 0xC4, 0x6E),
    (0x2C, 0x33, 0x3D),  # the hairline under the title bar
)

SCALE = 2
CELL_WIDTH = 6 * SCALE
CELL_HEIGHT = 9 * SCALE
PAD_X = 16
PAD_Y = 12
TITLE_HEIGHT = 13 * SCALE


@dataclass(frozen=True, slots=True)
class Run:
    """A stretch of one line drawn in one colour."""

    text: str
    ink: int


def screen_of(
    printed: list[Printed], shown: int, typed: str, second: str | None
) -> list[list[Run]]:
    """The rows on the terminal at one moment: the first command, what it has
    printed so far, and — once the install has finished — the second command."""
    rows: list[list[Run]] = [_prompt(typed if second is None else INSTALL)]
    rows += [_output(line.text) for line in printed[:shown]]
    if second is not None:
        rows.append(_prompt(second))
    return rows


def _prompt(command: str) -> list[Run]:
    return [Run("~ ", DIM), Run("% ", ACCENT), Run(command, BRIGHT)]


def _output(text: str) -> list[Run]:
    return [Run(text, AMBER if text.startswith("WARNING") else NORMAL)]


def _height(rows: int) -> int:
    return TITLE_HEIGHT + PAD_Y * 2 + rows * CELL_HEIGHT


# --------------------------------------------------------------------------
# The drawing
# --------------------------------------------------------------------------


class Canvas:
    """A grid of palette indices, which is what a GIF frame is."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray([BACKGROUND]) * (width * height)
        # Every character this canvas was asked for and could not draw. Kept by
        # the thing that does the drawing rather than by a list of the strings
        # somebody remembered: the title, the prompt and whatever pipx prints all
        # arrive here, and the one that goes wrong will be the one nobody listed.
        self.missing: set[str] = set()

    def fill(self, x: int, y: int, width: int, height: int, ink: int) -> None:
        for row in range(max(0, y), min(self.height, y + height)):
            start = row * self.width + max(0, x)
            self.pixels[start : start + width] = bytes([ink]) * width

    def chrome(self, title: str) -> None:
        """The window around the text: a bar, three dots, and a hairline."""
        self.fill(0, 0, self.width, TITLE_HEIGHT, CHROME)
        self.fill(0, TITLE_HEIGHT - SCALE, self.width, SCALE, EDGE)
        for index, ink in enumerate((RED_DOT, AMBER_DOT, GREEN_DOT)):
            self.disc(12 + index * 18, TITLE_HEIGHT // 2, 4, ink)
        left = (self.width - len(title) * CELL_WIDTH) // 2
        self.text(left, (TITLE_HEIGHT - 7 * SCALE) // 2, title, DIM)

    def disc(self, centre_x: int, centre_y: int, radius: int, ink: int) -> None:
        for y in range(centre_y - radius, centre_y + radius + 1):
            for x in range(centre_x - radius, centre_x + radius + 1):
                if (x - centre_x) ** 2 + (y - centre_y) ** 2 <= radius * radius:
                    self.pixels[y * self.width + x] = ink

    def text(self, x: int, y: int, text: str, ink: int) -> None:
        for column, character in enumerate(text):
            self.glyph(x + column * CELL_WIDTH, y, character, ink)

    def glyph(self, x: int, y: int, character: str, ink: int) -> None:
        rows = GLYPHS.get(character) or GLYPHS.get(_folded(character))
        if rows is None:
            self.missing.add(character)
            rows = GLYPHS["?"]
        for row, bits in enumerate(rows):
            for column in range(5):
                if not bits >> (4 - column) & 1:
                    continue
                self.fill(x + column * SCALE, y + row * SCALE, SCALE, SCALE, ink)


# What a five-pixel-wide font cannot draw and something else stands in for. The
# sparkle is the declared one; the rest are punctuation that would otherwise
# come out as `?`.
FOLDED = {"✨": "✦", "🌟": "✦", "⭐": "✦", "—": "-", "–": "-", "…": "."}  # noqa: RUF001


def _folded(character: str) -> str:
    """The substitutions this font makes, all of them declared in the docstring."""
    return FOLDED.get(character, character)


def draw(rows: list[list[Run]], width: int, height: int, cursor: tuple[int, int] | None) -> Canvas:
    """One whole frame."""
    canvas = Canvas(width, height)
    # `Terminal`, and nothing else. A real title bar there reads
    # `<the account's name> — -zsh`, which names a person.
    canvas.chrome("Terminal")
    for index, runs in enumerate(rows):
        x = PAD_X
        y = TITLE_HEIGHT + PAD_Y + index * CELL_HEIGHT
        for run in runs:
            canvas.text(x, y, run.text, run.ink)
            x += len(run.text) * CELL_WIDTH
    if cursor is not None:
        column, row = cursor
        canvas.fill(
            PAD_X + column * CELL_WIDTH,
            TITLE_HEIGHT + PAD_Y + row * CELL_HEIGHT,
            5 * SCALE,
            7 * SCALE,
            BRIGHT,
        )
    return canvas


# --------------------------------------------------------------------------
# The GIF
# --------------------------------------------------------------------------


class Picture:
    """A GIF written a frame at a time, each frame carrying only what changed."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.chunks: list[bytes] = []
        self.previous: bytearray | None = None
        self.pending: int = 0

    def add(self, canvas: Canvas, delay: int) -> None:
        """Add a frame that stays on screen for `delay` hundredths of a second."""
        if self.previous is not None and canvas.pixels == self.previous:
            self.pending += delay
            return
        if self.pending and self.chunks:
            self._extend_last(self.pending)
            self.pending = 0
        box = self._changed(canvas.pixels)
        self.chunks.append(self._frame(canvas.pixels, box, delay))
        self.previous = bytearray(canvas.pixels)

    def _extend_last(self, delay: int) -> None:
        chunk = self.chunks[-1]
        was = struct.unpack("<H", chunk[4:6])[0]
        self.chunks[-1] = chunk[:4] + struct.pack("<H", min(0xFFFF, was + delay)) + chunk[6:]

    def _changed(self, pixels: bytearray) -> tuple[int, int, int, int]:
        if self.previous is None:
            return (0, 0, self.width, self.height)
        rows = [
            row
            for row in range(self.height)
            if pixels[row * self.width : (row + 1) * self.width]
            != self.previous[row * self.width : (row + 1) * self.width]
        ]
        if not rows:
            return (0, 0, 1, 1)
        top, bottom = rows[0], rows[-1]
        left, right = self.width, 0
        for row in rows:
            start = row * self.width
            for column in range(self.width):
                if pixels[start + column] != self.previous[start + column]:
                    left = min(left, column)
                    right = max(right, column)
        return (left, top, right - left + 1, bottom - top + 1)

    def _frame(self, pixels: bytearray, box: tuple[int, int, int, int], delay: int) -> bytes:
        left, top, width, height = box
        cut = bytearray()
        for row in range(top, top + height):
            start = row * self.width + left
            cut += pixels[start : start + width]
        # Disposal 1 — leave the frame where it is — and no transparent index.
        # `\x01` in this byte is the transparency flag rather than the disposal
        # method: it makes the background of every partial frame see-through,
        # so a line that is erased stays on the screen underneath.
        return (
            b"\x21\xf9\x04\x04"
            + struct.pack("<H", delay)
            + b"\x00\x00"
            + b"\x2c"
            + struct.pack("<HHHH", left, top, width, height)
            + b"\x00"
            + bytes([_MIN_CODE_SIZE])
            + _lzw(bytes(cut))
        )

    def written(self) -> bytes:
        if self.pending and self.chunks:
            self._extend_last(self.pending)
            self.pending = 0
        table = bytearray()
        for red, green, blue in PALETTE:
            table += bytes((red, green, blue))
        table += bytes(3 * ((1 << (_MIN_CODE_SIZE)) - len(PALETTE)))
        return (
            b"GIF89a"
            + struct.pack("<HH", self.width, self.height)
            + bytes((0xF0 | (_MIN_CODE_SIZE - 1), 0, 0))
            + bytes(table)
            + b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
            + b"".join(self.chunks)
            + b"\x3b"
        )


_MIN_CODE_SIZE = 4


def _lzw(indices: bytes) -> bytes:
    """GIF's variable-width LZW, and its sub-blocks.

    The one subtlety is where the code width grows, and the two sides do not
    say it the same way — which is not a discrepancy, it is the one-code lag
    written down. The encoder grows when the code it has just handed out no
    longer fits, `next_code > 1 << width`; the decoder has assigned one code
    fewer at the same point in the stream, so it grows at `>=`. Measured on a
    run of 300 identical pixels, which is the case that puts every emitted code
    one step ahead of the table: `>` on both sides reads 128 pixels and then
    hits the end marker in the middle of the data.
    """
    clear = 1 << _MIN_CODE_SIZE
    end = clear + 1
    table: dict[bytes, int] = {bytes([value]): value for value in range(clear)}
    next_code = end + 1
    width = _MIN_CODE_SIZE + 1

    bits = _Bits()
    bits.write(clear, width)
    buffer = b""
    for index in indices:
        candidate = buffer + bytes([index])
        if candidate in table:
            buffer = candidate
            continue
        bits.write(table[buffer], width)
        if next_code == 4096:
            bits.write(clear, width)
            table = {bytes([value]): value for value in range(clear)}
            next_code = end + 1
            width = _MIN_CODE_SIZE + 1
        else:
            table[candidate] = next_code
            next_code += 1
            if next_code > (1 << width) and width < 12:
                width += 1
        buffer = bytes([index])
    if buffer:
        bits.write(table[buffer], width)
    bits.write(end, width)

    payload = bits.done()
    blocks = bytearray()
    for start in range(0, len(payload), 255):
        block = payload[start : start + 255]
        blocks += bytes([len(block)]) + block
    return bytes(blocks) + b"\x00"


class _Bits:
    """Least-significant bit first, which is the order GIF packs codes in."""

    def __init__(self) -> None:
        self.out = bytearray()
        self.held = 0
        self.count = 0

    def write(self, code: int, width: int) -> None:
        self.held |= code << self.count
        self.count += width
        while self.count >= 8:
            self.out.append(self.held & 0xFF)
            self.held >>= 8
            self.count -= 8

    def done(self) -> bytes:
        if self.count:
            self.out.append(self.held & 0xFF)
            self.held = 0
            self.count = 0
        return bytes(self.out)


# --------------------------------------------------------------------------
# The timeline
# --------------------------------------------------------------------------

TICK = 4  # hundredths of a second per frame, which is 25 a second
TYPING = 4  # hundredths between two keystrokes
BEFORE_RETURN = 40
AFTER_INSTALL = 90
AT_THE_END = 180
BLINK = 45


def compose(printed: list[Printed], took: float) -> tuple[Picture, set[str]]:
    """Build the whole picture: the typing, the install at its own speed, the last line.

    Comes back with every character no glyph was found for, which is nothing at
    all when this works and is the reason `main` refuses when it is not.
    """
    height = _height(len(printed) + 2)
    width = PAD_X * 2 + CELL_WIDTH * max(
        len(PROMPT + INSTALL) + 1, *(len(line.text) for line in printed)
    )
    picture = Picture(width, height)
    missing: set[str] = set()

    def frame(rows: list[list[Run]], delay: int, cursor: tuple[int, int] | None = None) -> None:
        canvas = draw(rows, width, height, cursor)
        missing.update(canvas.missing)
        picture.add(canvas, delay)

    # The first command, typed.
    for length in range(len(INSTALL) + 1):
        typed = INSTALL[:length]
        frame(screen_of(printed, 0, typed, None), TYPING, (len(PROMPT) + length, 0))
    frame(screen_of(printed, 0, INSTALL, None), BEFORE_RETURN, (len(PROMPT) + len(INSTALL), 0))

    # The install, replayed against the clock it was recorded with.
    elapsed = 0.0
    for shown in range(1, len(printed) + 1):
        rows = screen_of(printed, shown, INSTALL, None)
        until = printed[shown - 1].at if shown < len(printed) else took
        waiting = max(TICK, round((until - elapsed) * 100))
        elapsed = until
        frame(rows, waiting)
    frame(screen_of(printed, len(printed), INSTALL, None), AFTER_INSTALL)

    # The word that opens the window, which is not run and prints nothing.
    last = len(printed) + 1
    for length in range(len(OPEN) + 1):
        frame(
            screen_of(printed, len(printed), INSTALL, OPEN[:length]),
            TYPING,
            (len(PROMPT) + length, last),
        )
    for _ in range(2):
        frame(
            screen_of(printed, len(printed), INSTALL, OPEN), BLINK, (len(PROMPT) + len(OPEN), last)
        )
        frame(screen_of(printed, len(printed), INSTALL, OPEN), BLINK)
    # It rests with the cursor showing, because a terminal waiting for somebody
    # to press Return is what the last line is, and the picture loops from here.
    frame(
        screen_of(printed, len(printed), INSTALL, OPEN), AT_THE_END, (len(PROMPT) + len(OPEN), last)
    )
    return picture, missing


# --------------------------------------------------------------------------
# The transcript, which is the half of this a test can read
# --------------------------------------------------------------------------


def transcript_of(printed: list[Printed], took: float) -> str:
    """What the picture shows, in text, because pixels are unreadable to a test.

    `install.gif` is the one artefact of the walkthrough a test cannot read.
    This file is written by the same run that writes the picture, and
    `test_what_a_stranger_downloads.py` reads it.
    """
    lines = [
        "This is what docs/images/install.gif shows, written by the same run of",
        "tools/record_install.py. The picture is pixels and no test can read one;",
        "this is what a test reads. Do not edit it by hand — record it again.",
        "",
        f"The install took {took:.1f} seconds, and the picture plays at that speed.",
        "",
        "Presented rather than recorded, and nothing else is:",
        "  - the prompt reads ~, while the run used a throwaway PIPX_HOME;",
        "  - pipx's note that the throwaway bin is not on PATH is dropped;",
        "  - the two emoji are drawn as a sparkle, because a 5x7 font has none.",
        "",
        "--- the screen ---",
        PROMPT + INSTALL,
    ]
    lines += [line.text for line in printed]
    lines += [PROMPT + OPEN, ""]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------


def font_proof(path: Path) -> None:
    """Every glyph this font has, drawn once, so the shapes can be looked at."""
    characters = sorted(GLYPHS)
    per_row = 32
    rows = [characters[start : start + per_row] for start in range(0, len(characters), per_row)]
    width = PAD_X * 2 + CELL_WIDTH * per_row
    height = _height(len(rows))
    canvas = Canvas(width, height)
    canvas.chrome("every glyph")
    for index, row in enumerate(rows):
        canvas.text(PAD_X, TITLE_HEIGHT + PAD_Y + index * CELL_HEIGHT, "".join(row), NORMAL)
    picture = Picture(width, height)
    picture.add(canvas, 100)
    path.write_bytes(picture.written())


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="record_install.py",
        description="Record `pipx install diglibrary` and draw it as a GIF that plays.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write docs/images/install.gif and install.txt. Without it, nothing here is touched.",
    )
    parser.add_argument(
        "--font-proof",
        action="store_true",
        help="Draw every glyph of the built-in font and stop.",
    )
    chosen = parser.parse_args()

    if chosen.font_proof:
        proof = Path(tempfile.gettempdir()) / "diglibrary-font-proof.gif"
        font_proof(proof)
        print(f"wrote {proof}")
        return 0

    printed, took = record()
    picture, missing = compose(printed, took)
    if missing:
        raise SystemExit(
            "this font has no glyph for "
            + " ".join(f"{character!r}" for character in sorted(missing))
            + ", and drawing it as `?` is a screen saying a word nobody said. "
            "Add the glyph, or add it to FOLDED. Nothing was written."
        )
    payload = picture.written()
    transcript = transcript_of(printed, took)

    if not chosen.apply:
        rehearsal = Path(tempfile.gettempdir()) / "diglibrary-install-rehearsal.gif"
        rehearsal.write_bytes(payload)
        print(transcript)
        print(f"rehearsal: {rehearsal} — {len(payload):,} bytes, {len(picture.chunks)} frames")
        print(f"nothing was written to {PICTURE.parent}. Pass --apply to write it.")
        return 0

    PICTURE.write_bytes(payload)
    TRANSCRIPT.write_text(transcript, encoding="utf-8")
    print(f"wrote {PICTURE} — {len(payload):,} bytes, {len(picture.chunks)} frames")
    print(f"wrote {TRANSCRIPT}")
    return 0


# --------------------------------------------------------------------------
# A 5x7 font, one line per glyph, written so the shape is the source.
# --------------------------------------------------------------------------

GLYPHS: dict[str, tuple[int, ...]] = {
    " ": (0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b00000),
    "!": (0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00000, 0b00100),
    '"': (0b01010, 0b01010, 0b00000, 0b00000, 0b00000, 0b00000, 0b00000),
    "#": (0b01010, 0b01010, 0b11111, 0b01010, 0b11111, 0b01010, 0b01010),
    "$": (0b00100, 0b01111, 0b10100, 0b01110, 0b00101, 0b11110, 0b00100),
    "%": (0b11000, 0b11001, 0b00010, 0b00100, 0b01000, 0b10011, 0b00011),
    "&": (0b01100, 0b10010, 0b10100, 0b01000, 0b10101, 0b10010, 0b01101),
    "'": (0b00100, 0b00100, 0b00000, 0b00000, 0b00000, 0b00000, 0b00000),
    "(": (0b00010, 0b00100, 0b01000, 0b01000, 0b01000, 0b00100, 0b00010),
    ")": (0b01000, 0b00100, 0b00010, 0b00010, 0b00010, 0b00100, 0b01000),
    "*": (0b00000, 0b00100, 0b10101, 0b01110, 0b10101, 0b00100, 0b00000),
    "+": (0b00000, 0b00100, 0b00100, 0b11111, 0b00100, 0b00100, 0b00000),
    ",": (0b00000, 0b00000, 0b00000, 0b00000, 0b00110, 0b00100, 0b01000),
    "-": (0b00000, 0b00000, 0b00000, 0b11111, 0b00000, 0b00000, 0b00000),
    ".": (0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b01100, 0b01100),
    "/": (0b00001, 0b00010, 0b00010, 0b00100, 0b01000, 0b01000, 0b10000),
    "0": (0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110),
    "1": (0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "2": (0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b01000, 0b11111),
    "3": (0b11111, 0b00010, 0b00100, 0b00010, 0b00001, 0b10001, 0b01110),
    "4": (0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010),
    "5": (0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110),
    "6": (0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110),
    "7": (0b11111, 0b10001, 0b00001, 0b00010, 0b00100, 0b00100, 0b00100),
    "8": (0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110),
    "9": (0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100),
    ":": (0b00000, 0b01100, 0b01100, 0b00000, 0b01100, 0b01100, 0b00000),
    ";": (0b00000, 0b01100, 0b01100, 0b00000, 0b01100, 0b00100, 0b01000),
    "<": (0b00010, 0b00100, 0b01000, 0b10000, 0b01000, 0b00100, 0b00010),
    "=": (0b00000, 0b00000, 0b11111, 0b00000, 0b11111, 0b00000, 0b00000),
    ">": (0b01000, 0b00100, 0b00010, 0b00001, 0b00010, 0b00100, 0b01000),
    "?": (0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b00000, 0b00100),
    "@": (0b01110, 0b10001, 0b00001, 0b01101, 0b10101, 0b10101, 0b01110),
    "A": (0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "B": (0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110),
    "C": (0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110),
    "D": (0b11100, 0b10010, 0b10001, 0b10001, 0b10001, 0b10010, 0b11100),
    "E": (0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111),
    "F": (0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000),
    "G": (0b01110, 0b10001, 0b10000, 0b10111, 0b10001, 0b10001, 0b01111),
    "H": (0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "I": (0b01110, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "J": (0b00111, 0b00010, 0b00010, 0b00010, 0b00010, 0b10010, 0b01100),
    "K": (0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001),
    "L": (0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111),
    "M": (0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001),
    "N": (0b10001, 0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001),
    "O": (0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "P": (0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000),
    "Q": (0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101),
    "R": (0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001),
    "S": (0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110),
    "T": (0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100),
    "U": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "V": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100),
    "W": (0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b10101, 0b01010),
    "X": (0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001),
    "Y": (0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100),
    "Z": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111),
    "[": (0b01110, 0b01000, 0b01000, 0b01000, 0b01000, 0b01000, 0b01110),
    "\\": (0b10000, 0b01000, 0b01000, 0b00100, 0b00010, 0b00010, 0b00001),
    "]": (0b01110, 0b00010, 0b00010, 0b00010, 0b00010, 0b00010, 0b01110),
    "^": (0b00100, 0b01010, 0b10001, 0b00000, 0b00000, 0b00000, 0b00000),
    "_": (0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b11111),
    "`": (0b01000, 0b00100, 0b00000, 0b00000, 0b00000, 0b00000, 0b00000),
    "a": (0b00000, 0b00000, 0b01110, 0b00001, 0b01111, 0b10001, 0b01111),
    "b": (0b10000, 0b10000, 0b11110, 0b10001, 0b10001, 0b10001, 0b11110),
    "c": (0b00000, 0b00000, 0b01111, 0b10000, 0b10000, 0b10000, 0b01111),
    "d": (0b00001, 0b00001, 0b01111, 0b10001, 0b10001, 0b10001, 0b01111),
    "e": (0b00000, 0b00000, 0b01110, 0b10001, 0b11111, 0b10000, 0b01110),
    "f": (0b00110, 0b01001, 0b01000, 0b11100, 0b01000, 0b01000, 0b01000),
    "g": (0b00000, 0b01111, 0b10001, 0b10001, 0b01111, 0b00001, 0b01110),
    "h": (0b10000, 0b10000, 0b11110, 0b10001, 0b10001, 0b10001, 0b10001),
    "i": (0b00100, 0b00000, 0b01100, 0b00100, 0b00100, 0b00100, 0b01110),
    "j": (0b00010, 0b00000, 0b00110, 0b00010, 0b00010, 0b10010, 0b01100),
    "k": (0b10000, 0b10000, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010),
    "l": (0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "m": (0b00000, 0b00000, 0b11010, 0b10101, 0b10101, 0b10101, 0b10101),
    "n": (0b00000, 0b00000, 0b11110, 0b10001, 0b10001, 0b10001, 0b10001),
    "o": (0b00000, 0b00000, 0b01110, 0b10001, 0b10001, 0b10001, 0b01110),
    "p": (0b00000, 0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000),
    "q": (0b00000, 0b01111, 0b10001, 0b10001, 0b01111, 0b00001, 0b00001),
    "r": (0b00000, 0b00000, 0b10110, 0b11000, 0b10000, 0b10000, 0b10000),
    "s": (0b00000, 0b00000, 0b01111, 0b10000, 0b01110, 0b00001, 0b11110),
    "t": (0b00100, 0b00100, 0b11111, 0b00100, 0b00100, 0b00101, 0b00010),
    "u": (0b00000, 0b00000, 0b10001, 0b10001, 0b10001, 0b10011, 0b01101),
    "v": (0b00000, 0b00000, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100),
    "w": (0b00000, 0b00000, 0b10001, 0b10101, 0b10101, 0b10101, 0b01010),
    "x": (0b00000, 0b00000, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001),
    "y": (0b00000, 0b10001, 0b10001, 0b10001, 0b01111, 0b00001, 0b01110),
    "z": (0b00000, 0b00000, 0b11111, 0b00010, 0b00100, 0b01000, 0b11111),
    "{": (0b00010, 0b00100, 0b00100, 0b01000, 0b00100, 0b00100, 0b00010),
    "|": (0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100),
    "}": (0b01000, 0b00100, 0b00100, 0b00010, 0b00100, 0b00100, 0b01000),
    "~": (0b00000, 0b00000, 0b01001, 0b10101, 0b10010, 0b00000, 0b00000),
    "✦": (0b00100, 0b00100, 0b01110, 0b11111, 0b01110, 0b00100, 0b00100),
}


if __name__ == "__main__":
    sys.exit(main())
