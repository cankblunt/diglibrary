"""Draw the application icon, with no image library at all.

A PNG is a zlib stream of filtered scanlines in a handful of chunks, and this
mark is circles, one capsule and one rounded bar — analytic geometry the whole
way down. That is why this is a script instead of a dependency, and why the icon
is redrawn at every size rather than resized: the tonearm is four pixels wide at
64px and would be mush if it came from a downscale.

``write_iconset`` writes the folder of PNGs, and ``iconutil`` turns it into the
.icns macOS wants. Drawing every size takes several seconds, which is why this
is asked for once and not done at every launch.

The design exists as a 1024-pixel source image. Every number below was
**measured** out of that image rather than judged by eye — the colours by
sampling, the radii by walking the centre row and column for colour boundaries,
the arm by walking its own axis and measuring the perpendicular run at each
step — so that the drawing reproduces the artwork rather than approximating it.
"""

import math
import struct
import zlib
from pathlib import Path

# Sampled from the source PNG.
CREAM = (0xEB, 0xDD, 0xC4)
INK = (0x13, 0x13, 0x13)
LABEL = (0xF2, 0xC2, 0x30)
GROOVE = (0x2A, 0x2A, 0x2A)

# Every measurement as a fraction of the side, so one set of numbers draws every
# size. The source was 1024 square.
# The corner is an arc of 224, not of the 210 where the straight edge begins:
# swept against the source, 224 leaves a third fewer disagreeing pixels than 210
# and the difference between 223 and 225 is noise.
CORNER = 224 / 1024
DISC = 337.5 / 1024
GROOVE_OUTER = 282 / 1024
GROOVE_INNER = 270 / 1024
LABEL_RADIUS = 133.5 / 1024
SPINDLE = 24.5 / 1024
PIVOT = (880.5 / 1024, 143 / 1024)
PIVOT_RADIUS = 55.7 / 1024
# Half-widths carry three quarters of a unit over the measured edge, which is
# what the feathering below eats: measured back against the source, 22 rendered
# as 21 and 22.75 renders as 22.5, which is what the source has.
ARM_HALF = 22.75 / 1024
# The shaft stops inside the headshell rather than at the tip. Reaching the tip
# put its round cap *past* the tip — the source ends flat there, and a bulge on
# the end of a tonearm is a different object.
ARM_REACH = 250 / 1024
HEAD_FROM = 246 / 1024
HEAD_TO = 337 / 1024
HEAD_HALF = 36.6 / 1024
HEAD_CORNER = 10 / 1024

# The arm runs down and to the left at exactly forty-five degrees.
ARM_AXIS = (-1 / math.sqrt(2), 1 / math.sqrt(2))

# Below this, the arm and the groove are drawn away rather than drawn badly. At
# 32px the arm is under a pixel and a half wide and the groove is one pixel of
# near-black on black: both become grey haze that softens the one shape that has
# to be recognised instantly, which is the record. Apple's own icons simplify at
# these sizes for the same reason.
DETAIL_FLOOR = 64

SIZES = (16, 32, 64, 128, 256, 512, 1024)


def draw(size: int) -> bytes:
    """Return the icon at one size, as RGBA rows."""
    detailed = size >= DETAIL_FLOOR
    centre = (size - 1) / 2
    disc = DISC * size
    label = LABEL_RADIUS * size
    spindle = SPINDLE * size
    pivot = (PIVOT[0] * size, PIVOT[1] * size)
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter type 0: none
        for x in range(size):
            alpha = _corner_alpha(x, y, size, CORNER * size)
            if alpha == 0:
                rows.extend((0, 0, 0, 0))
                continue
            distance = math.hypot(x - centre, y - centre)
            colour = CREAM
            colour = _over(colour, INK, _disc(distance, disc, size))
            if detailed:
                # A single groove, which is what says "record" rather than
                # "black circle". It sits under everything else, because the arm
                # crosses it.
                colour = _over(
                    colour,
                    GROOVE,
                    _band(distance, GROOVE_INNER * size, GROOVE_OUTER * size, size),
                )
            colour = _over(colour, LABEL, _disc(distance, label, size))
            colour = _over(colour, INK, _disc(distance, spindle, size))
            if detailed:
                # **The arm changes colour where it crosses the record**, and
                # that is the design rather than an accident of it: a black arm
                # on black vinyl is an invisible arm. Outside the disc it is ink
                # on cream; over the disc it is cream on ink.
                on_vinyl = distance < disc
                arm_ink = CREAM if on_vinyl else INK
                colour = _over(colour, arm_ink, _pivot_coverage(x, y, pivot, size))
                colour = _over(colour, arm_ink, _arm_coverage(x, y, pivot, size))
                colour = _over(colour, arm_ink, _head_coverage(x, y, pivot, size))
            rows.extend((*colour, alpha))
    return bytes(rows)


