"""Regenerate every icon asset the app serves, from design/icon/source.png.

    python design/icon/build.py

Run it after replacing the source. The source is the owner's own artwork, kept
in the repo at 512px -- the size of the largest asset generated from it, so
nothing here is ever upscaled -- trimmed to its alpha bounds and squared, so
the icon sits centred rather than wherever the export happened to leave it.

Every size is a straight downscale. The artwork before this was a detailed
illustration that turned to mush at 16px and needed the frame tightened to the
skull to survive; this one is a badge -- a cream skull on a black disc, one
silhouette, high contrast -- and it reads small on its own. Checked against the
owner's own 16, 32 and 48px exports: indistinguishable, so there is nothing for
a crop to rescue and a crop would only differ from what they exported.

Anything a browser puts in a tab strip keeps its transparent ground, while the
iOS and Windows tiles get the app's own --bg: neither platform honours
transparency there, and both would otherwise pick a colour themselves.
"""

import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "source.png")
OUT = os.path.join(ROOT, "lox", "web", "static", "images")

INK = "#17150f"  # --bg, for the platforms that will not take a clear ground

TRANSPARENT = {
    "favicon-16x16.png": 16,
    "favicon-32x32.png": 32,
    "favicon-96x96.png": 96,
    "favicon-128.png": 128,
    "favicon-196x196.png": 196,
    # The largest anything displays this is the login page at 56px, so 256
    # is already generous on a 3x screen. At 512 it was a third of a megabyte
    # fetched to draw a 32px mark in the sidebar.
    "logo.png": 256,
}

ON_INK = {
    "apple-touch-icon-57x57.png": 57,
    "apple-touch-icon-60x60.png": 60,
    "apple-touch-icon-72x72.png": 72,
    "apple-touch-icon-76x76.png": 76,
    "apple-touch-icon-114x114.png": 114,
    "apple-touch-icon-120x120.png": 120,
    "apple-touch-icon-144x144.png": 144,
    "apple-touch-icon-152x152.png": 152,
    "mstile-70x70.png": 70,
    "mstile-144x144.png": 144,
    "mstile-150x150.png": 150,
    "mstile-310x310.png": 310,
}

#: Sizes inside the .ico. It stops at 64 on purpose: a browser reaching into
#: an .ico wants 16, 32 or 48, and the larger sizes are declared as their own
#: PNG links anyway. Carrying 128 and 256 in here as well took the file from
#: 21KB to 143KB, fetched on every cold load to be ignored.
ICO_SIZES = [16, 24, 32, 48, 64]


def source() -> Image.Image:
    """The artwork, as a centred square with a clear ground."""
    return Image.open(SOURCE).convert("RGBA")


def render(px: int) -> Image.Image:
    """The icon at one size.

    Args:
        px: Width and height in pixels.

    Returns:
        An RGBA image, transparent everywhere the artwork is not.
    """
    return source().resize((px, px), Image.LANCZOS)


def padded(px: int, ratio: float = 0.86) -> Image.Image:
    """The icon inset on a solid ground, the way a home-screen icon wants it."""
    plate = Image.new("RGBA", (px, px), INK)
    inner = max(1, round(px * ratio))
    plate.alpha_composite(render(inner), ((px - inner) // 2, (px - inner) // 2))
    return plate


def main() -> int:
    if not os.path.exists(SOURCE):
        print(f"no source artwork at {SOURCE}", file=sys.stderr)
        return 1

    os.makedirs(OUT, exist_ok=True)
    written = []

    for name, px in TRANSPARENT.items():
        render(px).save(os.path.join(OUT, name))
        written.append(name)

    for name, px in ON_INK.items():
        padded(px).save(os.path.join(OUT, name))
        written.append(name)

    wide = Image.new("RGBA", (310, 150), INK)
    wide.alpha_composite(render(120), (95, 15))
    wide.save(os.path.join(OUT, "mstile-310x150.png"))
    written.append("mstile-310x150.png")

    # One .ico holding every size a browser might reach for, each resized from
    # the source rather than from one another, so no entry is a downscale of a
    # downscale.
    frames = [render(s) for s in ICO_SIZES]
    frames[-1].save(
        os.path.join(OUT, "favicon.ico"),
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=frames[:-1],
    )
    written.append("favicon.ico")

    stale = os.path.join(OUT, "icon.svg")
    if os.path.exists(stale):
        # The artwork is an illustration, not geometry, so there is no honest
        # SVG to serve. Advertising one that is a raster in a wrapper is worse
        # than not advertising one.
        os.remove(stale)
        print("removed icon.svg -- the artwork is a raster now")

    print(f"wrote {len(written)} assets to {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
