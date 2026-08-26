"""Regenerate every icon asset the app serves, from design/icon/marks.py.

    python design/icon/build.py

Run it after changing the mark. Two grounds are used deliberately: anything a
browser puts in a tab strip is transparent, so it sits on whatever colour the
browser is; the iOS and Windows tiles get the app's own --bg, because neither
platform honours transparency there and both would otherwise composite the
mark onto black or white and pick the wrong one.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from marks import AMBER, render, svg  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "lox", "web", "static", "images")

INK = "#17150f"  # --bg, for the platforms that will not take a clear ground

#: Everything served with a clear ground: tab strips, and the .ico inside them.
TRANSPARENT = {
    "favicon-16x16.png": 16,
    "favicon-32x32.png": 32,
    "favicon-96x96.png": 96,
    "favicon-128.png": 128,
    "favicon-196x196.png": 196,
    "logo.png": 512,
}

#: Home-screen and tile art, on the app's own ground.
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


def padded(px: int, ratio: float = 0.80):
    """The mark inset on a solid ground, the way a home-screen icon wants it."""
    from PIL import Image  # noqa: PLC0415

    plate = Image.new("RGBA", (px, px), INK)
    inner = max(1, round(px * ratio))
    plate.alpha_composite(render(inner, AMBER), ((px - inner) // 2, (px - inner) // 2))
    return plate


def main() -> int:
    from PIL import Image  # noqa: PLC0415

    os.makedirs(OUT, exist_ok=True)
    written = []

    with open(os.path.join(OUT, "icon.svg"), "w", encoding="utf-8") as handle:
        handle.write(svg())
    written.append("icon.svg")

    for name, px in TRANSPARENT.items():
        render(px, AMBER).save(os.path.join(OUT, name))
        written.append(name)

    for name, px in ON_INK.items():
        padded(px).save(os.path.join(OUT, name))
        written.append(name)

    # The wide tile is the only asset that is not a square.
    wide = Image.new("RGBA", (310, 150), INK)
    wide.alpha_composite(render(112, AMBER), (99, 19))
    wide.save(os.path.join(OUT, "mstile-310x150.png"))
    written.append("mstile-310x150.png")

    # One .ico holding every size a browser might reach for, all transparent.
    largest = render(256, AMBER)
    largest.save(
        os.path.join(OUT, "favicon.ico"),
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
    )
    written.append("favicon.ico")

    print(f"wrote {len(written)} assets to {os.path.relpath(OUT, ROOT)}")
    for name in written:
        print("  " + name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
