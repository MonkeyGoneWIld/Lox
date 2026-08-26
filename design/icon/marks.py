"""The Lox mark, as geometry.

One shape, three pieces: a solid head over two stripes, pointing up. It is the
cut of a lox fillet, and it is the direction everything in this app is going --
Deezer down, tracker up. It replaces a cartoon fish from Flaticon that came
with the upstream project, sat on an opaque pale-blue square, and belonged to
somebody else.

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


#: The mark, as polygons in the 32x32 box. Centred on 16 vertically: the head
#: runs 1.75..12.25 and the last stripe ends at 30.25, before the shrink.
LOX = _shrink(
    [
        [(4, 12.25), (16, 1.75), (28, 12.25), (16, 7.55)],
        _chevron(13.75, 5.0, 4.3, 5.5, 26.5),
        _chevron(20.95, 5.0, 4.3, 5.5, 26.5),
    ]
)


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
    _draw(polys or LOX, ImageDraw.Draw(big), px * SS / 32.0, fill)
    return big.resize((px, px), Image.LANCZOS)


def svg() -> str:
    """The mark as SVG, which is what a modern browser puts in the tab.

    Carries both accents: the tab strip follows the reader's system theme, and
    the deep amber is what the app's own light theme uses.
    """
    paths = "\n".join(
        "    <path d=" + chr(34) + "M" + " L".join(f"{x:.2f} {y:.2f}" for x, y in poly) + " Z" + chr(34) + "/>"
        for poly in LOX
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" role="img" aria-label="Lox">
  <title>Lox</title>
  <style>
    path {{ fill: {AMBER}; }}
    @media (prefers-color-scheme: light) {{ path {{ fill: {AMBER_ON_LIGHT}; }} }}
  </style>
  <g>
{paths}
  </g>
</svg>
"""


# --- the ones that lost, kept so the choice can be looked at again ----------


def _alt_stripes(draw, s):
    """Three stripes and no head: purer, but it reads as rank insignia."""
    _draw([_chevron(a, 5.6, 4.9, 4.5, 27.5) for a in (2.8, 12.0, 21.2)], draw, s, AMBER)


def _alt_spectral(draw, s):
    """The spectrogram: honest about the product, quiet about the name."""
    for i, top in enumerate((13.0, 5.0, 17.0, 9.0)):
        x = 5.0 + i * 7.0
        draw.rectangle([x * s, top * s, (x + 4.5) * s, 28 * s], fill=AMBER)


def _alt_monogram(draw, s):
    """An L: legible at every size, and belongs to every app starting with L."""
    _draw([[(8, 7), (11, 4), (14, 4), (14, 21), (26, 21), (26, 28), (8, 28)]], draw, s, AMBER)


def _alt_tail(draw, s):
    """A tail fluke: boldest at 16px, but it is just an arrow."""
    _draw([[(16, 5), (5, 27), (16, 18), (27, 27)]], draw, s, AMBER)


ALTERNATES = {
    "Stripes": _alt_stripes,
    "Spectral": _alt_spectral,
    "Monogram": _alt_monogram,
    "Tail": _alt_tail,
}
