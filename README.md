# Lox

**A Deezer-to-Gazelle upload pipeline with a web UI.** Find a release on Deezer, check whether RED and OPS already have
it, look at what the checker found, download it in FLAC, and upload it to whichever trackers are missing it — without
leaving the browser.

> **Built on [smoked-salmon](https://github.com/smokin-salmon/smoked-salmon) `0.10.0`**, whose tagging, spectral,
> transcoding and upload machinery this uses under Apache-2.0. Lox is not affiliated with that project and is not
> maintained by it — please do not take problems here to them. If you want the supported, reviewed thing, use
> [smokin-salmon/smoked-salmon](https://github.com/smokin-salmon/smoked-salmon). Everything Deezer-shaped, the web UI,
> the tracker budget, the checker and the per-tracker seeding layout are Lox's.
>
> Written with heavy help from an LLM. Read the code before you point it at an account you care about.

---

## The one thing to understand first

**Trackers are only contacted when you press a button that says so.**

RED and OPS have small API budgets and punish bursts with hours-long lockouts. So every tracker call in this app goes
through a single gateway that spends from a token bucket, spaces calls apart, and opens a circuit breaker after repeated
failures. Search, Explore, album metadata, FLAC checks and downloads never touch a tracker at all.

The sidebar shows the remaining budget per tracker at all times. When a scan would overdraw it, the scan **stops early
and keeps its place** rather than blowing the limit.

---

## What it does

### Check one album, then upload it

Open any album and press **Check RED** or **Check all**. The checker searches the tracker, opens each candidate torrent
group, and tells you what it found — including the groups it *rejected* and why:

```
RED · not on tracker · 4 call(s), 2 search(es) · artist page
  ├─ Ana Vidal — Nocturne Variations (Remixes) (2026)   title mismatch (0.81)     WEB FLAC Lossless
  └─ Anna Vidale — Nocturnes (2019)                     artist mismatch            CD FLAC Lossless
OPS · not on tracker · 2 call(s), 1 search(es)
  └─ Ana Vidal — Nocturne Variations (2026)             no WEB FLAC in group       CD MP3 320
```

Every group is a link. Open them, confirm the checker got it right, then press **Download & upload to RED + OPS**. The
near misses are the point — a "missing" verdict is only trustworthy if you can see what it looked at.

### Check a list of requests against Deezer

Paste request IDs or URLs, or upload a `.txt` list. For each request lox fetches it from the tracker, searches Deezer,
scores the match, then rejects it unless it clears every gate independently:

| Gate | Rule |
|---|---|
| Artist score | ≥ 0.50 |
| Title score | ≥ 0.40 |
| Combined | ≥ `checker.min_confidence` (0.70) |
| Track count | Exact match when it can be determined |
| Availability | Every track streamable in your region |
| FLAC | All tracks lossless, when the request is FLAC-only |
| External sources | Discogs, MusicBrainz, Bandcamp, Beatport, Qobuz, Tidal, Apple Music and Metal-Archives must agree on the track count |

Filling a request with the wrong edition is worse than not filling it, so a great artist score cannot rescue a poor title
score. Only the request fetch costs tracker budget; everything after it is free.

### Saved Deezer searches

The **Scan** tab keeps re-runnable queries — new releases by genre, a chart, an album search, an artist's
discography, a playlist, or a channel module. Running one is free and drops its albums straight into the scan.
Set one up for new releases in your genre and it becomes a one-click routine: paste or pick the sources, press **Scan**,
and it expands them and checks everything that comes out against the trackers you picked, without stopping to ask you to
confirm a list you did not choose.

### Explore

Deezer channels, charts by genre, and editorial new releases. Channels are read out of the page's `__DZR_APP_STATE__`,
which is the surface deemix never exposed. Any channel module can be sent to the Scan tab with one click.

### Downloading

With an ARL set, downloads happen in-process: stream URLs resolved through Deezer's media API, the Blowfish-striped
payload decrypted as it streams, tags and cover art written from the Deezer metadata. The result is a plain release
folder the upload flow already understands.

### Hardlinked per-tracker folders

Uploading one release to two trackers needs two torrents pointed at two paths. Rather than keep two copies, lox
hardlinks — the same thing cross-seed does:

```
/links/
├── RED/
│   └── Ana Vidal - Nocturne Variations (2026) [WEB FLAC]/
│       ├── 01. Nocturne I.flac    → hardlink
│       └── cover.jpg              → hardlink
└── OPS/
    └── Ana Vidal - Nocturne Variations (2026) [WEB FLAC]/
        ├── 01. Nocturne I.flac    → hardlink
        └── cover.jpg              → hardlink
```

A 500 MB release costs 500 MB no matter how many trackers it goes to. Point qBittorrent at `/links` and both torrents
seed from their own directory.

**Hardlinks cannot cross filesystems.** `linking.link_dir` must be on the same volume as your downloads. If it isn't,
lox fails loudly rather than silently making real copies — unless you set `linking.fallback_to_copy = true`.

The seedbox entry that injects into your torrent client is tracker-aware: set `tracker = "RED"` to restrict an entry to
one tracker, and use `{tracker}` in `directory` and `label` so one entry serves both with the right save path and
category. Leaving `directory` empty derives it from `linking.link_dir` automatically.

### Dry run

```bash
lox up "/music/Ana Vidal - Nocturne Variations" --dry-run
```

Or tick **Dry run** in the Uploads tab, or set `upload.dry_run = true` in config.

Everything runs — tagging, renaming, spectral generation and upload, cover handling, description assembly, hardlinking,
`.torrent` creation — except the two steps you cannot take back:

| Step | Dry run |
|---|---|
| Post the torrent to the tracker | skipped, prints the payload it would have sent |
| Fill a request | skipped (it rides along with the upload) |
| File a lossy-master report | skipped |
| Edit a torrent description | skipped |
| Transfer to seedbox / add to download client | skipped, prints the save path and category it would have used |
| Upload cover art and spectrals to an image host | skipped, stand-in links on `dry-run.invalid` |
| Copy the URL to your clipboard | skipped |

Everything else happens for real, because those are the parts worth checking before a real run: the files are tagged,
the folder is renamed, the downconversion runs and its output is **kept** — listed on the result with a Delete button,
rather than removed before you can look at it. The descriptions are printed in full, not summarised as a character
count, since reading them is the point of rehearsing.

The `.torrent` is still written so you can inspect it, but its comment is left blank rather than stamped with a URL
containing a placeholder torrent ID.

---

## Install

Needs Python 3.11+, plus `sox`, `flac`, `lame` and `mp3val` on PATH.

```bash
uv tool install git+https://github.com/MonkeyGoneWIld/Lox
```

Then write a config (see [`data/config.default.toml`](data/config.default.toml) for every option) and run:

```bash
lox ui
```

The UI comes up on `http://127.0.0.1:5015` — change it under `[upload.web_interface]`. The `lox` command still works
as an alias, and every upstream CLI command is unchanged.

---

## Docker

```bash
cp .env.example .env
openssl rand -hex 32   # put this in LOX_AUTH_TOKEN
docker compose up -d
```

That is the whole setup. **No config file.** [`docker-compose.yml`](docker-compose.yml) supplies the handful of
bootstrap values through `LOX_*` environment variables; everything else is configured in the UI under Settings, each
section with a Test button, and stored in `settings.toml` on the mounted volume.

Two things will bite you if you skip them:

**Set `LOX_AUTH_TOKEN`.** The UI can spend your tracker API budget, read your authenticated Deezer session, and start
uploads to your tracker accounts. The compose file binds to `127.0.0.1` by default for exactly that reason. lox prints
a loud warning at startup if you bind publicly without a token.

**Put the download directory and the seeding directory on the same filesystem.** Different volumes means no hardlinks.
Mounting their common parent as one volume is the simplest guarantee — and the Seeding layout test will tell you
whether it actually worked.

On first load the UI asks for the token and stores an httpOnly cookie for 30 days. Sign out from Settings. A `?token=`
query parameter and the `X-Auth-Token` header both still work for scripts and the healthcheck.

## Configuration

Almost everything is set in the UI under **Settings** — 82 settings across 16 sections grouped into Accounts,
Uploading, Files and Maintenance, applied without a restart. Sixteen things can be tested against the real service
rather than checked for being non-empty: eight whole sections, and eight individual credentials that stand on their
own. A section holding four image-host keys cannot be answered by one button at the top, so each key has its own:

| Test | What it actually proves |
|---|---|
| Deezer | Logs in with the ARL and reports the account, plus whether it holds a streaming licence — a valid ARL without one cannot download |
| RED / OPS / DIC | Calls `index`, reports your username and whether it authenticated by API key or session cookie |
| Seeding layout | Creates a real hardlink between the download and seeding directories and compares inodes |
| Torrent client | Connects to every configured client and reports its version |
| Discord | Posts a test message |
| Paths | Checks every directory exists and is writable |
| ptpimg / ptscreens / oeimg / imgbb | Each key against its own host. Nothing is uploaded — a settings test should not put a real file on a public host |
| Discogs / Apple Music / Qobuz / Tidal | Each token against its own API, with a known lookup |

### Logs

lox writes a rolling log to `<settings dir>/logs/lox.log`, or wherever `LOX_LOG_DIR` points. It is bounded twice:
**8 MB per file** and **1 GB in total**, with the oldest files dropped once the cap is reached. Both limits are
editable under Settings → Debug.

Everything is redacted at the formatter, so ARLs, session cookies, API keys and webhook URLs never reach disk — the log
is safe to share. Turn on Debug mode for verbose output; Settings → Debug tails it live and offers both the log file
and a diagnostics bundle for download.

UI settings live in `settings.toml`. Nothing ever rewrites `config.toml`, and deleting `settings.toml` reverts to
whatever the bootstrap says.

### Bootstrap

Three values are read before a web server exists, so they cannot come from a page the server has not started yet:

| Environment | Config key |
|---|---|
| `LOX_HOST` | `upload.web_interface.host` |
| `LOX_PORT` | `upload.web_interface.port` |
| `LOX_AUTH_TOKEN` | `upload.web_interface.auth_token` |

The directories are bootstrapped the same way, but they are *not* in that list — the server does not need them to serve
a page, and a wrong path has to be fixable from the UI rather than only by editing compose:

| Environment | Config key | |
|---|---|---|
| `LOX_DOWNLOAD_DIR` | `directory.download_directory` | Created if missing. |
| `LOX_TORRENTS_DIR` | `directory.dottorrents_dir` | Created if missing. |
| `LOX_TMP_DIR` | `directory.tmp_dir` | Optional. Created if missing. |
| `LOX_STATE_DIR` | `checker.state_dir` | Optional. Created if missing. |
| `LOX_LOG_DIR` | `logging.directory` | Optional. Created if missing. |
| `LOX_SETTINGS_DIR` | — | Optional. Where `settings.toml` and the database go. |

Anything set under Settings → Paths overrides the environment. The environment wins over `config.toml`, so a stale
mounted file cannot override a deployment. With the environment set, no config file is needed at all.

### When configuration is wrong

lox starts anyway and shows a banner naming what is wrong, and the operations that need the setting refuse with a
message pointing at it. Only a bad bind address or an auth token under 16 characters still stops startup — everything
else is something you fix on the settings page, and exiting on the way up means never reaching that page. A container
that restart-loops over a path you could have corrected in the UI is worse than one that starts and tells you.

Directories are created when missing, so the interesting failures are the ones creation cannot fix. `isdir()` returns
False for a volume mounted from the wrong host path, a volume mounted but empty, *and* a directory that is right there
but owned by a uid the container is not running as — so lox reports what it actually found at the nearest parent that
does exist, and the uid it is running as, rather than only saying the path is missing.

Outside Docker, `config.toml` is looked for at the repo root, then `~/.config/lox/`, then `~/.config/smoked-salmon/` —
upstream's location, still read so an existing install keeps working. Nothing is ever written there.

### Request filters

The Requests tab builds its own filters from whichever tracker is selected, because the two do not agree. Both run
Gazelle and both search pages look identical, but the parameter names differ and so do the numeric IDs behind the
labels — media `WEB` is `7` on RED and `1` on OPS, encoding `Lossless` is `8` on RED and `0` on OPS, and the tag mode
is `tags_type=0|1` on one and `tag_mode=any|all` on the other. A shared table would not fail on one of them; it would
succeed with the wrong filter and report nothing unusual.

`lox/checker/request_filters.py` holds one vocabulary per tracker, transcribed from their live search pages rather
than from the Gazelle source, so it matches what those sites are running. RED alone offers *include old* and
*search descriptions*; OPS alone offers a bounty range. A tracker with no entry there — DIC — gets only the filters
that need no IDs, and the UI says so rather than offering options that would search for the wrong thing.

Each page of results is one tracker call and holds 25 requests, so the fetch size is chosen in pages — the unit the
cost is actually measured in — with that cost shown next to the button.

### Getting your ARL

Log into Deezer, open developer tools → Application → Cookies → `https://www.deezer.com`, and copy the `arl` value. It
is a full session credential: anyone holding it is logged into your account. Paste it into Settings → Deezer and press
Test.

## What Lox changes

Beyond the web UI, the Deezer integration and the checker, these are the behavioural differences from the upstream
pipeline it is built on:

- **Deezer only** — metadata search is restricted to Deezer. Bandcamp, Beatport, Discogs, iTunes, JunoDownload,
  MusicBrainz, Qobuz and Tidal are disabled as *sources*, though several are still used to verify request track counts.
- **Folders are moved, not copied** — releases are moved into `download_directory` with `shutil.move`, and an emptied
  source parent is removed. The `hardlinks` and `remove_source_dir` options are gone; use `[linking]` instead.
- **Lossy-master prompts always ask** — even with `yes_all`. Auto-answering "no" asserts something about a release
  nobody looked at. In the web UI the prompt appears in the upload console and you answer it there.
- **No upstream footer** — the "Uploaded with smoked-salmon" line is dropped from torrent and transcode descriptions.
- **Icons** point at `img.onlyimage.org` rather than `ptpimg.me`.

---

## How it fits together

```
lox/deezer/     gw.py, crypto.py, download.py, explore.py
                   Private gw-light API, Blowfish stream decryption, channels

lox/checker/    gateway.py    every tracker call, budgeted and breakered
                   matching.py   title/artist/edition heuristics, request scoring
                   missing.py    collect (free) then check (budgeted)
                   requests_check.py + trackcount.py
                   watchlists.py saved searches
                   store.py      debounced atomic JSON state

lox/seeding/    links.py      hardlinked per-tracker folders

lox/web/        api.py        JSON API, auth middleware, path validation
                   jobs.py       background jobs with progress
                   static/       no-build SPA
```

---

## Status and caveats

Verified where it could be. Around 440 assertions run in CI across twelve suites, covering the prompt bridge, the
metadata form, the request filters and their per-tracker IDs, the dry run, the settings page, and the Deezer credit
rules. The UI pieces are driven in a real browser against the shipped script and stylesheet rather than a copy of them.
Ruff and basedpyright run on every push.

**What has not been exercised against the real thing:** the download chain (Deezer's media token flow and the Blowfish
decryption), the channel page scraping, live tracker calls under real rate limits, and a live torrent-client injection.
Deezer changes those surfaces; expect the download path to be the first thing that breaks.

The tracker budget defaults are conservative guesses, not measured limits. Tune `[checker]` to what your trackers
actually allow.

---

## Licence and attribution

Apache-2.0, the same licence as [smoked-salmon](https://github.com/smokin-salmon/smoked-salmon), from which the
tagging, spectral, transcoding and Gazelle upload machinery comes. The `LICENSE` file and upstream's copyright notices
are retained, and the full commit history is preserved so authorship is traceable.

deemix is GPL-3.0 and none of its code is used here — the UI resembles it, it does not reuse it.
