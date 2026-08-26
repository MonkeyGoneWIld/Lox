"""The Lox icon, as shapes.

A pirate flag over a pink disc, with music notes around it -- drawn from the
reference the owner supplied rather than embedding it, so the mark is the
project's own geometry and carries nobody else's licence.

Everything is defined on a 512x512 grid and scaled, so one set of numbers
produces the 16px favicon and the 512px app icon. Two emitters read the same
shapes: `render` rasterises with Pillow, `svg` writes the scalable copy a
modern browser prefers. There is no third place a colour or a coordinate can
drift.

Only two primitives, because two are enough and every extra one is another
thing the two emitters can disagree about: a circle, and a polygon. Ellipses
and arcs are turned into polygons here, once, so both emitters draw the same
points.
"""

import math

# --- palette ----------------------------------------------------------------

PINK = "#fb3b8f"          # the disc
PINK_SHADE = "#ef1f7b"    # its shaded side, lower right
FLAG = "#263238"          # the flag's near face
FLAG_SHADE = "#1b262c"    # the face turned away from the light
FLAG_FOLD = "#37474f"     # the tail, folded forward and catching more light
POLE = "#dce9f5"
POLE_LIT = "#ffffff"
BONE = "#ffffff"          # the skull, lit side
BONE_SHADE = "#d7e6f4"    # and its shaded half
NOTE = "#263238"

SIZE = 512.0


def _ellipse(cx, cy, rx, ry, rot=0.0, steps=48):
    """An ellipse as a polygon, so both emitters draw identical points."""
    angle = math.radians(rot)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    points = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        x, y = rx * math.cos(t), ry * math.sin(t)
        points.append((cx + x * cos_a - y * sin_a, cy + x * sin_a + y * cos_a))
    return points


def _arc(cx, cy, r, start, end, steps=40):
    """Points along a circular arc, degrees, clockwise on screen."""
    return [
        (
            cx + r * math.cos(math.radians(start + (end - start) * i / steps)),
            cy + r * math.sin(math.radians(start + (end - start) * i / steps)),
        )
        for i in range(steps + 1)
    ]


def _note(x, y, scale=1.0, stem_up=True):
    """One quaver: a slanted head, a stem, and a flag."""
    s = scale
    head = _ellipse(x, y, 15 * s, 11 * s, rot=-22)
    stem_x = x + 13 * s if stem_up else x - 13 * s
    top = y - 52 * s if stem_up else y + 52 * s
    stem = [(stem_x - 4 * s, y), (stem_x + 2 * s, y), (stem_x + 2 * s, top), (stem_x - 4 * s, top)]
    tail = [
        (stem_x + 2 * s, top),
        (stem_x + 2 * s, top + 20 * s),
        (stem_x + 14 * s, top + 30 * s),
        (stem_x + 17 * s, top + 14 * s),
        (stem_x + 9 * s, top + 6 * s),
    ]
    return [(head, NOTE), (stem, NOTE), (tail, NOTE)]


def _beamed(x, y, scale=1.0):
    """Two quavers under one beam, which is what most of the reference uses."""
    s = scale
    gap = 46 * s
    top = y - 54 * s
    shapes = [
        (_ellipse(x, y, 15 * s, 11 * s, rot=-22), NOTE),
        (_ellipse(x + gap, y - 12 * s, 15 * s, 11 * s, rot=-22), NOTE),
        ([(x + 9 * s, y), (x + 15 * s, y), (x + 15 * s, top), (x + 9 * s, top)], NOTE),
        (
            [
                (x + gap + 9 * s, y - 12 * s),
                (x + gap + 15 * s, y - 12 * s),
                (x + gap + 15 * s, top - 12 * s),
                (x + gap + 9 * s, top - 12 * s),
            ],
            NOTE,
        ),
        # The beam, sloping to meet the second stem where it ends.
        (
            [
                (x + 9 * s, top),
                (x + gap + 15 * s, top - 12 * s),
                (x + gap + 15 * s, top + 5 * s),
                (x + 9 * s, top + 17 * s),
            ],
            NOTE,
        ),
    ]
    return shapes


