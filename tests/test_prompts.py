"""Check the CLI-prompt to UI-control bridge against real pipeline output."""

import asyncio
import sys

from lox.flow import Flow
from lox.upload_flow import FlowPrompts, default_letter, parse_extra_options, parse_options, strip_ansi

results: list[tuple[str, bool, str]] = []


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
    check("lossy prompt yields five options", len(options) == 5, str([o["value"] for o in options]))
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
    task3 = asyncio.create_task(prompts3._confirm("Would you like to rename the files?", default=True))
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

    # --- downconversion menu becomes a multi-select -------------------
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
    check("downconversion is a multi-select",
          step6.kind == "multi" and [o["label"] for o in step6.options] == ["MP3 320", "MP3 V0"],
          str([o["label"] for o in step6.options]))
    flow6.answer(step6.id, ["1", "2"])
    check("selection returned space-separated", await asyncio.wait_for(task6, timeout=2) == "1 2")

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
    check("list items attach to their field",
          any(r["group"] == "ARTISTS" and "Ryan Charles" in r["before"] for r in meta["rows"]))
    flow8.answer(step8.id, "n")
    await asyncio.wait_for(task8, timeout=2)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
