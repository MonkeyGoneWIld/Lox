"""The upload page's layout, pinned where it has silently regressed before.

Every check here is a thing that shipped wrong and had to be reported by
someone looking at the screen, because nothing in CI can see a screen. They are
asserted against the stylesheet and the script rather than a rendered page, so
they are narrow by construction: they cannot tell you the layout is good, only
that the specific mistakes below have not come back.

What they cover:

  * the question card fills its panel, and nothing inside it is capped again
  * spectrals sit against the left edge, in line with everything else
  * the Cancel button stays beside the run it cancels
  * what is already in the group is shown, not folded away
  * a hint never pushes its own input out of line with its neighbours'
  * a dry run offers to clear up after itself, one folder at a time or all
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(os.path.dirname(ROOT), "lox", "web", "static")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def read(*parts: str) -> str:
    """One of the shipped assets, as text."""
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as handle:
        return handle.read()


def rule(css: str, selector: str) -> str:
    """Every declaration written for one exact selector, joined.

    A selector can appear in more than one rule -- the layout of ``.step`` is
    set in one place and its colours in another -- so all of them count, or a
    cap reintroduced in the second rule would pass a check reading the first.

    Args:
        css: The stylesheet.
        selector: An exact selector, e.g. ``.spectrals``.

    Returns:
        The declarations, or an empty string if the selector is not styled.
    """
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    found = [
        " ".join(match[2].split())
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", stripped)
        if selector in [s.strip() for s in match[1].split(",")]
    ]
    return " ".join(found)


def main() -> int:
    css = read("css", "app.css")
    js = read("scripts", "app.js")

    # --- the card fills its panel --------------------------------------
    #
    # It was capped at 1100px inside a panel most of the screen wide, and then
    # the things inside it were capped again -- so the grey box stopped halfway
    # across the panel and the content stopped halfway across the grey box.
    check("the question card is not capped short of its panel",
          "max-width" not in rule(css, ".step"), rule(css, ".step"))
    check("neither is the row of matches", "max-width" not in rule(css, ".matches"), rule(css, ".matches"))
    check("nor the typed answer beside the buttons",
          "max-width" not in rule(css, ".step-text-answer"), rule(css, ".step-text-answer"))
    # The prompt is the exception, and the only one: a 1900px line of prose is
    # not readable, a 1900px row of buttons is fine.
    check("the prompt is still held to a reading width",
          "max-width" in rule(css, ".step-prompt"), rule(css, ".step-prompt"))
    check("and the detail line is free to use the width",
          "max-width: none" in rule(css, ".step > .hint"), rule(css, ".step > .hint"))

    # "Use this" came out stacked over two lines in a box narrower than the
    # word, because the button was a flex item free to shrink in a nowrap row.
    check("the Use this button cannot be shrunk to fit",
          "flex: none" in rule(css, ".step-text-answer button"), rule(css, ".step-text-answer button"))

    # --- spectrals on the left -----------------------------------------
    spectrals = rule(css, ".spectrals")
    check("spectrals sit against the left edge",
          "margin: 14px auto 0 0" in spectrals, spectrals)
    step_with = rule(css, ".step:has(.spectrals)")
    check("and the panel stops hugging them on the wrong side",
          "padding-left: 0" in step_with and "margin-left: auto" not in step_with, step_with)
    check("with a cap so the images stop upscaling", "max-width" in step_with, step_with)

    # --- Cancel beside the run it cancels ------------------------------
    #
    # h2 { flex: 1 } threw the state tag and the Cancel button to the far right
    # edge of the panel, a foot away from anything they referred to.
    head = rule(css, ".flow-card h2")
    check("the flow title does not push Cancel to the far edge",
          "flex: 1;" not in head and "flex: 1 " not in head, head)

    # --- evidence is shown, not folded away ----------------------------
    torrents = js[js.index("function torrentTable"):js.index("function stepTable")]
    check("what is already in the group is open however much there is",
          "open: true" in torrents and "rows.length <= 12" not in torrents, "")

    # --- a hint never breaks a row's alignment -------------------------
    #
    # A hint beside the label that wrapped pushed its own input a line below
    # its neighbours', which is why "Original release year" sat lower than
    # "Edition year" in the same row.
    form = js[js.index("function metadataForm"):js.index("function editorStep")]
    check("a narrow field's hint goes under its control",
          "meta-form-note" in form and "!isWide(section)" in form, "")
    check("only a full-width field puts its hint beside the label",
          "section.hint && isWide(section)" in form, "")
    check("and the label row is one line by construction",
          "meta-form-note" in css, "")

    # --- clearing up after a dry run -----------------------------------
    #
    # A rehearsal hardlinks a per-tracker folder and may transcode a
    # downconversion into it. Neither is ever used, and neither was ever
    # offered for deletion beyond the transcode.
    left = js[js.index("function leftovers"):]
    left = left[: left.index("\n  }\n")]
    check("a dry run lists the seeding folder it left behind",
          "Seeding folder" in left and "o.folder !== result.folder" in left, "")
    check("and the downconversion", "result.transcodes" in left and "Downconversion" in left, "")
    check("each with its own Delete", "'/api/folders/delete'" in left, "")
    check("and one button for all of them", "Delete all ${items.length}" in left, "")
    check("which asks before removing anything", left.count("confirm(") == 2, str(left.count("confirm(")))
    check("only a dry run offers the seeding folder, since a real one is using it",
          "if (result.dry_run)" in left, "")
    check("the result panel renders it", "leftovers(result)" in js, "")

    # --- the look is a decision, not a default -----------------------
    #
    # The palette this replaced was #a238ff violet on #16161d with system-ui
    # and 10px radii. That exact combination is the house style of nearly every
    # generated app, which is what made this one read as one. Each half of it
    # is pinned so it cannot drift back.
    import re as _re  # noqa: PLC0415

    tokens = rule(css, ":root")
    # Comments stripped first: the header names the palette it replaced, and
    # naming it is the point of the comment.
    live = _re.sub(r"/\*.*?\*/", "", css, flags=_re.S)
    check("the violet is gone", "#a238ff" not in live and "#7b2bc4" not in live, "")
    check("and the blue-black with it", "#16161d" not in live, "")
    check("one accent, and it is the amber",
          "--accent: #e8a33d" in tokens, tokens[tokens.find("--accent"):][:24])
    check("corners are squared, not pilled",
          "--radius: 3px" in tokens, tokens[tokens.find("--radius"):][:20])
    check("no pill radius survives anywhere", "border-radius: 99px" not in css, "")
    check("both faces are named as tokens",
          "--sans:" in tokens and "--mono:" in tokens, "")
    check("and nothing falls back to system-ui", "system-ui" not in live, "")

    # Every colour a state is drawn in has to exist in both themes, or the
    # light theme silently inherits a dark one and washes out.
    light = rule(css, ':root[data-theme="light"]')
    for token in ("--accent", "--ok", "--bad", "--text-faint", "--border-soft"):
        check(f"{token} is defined for the light theme too", token + ":" in light, "")

    # --- the fonts are ours ------------------------------------------
    #
    # A <link> to fonts.googleapis.com would announce every page load of a
    # private instance -- one holding tracker sessions and a Deezer ARL -- to a
    # third party, and would leave the UI wrong on a LAN with no route out.
    shell = read("..", "templates", "app.html")
    fonts = read("css", "fonts.css")
    check("no page asks a font host for anything",
          "fonts.googleapis.com" not in shell and "fonts.gstatic.com" not in shell
          and "gstatic" not in fonts, "")
    check("the faces are served from here",
          fonts.count("url('../fonts/") >= 12, str(fonts.count("url('../fonts/")))
    check("and every one of them is on disk",
          all(os.path.exists(os.path.join(STATIC, "fonts", name))
              for name in _re.findall(r"url\('\.\./fonts/([^']+)'\)", fonts)), "")
    check("a face is only fetched for text that needs it",
          fonts.count("unicode-range:") == fonts.count("@font-face"), "")
    check("and the stylesheet is versioned like the others",
          "fonts.css?v=" in shell, "")

    # --- the nav says what the app does ------------------------------
    #
    # Eight peers said nothing. Four of these are ways to find work and any can
    # start you off; three are stages one release passes through.
    check("the sidebar is grouped", shell.count('class="nav-group"') == 3,
          str(shell.count('class="nav-group"')))
    check("the pipeline is numbered because the order is real",
          all(f'class="nav-step">{n}<' in shell for n in (1, 2, 3)), "")
    check("its stages are named for what they are doing",
          all(w in shell for w in ("Queue", "Downloading", "Uploading")), "")
    check("and each carries a count", shell.count('class="nav-count"') == 3,
          str(shell.count('class="nav-count"')))
    check("a stage with nothing in it stays quiet",
          "n ? String(n) : ''" in js, "")

    # The one interruption worth colouring from anywhere in the app.
    check("a blocked run is visible from every screen",
          ".nav-item.needs-you" in css and "railNeedsYou" in js, "")
    check("counted off the cards, after their state is set",
          """$$('.flow-head[data-state="waiting"]').length""" in js, "")
    check("the tab name survives the number and the count beside it",
          "navLabel(view)" in js and ".nav-label" in js, "")

    # --- looking at a secret is not editing it ------------------------
    #
    # The input event is what records a change; setting .value in code does not
    # fire one, and re-masking has to put the box back exactly as it was, or a
    # look would cost a write on the next Save.
    reveal = js[js.index("function revealButton"):]
    reveal = reveal[: reveal.index("\n  }\n")]
    check("a secret is fetched only when asked for",
          "/api/settings/secret" in js, "")
    check("re-masking restores the field it found",
          "input.type = 'password'" in reveal and "input.value = ''" in reveal
          and "input.placeholder = placeholder" in reveal, "")
    check("and nothing in the reveal marks the form dirty",
          "markDirty" not in reveal and "state.pending" not in reveal, "")
    check("what is already typed is shown without a round trip",
          "if (input.value)" in reveal, "")
    check("the torrent client password gets one too",
          "/api/settings/seedboxes/secret" in js, "")

    # ICON() is declared near the bottom of the file. A const initialised above
    # it runs first and throws before the app mounts, which for a script with no
    # build step takes the whole UI down -- so these stay lazy.
    check("the eye icons are not evaluated before ICON exists",
          "const eyeIcon = () => ICON(" in js and "const ICON_EYE =" not in js, "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
