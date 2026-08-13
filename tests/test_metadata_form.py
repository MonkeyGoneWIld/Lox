"""The metadata screen is one form, and the questions around it show their work.

The pipeline edits metadata as a menu: it prints the record, asks which single
field you want to change, opens an editor for that field, prints the record and
asks again. Four changes meant four round trips and you could never see the
release while editing part of it. This checks the replacement -- the whole
record in one form -- against the real review_metadata call path, plus the
things that were invisible around it: the folder rename, the downconversion
menu, and what a dry run leaves on disk.
"""

import asyncio
import os
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_metaform")
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5101",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
        "LOX_TMP_DIR": os.path.join(BASE, "spectrals"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

from lox.errors import InvalidMetadataError  # noqa: E402
from lox.flow import Flow  # noqa: E402
from lox.upload_flow import FlowPrompts  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


async def wait_for_step(flow, timeout: float = 2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if flow.step is not None:
            return flow.step
        await asyncio.sleep(0.01)
    raise AssertionError("no step appeared")


def stub_spectrals():
    """Let FlowPrompts.__enter__ run where the audio wheels are not built."""
    try:
        import lox.uploader.spectrals  # noqa: F401

        return
    except ImportError:
        pass
    parent = sys.modules.get("lox.uploader") or types.ModuleType("lox.uploader")
    module = types.ModuleType("lox.uploader.spectrals")
    module.view_spectrals = lambda *_a, **_k: None
    parent.spectrals = module
    sys.modules.setdefault("lox.uploader", parent)
    sys.modules["lox.uploader.spectrals"] = module


def fresh_metadata() -> dict:
    return {
        "artists": [("Mohamed Hamaki", "main"), ("Sherine", "main")],
        "tracks": {
            "1": {
                "1": {"title": "Beyoulolek Eih", "artists": [("Mohamed Hamaki", "main")]},
                "2": {"title": "Mesheety", "artists": [("Mohamed Hamaki", "main"), ("Sherine", "main")]},
            }
        },
        "title": "Sammaouny",
        "year": "2026",
        "group_year": "2026",
        "genres": ["Pop"],
        "urls": ["https://www.deezer.com/album/997507401"],
        "label": "The Basement Records",
        "catno": None,
        "edition_title": None,
        "upc": "729771312036",
        "comment": None,
        "rls_type": "Album",
    }


async def main() -> int:
    stub_spectrals()
    import lox.tagger.review as review

    # --- the whole record is one form -------------------------------
    flow = Flow("upload", "meta")
    metadata = fresh_metadata()
    original_review = review.review_metadata

    with FlowPrompts(flow, "") as prompts:
        check("review_metadata itself is replaced, not just its editors",
              review.review_metadata is not original_review, review.review_metadata.__qualname__)

        # from-import copies the reference, so the callers have to be patched too.
        import lox.tagger as tagger_package

        check("and on the modules that imported it by name",
              tagger_package.review_metadata is review.review_metadata, "")

        task = asyncio.create_task(review.review_metadata(metadata, lambda _m: None))
        step = await wait_for_step(flow)

        check("it is one edit step, not a menu", step.kind == "edit", step.kind)
        check("carrying the whole record", step.edit_shape == "metadata", str(step.edit_shape))

        # Grouped now: each option is a titled group of fields.
        groups = [g["group"] for g in step.options]
        check("the form is grouped, not one long run",
              groups == ["The release", "Credits", "This edition", "Filing", "Tracks"], str(groups))
        fields = [f for group in step.options for f in group["fields"]]
        keys = [f["key"] for f in fields]
        for key in ("artists", "title", "rls_type", "year", "group_year", "edition_title",
                    "label", "catno", "upc", "genres", "urls", "comment", "tracks"):
            check(f"the form has {key}", key in keys, str(keys))

        by_key = {f["key"]: f for f in fields}
        check("artists come through as rows with roles",
              by_key["artists"]["rows"] == [{"name": "Mohamed Hamaki", "role": "main"},
                                            {"name": "Sherine", "role": "main"}],
              str(by_key["artists"]["rows"]))
        check("release type is a select with the tracker's own list",
              by_key["rls_type"]["kind"] == "select" and "Album" in by_key["rls_type"]["choices"],
              str(by_key["rls_type"]["kind"]))
        check("years are number fields", by_key["year"]["kind"] == "number", by_key["year"]["kind"])
        check("genres and urls are lists",
              by_key["genres"]["kind"] == "list" and by_key["urls"]["kind"] == "list", "")
        check("the comment is a text area", by_key["comment"]["kind"] == "textarea", "")
        check("every track is listed", len(by_key["tracks"]["rows"]) == 2, str(by_key["tracks"]["rows"]))
        check("with its current title", by_key["tracks"]["rows"][0]["value"] == "Beyoulolek Eih", "")
        check("and who is on it", "Sherine" in by_key["tracks"]["rows"][1]["artists"], "")
        check("values are the ones already found, not blanks",
              by_key["title"]["value"] == "Sammaouny" and by_key["label"]["value"] == "The Basement Records", "")

        # Answer the whole thing at once, which is the point.
        flow.answer(step.id, {
            "artists": [{"name": "Mohamed Hamaki", "role": "main"}, {"name": "Sherine", "role": "guest"}],
            "title": "Sammaouny (Deluxe)",
            "rls_type": "EP",
            "year": "2027",
            "group_year": "1994",
            "edition_title": "Deluxe",
            "label": "Rotana",
            "catno": "R-123",
            "upc": "",
            "genres": ["Pop", "Arabic", "  "],
            "urls": [],
            "comment": "Ripped from Deezer",
            "tracks": {"1/1": "Beyoulolek Eih (Remastered)", "1/2": "Mesheety"},
        })
        await asyncio.wait_for(task, timeout=2)

    check("one save changed every field",
          metadata["title"] == "Sammaouny (Deluxe)" and metadata["rls_type"] == "EP"
          and metadata["year"] == "2027" and metadata["group_year"] == "1994"
          and metadata["edition_title"] == "Deluxe" and metadata["label"] == "Rotana"
          and metadata["catno"] == "R-123",
          str([metadata["title"], metadata["rls_type"], metadata["year"], metadata["group_year"]]))
    check("a cleared field becomes empty rather than keeping the old value",
          metadata["upc"] is None, str(metadata["upc"]))
    check("a role change lands", metadata["artists"] == [("Mohamed Hamaki", "main"), ("Sherine", "guest")],
          str(metadata["artists"]))
    check("and follows through to the tracks",
          metadata["tracks"]["1"]["2"]["artists"] == [("Mohamed Hamaki", "main"), ("Sherine", "guest")],
          str(metadata["tracks"]["1"]["2"]["artists"]))
    check("blank list rows are dropped", metadata["genres"] == ["Pop", "Arabic"], str(metadata["genres"]))
    check("an emptied list is emptied", metadata["urls"] == [], str(metadata["urls"]))
    check("the comment is kept", metadata["comment"] == "Ripped from Deezer", str(metadata["comment"]))
    check("a track title change lands",
          metadata["tracks"]["1"]["1"]["title"] == "Beyoulolek Eih (Remastered)",
          metadata["tracks"]["1"]["1"]["title"])
    check("the editors are put back afterwards", review.review_metadata is original_review, "")

    # --- an invalid record reopens the form, with the reason ---------
    flow2 = Flow("upload", "meta-invalid")
    metadata2 = fresh_metadata()
    seen = {"n": 0}

    def validator(meta):
        seen["n"] += 1
        if seen["n"] == 1:
            raise InvalidMetadataError("Release type is required.")

    with FlowPrompts(flow2, ""):
        task2 = asyncio.create_task(review.review_metadata(metadata2, validator))
        first = await wait_for_step(flow2)
        flow2.answer(first.id, {"title": "Sammaouny"})
        second = await wait_for_step(flow2)
        check("an invalid record comes back as the form again", second.kind == "edit", second.kind)
        check("with the reason on it", "Release type is required." in (second.detail or ""), str(second.detail))
        flow2.answer(second.id, {"title": "Sammaouny"})
        await asyncio.wait_for(task2, timeout=2)
    check("and it is not a yes/no about revisiting a step", seen["n"] == 2, str(seen["n"]))

    # --- leaving it alone changes nothing ---------------------------
    flow3 = Flow("upload", "meta-cancel")
    metadata3 = fresh_metadata()
    with FlowPrompts(flow3, ""):
        task3 = asyncio.create_task(review.review_metadata(metadata3, lambda _m: None))
        step3 = await wait_for_step(flow3)
        flow3.answer(step3.id, None)
        await asyncio.wait_for(task3, timeout=2)
    check("leaving the form unchanged leaves the metadata unchanged",
          metadata3 == fresh_metadata(), str(metadata3["title"]))

    # --- the folder rename says what it is renaming ------------------
    flow4 = Flow("upload", "rename")
    p4 = FlowPrompts(flow4, "")
    for line in (
        "Renaming folder...",
        "Old folder name        : Mohamed Hamaki - Sammaouny (2026) [WEB FLAC]",
        "New pending folder name: Mohamed Hamaki & Sherine - Sammaouny (2026) [WEB FLAC]",
    ):
        p4._echo(line)
    answer = p4._confirm("Would you like to replace the original folder name?", default=True)
    task4 = asyncio.ensure_future(answer)
    step4 = await wait_for_step(flow4)
    folder = next((t for t in step4.tables if t["kind"] == "folder"), None)
    check("the rename question carries the names", folder is not None, str([t["kind"] for t in step4.tables]))
    check("the old name is there",
          folder["rows"][0]["before"] == "Mohamed Hamaki - Sammaouny (2026) [WEB FLAC]",
          str(folder["rows"][0]))
    check("and the new one",
          folder["rows"][0]["after"] == "Mohamed Hamaki & Sherine - Sammaouny (2026) [WEB FLAC]",
          str(folder["rows"][0]))
    check("they are not offered as buttons", not step4.options, str(step4.options))
    flow4.answer(step4.id, True)
    await asyncio.wait_for(task4, timeout=2)

    # --- the downconversion menu offers formats, once each -----------
    flow5 = Flow("upload", "downconvert")
    p5 = FlowPrompts(flow5, "")

    async def downconvert():
        # Exactly what the pipeline prints, renames and all.
        p5._echo("Proposed filename changes:")
        p5._echo("   Mesheety.flac >>> 02. Mohamed Hamaki - Mesheety.flac")
        p5._echo("   Tooba.flac >>> 03. Mohamed Hamaki - Tooba.flac")
        p5._confirm("Would you like to rename the files?", default=True)
        p5._echo("Downconversion Options")
        p5._echo("Current format: Lossless (44.1 kHz)")
        p5._echo("Available downconversion formats:")
        p5._echo("  1. MP3 320")
        p5._echo("  2. MP3 V0")
        p5._echo("  0. Skip downconversion")
        p5._echo("  *. All formats")
        return await p5._prompt(
            'Select formats to convert (space-separated list of IDs, "0" for none, "*" for all)', default="*"
        )

    task5 = asyncio.create_task(downconvert())
    step5 = await wait_for_step(flow5)
    labels = [o["label"] for o in step5.options]
    check("the formats are offered", labels == ["MP3 320", "MP3 V0", "Every format", "Do not convert"], str(labels))
    check("no filename is offered as a format", not any(">>>" in label for label in labels), str(labels))
    check("skipping is offered once, not twice",
          len([label for label in labels if label in ("Do not convert", "Skip downconversion")]) == 1, str(labels))
    check("every format is the highlighted answer", step5.default == "*", str(step5.default))
    flow5.answer(step5.id, "*")
    check("and it is the answer the pipeline receives", await asyncio.wait_for(task5, timeout=2) == "*", "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
