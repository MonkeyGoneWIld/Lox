"""The Lox mark, as geometry.

Three bars off a baseline: the spectrogram, which is the screen this app is
really about and the one thing a user looks at on every upload. It replaces a
cartoon fish from Flaticon that came with the upstream project, sat on an
opaque pale-blue square, and belonged to somebody else.

Two things here are load-bearing and both were learned at 16px. The bars are
FAT -- three of them, 7 units wide -- because four thin ones turned into a
smudge in a tab strip. And each carries an ink outline, because some browsers
tint the tab from the icon's own colour: an amber mark then lands on an amber
tab and disappears completely. The outline barely shows on a dark strip,
sharpens the mark on a light one, and is the whole reason the icon survives a
tab the same colour as itself without going back to a tile.

Everything is drawn in a 32x32 unit square, so the same numbers produce the
16px favicon and the 512px app icon. The four candidates that lost are kept in
`ALTERNATES` because a mark is chosen by looking at it next to the others, and
the next person to want a change should get to look at the same sheet.

Nothing here paints a background. The mark is all that is drawn and the alpha
channel carries the rest, which is the whole point: in a tab strip the icon has
to sit on whatever colour the browser is, not on a blue tile.
"""

from PIL import Image, ImageDraw

AMBER = "#e8a33d"  # --accent, the app's own
AMBER_ON_LIGHT = "#a8681c"  # --accent from the light theme
SS = 8  # supersampling factor; at 16px the antialiasing is most of the drawing


def _chevron(apex_y: float, rise: float, thick: float, x0: float, x1: float):
    """One stripe: a chevron of constant vertical thickness."""
    mid = (x0 + x1) / 2
    return [
        (x0, apex_y + rise),
        (mid, apex_y),
        (x1, apex_y + rise),
        (x1, apex_y + rise + thick),
        (mid, apex_y + thick),
        (x0, apex_y + rise + thick),
    ]


def _shrink(polys, k: float = 0.94, c: float = 16.0):
    """Pull the mark off the edges of its box, about the centre."""
    return [[(c + (x - c) * k, c + (y - c) * k) for x, y in poly] for poly in polys]


#: How far the ink outline stands out from each bar, in grid units. The bars
#: sit 1.5 apart, so anything past ~1.2 closes that gap with ink and the three
#: bars merge into one badge -- which is the tile this mark exists to avoid.
#: 1.2 is the most edge that still leaves daylight between the bars.
OUTLINE = 1.2

#: The ink the outline is drawn in. Dark in both themes on purpose -- against a
#: dark strip it barely shows, and everywhere else it is the thing separating
#: the mark from whatever is behind it.
OUTLINE_INK = "#17150f"


def _bar(x: float, top: float, width: float = 7.0, floor: float = 27.5, grow: float = 0.0):
    """One band, standing on the baseline."""
    return [
        (x - grow, top - grow),
        (x + width + grow, top - grow),
        (x + width + grow, floor + grow),
        (x - grow, floor + grow),
    ]


#: The bars, as (x, top). Three of them 7 wide on an 8.5 pitch, so the block
#: runs 4..28 -- centred on 16, and 3.5px per bar at favicon size.
BARS: tuple[tuple[float, float], ...] = ((4.0, 13.0), (12.5, 4.0), (21.0, 9.5))

#: The mark, as polygons in the 32x32 box.
LOX = [_bar(x, top) for x, top in BARS]

#: The same bars grown by the outline, drawn underneath them.
LOX_OUTLINE = [_bar(x, top, grow=OUTLINE) for x, top in BARS]


def _draw(polys, draw, s: float, fill: str) -> None:
    for poly in polys:
        draw.polygon([(x * s, y * s) for x, y in poly], fill=fill)


def render(px: int, fill: str = AMBER, background: str | None = None, polys=None) -> Image.Image:
    """The mark at one size, transparent everywhere the mark is not.

    Args:
        px: Width and height in pixels.
        fill: The colour of the mark.
        background: A colour to sit it on, or None to leave the ground clear.
        polys: An alternate mark, for the comparison sheet.

    Returns:
        An RGBA image.
    """
    big = Image.new("RGBA", (px * SS, px * SS), background or (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    scale = px * SS / 32.0
    if polys is None:
        # Outline first, mark on top, so exactly OUTLINE units of ink stand
        # out on every side.
        _draw(LOX_OUTLINE, draw, scale, OUTLINE_INK)
    _draw(polys or LOX, draw, scale, fill)
    return big.resize((px, px), Image.LANCZOS)


def svg() -> str:
    """The mark as SVG, which is what a modern browser puts in the tab.

    Carries both accents: the tab strip follows the reader's system theme, and
    the deep amber is what the app's own light theme uses.
    """
    def paths_for(polys) -> str:
        return "\n".join(
            "    <path d=" + chr(34) + "M"
            + " L".join(f"{x:.2f} {y:.2f}" for x, y in poly) + " Z" + chr(34) + "/>"
            for poly in polys
        )

    edge = paths_for(LOX_OUTLINE)
    mark = paths_for(LOX)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" role="img" aria-label="Lox">
  <title>Lox</title>
  <style>
    .mark {{ fill: {AMBER}; }}
    .edge {{ fill: {OUTLINE_INK}; }}
    @media (prefers-color-scheme: light) {{ .mark {{ fill: {AMBER_ON_LIGHT}; }} }}
  </style>
  <g class="edge">
{edge}
  </g>
  <g class="mark">
{mark}
  </g>
</svg>
"""


# --- the ones that lost, kept so the choice can be looked at again ----------


def _alt_stripes(draw, s):
    """Three stripes and no head: purer, but it reads as rank insignia."""
    _draw([_chevron(a, 5.6, 4.9, 4.5, 27.5) for a in (2.8, 12.0, 21.2)], draw, s, AMBER)


def _alt_fillet(draw, s):
    """A solid head over two stripes: the cut of a fillet, pointing up."""
    _draw(
        _shrink(
            [
                [(4, 12.25), (16, 1.75), (28, 12.25), (16, 7.55)],
                _chevron(13.75, 5.0, 4.3, 5.5, 26.5),
                _chevron(20.95, 5.0, 4.3, 5.5, 26.5),
            ]
        ),
        draw,
        s,
        AMBER,
    )


def _alt_monogram(draw, s):
    """An L: legible at every size, and belongs to every app starting with L."""
    _draw([[(8, 7), (11, 4), (14, 4), (14, 21), (26, 21), (26, 28), (8, 28)]], draw, s, AMBER)


def _alt_tail(draw, s):
    """A tail fluke: boldest at 16px, but it is just an arrow."""
    _draw([[(16, 5), (5, 27), (16, 18), (27, 27)]], draw, s, AMBER)


ALTERNATES = {
    "Fillet": _alt_fillet,
    "Stripes": _alt_stripes,
    "Monogram": _alt_monogram,
    "Tail": _alt_tail,
}
