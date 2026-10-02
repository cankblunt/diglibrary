"""The encoder behind `install.gif`, checked by reading its own output back.

`tools/record_install.py` writes a GIF with no image library, because there is
no image library here and there is not going to be one. That is the same choice
`icon.py` makes, and it has the same exposure: an encoder that is subtly wrong
does not raise, it draws.

The width of an LZW code grows partway through the stream. An encoder that
grows it one code later than a decoder does produces a picture with one colour
missing from it and everything else in place, which an image viewer accepts
without complaint. Decoding the file here is what finds it.

The round trip is the whole assertion: what a decoder reads out has to be the
pixels that were drawn in. A run of one value repeated is the case that matters
and the case a picture is full of — a blank line, a margin, a title bar — and it
is the one that fails, because every code it emits is the entry defined by the
code before it.
"""

import struct
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "tools"))

import record_install  # noqa: E402


def _decode(payload: bytes, minimum: int) -> bytes:
    """GIF's LZW, read the way a decoder reads it.

    Written from the format rather than from the encoder beside it, which is the
    only reason it can disagree with it. The width grows at `>=` here and at `>`
    there: the decoder has assigned one code fewer at the same point in the
    stream, and that difference is the defect this file exists for.
    """
    clear, end = 1 << minimum, (1 << minimum) + 1
    table = [bytes([value]) for value in range(clear)] + [b"", b""]
    width = minimum + 1
    out = bytearray()
    previous: bytes | None = None
    at = 0

    def read(count: int) -> int:
        nonlocal at
        value = 0
        for step in range(count):
            value |= ((payload[(at + step) >> 3] >> ((at + step) & 7)) & 1) << step
        at += count
        return value

    while True:
        code = read(width)
        if code == end:
            return bytes(out)
        if code == clear:
            table = [bytes([value]) for value in range(clear)] + [b"", b""]
            width = minimum + 1
            previous = None
            continue
        if code < len(table):
            entry = table[code]
        else:
            assert previous is not None, "a stream that starts by naming an entry it has not made"
            entry = previous + previous[:1]
        if previous is not None:
            table.append(previous + entry[:1])
        out += entry
        previous = entry
        if len(table) >= (1 << width) and width < 12:
            width += 1


def _unblock(blocks: bytes) -> bytes:
    """Sub-blocks back into one stream."""
    payload = bytearray()
    at = 0
    while blocks[at]:
        size = blocks[at]
        payload += blocks[at + 1 : at + 1 + size]
        at += 1 + size
    return bytes(payload)


def test_every_shape_of_pixels_survives_the_encoder() -> None:
    """Read back what was written in, over the cases that break LZW differently."""
    import random

    random.seed(576)
    cases = {
        "one long run": bytes([0]) * 6000,
        "a run with something in it": bytes([0]) * 3000 + bytes([3]) * 40 + bytes([0]) * 3000,
        "every value, in order, over and over": bytes(range(11)) * 900,
        "noise, which fills the table fastest": bytes(random.randrange(11) for _ in range(40000)),
        "nothing at all but one pixel": bytes([7]),
    }
    for name, pixels in cases.items():
        read_back = _decode(_unblock(record_install._lzw(pixels)), record_install._MIN_CODE_SIZE)
        assert read_back == pixels, (
            f"{name}: {len(pixels)} pixels went in and {len(read_back)} came out, "
            "so the picture this writes is not the picture it drew"
        )


def test_a_whole_frame_reads_back_as_the_screen_it_was_drawn_from() -> None:
    """A terminal, drawn, encoded and read back."""
    printed = [
        record_install.Printed(at=0.4, text="creating virtual environment..."),
        record_install.Printed(at=2.0, text="WARNING: Skipping setuptools as it is not installed."),
        record_install.Printed(at=3.1, text="  installed package diglibrary 9.9.9"),
    ]
    rows = record_install.screen_of(printed, len(printed), record_install.INSTALL, "diglibrary")
    width = record_install.PAD_X * 2 + record_install.CELL_WIDTH * 64
    height = record_install._height(len(rows))
    drawn = record_install.draw(rows, width, height, (4, 0))

    picture = record_install.Picture(width, height)
    picture.add(drawn, 100)
    payload = picture.written()

    at = 6
    _, _, packed = struct.unpack("<HHB", payload[at : at + 5])
    at += 7 + 3 * (1 << ((packed & 7) + 1))
    while payload[at] == 0x21:
        at += 2
        while payload[at]:
            at += 1 + payload[at]
        at += 1
    assert payload[at] == 0x2C, "the first thing after the header is not an image"
    left, top, box_width, box_height, _ = struct.unpack("<HHHHB", payload[at + 1 : at + 10])
    assert (left, top, box_width, box_height) == (
        0,
        0,
        width,
        height,
    ), "the first frame does not cover the screen, so a viewer has nothing to start from"
    at += 10
    minimum = payload[at]

    read_back = _decode(_unblock(payload[at + 1 :]), minimum)
    assert read_back == bytes(drawn.pixels), "the frame read back is not the frame that was drawn"

    # The typed command is the only white on the screen, and a code width grown
    # late loses exactly that colour. Named here so the failure says so.
    assert record_install.BRIGHT in read_back, "the typed command is not in the picture"


def test_a_character_with_no_glyph_is_reported_by_whatever_drew_it() -> None:
    """A character with no glyph draws a `?`, which nobody typed.

    An em dash is not in a 5x7 font. Listing the characters the recording is
    known to put on a screen and asking whether the font has them misses the
    one nobody listed, so the question is asked of the drawing instead:
    whatever is drawn reports what it could not draw, whether it came from
    pipx, from the prompt or from the title bar, and `record_install.py`
    refuses to write when the set is not empty.
    """
    printed = [record_install.Printed(at=0.1, text="done — ready ⚡")]
    rows = record_install.screen_of(printed, 1, record_install.INSTALL, None)
    width = record_install.PAD_X * 2 + record_install.CELL_WIDTH * 64
    canvas = record_install.draw(rows, width, record_install._height(len(rows)), None)

    assert canvas.missing == {"⚡"}, (
        "the drawing does not report what it could not draw, so a question mark "
        f"reaches the picture in silence: {canvas.missing}"
    )

    clean = record_install.screen_of(
        [record_install.Printed(at=0.1, text="done! ✨ 🌟 ✨")],
        1,
        record_install.INSTALL,
        "diglibrary",
    )
    canvas = record_install.draw(clean, width, record_install._height(len(clean)), None)
    assert (
        canvas.missing == set()
    ), f"the recording's own screen cannot be drawn by this font: {canvas.missing}"
