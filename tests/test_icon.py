"""The icon, which is the one asset a user looks at without opening the app.

The mark this replaced failed in three ways at once, and each one is checked
here because each one shipped: it was painted on an opaque pale-blue square, so
it showed as a tile in a dark tab strip; it was a Flaticon drawing credited to
somebody else in the page footer; and nothing versioned it, so a browser that
had seen it once kept showing it.

These read the built assets rather than rebuilding them, so they fail if
someone changes design/icon/marks.py and forgets to run build.py.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
IMAGES = os.path.join(REPO, "lox", "web", "static", "images")
TEMPLATES = os.path.join(REPO, "lox", "web", "templates")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main() -> int:
    from PIL import Image

    # --- the tab art carries no ground ------------------------------------
    # A favicon sits on whatever colour the browser is. Anything opaque here is
    # the bug that was reported.
    tab_art = [
        "favicon-16x16.png",
        "favicon-32x32.png",
        "favicon-96x96.png",
        "favicon-128.png",
        "favicon-196x196.png",
        "logo.png",
        "favicon.ico",
    ]
    for name in tab_art:
        path = os.path.join(IMAGES, name)
        if not os.path.exists(path):
            check(f"{name} is built", False, "missing -- run design/icon/build.py")
            continue
        image = Image.open(path).convert("RGBA")
        corners = [
            image.getpixel((0, 0)),
            image.getpixel((image.width - 1, 0)),
            image.getpixel((0, image.height - 1)),
            image.getpixel((image.width - 1, image.height - 1)),
        ]
        clear = all(pixel[3] == 0 for pixel in corners)
        check(f"{name} has no tile behind it", clear, str(corners[0]))

        # histogram() rather than walking pixels: it is the one spelling that
        # has meant the same thing across Pillow versions.
        alpha = image.getchannel("A").histogram()
        share = sum(alpha[9:]) / (image.width * image.height)
        # The upper bound is the point of this: a disc inscribed in a square is
        # pi/4, about 79%, and a solid tile is 100%. Anything approaching the
        # latter means the ground came back.
        check(f"{name} is a mark and not a filled tile", 0.05 < share < 0.90, f"{share:.0%} inked")

    # A tab reaches for 16 or 32; the rest are for everything else.
    ico = Image.open(os.path.join(IMAGES, "favicon.ico"))
    sizes = {w for w, _ in ico.ico.sizes()}
    check("the .ico carries the sizes a tab asks for", {16, 32} <= sizes, str(sorted(sizes)))

    # --- the home-screen tiles do carry one, deliberately -----------------
    # iOS and Windows do not honour transparency there and would pick a ground
    # for us, so the app's own --bg is baked in.
    tile = Image.open(os.path.join(IMAGES, "apple-touch-icon-152x152.png")).convert("RGBA")
    check("the ios tile is on the app's own ground",
          tile.getpixel((0, 0)) == (23, 21, 15, 255), str(tile.getpixel((0, 0))))

    # --- the scalable one, which is what a modern browser prefers ---------
    svg = read(os.path.join(IMAGES, "icon.svg"))
    check("an svg icon is built", svg.startswith("<svg"), svg[:40])
    check("and it paints no background", "<rect" not in svg and "background" not in svg, "")
    check("it is self-contained, with nothing to fetch",
          "http" not in svg.replace("http://www.w3.org/2000/svg", ""), "")
    check("and draws the disc the mark is built on", "<circle" in svg, "")
    check("in the icon's own palette", "#fb3b8f" in svg.lower(), "")

    # --- the page asks for it, and asks for a fresh copy ------------------
    app_html = read(os.path.join(TEMPLATES, "app.html"))
    check("the page links the svg icon", 'type="image/svg+xml"' in app_html, "")
    check("with the .ico behind it for the rest", "favicon.ico" in app_html, "")
    for asset in ("icon.svg", "favicon.ico"):
        check(f"{asset} is versioned, because a favicon caches hard",
              f"{asset}?v=" in app_html, "")

    # ... and the fingerprint has to actually move when the icon does, or the
    # version on those URLs is decoration.
    web_init = read(os.path.join(REPO, "lox", "web", "__init__.py"))
    start = web_init.index("def _asset_version")
    body = web_init[start:start + 900]
    for asset in ("images/icon.svg", "images/favicon.ico"):
        check(f"the asset fingerprint covers {asset}", asset in body, "")

    # --- nobody else's mark, nobody else's credit -------------------------
    layout = read(os.path.join(TEMPLATES, "layout.html"))
    for word in ("Flaticon", "freepik", "Freepik", "creativecommons"):
        check(f"no {word} credit survives the mark it credited",
              word not in layout and word not in app_html, "")
    check("the windows tile colour is the app's, not white",
          "#FFFFFF" not in layout, "")

    # --- the mark is reproducible ----------------------------------------
    # Drawn from geometry in the repo rather than an image dropped in, so it
    # rebuilds at any size and carries nobody else's licence.
    for name in ("artwork.py", "build.py"):
        check(f"design/icon/{name} is in the repo",
              os.path.exists(os.path.join(REPO, "design", "icon", name)), "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
