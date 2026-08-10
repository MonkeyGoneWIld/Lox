"""Start the app with an unusable download directory and prove it still comes up.

This is the deploy failure this test exists for: LOX_DOWNLOAD_DIR pointed at a
path the volume mount did not provide, config validation raised, the process
exited, Docker restarted it, and it exited again — a loop with no way in to fix
the setting that caused it.

A wrong path is now a reported problem rather than a refusal to start, so the
sequence a user actually needs works: the server comes up, says what is wrong,
declines the operations that need the directory with a message naming the
setting, and clears the problem when the path is corrected — no restart.

Missing directories are simply created, so to reach the failure at all this
points the download directory at a path *under a regular file*, which no amount
of creating can fix. That stands in for the cases a container really hits: a
volume mounted from the wrong host path, or one owned by another uid.

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

# A file, not a directory. makedirs cannot climb through it.
BLOCKER = os.path.join(ROOT, "not-a-directory")
with open(BLOCKER, "w", encoding="utf-8") as handle:
    handle.write("stands in for a volume that is not mounted\n")

BROKEN = os.path.join(BLOCKER, "media", "deemix")
WORKING = os.path.join(ROOT, "media")
CREATED = os.path.join(ROOT, "made-on-demand", "deemix")
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

    check("import survives an unusable download directory", True)
    check("the bad path is kept, not silently replaced", lox.cfg.directory.download_directory == BROKEN)
    check("the problem is recorded", any(p["key"] == "directory.download_directory" for p in problems()))
    # "Does not exist" would be true and useless. The message has to say what is
    # actually there, because that is what tells a mount problem apart from a
    # permissions problem.
    check(
        "the message names what is standing in the way",
        any(BLOCKER in p["message"] and "in the way" in p["message"] for p in problems()),
        next((p["message"] for p in problems() if p["key"] == "directory.download_directory"), ""),
    )

    # The other shape of the same failure, and the more common one: the volume
    # is mounted, but from a host path that has nothing in it. Creating the
    # directory then "succeeds" inside the container and the downloads vanish on
    # the next restart, so an empty parent is worth saying out loud.
    from lox.config.validations import diagnose

    empty = os.path.join(ROOT, "empty-mount")
    os.makedirs(empty, exist_ok=True)
    check("an empty parent is called out as a probable wrong mount",
          "empty" in diagnose(os.path.join(empty, "media", "deemix")),
          diagnose(os.path.join(empty, "media", "deemix")).strip())

    # The case a container hits when the volume is owned by another uid and it
    # runs as PUID:PGID. isdir() says False for a directory that is plainly
    # there, so the message has to talk about ownership, not existence.
    locked = os.path.join(ROOT, "locked")
    os.makedirs(locked, exist_ok=True)

    # Verified everywhere by denying the traverse check directly: this asserts
    # the branch is wired to the right message, which is the part that can be
    # wrong in the source.
    real_access = os.access
    os.access = lambda path, mode, **kw: False if os.path.abspath(path) == locked else real_access(path, mode, **kw)
    try:
        said = diagnose(os.path.join(locked, "deemix"))
    finally:
        os.access = real_access
    check("an unreadable parent is reported as ownership, not absence",
          "cannot look inside" in said, said.strip())

    # And for real where the OS can do it. Root ignores the permission bits,
    # which would make this vacuous.
    if os.name == "posix" and os.getuid() != 0:
        os.chmod(locked, 0o000)
        try:
            said = diagnose(os.path.join(locked, "deemix"))
            check("real unreadable directory says the same, and names the uid",
                  "cannot look inside" in said and "uid" in said, said.strip())
        finally:
            os.chmod(locked, 0o700)
    else:
        check("real chmod case skipped (needs a non-root posix host)", True)

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

            # A path that is merely absent is not a problem at all: make it.
            async with s.put(
                f"{BASE}/api/settings", json={"changes": {"directory.download_directory": CREATED}}
            ) as r:
                saved = await r.json()
            check("a missing directory is created rather than reported",
                  not has_path_problem(saved) and os.path.isdir(CREATED), str(saved.get("problems")))
    finally:
        await runner.cleanup()
        shutil.rmtree(ROOT, ignore_errors=True)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
