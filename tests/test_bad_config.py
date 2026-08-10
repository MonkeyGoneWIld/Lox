"""Start the app with a broken download directory and prove it still comes up.

This is the deploy failure this test exists for: LOX_DOWNLOAD_DIR pointed at a
path the volume mount did not provide, config validation raised, the process
exited, Docker restarted it, and it exited again — a loop with no way in to fix
the setting that caused it.

A wrong path is now a reported problem rather than a refusal to start, so the
sequence a user actually needs works: the server comes up, says what is wrong,
declines the operations that need the directory with a message naming the
setting, and clears the problem when the path is corrected — no restart.

Run it directly. It sets its own environment before importing lox, because the
config is read at import time:

    python tests/test_bad_config.py
"""

import asyncio
import os
import shutil
import sys
import tempfile

import aiohttp

PORT = 5098
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "0123456789abcdef0123456789abcdef"

ROOT = tempfile.mkdtemp(prefix="lox-badconfig-")
BROKEN = os.path.join(ROOT, "never-mounted", "deemix")
WORKING = os.path.join(ROOT, "media")
os.makedirs(WORKING, exist_ok=True)

# Must happen before lox is imported: setup_config() runs at import.
os.environ.update(
    {
        "LOX_HOST": "127.0.0.1",
        "LOX_PORT": str(PORT),
        "LOX_AUTH_TOKEN": TOKEN,
        "LOX_DOWNLOAD_DIR": BROKEN,
        "LOX_TORRENTS_DIR": os.path.join(ROOT, "torrents"),
        "LOX_TMP_DIR": os.path.join(ROOT, "spectrals"),
        "LOX_STATE_DIR": os.path.join(ROOT, "state"),
        "LOX_SETTINGS_DIR": os.path.join(ROOT, "config"),
    }
)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def has_path_problem(payload: dict) -> bool:
    """True if the payload reports the download directory as a problem."""
    return any(p["key"] == "directory.download_directory" for p in payload.get("problems") or [])


async def main() -> int:
    """Run the checks."""
    # Importing at all is the first assertion: this used to be the exit.
    import lox
    from lox.config.validations import problems
    from lox.web import create_app_async

    check("import survives a missing download directory", True)
    check("the bad path is kept, not silently replaced", lox.cfg.directory.download_directory == BROKEN)
    check("the problem is recorded", any(p["key"] == "directory.download_directory" for p in problems()))
    check(
        "the message says the directory is not created for you",
        any("will not create it" in p["message"] for p in problems()),
    )

    runner = await create_app_async()
    check("the server starts anyway", True)

    try:
        async with aiohttp.ClientSession(headers={"X-Auth-Token": TOKEN}) as s:
            async with s.get(f"{BASE}/api/status") as r:
                check("status responds", r.status == 200, f"got {r.status}")
                status = await r.json()
            check("status carries the problem so the UI can show a banner", has_path_problem(status))

            async with s.get(f"{BASE}/api/folders") as r:
                folders = await r.json()
            # An empty list reads as "nothing to upload", which is a different
            # thing from "lox cannot see your library".
            check("the folder list explains itself rather than looking empty", bool(folders.get("error")))

            async with s.post(f"{BASE}/api/download", json={"album_id": "1"}) as r:
                body = await r.json()
                check(
                    "downloading is refused with a message naming the setting",
                    r.status == 400 and "Settings" in body.get("error", ""),
                    body.get("error", ""),
                )

            # The recovery path: fix it in the UI, which means it must not be a
            # bootstrap-only key.
            async with s.put(
                f"{BASE}/api/settings", json={"changes": {"directory.download_directory": WORKING}}
            ) as r:
                saved = await r.json()
            check(
                "the download directory is editable in the UI",
                r.status == 200 and "directory.download_directory" in (saved.get("saved") or []),
                str(saved.get("error", "")),
            )
            check("saving re-runs the checks", not has_path_problem(saved))

            async with s.get(f"{BASE}/api/status") as r:
                status = await r.json()
            check("the banner clears without a restart", not has_path_problem(status))

            async with s.get(f"{BASE}/api/folders") as r:
                folders = await r.json()
            check("the folder list works once corrected", not folders.get("error"), str(folders.get("error", "")))
    finally:
        await runner.cleanup()
        shutil.rmtree(ROOT, ignore_errors=True)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