def _pivot_coverage(x: int, y: int, pivot: tuple[float, float], size: int) -> float:
    """The bearing the arm swings on, at the top right."""
    return _disc(math.hypot(x - pivot[0], y - pivot[1]), PIVOT_RADIUS * size, size)


def _arm_coverage(x: int, y: int, pivot: tuple[float, float], size: int) -> float:
    """The shaft: a capsule from the bearing along the axis, with round caps."""
    along, across = _axial(x, y, pivot)
    reach = ARM_REACH * size
    if along < 0:
        return 0.0
    clamped = min(along, reach)
    to_line = math.hypot(along - clamped, across)
    return _disc(to_line, ARM_HALF * size, size)


def _head_coverage(x: int, y: int, pivot: tuple[float, float], size: int) -> float:
    """The headshell: a rounded bar at the far end, wider than the shaft.

    A rounded rectangle rather than a capsule, because the source ends nearly
    square — measured, the half-width holds at its full 36/1024 to within seven
    pixels of the tip.
    """
    along, across = _axial(x, y, pivot)
    radius = HEAD_CORNER * size
    half_length = (HEAD_TO - HEAD_FROM) * size / 2 - radius
    half_width = HEAD_HALF * size - radius
    middle = (HEAD_TO + HEAD_FROM) * size / 2
    dx = max(abs(along - middle) - half_length, 0.0)
    dy = max(abs(across) - half_width, 0.0)
    return _disc(math.hypot(dx, dy), radius, size)


def _axial(x: int, y: int, pivot: tuple[float, float]) -> tuple[float, float]:
    """This point in the arm's own frame: along the axis, and across it."""
    ox, oy = x - pivot[0], y - pivot[1]
    return ox * ARM_AXIS[0] + oy * ARM_AXIS[1], -ox * ARM_AXIS[1] + oy * ARM_AXIS[0]


def _corner_alpha(x: int, y: int, size: int, radius: float) -> int:
    """Antialias the rounded corner by measuring distance past the arc centre."""
    cx = min(max(x, radius), size - 1 - radius)
    cy = min(max(y, radius), size - 1 - radius)
    distance = math.hypot(x - cx, y - cy)
    return _coverage(radius - distance, size)


def _band(distance: float, inner: float, outer: float, size: int) -> float:
    """Coverage of an annulus at this distance, softened at both edges."""
    return min(_coverage(outer - distance, size), _coverage(distance - inner, size)) / 255


def _disc(distance: float, radius: float, size: int) -> float:
    return _coverage(radius - distance, size) / 255


def _coverage(signed: float, size: int) -> int:
    """Turn a signed distance into 0..255, feathered over roughly one pixel."""
    feather = max(0.8, size / 512)
    return max(0, min(255, round((signed / feather + 0.5) * 255)))


def _over(base: tuple[int, ...], ink: tuple[int, ...], coverage: float) -> tuple[int, ...]:
    if coverage <= 0:
        return base
    return tuple(round(b + (i - b) * coverage) for b, i in zip(base, ink, strict=True))


def write_png(path: Path, size: int) -> None:
    """Write one square RGBA PNG."""
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(draw(size), 9))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(payload)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def write_iconset(folder: Path) -> int:
    """Write the full iconset macOS expects, both scales of every size.

    Every image is redrawn at its own size rather than resized from the largest:
    the tonearm is four pixels wide at 64px and would be mush out of a downscale.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        write_png(folder / f"icon_{size}x{size}.png", size)
        if size > 16:
            write_png(folder / f"icon_{size // 2}x{size // 2}@2x.png", size)
    return len(list(folder.glob("*.png")))
