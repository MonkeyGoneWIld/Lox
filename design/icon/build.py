"""Regenerate every icon asset the app serves, from design/icon/source.png.

    python design/icon/build.py

Run it after replacing the source. The source is the owner's own artwork, kept
in the repo at 512px -- the size of the largest asset generated from it, so
nothing here is ever upscaled -- trimmed to its alpha bounds and squared, so
the icon sits centred rather than wherever the export happened to leave it.

Two things are deliberate.

Small sizes are CROPPED, not just shrunk. This is a detailed illustration:
headphones, a bandana, an eye patch, a full set of teeth. Shrunk whole to 16px
it is a grey speck with the skull too small to make out, so at and below 32px
the frame is tightened to the skull -- the same icon, framed for the size,
which is what every platform icon set does. Above that the whole illustration
is used.

And anything a browser puts in a tab strip keeps its transparent ground, while
the iOS and Windows tiles get the app's own --bg: neither platform honours
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

#: At and below this, the frame is tightened to the skull.
SMALL = 32
#: How much of the frame the tightened crop keeps. Measured by rendering the
#: candidates side by side at 16, 20, 24 and 32px on light and dark: the whole
#: illustration is a speck, 68% fills the frame edge to edge and reads as a
#: busy rectangle, and this is where the eye patch survives to 20px.
CROP_KEEP = 0.58
#: Nudged up, because the bandana makes the artwork top-heavy.
CROP_RISE = 0.06
#: And inset inside its frame, so the small sizes keep a silhouette instead of
#: running artwork into all four corners. 0.88 rather than 0.92 because at
#: 16px the difference is a whole pixel, and at 0.92 the headband still
#: reached one corner.
SMALL_INSET = 0.88

TRANSPARENT = {
    "favicon-16x16.png": 16,
    "favicon-32x32.png": 32,
    "favicon-96x96.png": 96,
    "favicon-128.png": 128,
    "favicon-196x196.png": 196,
    "logo.png": 512,
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

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def source() -> Image.Image:
    """The artwork, as a centred square with a clear ground."""
    return Image.open(SOURCE).convert("RGBA")


def render(px: int) -> Image.Image:
    """The icon at one size, framed for that size.

    Args:
        px: Width and height in pixels.

    Returns:
        An RGBA image, transparent everywhere the artwork is not.
    """
    art = source()
    if px > SMALL:
        return art.resize((px, px), Image.LANCZOS)

    width = art.width
    keep = int(width * CROP_KEEP)
    left = (width - keep) // 2
    top = max(0, left - int(width * CROP_RISE))
    art = art.crop((left, top, left + keep, top + keep))

    inner = max(1, round(px * SMALL_INSET))
    frame = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    frame.alpha_composite(art.resize((inner, inner), Image.LANCZOS),
                          ((px - inner) // 2, (px - inner) // 2))
    return frame


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

    # One .ico holding every size a browser might reach for. Built per size
    # rather than by handing Pillow one image, so the small entries get the
    # tightened crop instead of the whole illustration squeezed into 16px.
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