def _skull():
    """Cranium, jaw, two angry eyes and a nose, in two tones."""
    cx, cy = 246.0, 258.0
    lit = _ellipse(cx, cy - 8, 66, 60)
    shapes = [
        (lit, BONE),
        ([p for p in lit if p[0] >= cx] + [(cx, cy + 52), (cx, cy - 68)], BONE_SHADE),
        ([(cx - 34, cy + 36), (cx + 34, cy + 36), (cx + 26, cy + 78), (cx - 26, cy + 78)], BONE),
        ([(cx, cy + 36), (cx + 34, cy + 36), (cx + 26, cy + 78), (cx, cy + 78)], BONE_SHADE),
        # Eyes: slanted inward, which is the whole expression.
        ([(cx - 52, cy - 26), (cx - 12, cy - 6), (cx - 20, cy + 20), (cx - 56, cy + 4)], FLAG),
        ([(cx + 52, cy - 26), (cx + 12, cy - 6), (cx + 20, cy + 20), (cx + 56, cy + 4)], FLAG),
        # Nose.
        ([(cx, cy + 14), (cx - 12, cy + 36), (cx + 12, cy + 36)], FLAG),
        # Two teeth-gaps, cut out of the jaw with the flag colour behind it.
        ([(cx - 11, cy + 40), (cx - 5, cy + 40), (cx - 6, cy + 76), (cx - 12, cy + 76)], FLAG),
        ([(cx + 5, cy + 40), (cx + 11, cy + 40), (cx + 12, cy + 76), (cx + 6, cy + 76)], FLAG),
    ]
    return shapes


def _shapes():
    """The whole icon, back to front."""
    out = [("circle", (256.0, 256.0, 256.0), PINK)]

    # The shaded side of the disc: an arc from lower-left round to upper-right,
    # closed across the middle.
    # Light from the upper left, so the shade is the lower-right half. The
    # chord closes the polygon on its own; the arc's own endpoints are it.
    out.append(("poly", _arc(256, 256, 256, -45, 135), PINK_SHADE))

    # The pole, and the light down its leading edge.
    out.append(("poly", [(430, 116), (455, 126), (338, 508), (313, 498)], POLE))
    out.append(("poly", [(430, 116), (438, 119), (321, 501), (313, 498)], POLE_LIT))

    # The flag: the face turned away first, then the near face over it, then
    # the tail folded forward on the left.
    out.append(("poly", [(292, 126), (436, 152), (430, 300), (292, 274)], FLAG_SHADE))
    out.append(("poly", [(150, 196), (424, 148), (392, 330), (150, 392)], FLAG))
    out.append(("poly", [(62, 212), (150, 196), (150, 392), (62, 366), (108, 290)], FLAG_FOLD))

    out.extend(("poly", pts, fill) for pts, fill in _skull())

    # Notes, kept clear of the flag so they read as their own thing.
    out.extend(("poly", pts, fill) for pts, fill in _beamed(126, 96, 0.86))
    out.extend(("poly", pts, fill) for pts, fill in _beamed(150, 455, 0.78))
    out.extend(("poly", pts, fill) for pts, fill in _note(452, 322, 0.80))
    return out


SHAPES = _shapes()


# --- emitters ---------------------------------------------------------------

SS = 8  # supersampling; at 16px the antialiasing is most of the drawing


def render(px: int, background=None):
    """The icon at one size, transparent outside the disc.

    Args:
        px: Width and height in pixels.
        background: A colour behind it, or None to leave the ground clear.

    Returns:
        An RGBA image.
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    big = Image.new("RGBA", (px * SS, px * SS), background or (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    scale = px * SS / SIZE
    for kind, data, fill in SHAPES:
        if kind == "circle":
            cx, cy, r = data
            draw.ellipse(
                [(cx - r) * scale, (cy - r) * scale, (cx + r) * scale, (cy + r) * scale], fill=fill
            )
        else:
            draw.polygon([(x * scale, y * scale) for x, y in data], fill=fill)
    return big.resize((px, px), Image.LANCZOS)


def svg() -> str:
    """The icon as SVG, which is what a modern browser puts in the tab."""
    body = []
    for kind, data, fill in SHAPES:
        if kind == "circle":
            cx, cy, r = data
            body.append(f'  <circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{fill}"/>')
        else:
            points = " ".join(f"{x:.1f},{y:.1f}" for x, y in data)
            body.append(f'  <polygon points="{points}" fill="{fill}"/>')
    drawn = "\n".join(body)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="Lox">\n'
        "  <title>Lox</title>\n"
        f"{drawn}\n"
        "</svg>\n"
    )
