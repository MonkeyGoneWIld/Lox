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
  * no card paints the colour of a form field, which reads as a sunken well
  * a section heading is the control that filters to it, not a button beside it
  * the queue's filter narrows what the buttons act on, and says what it hid
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
    # Counted on the class rather than the exact attribute: two of the groups
    # carry a second class, and matching the literal string missed them.
    groups = _re.findall(r'class="nav-group[^"]*"', shell)
    check("the sidebar is grouped", len(groups) == 3, str(groups))

    # The bullets were never a decision: `list-style` on the parent does not
    # reach a <ul>, whose own UA rule wins over the inherited value, so turning
    # .nav from a <ul> into a <nav> put the browser's markers back on every
    # entry. Pinned because it is invisible in the markup and only shows on
    # screen.
    check("no list markers can come back", "list-style: none" in rule(css, ".nav ul"),
          rule(css, ".nav ul"))
    check("and the lists carry no indent of their own",
          "padding: 0" in rule(css, ".nav ul"), rule(css, ".nav ul"))

    # The pipeline is drawn as one thing, not three numbered rows.
    check("the stages sit on a line", "::before" in css and ".nav-pipeline ul" in css, "")
    check("the marker punches through it",
          "background: var(--bg-raised)" in rule(css, ".nav-step"), rule(css, ".nav-step"))
    check("every entry is iconed rather than bulleted",
          shell.count('class="nav-icon"') == 5, str(shell.count('class="nav-icon"')))
    check("and a blocked stage lights its marker, not only its label",
          "needs-you:not(.active) .nav-step" in css, "")
    check("a stage holding something is marked live",
          "has-work" in css and "has-work" in js, "")
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
    # --- one surface per card ----------------------------------------
    # --bg-input is the colour of a form field. Painted on a card it reads as
    # a well sunk into the panel, which is how the spectrals ended up in a
    # darker box than everything around them: the step was a well, and the
    # image painted a third colour inside it. A card either inherits the panel
    # it sits on or draws a border -- it never paints the field colour.
    for name in (".step", ".match", ".card-art", ".album-art", ".request-cover", ".dl-art"):
        body = rule(css, name)
        check(f"{name} does not paint the field colour",
              "var(--bg-input)" not in body, body.strip()[:70])
    check("the step is a bordered card, not a sunken one",
          "border: 1px solid var(--border)" in rule(css, ".step"), "")
    check("and the spectral image adds no surface of its own",
          "background:" not in rule(css, ".spectral-pair img"), "")
    check("a group inside the card is drawn with its border alone",
          "background: transparent" in rule(css, ".meta-group"), "")
    check("the sticky settings bar matches the panel it sits on",
          "var(--bg-raised)" in rule(css, ".settings-bar"), "")

    # --- the heading is the filter ------------------------------------
    # There was an "Only these" button beside each heading. The heading
    # already names the thing and carries its count; it is the control.
    Q = chr(39)
    flat = js.replace(chr(34), Q)
    check("no button repeats what the heading already says",
          "Only these" not in js, "")
    head = flat[flat.index("section-head") - 40:][:420] if "section-head" in flat else ""
    check("the heading filters to its own kind",
          all(w in head for w in ("role: " + Q + "button" + Q, "selectSearchType(kind)")), "")
    check("reachable from the keyboard",
          "tabindex: " + Q + "0" + Q in flat and "e.key === " + Q + "Enter" + Q in flat, "")
    check("with the count set apart from the name",
          all(Q + n + Q in flat for n in ("section-title", "section-count")), "")
    check("and it says what clicking will do", "Show only ${" in js, "")
    check("the arrow only appears under the pointer",
          ".section-head:hover .section-go" in css, "")

    # --- the queue filters what you can see, and says when it does ----
    # Two different things sit on this page and must not be confused: the
    # Settings rules decide what belongs in the queue and persist; this narrows
    # what is drawn and forgets itself. The dangerous overlap is the buttons --
    # "Download selected" acting on a row scrolled out of existence by a filter
    # would be indefensible, so the selection is scoped to the filtered rows.
    for control in ("found-search", "found-tracker", "found-source", "found-filter-clear"):
        check(f"the queue has a {control}", f'id="{control}"' in shell, "")
    # Every list is one component now: sortable headers, a filter in the
    # column it filters, and a selection derived from the rows rather than
    # from the checkboxes.
    check("there is one table, not one per list", "function dataTable" in js, "")
    check("its columns sort", "th-sort" in js and "view.dir = -view.dir" in js, "")
    check("each filter sits in the column it filters", "th-filter" in js, "")
    check("shift extends a selection from the last box clicked",
          "e.shiftKey && view.lastIndex !== null" in js, "")

    # Counting the checkboxes meant a row with no id added `undefined` to the
    # set, so "17 selected" was one more than the list held and clearing left
    # that one behind.
    check("counts come from the rows, never the checkboxes",
          "function countSelected" in js, "")
    check("the selection follows what the table is showing, filters included",
          "tableView('queue').shown" in js, "")
    check("and the buttons act on that same list",
          "const foundSelection = () =>" in js and "tableView('queue').shown" in js, "")

    # A rule that hides rows without saying so is indistinguishable from a
    # scan that found nothing, which is how this page loses someone's trust.
    check("held-back rows are counted on the page", 'id="found-held"' in shell, "")
    check("with a way to look at them", 'id="found-held-toggle"' in shell, "")
    check("each carrying the reason it was held", "held_reason" in js, "")
    check("and the rule itself said in words", "state.foundRule" in js, "")
    check("the filter is not persisted, because it is not a setting",
          "foundFilter: { text: '', tracker: '', source: '' }" in js.replace('"', "'"), "")

    # --- the search results are a list you can work with --------------
    # Taking twenty of thirty covers was twenty clicks, and there was no way to
    # take all of them at all.
    check("there is a select-all", "function selectAllVisible" in js, "")
    # It used to be a strip above every grid, on screen whether or not you were
    # selecting anything. It lives in the bar that only exists while a batch
    # does, so the controls appear with the thing they control.
    check("and it lives in the pick bar, not a strip of its own",
          "grid-tools-host" not in js and "function selectAllBar" not in js, "")
    check("with no count on the buttons", "Select all ${" not in js, "")
    check("shift-click takes the run between two ticks",
          "e.shiftKey" in js and "function pickClicked" in js, "")
    check("which needs the click event, not change -- change carries no shift key",
          "onchange: (e) => togglePick" not in js, "")
    check("the range is ordered by what is on screen",
          "const pickableCards" in js and "data-album" in js, "")
    check("and a bulk pick redraws the bar once, not once per card",
          "let bulkPicking" in js and "function inBulk" in js, "")

    # A filtered search dropped the section headings, and their margin was the
    # only thing holding the covers off the search bar.
    check("the search bar keeps its distance from the results",
          "margin-bottom: 18px" in rule(css, ".searchbar"), rule(css, ".searchbar").strip()[:70])

    # "Check trackers" moved you to another tab, pasted some URLs, and left the
    # button you had already pressed waiting under a different name.
    bulk = js[js.index("async function bulkCheck"):]
    end_marker = chr(10) + "  }" + chr(10)
    bulk = bulk[: bulk.index(end_marker)]
    check("pressing Check trackers checks the trackers", "await missingScan()" in bulk, "")
    check("on what you picked, not on whatever was left in the box",
          "box.value = urls.join" in bulk, "")

    # --- a pasted request says which tracker it is on -----------------
    check("a request URL is read for its tracker", "function trackerFromUrl" in js, "")
    check("all three of them", all(t in js for t in ("redacted", "orpheus", "dicmusic")), "")
    check("and the check is grouped by tracker, not by the toggle",
          "groups.set(code" in js and "request_ids: ids" in js, "")
    check("a pasted row is filled in from what the check found",
          "function fillPastedRequestRow" in js, "")

    # --- the pick control is a ring, not a form checkbox --------------
    # The browser's own checkbox is a filled white square with a blue tick. On
    # top of album art it reads as a piece of chrome that landed there.
    pick = rule(css, ".card-pick input")
    check("the pick control is drawn by us, not the browser",
          "appearance: none" in pick, pick.strip()[:60])
    check("and it is a circle", "border-radius: 50%" in pick, "")
    check("with the art showing through the middle",
          "background: transparent" in pick, "")
    check("and its own contrast, because the art behind it can be any colour",
          "box-shadow" in pick, "")

    checked = rule(css, ".card-pick input:checked")
    check("taken, it fills with the one signal colour",
          "background-color: var(--accent)" in checked, checked.strip()[:70])
    # The trap this hit: `background: var(--accent)` is a shorthand carrying a
    # var(), which cannot be resolved at parse time. The background-image in
    # the same rule then collapses the rest of it, and the circle renders with
    # its tick and no fill at all.
    check("set as a longhand, or the background-image below it wipes the fill",
          "background: var(--accent)" not in checked, "")
    check("and carries a tick", "background-image" in checked, "")

    # --- a click joins the batch once there is one --------------------
    check("what a click means depends on whether a batch is open",
          "const selecting = () => state.picked.size > 0" in js, "")
    check("a card click selects while one is", "if (isAlbum && albumId && selecting())" in js, "")
    check("and shift still takes the run from the card body",
          "pickClicked(id, item, !state.picked.has(id), e.shiftKey)" in js, "")
    check("with nothing picked it still opens the release",
          "else if (albumId) openAlbum(albumId);" in js, "")

    # --- the artist page is a discography, so it gets a select-all ----
    check("the artist page needs no select-all of its own either",
          "grid-tools-host" not in js, "")

    # --- the circle is the state, not a second opinion about it -------
    # Picking from the card body left the circle empty on a picked card,
    # because only the checkbox path set it. Pressing that circle then argued
    # with the state behind it.
    toggle = js[js.index("function togglePick("):]
    toggle = toggle[: toggle.index(chr(10) + "  }" + chr(10))]
    check("one function sets the set, the outline and the circle together",
          "state.picked" in toggle and "classList.toggle('picked'" in toggle
          and "box.checked = on" in toggle, "")
    check("and it finds the cards itself rather than being handed one",
          "document.querySelectorAll(`.card[data-album=" in toggle, "")
    check("ids are strings on both sides of the set",
          "state.picked.has(String(albumId))" in js, "")

    # --- what a batch changes about the whole page --------------------
    check("the page says when a batch is open",
          "classList.toggle('picking', count > 0)" in js, "")
    check("and the per-card download and upload buttons go while it is",
          "display: none" in rule(css, "body.picking .card-actions"), "")
    check("guarded in the handler too, not only in the stylesheet",
          js.count("if (selecting()) return;") >= 2, str(js.count("if (selecting()) return;")))

    # --- a batch belongs to the list it came from ---------------------
    tabs = js[js.index("$$('#explore-tabs button')"):]
    tabs = tabs[: tabs.index("loadExplore();")]
    check("changing a Browse tab drops the batch", "clearPicks();" in tabs, "")

    # --- select-all wherever releases can be picked -------------------
    explore = js[js.index("async function loadExplore"):]
    explore = explore[: explore.index(chr(10) + "  }" + chr(10))]
    check("Browse needs no select-all of its own now", "grid-tools-host" not in explore, "")

    # The tip was noise: shift-click is worth knowing once, not on every grid.
    check("no shift-click tip rides along with the button",
          "Shift-click to take a run" not in js, "")

    # --- the bar carries the whole batch, in two halves ---------------
    bar = js[js.index("function renderPickBar()"):]
    bar = bar[: bar.index(chr(10) + "  }" + chr(10))]
    for label in ("Download", "Download & upload", "Check trackers", "Select all", "Clear all"):
        check(f"the bar has {label!r}", f"'{label}'" in bar, "")
    check("acting on the batch and changing it sit at opposite ends",
          "bar-gap" in bar and "flex: 1 1 auto" in rule(css, ".bar-gap"), "")
    check("select all goes once everything on screen is taken",
          "onScreen && !allTaken" in bar, "")
    check("and the bar is emptied when it closes, not just hidden",
          "bar.replaceChildren();" in bar, "")

    # --- a shift-click is a range, not a text drag --------------------
    check("cards do not take a text selection",
          "user-select: none" in rule(css, ".card"), rule(css, ".card").strip()[:70])

    # --- every card showing a release agrees about it -----------------
    check("all copies of a release are marked, not the first one found",
          "document.querySelectorAll(`.card[data-album=" in js, "")

    # --- leaving the list drops the batch -----------------------------
    view = js[js.index("function setView(view)"):]
    view = view[: view.index(chr(10) + "  }" + chr(10))]
    check("changing view drops the batch", "clearPicks();" in view, "")
    check("only when the view actually changes", "state.view !== view" in view, "")
    check("changing the search type drops it",
          "clearPicks();" in js[js.index("function selectSearchType"):][:200], "")
    check("and so does a genre filter",
          "clearPicks();" in js[js.index("state.exploreGenre = g.id;"):][:120], "")

    # --- the request form is the tracker's form -----------------------
    # It was four columns of scrolling boxes, which turned fifteen release
    # types into a 132px list you had to scroll to reach "Unknown".
    check("the search form is rows, not scrolling columns",
          "function formRow" in js and "max-height: 132px" not in css, "")
    check("with a label beside its controls, not above them",
          "grid-template-columns:" in rule(css, ".reqrow")
          and "1fr" in rule(css, ".reqrow"), rule(css, ".reqrow").strip()[:60])

    # A text box was stretching the width of the panel because the global
    # "inputs fill their field" rule outscores a plain `.reqfield
    # input[type=search]` -- four :not() attribute selectors against one class
    # and one attribute. The narrow rule has to carry the same weight or it
    # loses however far down the file it sits.
    widths = rule(css, '.reqfield input:not([type="checkbox"]):not([type="radio"])'
                       ':not([type="button"]):not([type="submit"])')
    check("a search box is as wide as what you type in it, not as wide as the page",
          "width: 420px" in widths, widths.strip()[:60])
    check("and a number box is narrower still",
          "width: 110px" in rule(css, '.reqfield input.reqsmall:not([type="checkbox"])'
                                      ':not([type="radio"]):not([type="button"])'
                                      ':not([type="submit"])'), "")

    # Every group ran into the next one, so fifteen release types and six
    # formats read as one undifferentiated field of ticks. Whitespace alone was
    # not enough to say where one setting ended and the next began, so each row
    # is a band with a rule under it.
    reqrow = rule(css, ".reqrow")
    check("each setting is separated from the next by more than air",
          "border-bottom" in reqrow and "padding" in reqrow, reqrow.strip()[:70])
    check("except the last, which would be a border around nothing",
          "border-bottom: 0" in rule(css, ".reqrow:last-child"), "")
    check("a group of ticks gets more room than a one-line row",
          "padding: 16px 0" in rule(css, ".reqrow:has(.reqgroup)"), "")
    check("and a group's All is set off from the ticks it governs",
          "border-bottom" in rule(css, ".reqgroup-head"), "")

    # --- the form opens on a real search, not on every box ticked ---------
    check("the page ticks what the tracker says to tick",
          "item.checked || []" in js, "")
    check("rather than everything", "checked: item.default" not in js, "")
    check("and a group's All reflects whether that is all of them",
          "options.every((name) => on.has(name))" in js, "")

    # --- a search can be watched and stopped ------------------------------
    # It used to be one request for every page at once: ask for forty and the
    # only options were to wait for forty tracker calls or reload the page,
    # having paid for them either way.
    check("pages are fetched one at a time", "start_page" in js, "")
    check("with a bar that moves as they land", "function requestsProgress" in js, "")
    check("and a Cancel that stops before the next page is paid for",
          "requestsAbort" in js and "abort()" in js, "")
    check("the Cancel button exists in the page", 'id="requests-cancel"' in shell, "")
    check("and the bar with it", 'id="requests-progress"' in shell, "")

    # --- the buttons say what they do -------------------------------------
    # "Search requests" and "Search and check" meant nothing from outside the
    # code: one read the tracker, the other looked each result up on Deezer,
    # and neither name said so. The one that does the whole job is the default,
    # and neither carries a note explaining it -- a button that needs one is
    # named wrong.
    check("the default button does the whole job",
          'class="primary" id="requests-fetch-check"' in shell, "")
    check("named for what the user came to do",
          "Fetch with Deezer Lookup" in shell, "")
    check("with the list-only one beside it", "Fetch Requests" in shell, "")
    check("and the old jargon gone",
          "Search requests" not in shell and "Search and check" not in shell, "")
    check("the box that decided WHETHER to look things up is gone",
          'id="requests-autocheck"' not in shell, "")

    # It was indistinguishable from the button beside it, and ticking it turned
    # "show me the list" into a run that spent budget on every row. It decides
    # when the lookups happen now, never whether.
    check("and is replaced by one that decides when",
          'id="requests-pipeline"' in shell, "")
    check("named for what it actually does",
          "Look up on Deezer as requests arrive" in shell, "")
    check("a list-only run never looks anything up, ticked or not",
          "thenCheck && ticked('requests-pipeline')" in js, "")
    check("each page's requests go off as that page lands",
          "lookUpLater(fresh)" in js, "")
    check("chained rather than parallel, so two jobs cannot race the budget",
          "chain = chain" in js, "")
    check("and Cancel stops the lookup it started, not just the pages",
          "state.checkCancelButton?.click()" in js, "")

    # --- how much of the search this is -----------------------------------
    # It was a toast: the one number that decides whether to read more pages,
    # shown for four seconds and then taken away.
    check("the coverage line is part of the page, not a notification",
          'id="requests-summary"' in shell, "")
    check("and stays until the next search replaces it",
          "function requestsSummary" in js, "")
    check("a partial read is marked as one",
          "border-left" in rule(css, ".requests-summary.partial"),
          rule(css, ".requests-summary.partial").strip()[:60])
    check("with more pages one click away", "Read more pages" in js, "")

    # It outlived the results it described: paste ten ids and the line still
    # said "showing 25 of about 42,925", about a list no longer on screen.
    check("and it goes when the results stop being a page search",
          "requestsSummary({ shown: null });" in js
          and "did not come from a page search" in js, "")
    check("or when a different tracker is picked",
          js.count("requestsSummary({ shown: null })") >= 4, str(js.count("requestsSummary({ shown: null })")))
    check("and so is the note under the file picker",
          "starts checking as soon as you pick it" not in shell, "")

    # --- already checked --------------------------------------------------
    # Answers were being stored -- they are what stops a second run paying for
    # the same lookups -- but nothing showed them.
    check("Requests has a tab for what has already been checked",
          'data-reqtab="history"' in shell, "")
    check("with its own filters", "function renderHistoryFilters" in js, "")
    check("a way to run them again", "function historyRerun" in js, "")
    check("and the re-run asks for a real re-run rather than being skipped",
          "recheck: true" in js, "")
    check("what a run skipped is shown rather than silently dropped",
          "function showSkipped" in js, "")
    check("with the offer to do them anyway", "Check them anyway" in js, "")

    # The window is a setting, offered where it is used.
    check("how long an answer is trusted is set on the page that uses it",
          "requests-recheck" in js, "")

    # Durations were a dropdown of seven guesses -- a day, a week, a month,
    # three months, a year -- which is fine until someone wants two months or
    # three years, and then there is nothing to pick.
    check("a duration is a number and a unit",
          "function durationControl" in js, "")
    check("with every unit anyone would reach for",
          "['days', 1], ['weeks', 7], ['months', 30], ['years', 365]" in js, "")
    check("stored as days whatever was typed", "function partsToDays" in js, "")
    check("and read back as the largest unit that fits, so 30 is one month",
          "function daysToParts" in js, "")
    check("the recheck window can also be turned off entirely",
          "never: true" in js, "")
    check("and the unit agrees with the number", "function pluralise" in js, "")

    # The same control on the history filter, which had the same seven guesses.
    check("the history filter takes a typed duration too",
          "history-age-dir" in js and "history-age-amount" in js, "")
    check("in either direction", "'in the last'" in js and "'not for'" in js, "")
    check("hidden until a direction is chosen", "function syncHistoryAge" in js, "")
    check("and the old fixed list is gone",
          "within:30" not in js and "before:90" not in js, "")

    # Two different ages, and the table only ever showed one. A request open
    # for two years and one posted yesterday are not the same proposition.
    check("the history says when the request was opened",
          "'OPENED'" in js and "created_age" in js, "")
    check("as well as when it was last looked up",
          "'LAST LOOKUP'" in js, "")
    check("each with the date behind the relative time",
          "function checkedOn" in js, "")
    check("and the date is not dropped when a row is due again",
          "].filter(Boolean).join" in js and "checkedOn(row.checked_at), stale" in js, "")

    # --- what the second tab is called ------------------------------------
    check("the lookup history has a name that says what it is",
          "Lookup History" in shell, "")
    check("rather than an adjective", "Already checked" not in shell, "")

    # --- the running-cost commentary is gone ------------------------------
    check("the standing 'checking costs one more call' line is gone",
          "costs one more call on top" not in shell, "")
    check("and the cost line only speaks when the budget is short",
          "Costs up to" not in js, "")

    check("every group of ticks can have its own All",
          "function checkGroup" in js and "'All'" in js, "")
    check("but a group can be rendered without one",
          "withAll" in js, "")
    check("and a group can start ticked or clear",
          "checked = true" in js or "checked," in js, "")

    # The page used to decide the order, the labels and the defaults itself,
    # and got all three wrong for one tracker or the other. The tracker
    # describes its own form now.
    check("the page renders the form the tracker describes",
          "spec.form" in js, "")
    check("rather than a fixed list of its own",
          "if (spec.release_types.length)" not in js, "")

    # This sat under the tags box on both trackers and explained the site's
    # own syntax to someone already looking at the site's own form.
    check("the tags box does not lecture about punctuation",
          "dots, not spaces" not in js, "")
    check("and the All follows the ticks under it, rather than only leading them",
          "function syncAll" in js, "")
    check("categories are offered at all", "requests-category" in js, "")
    check("and sent with the search", "['category', 'requests-category']" in js, "")
    flat_js = js.replace(chr(34), "'")
    check("tag match is two radios, as on the form",
          "type: " + "'" + "radio" in flat_js
          and "name: " + "'" + "requests-tags-mode" in flat_js, "")
    check("read back from whichever is picked",
          "requests-tags-mode" + "'" + "]:checked" in flat_js, "")
    check("with the old wording gone", "Fetch open requests" not in shell, "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(main())
