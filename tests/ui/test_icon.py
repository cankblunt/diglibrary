"""The generated icon has to stay the reference drawing.

`diglibrary.desktop.icon` reproduces `docs/brand/diglibrary-icon-1024.png` from
numbers measured out of it — twenty-odd constants, every one of which reads like
something a tidy-minded person would round off. Nothing would say if they were.

So the reference artwork lives in the repository and this compares against it.
The threshold is deliberately loose: two rasterisers never agree along a long
diagonal edge, and the arm is one. What it catches is a constant that moved.

The drawing code is inside the package, so that an installation which never had
a checkout can still put an icon in somebody's Dock. The artwork it is compared
against stays in `docs/`, where it is not shipped and not needed at runtime.
"""

import math
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

from diglibrary.desktop import icon as make_icon

PROJECT = Path(__file__).parent.parent.parent
REFERENCE = PROJECT / "docs" / "brand" / "diglibrary-icon-1024.png"


def _decode(path: Path) -> tuple[int, list[bytes], int]:
    """Read an RGBA PNG file back to rows."""
    return _decode_bytes(path.read_bytes())


def _decode_bytes(data: bytes) -> tuple[int, list[bytes], int]:
    """Read an RGBA PNG back to rows, undoing the per-scanline filters.

    Split from `_decode` so the same decoder reads a file on disk and a stream
    lifted out of an `.icns`, rather than a second one being written for the
    second caller.
    """
    position, pixels, meta = 8, b"", {}
    while position < len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        if kind == b"IHDR":
            width, height, _, colour = struct.unpack(">IIBB", body[:10])
            meta = {"width": width, "height": height, "colour": colour}
        elif kind == b"IDAT":
            pixels += body
        position += 12 + length
    raw = zlib.decompress(pixels)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[meta["colour"]]
    stride = meta["width"] * channels
    rows: list[bytes] = []
    previous = bytearray(stride)
    at = 0
    for _ in range(meta["height"]):
        filter_type = raw[at]
        line = bytearray(raw[at + 1 : at + 1 + stride])
        at += 1 + stride
        for index in range(stride):
            left = line[index - channels] if index >= channels else 0
            up = previous[index]
            corner = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                line[index] = (line[index] + left) & 0xFF
            elif filter_type == 2:
                line[index] = (line[index] + up) & 0xFF
            elif filter_type == 3:
                line[index] = (line[index] + (left + up) // 2) & 0xFF
            elif filter_type == 4:
                guess = left + up - corner
                nearest = min(
                    (abs(guess - left), left), (abs(guess - up), up), (abs(guess - corner), corner)
                )[1]
                line[index] = (line[index] + nearest) & 0xFF
        rows.append(bytes(line))
        previous = line
    return meta["width"], rows, channels


def test_the_generated_icon_is_still_the_reference_drawing() -> None:
    """Every fourth pixel, which is sixty-five thousand of them."""
    width, reference, channels = _decode(REFERENCE)
    assert width == 1024
    drawn = make_icon.draw(1024)
    stride = 1024 * 4 + 1  # the leading filter byte of each row

    step = 4
    compared = differing = 0
    for y in range(0, 1024, step):
        mine = drawn[y * stride + 1 : (y + 1) * stride]
        theirs = reference[y]
        for x in range(0, 1024, step):
            compared += 1
            gap = max(
                abs(mine[x * 4 + channel] - theirs[x * channels + channel]) for channel in range(4)
            )
            if gap > 16:
                differing += 1

    share = differing / compared
    assert share < 0.02, (
        f"the icon has drifted from docs/brand/diglibrary-icon-1024.png: "
        f"{share:.1%} of sampled pixels differ (two rasterisers differ by about 1%)"
    )


def test_the_small_sizes_drop_the_tonearm_rather_than_smearing_it() -> None:
    """Below 64px the arm is under two pixels wide, so it is left out.

    Asserted where the arm's shaft would be, well clear of the record: at 32px
    that point has to be plain cream, and at 64px it must not be.
    """
    for size, expect_arm in ((32, False), (64, True), (128, True)):
        rows = make_icon.draw(size)
        stride = size * 4 + 1
        # A point on the arm's axis, between the bearing and the record's edge.
        along = 140 / 1024 * size
        x = round(make_icon.PIVOT[0] * size + make_icon.ARM_AXIS[0] * along)
        y = round(make_icon.PIVOT[1] * size + make_icon.ARM_AXIS[1] * along)
        pixel = rows[y * stride + 1 + x * 4 : y * stride + 1 + x * 4 + 3]
        cream = all(abs(a - b) <= 20 for a, b in zip(pixel, make_icon.CREAM, strict=True))
        assert cream is not expect_arm, (
            f"at {size}px the arm is "
            f"{'missing' if expect_arm else 'being drawn where it turns to haze'}"
        )


def test_the_reference_is_reproduced_at_full_size_without_being_shipped() -> None:
    """The iconset is generated, so the artwork is a reference and not an asset.

    If the build ever starts *resizing* that PNG instead of drawing each size,
    the 16px icon becomes a grey smudge — the arm is a third of a pixel wide
    there. This states the direction of the dependency.
    """
    assert not any(
        (PROJECT / "src").rglob("*.png")
    ), "the window ships no bitmaps; the mark in its header is drawn as SVG"
    assert math.isclose(make_icon.CORNER * 1024, 224.0)


def test_the_icon_this_package_ships_is_the_drawing_it_claims_to_be() -> None:
    """It is carried rather than drawn, and a carried picture can go stale.

    A `.icns` cached and returned on sight can be older than the drawing that
    is supposed to have produced it. Drawing all thirteen sizes at the moment
    somebody asks avoids that and takes several seconds, which is affordable
    for a typed command and not for a box ticked in the first-run window. So
    the picture ships, and this stands in for the expiry it does not have.

    **The comparison is of the whole file, and the cheaper one does not exist.**
    `iconutil` re-encodes every member it packs, so a size decoded out of the
    shipped icon does not equal the drawing that produced it: a large share of
    the pixels differ, by up to 255 per channel, because RGB is zeroed wherever
    alpha is. The picture is correct; the pixels are not the drawing's. What is
    reliable is that `iconutil` is deterministic: building twice from one
    iconset gives identical bytes.

    This is the slowest test in the folder. That is the price of the file being
    provably what the drawing beside it makes.
    """
    shipped = PROJECT / "src/diglibrary/desktop/DigLibrary.icns"
    assert shipped.is_file(), "this package ships no icon, and the bundle copies one"

    with tempfile.TemporaryDirectory() as scratch:
        iconset = Path(scratch) / "DigLibrary.iconset"
        make_icon.write_iconset(iconset)
        rebuilt = Path(scratch) / "rebuilt.icns"
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(rebuilt)],
            check=True,
            capture_output=True,
        )
        assert shipped.read_bytes() == rebuilt.read_bytes(), (
            "the icon this package ships is not what the drawing beside it "
            "produces — regenerate src/diglibrary/desktop/DigLibrary.icns"
        )
