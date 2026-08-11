"""Check the CLI-prompt to UI-control bridge against real pipeline output."""

import asyncio
import contextlib
import sys
import types

from lox.flow import Flow
from lox.upload_flow import FlowPrompts, default_letter, parse_extra_options, parse_options, strip_ansi

results: list[tuple[str, bool, str]] = []


@contextlib.contextmanager
def stub_spectrals():
    """Make lox.uploader.spectrals importable without the audio stack.

    FlowPrompts swaps the spectral viewer out, so entering it needs that module
    to exist -- and it needs numpy and av, which are not installed on every
    Python this test has to run on. Where the real one imports, it is used.
    """
    try:
        import lox.uploader.spectrals  # noqa: F401

        yield
        return
    except ImportError:
        pass

    parent = sys.modules.get("lox.uploader") or types.ModuleType("lox.uploader")
    module = types.ModuleType("lox.uploader.spectrals")
    module.view_spectrals = lambda *_a, **_k: None
    parent.spectrals = module
    saved = {name: sys.modules.get(name) for name in ("lox.uploader", "lox.uploader.spectrals")}
    sys.modules["lox.uploader"] = parent
    sys.modules["lox.uploader.spectrals"] = module
    try:
        yield
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


async def wait_for_step(flow, timeout: float = 2.0):
    """Block until a question appears."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if flow.step is not None:
            return flow.step
        await asyncio.sleep(0.01)
    raise AssertionError("no step appeared")


async def main() -> int:
    """Run the checks."""
    # --- bracket prompts become buttons -------------------------------
    lossy = strip_ansi(
        "\x1b[35m\nIs this release lossy mastered? "
        "[y]es, [N]o, [r]eopen spectrals, [a]bort, [d]elete music folder\x1b[0m"
    )
    options = parse_options(lossy)
    # Four, not five: "reopen spectrals" reran the terminal viewer, and the
    # spectrals are already on the page.
    check("lossy prompt yields the four answers that do something",
          [o["value"] for o in options] == ["y", "n", "a", "d"], str([o["value"] for o in options]))
    check("destructive answers marked", [o["value"] for o in options if o["danger"]] == ["a", "d"])
    check("capitalised initial is the default", default_letter(lossy) == "n")

    # --- duplicate groups printed before a prompt become options ------
    flow = Flow("upload", "test")
    prompts = FlowPrompts(flow)

    async def driver():
        # Exactly what the dupe checker emits, including the split write.
        prompts._echo("Results matching this release were found on OPS:")
        prompts._echo(" 01 >> 1605624 | ", nl=False)
        prompts._echo("Charles Ryan - Jiggy Buckaroo (2025) [Album] [Tags: rap] | "
                      "https://orpheus.network/torrents.php?id=1605624")
        return await prompts._prompt(
            "Would you like to upload to an existing group? Paste a URL, pick from groups found or "
            "[N]ew group / [a]bort / [d]elete music folder"
        )

    task = asyncio.create_task(driver())
    step = await wait_for_step(flow)

    values = [o["value"] for o in step.options]
    check("found group offered as an option", any("1605624" in str(v) for v in values), str(values[:1]))
    check("group option is the url", values[0].startswith("https://orpheus.network"), values[0])
    check("group label is readable",
          "Jiggy Buckaroo" in step.options[0]["label"], step.options[0]["label"])
    check("bracket options still present", {"n", "a", "d"} <= set(values[1:]), str(values[1:]))
    check("split writes joined into one note",
          any("1605624" in e["message"] and "orpheus" in e["message"] for e in flow.events))

    flow.answer(step.id, values[0])
    answer = await asyncio.wait_for(task, timeout=2)
    check("chosen url returned to the pipeline", answer == values[0])

    # --- groups do not leak into the next, unrelated question ---------
    flow2 = Flow("upload", "test2")
    prompts2 = FlowPrompts(flow2)
    task2 = asyncio.create_task(prompts2._prompt("Any comment for the report?"))
    step2 = await wait_for_step(flow2)
    check("plain prompt stays a text field", step2.kind == "text", step2.kind)
    flow2.answer(step2.id, "none")
    await asyncio.wait_for(task2, timeout=2)

    # --- confirm maps to a yes/no ------------------------------------
    flow3 = Flow("upload", "test3")
    prompts3 = FlowPrompts(flow3)
    # ensure_future, not create_task: a confirm is an awaitable object rather
    # than a coroutine, so that it also has a truth value for the call sites
    # upstream never awaits.
    task3 = asyncio.ensure_future(prompts3._confirm("Would you like to rename the files?", default=True))
    step3 = await wait_for_step(flow3)
    check("confirm becomes a confirm step", step3.kind == "confirm" and step3.default is True)
    flow3.answer(step3.id, False)
    check("confirm returns the answer", await asyncio.wait_for(task3, timeout=2) is False)

    # --- prose options: spectral host retry and the next-tracker offer ---
    hosts = parse_extra_options(
        "Some spectrals failed to upload. Which image host would you like to retry with? "
        "(Options: ptpimg, catbox, ptscreens, oeimg, imgbb, imgbox)"
    )
    check("host retry becomes buttons", [o["value"] for o in hosts] ==
          ["ptpimg", "catbox", "ptscreens", "oeimg", "imgbb", "imgbox"], str(len(hosts)))

    tracker = parse_extra_options("Would you like to upload to another tracker? Your choices are OPS or [n]one.")
    check("next-tracker offer becomes buttons", [o["value"] for o in tracker] == ["OPS"], str(tracker))

    # --- metadata results become buttons ------------------------------
    flow4 = Flow("upload", "meta")
    p4 = FlowPrompts(flow4)

    async def meta():
        p4._echo("Results for Deezer:")
        p4._echo("> 01 Ryan Charles - Jiggy Buckaroo {Tracks: 14} | https://www.deezer.com/album/823528971")
        return await p4._prompt(
            'Which metadata results would you like to use? Other options: paste URLs, [m]anual, [a], '
            'prefix choice or URL with "*" to indicate source (WEB)'
        )

    task4 = asyncio.create_task(meta())
    step4 = await wait_for_step(flow4)
    check("metadata result offered", step4.options[0]["value"] == "1", str(step4.options[0]))
    check("metadata label readable", "Jiggy Buckaroo" in step4.options[0]["label"])
    flow4.answer(step4.id, "1")
    check("index returned to the pipeline", await asyncio.wait_for(task4, timeout=2) == "1")

    # --- "press enter" becomes a Continue button ----------------------
    flow5 = Flow("upload", "enter")
    p5 = FlowPrompts(flow5)

    async def press():
        p5._echo("Spectrals are available at http://192.168.1.25:5015/spectrals")
        return await p5._prompt("Press enter once you are finished viewing to continue the uploading process")

    task5 = asyncio.create_task(press())
    step5 = await wait_for_step(flow5)
    check("press-enter is a single Continue button",
          step5.kind == "choice" and [o["label"] for o in step5.options] == ["Continue"], str(step5.options))
    flow5.answer(step5.id, "")
    await asyncio.wait_for(task5, timeout=2)

    # --- downconversion is one decision, so one click -----------------
    # It used to render as checkboxes, with the prompt's own "none" and "all"
    # added as two more, every one pre-ticked -- combinations that contradict
    # each other and that nobody could type at the real prompt.
    flow6 = Flow("upload", "downconv")
    p6 = FlowPrompts(flow6)

    async def downconv():
        p6._echo("  1. MP3 320")
        p6._echo("  2. MP3 V0")
        return await p6._prompt(
            'Select formats to convert (space-separated list of IDs, "0" for none, "*" for all)', default="*"
        )

    task6 = asyncio.create_task(downconv())
    step6 = await wait_for_step(flow6)
    check("downconversion offers one button per answer",
          step6.kind == "choice"
          and [o["label"] for o in step6.options] == ["MP3 320", "MP3 V0", "Every format", "Do not convert"],
          str([o["label"] for o in step6.options]))
    check("not converting is the default", step6.default == "0", str(step6.default))
    flow6.answer(step6.id, "1")
    check("the chosen format is returned", await asyncio.wait_for(task6, timeout=2) == "1")

    # --- picking spectrals is three buttons, not a typed list ----------
    flow9 = Flow("upload", "specids")
    p9 = FlowPrompts(flow9)
    task9 = asyncio.create_task(p9._prompt(
        'What spectral IDs would you like to upload to ptpimg? '
        '(space-separated list of IDs, "0" for none, "*" for all, or "+" for a randomized selection)'
    ))
    step9 = await wait_for_step(flow9)
    check("spectral IDs offer all, some, none",
          [o["value"] for o in step9.options] == ["*", "+", "0"], str([o["value"] for o in step9.options]))
    flow9.answer(step9.id, "*")
    check("the spectral choice is returned", await asyncio.wait_for(task9, timeout=2) == "*")

    # --- option labels say what the button does ------------------------
    labels = {o["value"]: o["label"] for o in parse_options(
        "Are there any metadata fields you would like to edit? [t]itle, [r]elease type, [n]othing")}
    check("a multi-word option keeps the whole label", labels.get("r") == "Release type", str(labels))
    bare = {o["value"]: o["label"] for o in parse_options("Other options: paste URLs, [m]anual, [a], prefix")}
    check("a bare letter is named, not left as a letter", bare.get("a") == "Abort", str(bare))

    # --- tag diff becomes a table -------------------------------------
    flow7 = Flow("upload", "retag")
    p7 = FlowPrompts(flow7)

    async def retag():
        p7._echo("Proposed tag changes:")
        p7._echo("> 01. Prairie Rose.flac")
        p7._echo("  tracknumber          ••• 1/14 >>> 1")
        p7._echo("  tracktotal           ••• None >>> 14")
        p7._echo("> 04. Stay Twangin'.flac")
        p7._echo("  artist               ••• Ryan Charles >>> Ryan Charles (feat. Ian Munsick)")
        p7._echo("")
        p7._echo("Album tags (applied to all):")
        p7._echo("> album         ••• Jiggy Buckaroo")
        p7._echo("> date          ••• 2025-12-05 >>> 2025")
        p7._echo("")
        return await p7._confirm("Would you like to auto-tag the files with the updated metadata?", default=True)

    task7 = asyncio.create_task(retag())
    step7 = await wait_for_step(flow7)
    tables = {tb["kind"]: tb for tb in step7.tables}
    check("tag diff captured as a table", "tags" in tables, str(list(tables)))
    rows = tables.get("tags", {}).get("rows", [])
    check("diff rows grouped by file",
          rows[0]["group"] == "01. Prairie Rose.flac" and rows[-1]["group"] == "04. Stay Twangin'.flac",
          f"{len(rows)} rows")
    check("before and after split out",
          rows[0]["before"] == "1/14" and rows[0]["after"] == "1" and rows[0]["changed"], str(rows[0]))
    album = tables.get("album_tags", {}).get("rows", [])
    check("album tags captured", len(album) == 2, str(len(album)))
    check("unchanged album tag not marked changed",
          album[0]["label"] == "album" and album[0]["changed"] is False, str(album[0]))
    check("changed album tag keeps both values",
          album[1]["before"] == "2025-12-05" and album[1]["after"] == "2025", str(album[1]))
    check("diff lines are not also loose notes",
          not any("•••" in e["message"] for e in flow7.events))
    flow7.answer(step7.id, True)
    await asyncio.wait_for(task7, timeout=2)

    # --- metadata comparison becomes a table --------------------------
    flow8 = Flow("upload", "meta2")
    p8 = FlowPrompts(flow8)

    async def compare():
        p8._echo("Pending metadata:")
        p8._echo("> TRACK COUNT   : 14")
        p8._echo("> ARTISTS:")
        p8._echo(">>>  Ryan Charles [main]")
        p8._echo(">>>  Ian Munsick [guest]")
        p8._echo("> TITLE         : Jiggy Buckaroo")
        p8._echo("")
        return await p8._prompt("Are there any metadata fields you would like to edit? [a]rtists, [n]othing")

    task8 = asyncio.create_task(compare())
    step8 = await wait_for_step(flow8)
    meta = next((tb for tb in step8.tables if tb["kind"] == "pending"), None)
    check("metadata comparison captured", meta is not None)
    labels = [r["label"] for r in (meta or {}).get("rows", []) if r["label"]]
    check("metadata fields captured", "TRACK COUNT" in labels and "TITLE" in labels, str(labels))
    # A field whose values arrive on following lines gets one row carrying all
    # of them, not an empty row plus a header plus a row per value.
    artists = [r for r in meta["rows"] if r["label"] == "ARTISTS"]
    check("a multi-value field is one row", len(artists) == 1, str(artists))
    check("every value is on it",
          "Ryan Charles [main]" in artists[0]["before"] and "Ian Munsick [guest]" in artists[0]["before"],
          artists[0]["before"])
    check("no empty rows are left behind",
          not any(r["label"] and not r["before"] for r in meta["rows"]),
          str([r["label"] for r in meta["rows"] if not r["before"]]))
    flow8.answer(step8.id, "n")
    await asyncio.wait_for(task8, timeout=2)

    # --- the group's existing torrents become a table ------------------
    flow9 = Flow("upload", "group")
    p9 = FlowPrompts(flow9)

    async def group_contents():
        p9._echo("Selected ID: 2617840 | Taylor Swift - The Life of a Showgirl (2025)")
        p9._echo("Torrents in this group:")
        p9._echo("> 2025 / 602478372049.1 / CD / FLAC / Lossless")
        p9._echo("> 2025 / 602488067102 /  602488067126 / WEB / FLAC / 24bit Lossless")
        p9._echo("> 2025 / Deluxe / So Punk on the Internet Version / WEB / MP3 / 320")
        p9._echo("> 2025 / Instrumentals / WEB / MP3 / V0 (VBR)")
        p9._echo("")
        return await p9._confirm("Are you sure you would you like to upload this torrent to this group?")

    task9 = asyncio.create_task(group_contents())
    step9 = await wait_for_step(flow9)
    listing = next((tb for tb in step9.tables if tb["kind"] == "torrents"), None)
    check("existing torrents captured as a table", listing is not None)
    rows9 = (listing or {}).get("rows", [])
    check("every torrent line is a row", len(rows9) == 4, str(len(rows9)))
    check("media, format and encoding are separate columns",
          rows9[0] == {"year": "2025", "edition": "602478372049.1", "media": "CD", "format": "FLAC",
                       "encoding": "Lossless"}, str(rows9[0]))
    check("a multi-part catalogue number stays in the edition",
          rows9[1]["edition"] == "602488067102 / 602488067126" and rows9[1]["encoding"] == "24bit Lossless",
          str(rows9[1]))
    check("an edition with a name keeps it",
          rows9[2]["edition"] == "Deluxe / So Punk on the Internet Version", str(rows9[2]))
    check("torrent lines are not also loose notes",
          not any(e["message"].startswith("> 2025 /") for e in flow9.events))
    flow9.answer(step9.id, True)
    await asyncio.wait_for(task9, timeout=2)

    # --- a found group is offered in full, with its link ---------------
    flow10 = Flow("upload", "match")
    p10 = FlowPrompts(flow10)
    long_label = ("Taylor Swift - The Life of a Showgirl (2025) [Album] "
                  "[Tags: pop, pop.rock, female.vocalist, synthpop, singer.songwriter, country]")

    async def match():
        p10._echo("Results matching this release were found on RED:")
        p10._echo(f" 01 >> 2617840 | {long_label} | https://redacted.sh/torrents.php?id=2617840")
        return await p10._prompt(
            "Would you like to upload to an existing group? Paste a URL, pick from groups found or "
            "[N]ew group / [a]bort / [d]elete music folder"
        )

    task10 = asyncio.create_task(match())
    step10 = await wait_for_step(flow10)
    group_option = step10.options[0]
    check("the group is marked as a match rather than a plain option", group_option.get("kind") == "group")
    check("the label is not truncated", group_option["label"] == long_label, group_option["label"][-40:])
    check("the tracker link is carried separately",
          group_option["url"] == "https://redacted.sh/torrents.php?id=2617840", group_option["url"])
    check("the bracket options are still plain buttons",
          [o["value"] for o in step10.options[1:]] == ["n", "a", "d"],
          str([o.get("kind") for o in step10.options[1:]]))
    flow10.answer(step10.id, group_option["value"])
    await asyncio.wait_for(task10, timeout=2)

    # --- the spectral viewer must not touch the package directory -------
    # The pipeline's own viewer symlinks into the installed package and starts a
    # second web server, which in a container is PermissionError [Errno 1] and
    # killed every upload right after the spectrals were compressed. A stand-in
    # is installed for the duration of the flow. The real module needs the audio
    # stack, so this uses a stub when those wheels are absent.
    import sys
    import types

    stub = types.ModuleType("lox.uploader.spectrals")

    async def real_viewer(_path, _ids):
        raise PermissionError(1, "Operation not permitted")

    stub.view_spectrals = real_viewer
    installed = "lox.uploader.spectrals" not in sys.modules
    if installed:
        sys.modules["lox.uploader.spectrals"] = stub
        sys.modules.setdefault("lox.uploader", types.ModuleType("lox.uploader"))
    spectrals_module = sys.modules["lox.uploader.spectrals"]

    flow11 = Flow("upload", "specs")
    p11 = FlowPrompts(flow11, folder="/data/media/deemix/Some Release")
    original_viewer = spectrals_module.view_spectrals
    with p11:
        check("the terminal spectral viewer is replaced while a flow runs",
              spectrals_module.view_spectrals is not original_viewer)
        await spectrals_module.view_spectrals("/config/spectrals/spectrals_Some Release", {1: "01.flac"})
        check("the stand-in does not raise where the real one would", True)
    check("the original viewer is restored afterwards",
          spectrals_module.view_spectrals is original_viewer)

    # --- the editor is a form, not vim --------------------------------
    # click.edit shells out to $EDITOR, which in a container is vim with no
    # terminal: it blocks forever and the upload hangs. Each blob it edits has
    # a known shape, so each becomes a form that serialises back to exactly the
    # text the pipeline's own parser expects.
    from lox.upload_flow import _editor_shape, _editor_text

    shape, rows = _editor_shape("Mohamed Hamaki (main)\nSherine (main)", ".txt")
    check("credits are recognised", shape == "artists", shape)
    check("names and roles are split out",
          rows == [{"name": "Mohamed Hamaki", "role": "main"}, {"name": "Sherine", "role": "main"}], str(rows))
    rows[1]["role"] = "guest"
    rows.append({"name": "New Guy", "role": "remixer"})
    check("edited credits serialise back to the pipeline's format",
          _editor_text(shape, rows, "") == "Mohamed Hamaki (main)\nSherine (guest)\nNew Guy (remixer)",
          _editor_text(shape, rows, ""))

    check("a single line is a title", _editor_shape("Sammaouny", ".txt")[0] == "title")
    check("several plain lines are a list", _editor_shape("Pop\nElectronic", ".txt")[0] == "list")
    check("json stays json", _editor_shape('{"1": {}}', ".json")[0] == "json")

    shape, rows = _editor_shape("Pop\nElectronic", ".txt")
    rows.append({"value": "House"})
    check("a list serialises one per line", _editor_text(shape, rows, "") == "Pop\nElectronic\nHouse")
    check("blank rows are dropped",
          _editor_text("list", [{"value": "Pop"}, {"value": "  "}], "") == "Pop")

    # --- filename changes are a table, not options --------------------
    flow10 = Flow("upload", "rename")
    p10 = FlowPrompts(flow10)

    async def rename():
        p10._echo("Proposed filename changes:")
        p10._echo("   01. Beyoulolek Eih.flac >>> 01. Mohamed Hamaki - Beyoulolek Eih.flac")
        p10._echo("   02. Mesheety.flac >>> 02. Mohamed Hamaki - Mesheety.flac")
        p10._echo("")
        return await p10._confirm("Would you like to rename the files?", default=True)

    task10 = asyncio.create_task(rename())
    step10 = await wait_for_step(flow10)
    renames = next((t for t in step10.tables if t["kind"] == "renames"), None)
    check("filename changes become a table", renames is not None, str([t["kind"] for t in step10.tables]))
    check("old and new are separate columns",
          renames["rows"][0]["before"] == "01. Beyoulolek Eih.flac"
          and renames["rows"][0]["after"] == "01. Mohamed Hamaki - Beyoulolek Eih.flac",
          str(renames["rows"][0]))
    check("they are not offered as options either",
          not any(">>>" in str(o.get("label", "")) for o in step10.options), str(step10.options))
    flow10.answer(step10.id, True)
    await asyncio.wait_for(task10, timeout=2)

    # --- reopen spectrals is gone -------------------------------------
    lossy_options = [o["value"] for o in parse_options(
        "Is this release lossy mastered? [y]es, [N]o, [r]eopen spectrals, [a]bort")]
    check("the reopen button is not offered", "r" not in lossy_options, str(lossy_options))

    # --- the metadata screens are forms that write to the metadata ----
    # They replace async functions that mutate the dict, rather than going
    # through click.edit -- which is synchronous, so an async replacement for it
    # returns a coroutine the pipeline never awaits, and the edit silently did
    # nothing at all.
    flow11 = Flow("upload", "meta-edit")
    p11 = FlowPrompts(flow11, "")
    editors = p11._metadata_editors()

    meta = {
        "artists": [("Mohamed Hamaki", "main"), ("Sherine", "main")],
        "tracks": {"1": {"1": {"artists": [("Mohamed Hamaki", "main"), ("Sherine", "main")]}}},
        "title": "Sammaouny", "year": "2026", "group_year": "2026",
        "genres": ["Pop"], "urls": [], "label": None, "catno": None,
        "edition_title": None, "upc": None, "comment": None,
    }

    async def answer_next(value):
        while flow11.step is None:
            await asyncio.sleep(0.01)
        flow11.answer(flow11.step.id, value)

    async def run_editor(name, value):
        task = asyncio.create_task(editors[name](meta))
        asyncio.create_task(answer_next(value))
        await asyncio.wait_for(task, timeout=2)

    await run_editor("_edit_artists",
                     [{"name": "Mohamed Hamaki", "role": "main"}, {"name": "Sherine", "role": "guest"}])
    check("a role change lands in the metadata",
          meta["artists"] == [("Mohamed Hamaki", "main"), ("Sherine", "guest")], str(meta["artists"]))
    check("and follows through to the tracks",
          meta["tracks"]["1"]["1"]["artists"] == [("Mohamed Hamaki", "main"), ("Sherine", "guest")],
          str(meta["tracks"]["1"]["1"]["artists"]))

    await run_editor("_edit_years", {"year": "2025", "group_year": "1994"})
    check("both years are editable", (meta["year"], meta["group_year"]) == ("2025", "1994"),
          f'{meta["year"]}/{meta["group_year"]}')

    await run_editor("_edit_edition_info",
                     {"edition_title": "Deluxe", "label": "Rotana", "catno": "R123", "upc": ""})
    check("edition fields are separate inputs",
          (meta["edition_title"], meta["label"], meta["catno"], meta["upc"]) == ("Deluxe", "Rotana", "R123", None),
          str([meta["edition_title"], meta["label"], meta["catno"], meta["upc"]]))

    await run_editor("_edit_genres", [{"value": "Pop"}, {"value": "Arabic"}, {"value": "  "}])
    check("a list drops its blank rows", meta["genres"] == ["Pop", "Arabic"], str(meta["genres"]))

    await run_editor("_edit_title", None)
    check("cancelling changes nothing", meta["title"] == "Sammaouny", meta["title"])

    # --- and the pipeline actually reaches them -----------------------
    # The forms above passed their tests while doing nothing at all in
    # production: they were built, but __enter__ only patched click, so the
    # pipeline ran its own _edit_artists, which called the patched click.edit
    # and tried to .split() the coroutine that came back. So run the real
    # review_metadata, through the real context manager, and let it dispatch.
    import lox.tagger.review as review

    original_editor = review._edit_artists
    with stub_spectrals():
        flow13 = Flow("upload", "meta-dispatch")
        live = {
            "artists": [("Mohamed Hamaki", "main")],
            "tracks": {"1": {"1": {"title": "Beyoulolek Eih", "artists": [("Mohamed Hamaki", "main")]}}},
            "title": "Sammaouny", "year": "2026", "group_year": "2026",
            "genres": ["Pop"], "urls": [], "label": None, "catno": None,
            "edition_title": None, "upc": None, "comment": None, "rls_type": "Album",
        }
        with FlowPrompts(flow13, "") as p13:
            check("the editors are installed, not just defined",
                  review._edit_artists is not original_editor, review._edit_artists.__qualname__)
            task13 = asyncio.create_task(review.review_metadata(live, lambda _m: None))

            step13 = await wait_for_step(flow13)
            check("the field menu is a set of buttons",
                  "a" in [o["value"] for o in step13.options], str([o["value"] for o in step13.options]))
            flow13.answer(step13.id, "a")

            step13b = await wait_for_step(flow13)
            check("choosing artists opens the form, not an editor", step13b.kind == "edit", step13b.kind)
            check("and it is the artist form", step13b.edit_shape == "artists", str(step13b.edit_shape))
            flow13.answer(step13b.id, [{"name": "Mohamed Hamaki", "role": "main"},
                                       {"name": "Sherine", "role": "guest"}])

            step13c = await wait_for_step(flow13)
            flow13.answer(step13c.id, "n")
            await asyncio.wait_for(task13, timeout=2)

            # "Manual" metadata is the other editor-backed screen. Left alone
            # it hands click.edit's coroutine to a JSON parser, fails, and
            # reopens the editor forever.
            import lox.tagger.metadata as tagger_metadata

            manual = tagger_metadata._get_manual_metadata({"genres": "Pop", "title": "Sammaouny"})
            check("manual metadata comes back as data, not a coroutine",
                  isinstance(manual, dict), type(manual).__name__)
            check("a single genre is still a list", manual["genres"] == ["Pop"], str(manual["genres"]))

        check("the edit reached the metadata the pipeline kept",
              live["artists"] == [("Mohamed Hamaki", "main"), ("Sherine", "guest")], str(live["artists"]))
        check("the flow ended without an error", flow13.error in (None, ""), str(flow13.error))
    check("and the module is put back afterwards", review._edit_artists is original_editor,
          review._edit_artists.__qualname__)

    # --- metadata candidates are cards, with the year and a link ------
    flow12 = Flow("upload", "meta-pick")
    p12 = FlowPrompts(flow12)

    async def pick():
        p12._echo("Results for Deezer:")
        p12._echo("> 01 Mohamed Hamaki - Sammaouny (2026) {Tracks: 18} | https://www.deezer.com/album/997507401")
        return await p12._prompt("Which metadata results would you like to use? Other options: [m]anual, [a]")

    task12 = asyncio.create_task(pick())
    step12 = await wait_for_step(flow12)
    card = step12.options[0]
    check("a metadata result is a card, not a bare button", card.get("kind") == "group", str(card))
    check("it links to Deezer", card.get("url") == "https://www.deezer.com/album/997507401", str(card.get("url")))
    check("the year and track count are pulled out",
          "2026" in card["detail"] and "18 tracks" in card["detail"], card["detail"])
    check("the label is not truncated", "Sammaouny" in card["label"], card["label"])
    flow12.answer(step12.id, "1")
    await asyncio.wait_for(task12, timeout=2)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
