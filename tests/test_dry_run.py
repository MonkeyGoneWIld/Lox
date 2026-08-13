"""A dry run should do everything a real one does except write anywhere.

It did not. A dry run of a new group posts nothing, so the group id it comes
back with is a placeholder -- and asking the tracker to list a group with that
id raised "0 does not exist" and aborted, which ended the run before the
downconversion question, the deferred spectral check and the seeding summary.
So the only parts of an upload a dry run ever showed were the ones before the
post.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "_dryrun")
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": "5100",
        "LOX_AUTH_TOKEN": "0123456789abcdef0123456789abcdef",
        "LOX_DOWNLOAD_DIR": os.path.join(BASE, "downloads"),
        "LOX_TORRENTS_DIR": os.path.join(BASE, "torrents"),
        "LOX_SETTINGS_DIR": os.path.join(BASE, "config"),
        "LOX_STATE_DIR": os.path.join(BASE, "state"),
    }
)
os.makedirs(os.environ["LOX_DOWNLOAD_DIR"], exist_ok=True)

import asyncclick as click  # noqa: E402

from lox import cfg  # noqa: E402
from lox.flow import Flow  # noqa: E402
from lox.upload_flow import FlowPrompts  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


PAYLOAD = {
    "submit": True,
    "type": 0,
    "title": "Sammaouny",
    "artists[]": ["Mohamed Hamaki", "Sherine"],
    "importance[]": [1, 2],
    "year": "2026",
    "releasetype": 1,
    "format": "FLAC",
    "bitrate": "Lossless",
    "media": "WEB",
    "remaster_year": "2026",
    "remaster_record_label": "The Basement Records",
    "tags": "pop, arabic",
    "requestid": 41688,
    "image": "https://example.invalid/cover.jpg",
    "album_desc": "[b][size=4]Tracklist[/size][/b]\n[b]01.[/b] Beyoulolek Eih [i](3:42)[/i]",
    "release_desc": "Encode Specifics: 16 bit 44.1 kHz\nReleased on 2026-07-31",
}


async def main() -> int:
    from lox.trackers.base import BaseGazelleApi

    lines: list[str] = []
    saved_secho, saved_echo = click.secho, click.echo
    click.secho = lambda message="", **_: lines.append(str(message))
    click.echo = lambda message="", **_: lines.append(str(message))
    try:
        torrent_id, group_id = BaseGazelleApi._log_dry_run_upload(
            type("Site", (), {"site_string": "RED"})(), PAYLOAD
        )
    finally:
        click.secho, click.echo = saved_secho, saved_echo

    out = "\n".join(lines)
    check("nothing is posted, so there is no torrent id", torrent_id == 0, str(torrent_id))
    check("and no group id for a new group", group_id == 0, str(group_id))
    check("it says which tracker it did not post to", "Not uploading to RED" in out, "")

    for field in ("title", "artists", "importance", "releasetype", "format", "bitrate", "media",
                  "remaster_year", "remaster_record_label", "tags", "requestid", "image"):
        check(f"the payload shows {field}", any(line.strip().startswith(field) for line in lines), "")

    check("bracketed list fields are found under their plain name",
          any("Mohamed Hamaki" in line for line in lines), "")
    check("the request it would fill is called out",
          "Would have filled request 41688" in out, "")

    # The descriptions are the part worth reading before committing to a post.
    check("the album description is printed in full",
          "| [b]01.[/b] Beyoulolek Eih [i](3:42)[/i]" in out, "")
    check("so is the release description",
          "| Released on 2026-07-31" in out, "")
    check("and their sizes still appear as fields",
          any("album description" in line and "chars" in line for line in lines), "")

    # --- the summary table still parses ------------------------------
    # The bridge turns "  key   value" into table rows; the description text
    # must not be mistaken for one.
    flow = Flow("upload", "dry")
    prompts = FlowPrompts(flow, "")
    for line in lines:
        prompts._echo(line)
    payload = prompts.dry_run_payload
    check("the table picked up the fields", payload.get("title") == "Sammaouny", str(payload.get("title")))
    check("including the request id", payload.get("requestid") == "41688", str(payload.get("requestid")))
    check("and did not swallow description text as a field",
          not any(k.startswith("|") for k in payload), str(list(payload)))

    # --- the group listing is skipped, not fatal ---------------------
    source = (
        "            if cfg.upload.dry_run and not group_id:",
        "                await print_torrents(gazelle_site, group_id, highlight_torrent_id=torrent_id)",
    )
    text = open(
        os.path.join(os.path.dirname(ROOT), "lox", "uploader", "__init__.py"), encoding="utf-8"
    ).read()
    check("a dry run with no group never asks the tracker to list it", source[0] in text, "")
    check("a real run still does", source[1] in text, "")
    check("the deferred spectral check is not skipped for want of a torrent id",
          "if spectrals_after and (torrent_id or cfg.upload.dry_run):" in text, "")

    # --- the writes really are still off ------------------------------
    # The seeding side is asserted on its source: importing it pulls in the
    # torrent-client libraries, which are not installed everywhere this runs,
    # and what matters is that the guard is in front of the transfer.
    seedbox = open(
        os.path.join(os.path.dirname(ROOT), "lox", "uploader", "seedbox.py"), encoding="utf-8"
    ).read()
    guard = seedbox.index("if cfg.upload.dry_run:")
    check("seeding is described rather than run",
          "[DRY RUN] Not running" in seedbox and guard < seedbox.index("Executing {len(self.tasks)}"), "")
    check("nothing reaches the download client in a dry run",
          guard < seedbox.index("_add_to_downloader(client"), "")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
